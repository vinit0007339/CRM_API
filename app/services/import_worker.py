"""Runs an import. CRM-230.

ONE BAD ROW MUST NOT COST THE OTHER NINE THOUSAND. Every row is wrapped: a
failure is counted, recorded with its original file row number, and the loop
moves on. An import that aborts on row 3 of 9,000 and reports "failed" leaves
the user with a partially-loaded CRM and nothing to act on.

IMPORTED RECORDS GO THROUGH THE SAME VALIDATION AS TYPED ONES. Attribute values
are routed and coerced by ``attribute_routing.route_values`` — the same call the
create endpoints make. A second, laxer path for imports is how a CRM ends up
with two populations of records that behave differently, and the one that came
in by import is always the bigger one.

CANCEL IS CHECKED BETWEEN BATCHES, and the count already committed is recorded.
An import is not a transaction: half the rows really are in the database, and
"cancelled" without a number leaves the user unable to decide whether to re-run
it.

PROGRESS IS WRITTEN IN BATCHES, not per row. A document write per row would
double the cost of the import to serve a progress bar that updates faster than
anyone can read.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import logger as _logger, settings
from app import foreign
from app.models.company import CompanyModel
from app.models.deal import DealModel
from app.models.note import CrmNoteModel
from app.object_id import PyObjectId

from app.services import contact_scope as cs
from app.services import (
    activity_recorder as ar, attribute_routing, dedupe,
    import_rules as ir, sanitise, stage_rules,
)

# How often progress is flushed and the cancel flag re-read. Small enough that
# a cancel feels immediate, large enough that the writes are noise.
BATCH_SIZE = 50

# ⚠️ NOT A SECOND COPY. These used to be literal sets written out again here,
# and the two copies drifted: `attribute_routing` learned to coerce dates and
# this file did not, which is how a deal was stored with
# `expected_close_date = "cylonsign.com"`. One definition, aliased.
COMPANY_SYSTEM_FIELDS = attribute_routing.SYSTEM_FIELDS["company"]
DEAL_SYSTEM_FIELDS = attribute_routing.SYSTEM_FIELDS["deal"]

# ⚠️ `CONTACT_SYSTEM_FIELDS` and `CONTACT_CONTROL_FIELDS` were here, with
# `_import_contact`. Contact import is gone because the CRM's people object is
# gone — the app has one contacts list, the marketing audience, and importing
# into that is `/lists` import, not this. See `routes_handler` for the whole
# reason.

# Columns that are not attributes but instructions: they say what to link this
# row to, which pipeline it belongs in, or which existing record it IS.
# Routing them as attributes would either fail validation or write junk into
# custom_fields.
DEAL_CONTROL_FIELDS = {"pipeline", "stage", "company", "owner",
                       "id", "company_id", "deal_id"}

# CRM-413. A note has exactly one importable field of its own. Everything else
# in a note file says which record the note belongs to.
NOTE_SYSTEM_FIELDS = {"body"}

# The reference columns, and the kind of record each one names. An ORDERED
# mapping, because the row's targets are reported in this order and a stable
# order keeps the error messages and the written arrays predictable.
NOTE_TARGET_FIELDS = (
    ("company_id", "company"),
    ("deal_id", "deal"),
    ("contact_id", "contact"),
    ("contact_email", "contact"),
)
NOTE_CONTROL_FIELDS = ({field for field, _ in NOTE_TARGET_FIELDS}
                       | {"id", "note_id", "is_pinned", "pinned", "author"})


class RowFailure(Exception):
    """A row that cannot be imported. The message is shown to the user."""


# ⚠️ `_NUMERIC_SYSTEM_FIELDS` and `_fallback_coerce` WERE HERE. They were a
# second, numbers-only copy of `attribute_routing._coerce`, and the import path
# is now routed through that one instead — see `_route`. Typing on the
# no-definitions path matters because pydantic does not validate on assignment:
# a string "250" in an `Optional[int]` column is written as text and every
# later sort and filter on it is wrong, and a non-date in an
# `Optional[datetime]` column cannot be read back at all.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _blanks(values: Dict[str, str]) -> set:
    """Fields the file MAPPED but left empty.

    Distinct from "fields the file did not have a column for": the first means
    "the user says this is empty", the second means "the user said nothing".
    Only the first may clear a value, and only when the option is on.
    """
    return {key for key, value in (values or {}).items()
            if str(value or "").strip() == ""}


class ImportRunner:
    """Owns one import. Instantiated by the route, run as a background task."""

    def __init__(self, *, import_id: Any, account_id: Any, entity_type: str,
                 headers: Sequence[str], rows: Sequence[Sequence[Any]],
                 mapping: Sequence[Optional[str]], options: Dict[str, Any],
                 actor_user_id: Any = None, actor_name: Optional[str] = None,
                 companies=None, deals=None, pipelines=None, attributes=None,
                 associations=None, activity=None, imports=None, contacts=None,
                 notes=None, allowed_note_parents: Sequence[str] = ()):
        self.import_id = import_id
        self.account_id = PyObjectId(str(account_id))
        self.entity_type = entity_type
        self.headers = list(headers or [])
        self.rows = list(rows or [])
        self.mapping = list(mapping or [])
        self.options = dict(options or {})
        self.actor_user_id = actor_user_id
        self.actor_name = actor_name

        self.companies = companies
        self.contacts = contacts
        self.notes = notes
        # ⚠️ FAIL CLOSED, and deliberately not `None means all`. A note
        # inherits its parent's permission (`notes.py::PARENTS`), so the route
        # computes which parent kinds this caller may write notes on and passes
        # them here; a note import is the one import whose rows can need a
        # permission the route-level check did not establish. Defaulting to the
        # empty set means a route that forgets to pass them imports nothing and
        # says why, rather than writing notes onto records the caller cannot
        # see.
        self.allowed_note_parents = frozenset(allowed_note_parents or ())
        self.deals = deals
        self.pipelines = pipelines
        self.attributes = attributes
        self.associations = associations
        self.activity = activity
        self.imports = imports

        self.counters = {"created": 0, "updated": 0, "skipped": 0, "failed": 0,
                         "linked_created": 0}
        self.errors: List[Dict[str, Any]] = []
        self.errors_truncated = False

        # Rows already seen in THIS file, by dedupe key. Without it, a file
        # listing the same company twice creates it twice even with
        # update_existing on — the second row is checked against the database
        # as it was before the import started.
        self._seen: Dict[str, Any] = {}
        self._pipeline_cache: Optional[List[Any]] = None
        self._attribute_cache: Optional[List[Any]] = None

    # ── entry point ──────────────────────────────────────────────────────────
    async def run(self) -> Dict[str, Any]:
        await self._patch(status="running", started_at=_now(),
                          total_rows=len(list(ir.iter_data_rows(self.rows))))
        processed = 0
        cancelled = False

        try:
            for row_number, row in ir.iter_data_rows(self.rows):
                if processed and processed % BATCH_SIZE == 0:
                    await self._flush(processed)
                    if await self._cancelled():
                        cancelled = True
                        break

                processed += 1
                try:
                    await self._import_row(row_number, row)
                except RowFailure as failure:
                    self._record_error(row_number, row, str(failure))
                except Exception as ex:  # pylint: disable=broad-except
                    # An unexpected error in one row is still one row. The
                    # message is deliberately generic — an internal traceback
                    # in a customer-facing error report is not helpful to them
                    # and is a disclosure risk.
                    _logger.exception(ex)
                    self._record_error(row_number, row,
                                       "This row could not be imported.")
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            await self._finish("failed", processed,
                               message="The import stopped unexpectedly. "
                                       "Rows already imported were kept.")
            return self.counters

        if cancelled:
            await self._finish("cancelled", processed, committed=processed)
        elif self.counters["failed"]:
            await self._finish("completed_with_errors", processed)
        else:
            await self._finish("completed", processed)
        return self.counters

    # ── one row ──────────────────────────────────────────────────────────────
    async def _import_row(self, row_number: int, row: Sequence[Any]) -> None:
        values = ir.row_to_values(row, self.mapping)
        # ⚠️ A LOOKUP, NOT if/elif. The old spelling fell through to a raise for
        # an unknown type, which was correct — but the same if/elif shape four
        # doors away (`attributes.py`, `defaults.py`, `column_rules.py`) handed
        # every unknown object type the DEAL branch, and Phase 6 found all four
        # at once. A dict cannot do that: an unknown key has no entry.
        importer = self._IMPORTERS.get(self.entity_type)
        if importer is None:
            raise RowFailure(f"Unknown import type: {self.entity_type}")
        await importer(self, values)

    # ── companies ────────────────────────────────────────────────────────────
    async def _import_company(self, values: Dict[str, str]) -> None:
        name = (values.get("name") or "").strip()
        if not name:
            # §11 of the requirements: company import requires `name`.
            raise RowFailure("Name is required.")

        dedupe_key = self.options.get("dedupe_key")
        # An export's id column is mapped as `id`; the option that matches on
        # it is called `company_id`. Reading the wrong one would silently
        # produce no key at all, which means "create" — so a file exported to
        # be re-imported would double every record instead of updating it.
        key = ir.dedupe_value(
            {**values, "company_id": values.get("company_id") or values.get("id")},
            dedupe_key)
        existing = await self._find_existing_company(key, values)

        typed, custom = await self._route(values, "company")

        if existing is not None:
            if not self.options.get("update_existing"):
                self.counters["skipped"] += 1
                return
            await self._apply_update(existing, typed, custom, _blanks(values))
            self.counters["updated"] += 1
            if key:
                self._seen[key] = existing
            return

        company = CompanyModel(
            created_by=self.account_id,
            created_by_user_id=self.actor_user_id,
            owner_id=self._owner_id(values),
            **dedupe.derive_name_fields(name),
            domain=dedupe.normalize_domain(values.get("domain")),
            source="import",
            custom_fields=custom,
        )
        for field, value in typed.items():
            if value is not None and field not in ("name", "domain"):
                setattr(company, field, value)
        # Re-applied AFTER the loop: `name` and `domain` are already in
        # `typed` (they are system columns), and letting the loop write them
        # would put the raw cell back over the normalised value — storing
        # "HTTPS://WWW.Acme.com/about" as the domain, which then matches
        # nothing and dedupes against nothing.
        for field, value in dedupe.derive_name_fields(name).items():
            setattr(company, field, value)
        company.domain = dedupe.normalize_domain(values.get("domain"))

        await self.companies.insert(company)
        self.counters["created"] += 1
        if key:
            self._seen[key] = company
        await self._record(ar.created(
            entity_type="company", name=company.name, source="import",
            account_id=self.account_id, entity_id=company.id,
            actor_user_id=self.actor_user_id, actor_name=self.actor_name))

    async def _find_existing_company(self, key: Optional[str], values: Dict[str, str]):
        """The record this row should update, or None to create one.

        A row created earlier in THIS run is already in the database — rows
        commit as they go, there is no transaction — so a plain query finds it.
        ``self._seen`` is only a cache to save that query on files with many
        repeats.

        MATCHING IS EXACT, NEVER FUZZY. ``find_duplicates`` scores candidates
        for the *duplicate warning*, where a near miss is a helpful suggestion
        a human then judges. Here the result silently overwrites a record, and
        a confident-but-wrong fuzzy match would edit the wrong company with
        nobody looking. So candidates come from the same place, and only an
        exact normalised match is accepted.
        """
        if not key:
            return None
        if key in self._seen:
            return self._seen[key]

        dedupe_key = self.options.get("dedupe_key")
        if dedupe_key == "domain":
            rows = await self.companies.list_by(
                {"created_by": self.account_id, "domain": key}, limit=1)
            return rows[0] if rows else None

        if dedupe_key == "company_id":
            # The id column an EXPORT produced (§10.2). This is the whole point
            # of forcing `id` into every export: edit the file, re-import it,
            # and the records update rather than doubling.
            #
            # get_scoped, so an id from someone else's export resolves to
            # nothing rather than to their record.
            return await self.companies.get_scoped(self.account_id, key)

        if dedupe_key == "name":
            # There is no stored normalised name to query, so narrow with the
            # existing candidate finder and compare in Python. "Acme Pvt Ltd"
            # and "ACME Limited" both reduce to "acme" and must match; "Acme
            # Foods" must not.
            candidates = await self.companies.find_duplicates(
                account_id=self.account_id, name=values.get("name"),
                min_confidence=dedupe.LOW)
            for candidate in candidates or []:
                if dedupe.normalize_company_name(candidate.get("name")) == key:
                    found = await self.companies.get_scoped(
                        self.account_id, candidate.get("id"))
                    if found is not None:
                        return found
            return None

        return None

    # ── deals ────────────────────────────────────────────────────────────────
    async def _import_deal(self, values: Dict[str, str]) -> None:
        name = (values.get("name") or "").strip()
        if not name:
            raise RowFailure("Name is required.")

        pipeline, stage = await self._resolve_stage(values)
        typed, custom = await self._route(values, "deal")

        deal = DealModel(
            created_by=self.account_id,
            created_by_user_id=self.actor_user_id,
            owner_id=self._owner_id(values),
            name=name,
            pipeline_id=pipeline.id,
            stage_id=stage["id"] if isinstance(stage, dict) else stage.id,
            source="import",
            custom_fields=custom,
        )
        for field, value in typed.items():
            if value is not None and field != "name":
                setattr(deal, field, value)
        deal.name = name

        if self.options.get("link_entities"):
            await self._attach_company(deal, values)

        await self.deals.insert(deal)
        self.counters["created"] += 1
        await self._record(ar.created(
            entity_type="deal", name=deal.name, source="import",
            account_id=self.account_id, entity_id=deal.id,
            actor_user_id=self.actor_user_id, actor_name=self.actor_name))

        if self.associations and deal.primary_company_id:
            try:
                await self.associations.link(
                    account_id=self.account_id, from_type="deal", from_id=deal.id,
                    to_type="company", to_id=deal.primary_company_id,
                    actor_user_id=self.actor_user_id)
            except Exception as ex:  # pylint: disable=broad-except
                _logger.exception(ex)

    # ── notes (CRM-413) ──────────────────────────────────────────────────────
    async def _import_note(self, values: Dict[str, str]) -> None:
        """One note row: a body, and the records it attaches to.

        ⚠️ THE BODY GOES THROUGH `sanitise`, THE SAME CALL `POST /crm/notes`
        MAKES. An import is not a sanitisation bypass — see that module's
        docstring: it is the *storing* of hostile HTML that matters, and an
        importer with its own laxer path would be the hole the whole module
        exists to close.

        ⚠️ AN UNRESOLVED REFERENCE IS A ROW FAILURE NAMING IT, never a note
        attached to nothing. A note with no parent is invisible: no screen
        lists it, `list_notes` requires a record to list for, and
        `_may_touch`'s orphan branch would be the only thing that could still
        read it. A file of 400 notes whose company ids came from another
        account would import "successfully" and show nowhere.
        """
        body = self._note_body(values.get("body"))
        if sanitise.is_empty(body):
            # Not "the cell was blank": `<p><br></p>` and `<script>x</script>`
            # both sanitise to nothing, and both mean there is no note here.
            raise RowFailure("This row has no note text.")

        targets = await self._resolve_note_targets(values)
        if not any(ids for ids in targets.values()):
            raise RowFailure("This row doesn't say which record the note "
                             "belongs to.")

        if not self.actor_user_id:
            # `author_id` is required by the model. Reaching the insert without
            # one would raise a pydantic error per row and arrive in the report
            # as "This row could not be imported."
            raise RowFailure("This import has no author to record notes under.")

        note = CrmNoteModel(
            created_by=self.account_id,
            body=body,
            # ⚠️ NEVER PINNED, whatever the file says — `is_pinned` is not in
            # `NOTE_COLUMNS` and is in `NOTE_CONTROL_FIELDS` so it cannot be
            # mapped. A pin is "the team reads this first", shared state shown
            # above every other note on the record; an import of 900 historical
            # notes that honoured a `pinned` column would pin hundreds of them
            # and bury the one note somebody actually pinned on purpose.
            is_pinned=False,
            company_ids=list(targets["company"]),
            deal_ids=list(targets["deal"]),
            contact_ids=list(targets["contact"]),
            author_id=self.actor_user_id,
            author_name=self.actor_name,
        )
        await self.notes.insert(note)
        self.counters["created"] += 1

        for payload in ar.note_activities(
            kind="note_added",
            note_id=note.id,
            preview=sanitise.plain_text(note.body, limit=140),
            targets=(("deal", note.deal_ids), ("company", note.company_ids),
                     ("contact", note.contact_ids)),
            account_id=self.account_id,
            # The row is on the record's timeline either way; `source` is what
            # tells a reader it arrived by import rather than being typed.
            source="import",
            actor_user_id=self.actor_user_id,
            actor_name=self.actor_name,
        ):
            await self._record(payload)

    @staticmethod
    def _note_body(raw: Any) -> str:
        """A spreadsheet cell as note HTML.

        A CSV cell is plain text, and its line breaks are the only structure it
        has — HTML collapses them, so a three-paragraph note would import as
        one run-on blob. They are converted to `<br>` BEFORE sanitising, so the
        tag goes through the same allow-list as everything else rather than
        being appended to the sanitiser's output, which would make this the one
        place that can add markup the allow-list never saw.

        Only when the cell contains no markup at all. A cell exported from a
        CRM that stores notes as HTML already has its own structure, and
        splitting that on newlines would add breaks between its tags.
        """
        text = "" if raw is None else str(raw)
        if "<" not in text:
            lines = text.splitlines()
            if len(lines) > 1:
                text = "<br>".join(lines)
        return sanitise.sanitise_html(text)

    async def _resolve_note_targets(self, values: Dict[str, str]
                                    ) -> Dict[str, List[Any]]:
        """``{kind: [id]}`` for every reference column the row filled in.

        ⚠️ RESOLVED, NOT TRUSTED. Every lookup is account-scoped, so an id
        copied from another account's export resolves to nothing and is
        reported, rather than attaching a note to a record the importer cannot
        see.

        ⚠️ A MISSING CONTACT IS NOT CREATED. `_resolve_company` creates a
        company a deal or contact row names, because the file is asserting that
        the company exists and the record needs somewhere to hang. A note is
        the opposite: it is history *about* someone, and inventing a nameless
        contact from an address in a note file would add a person to the CRM
        that nobody put there — then the note would be "attached" to a record
        with no other content, which is worse than a reported error.
        """
        found: Dict[str, List[Any]] = {"company": [], "deal": [], "contact": []}
        for field, kind in NOTE_TARGET_FIELDS:
            raw = str(values.get(field) or "").strip()
            if not raw:
                continue
            if kind not in self.allowed_note_parents:
                # Checked per row, not once per import: holding `deals:write`
                # starts a note import, and a row in the same file pointing at
                # a company is a row this caller may not write.
                raise RowFailure(
                    f"You don't have permission to add notes to a {kind}.")
            record = await self._find_note_target(field, raw)
            if record is None:
                raise RowFailure(
                    f'{ir.NOTE_LABELS.get(field, field)} "{raw}" '
                    f"doesn't match any {kind} in this account.")
            if record.id not in found[kind]:
                found[kind].append(record.id)
        return found

    def _db(self):
        """The database handle, borrowed from whichever CRUD we were given.

        ⚠️ NOT `Database.db`. The worker is constructed with CRUDs by its
        caller, and the integration suite hands it CRUDs bound to a FAKE
        database — reading the global here would make every import test either
        skip the contact lookup or talk to a real Mongo. Borrowing the session
        keeps the worker testable and keeps `foreign` honest about where its
        database comes from.
        """
        for crud in (self.notes, self.companies, self.deals, self.contacts):
            session = getattr(crud, "db_session", None)
            if session is not None:
                return session
        return None

    def _crm_contacts(self):
        """`CrmContactCrud`, built once.

        ⚠️ CACHED, BECAUSE THIS IS CALLED PER ROW. `_find_note_target` runs for
        every reference column of every row; constructing a CRUD object five
        thousand times is pointless work in the hot path of an import. The
        route may also pass one in — `__init__` already accepts `contacts=`,
        left over from when the CRM owned this object — so that is preferred
        when present.
        """
        if self.contacts is not None:
            return self.contacts
        if getattr(self, "_crm_contacts_crud", None) is None:
            from app.crud.contact import CrmContactCrud

            self._crm_contacts_crud = CrmContactCrud(self._db())
        return self._crm_contacts_crud

    async def _crm_contact_by_email(self, raw: str):
        from app.services import contact_rules as cr

        address = cr.normalize_email(raw)
        if not address:
            return None
        return await self._crm_contacts().by_email(
            account_id=self.account_id, email=address)

    async def _find_note_target(self, field: str, raw: str):
        """One reference column's record, or None.

        A lookup, not if/elif — the shape that handed four unknown object types
        another object's branch in Phase 6.

        ⚠️ A CONTACT HERE IS A PLATFORM CONTACT. The CRM's own people object is
        gone, so `contact_id` and `contact_email` resolve against the marketing
        `contacts` collection — the same thing an association's contact side,
        `EntityPicker` and a company's Contacts tab point at. Always
        account-scoped, through `contact_scope`: the collection's email index is
        globally unique and its rows are shared between accounts, so an
        unscoped lookup would attach a note to another tenant's person.
        """
        if field == "contact_email" and settings.contacts_source == "crm":
            # ⚠️ NORMALISE, THEN MATCH EXACTLY — the opposite of the platform
            # branch below. `crm_contacts.email` is stored lowercased by
            # `contact_rules.normalize_email` and the unique index is on that
            # value, so `CrmContactCrud.by_email` requires a normalised
            # address and a case-insensitive regex would be both slower and
            # wrong (it would match outside the index).
            return await self._crm_contact_by_email(raw)
        if field == "contact_id" and settings.contacts_source == "crm":
            try:
                return await self._crm_contacts().get_scoped(
                    self.account_id, raw)
            except Exception:  # pylint: disable=broad-except
                # Same rule as the company/deal branch below: an unparseable
                # cell is an unresolved reference, not a crash.
                return None
        if field == "contact_email":
            # ⚠️ CASE-INSENSITIVE, UNLIKE A CRM LOOKUP. `Contact.email` is
            # deliberately NOT lowercased on write — it carries a unique index
            # and folding case would collide existing rows — so an exact match
            # misses `Asha@Acme.com` for a file that says `asha@acme.com`.
            # `app/ingestion/service.py` resolves it the same way.
            address = str(raw or "").strip()
            if not address:
                return None
            # ⚠️ THROUGH `foreign`, NOT A BEANIE MODEL. `contacts` belongs to
            # the platform; this service reads it and never registers it. The
            # case-insensitive match and the account scope both moved into
            # `foreign.contact_by_email` with their reasons — read them there.
            return await foreign.contact_by_email(
                self._db(), self.account_id, address)
        if field == "contact_id":
            return await foreign.contact_by_id(
                self._db(), self.account_id, raw)
        cruds = {"company_id": self.companies, "deal_id": self.deals}
        crud = cruds.get(field)
        if crud is None:
            return None
        try:
            return await crud.get_scoped(self.account_id, raw)
        except Exception:  # pylint: disable=broad-except
            # A cell that is not a valid ObjectId ("Acme", "#N/A", a trailing
            # space Excel left behind) raises on conversion. That is an
            # unresolved reference, which the caller reports by name — not a
            # crash that costs the row its message.
            return None

    async def _resolve_stage(self, values: Dict[str, str]):
        """Pipeline and stage BY NAME, falling back to the account default.

        Names, not ids: nobody's spreadsheet contains our ObjectIds. An unknown
        name is a row failure that says which name was not found — "invalid
        stage" would send the user to check all of them.
        """
        pipelines = await self._pipelines()
        if not pipelines:
            raise RowFailure("This account has no pipelines to import leads into.")

        wanted_pipeline = (values.get("pipeline") or "").strip().lower()
        pipeline = None
        if wanted_pipeline:
            pipeline = next((p for p in pipelines
                             if (p.name or "").strip().lower() == wanted_pipeline), None)
            if pipeline is None:
                raise RowFailure(f'No pipeline called "{values.get("pipeline")}".')
        else:
            pipeline = next((p for p in pipelines if getattr(p, "is_default", False)),
                            pipelines[0])

        stages = list(getattr(pipeline, "stages", None) or [])
        if not stages:
            raise RowFailure(f'Pipeline "{pipeline.name}" has no stages.')

        wanted_stage = (values.get("stage") or "").strip().lower()
        if not wanted_stage:
            # ⚠️ NOT `stages[0]`. That was the index, with no open check at
            # all: a pipeline whose stored array happens to begin with Won
            # would have made every stage-less imported row a WON deal, and a
            # jumbled array would have scattered them. Same rule as
            # `POST /crm/deals` and the lead door now.
            default_stage = stage_rules.first_open_stage(stages)
            if default_stage is None:
                raise RowFailure(
                    f'Pipeline "{pipeline.name}" has no open stage to import '
                    "leads into.")
            return pipeline, default_stage

        for stage in stages:
            label = getattr(stage, "name", None) or (
                stage.get("name") if isinstance(stage, dict) else None)
            if (label or "").strip().lower() == wanted_stage:
                return pipeline, stage
        raise RowFailure(
            f'No stage called "{values.get("stage")}" in pipeline '
            f'"{pipeline.name}".')

    async def _attach_company(self, deal: DealModel, values: Dict[str, str]) -> None:
        """Link the deal to a company named in the file, creating it if needed.

        Creating is counted separately (``linked_created``) and reported, so
        the summary is honest about a side effect the user did not explicitly
        ask for: importing 200 deals can silently produce 180 new companies,
        and finding that out afterwards is unpleasant.
        """
        company = await self._resolve_company(values)
        if company is None:
            return
        deal.primary_company_id = company.id
        deal.primary_company_name = company.name

    async def _resolve_company(self, values: Dict[str, str]):
        """The company a `company` cell names, created if it does not exist.

        Shared by the deal and contact importers (CRM-412) so the two cannot
        disagree about which company "Acme Pvt Ltd" is. Matched on the
        normalised domain when the cell looks like one, on the name otherwise.

        Creating is counted separately (``linked_created``) and reported, so the
        summary is honest about a side effect the user did not explicitly ask
        for: importing 200 records can silently produce 180 new companies, and
        finding that out afterwards is unpleasant.
        """
        raw = (values.get("company") or "").strip()
        if not raw or self.companies is None:
            return None

        domain = dedupe.normalize_domain(raw)
        query = ({"created_by": self.account_id, "domain": domain} if domain
                 else {"created_by": self.account_id, "name": raw})
        found = await self.companies.list_by(query, limit=1)
        if found:
            return found[0]

        company = CompanyModel(
            created_by=self.account_id,
            created_by_user_id=self.actor_user_id,
            owner_id=self._owner_id(values),
            **dedupe.derive_name_fields(raw),
            domain=domain,
            source="import",
        )
        await self.companies.insert(company)
        self.counters["linked_created"] += 1
        return company

    # ── shared ───────────────────────────────────────────────────────────────
    async def _route(self, values: Dict[str, str], object_type: str
                     ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Split and coerce, through the SAME path the create endpoints use.

        ⚠️ `object_type` IS PASSED ON. Without it, `route_values` cannot tell a
        system column from a custom one on the no-definitions path, which is
        why this method used to re-split and re-coerce the result itself. That
        second copy covered numbers only, so an unparseable
        `expected_close_date` was written to a typed column verbatim and the
        whole pipeline's list 500ed on the next read.
        """
        payload = {k: v for k, v in values.items()
                   if v != ""
                   and k not in DEAL_CONTROL_FIELDS | NOTE_CONTROL_FIELDS}
        if not payload:
            return {}, {}
        attributes = await self._attributes(object_type)
        typed, custom, error = attribute_routing.route_values(
            payload, attributes, object_type)
        if error:
            # The message names the offending attribute and value, because
            # "invalid value" in a report of 400 rows is unactionable.
            raise RowFailure(error)
        return typed, custom

    async def _apply_update(self, existing, typed: Dict[str, Any],
                            custom: Dict[str, Any],
                            blanks: Optional[set] = None) -> None:
        """Update in place. ``unset_empty_attributes`` decides what blank means.

        Off (the default): a blank cell means "leave this alone", so a file
        with only the columns you wanted to change works as expected.
        On: a blank cell CLEARS the field, which is what you want when the file
        is the source of truth. Guessing either way silently is how an import
        wipes data nobody asked it to touch.

        ``blanks`` is passed in separately because empty cells never reach
        ``typed``/``custom`` — they are filtered out before routing, since
        coercing "" to a number or a date would fail. Without them the option
        had nothing to act on and silently did nothing.
        """
        unset = bool(self.options.get("unset_empty_attributes"))

        for field, value in typed.items():
            if value is None:
                continue
            setattr(existing, field, value)

        merged = dict(getattr(existing, "custom_fields", None) or {})
        for field, value in custom.items():
            if value in (None, ""):
                continue
            merged[field] = value

        if unset:
            known = set(COMPANY_SYSTEM_FIELDS) | set(DEAL_SYSTEM_FIELDS)
            for field in (blanks or set()):
                if field in DEAL_CONTROL_FIELDS | NOTE_CONTROL_FIELDS:
                    continue
                if field in known and hasattr(existing, field):
                    setattr(existing, field, None)
                else:
                    merged.pop(field, None)

        existing.custom_fields = merged
        existing.updated_at = _now()
        await existing.save()

    # Declared here, after the methods it names. One entry per importable
    # object; an unknown entity type simply has none.
    _IMPORTERS = {
        "company": _import_company,
        "deal": _import_deal,
        "note": _import_note,
    }

    def _owner_id(self, values: Dict[str, str]) -> Any:
        # An owner column is not resolved to a user here: matching people by
        # name out of a spreadsheet assigns records to the wrong person often
        # enough that it is worse than not doing it. `default_owner_id` is the
        # explicit, unambiguous alternative the wizard offers.
        return self.options.get("default_owner_id") or self.actor_user_id

    async def _pipelines(self) -> List[Any]:
        if self._pipeline_cache is None:
            self._pipeline_cache = await self.pipelines.list_by(
                {"created_by": self.account_id}, limit=100)
        return self._pipeline_cache

    async def _attributes(self, object_type: str) -> List[Any]:
        # Read once per import, not once per row: 9,000 rows would otherwise be
        # 9,000 identical queries.
        if self._attribute_cache is None:
            self._attribute_cache = await self.attributes.list(
                account_id=self.account_id, object_type=object_type)
        return self._attribute_cache

    async def _record(self, activity: Optional[Dict[str, Any]]) -> None:
        if not self.activity or not activity:
            return
        try:
            await ar.record(self.activity.write, activity)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)

    def _record_error(self, row_number: int, row: Sequence[Any],
                      message: str) -> None:
        self.counters["failed"] += 1
        if len(self.errors) >= ir.MAX_ERROR_ROWS:
            # The COUNT stays exact; only the samples stop. See the model.
            self.errors_truncated = True
            return
        self.errors.append({
            "row_number": row_number,
            "message": message,
            "row": [("" if cell is None else str(cell)) for cell in row],
        })

    # ── progress ─────────────────────────────────────────────────────────────
    async def _cancelled(self) -> bool:
        try:
            row = await self.imports.get_scoped(self.account_id, self.import_id)
            return bool(row and row.cancel_requested)
        except Exception as ex:  # pylint: disable=broad-except
            # If we cannot read the flag, keep going: finishing an import the
            # user tried to cancel is a smaller harm than abandoning one they
            # did not.
            _logger.exception(ex)
            return False

    async def _flush(self, processed: int) -> None:
        await self._patch(processed_rows=processed, **self._counter_fields())

    def _counter_fields(self) -> Dict[str, Any]:
        return {
            "created_count": self.counters["created"],
            "updated_count": self.counters["updated"],
            "skipped_count": self.counters["skipped"],
            "failed_count": self.counters["failed"],
            "linked_created_count": self.counters["linked_created"],
        }

    async def _finish(self, status: str, processed: int, *,
                      committed: Optional[int] = None,
                      message: Optional[str] = None) -> None:
        summary = ir.summarise(self.counters)
        if self.counters["linked_created"]:
            summary += f", {self.counters['linked_created']:,} companies created by linking"
        if status == "cancelled":
            summary = f"Cancelled. {summary} before stopping."
        fields: Dict[str, Any] = {
            "status": status,
            "processed_rows": processed,
            "finished_at": _now(),
            "message": message or summary,
            "errors": self.errors,
            "errors_truncated": self.errors_truncated,
            **self._counter_fields(),
        }
        if committed is not None:
            fields["committed_at_cancel"] = committed
        await self._patch(**fields)

    async def _patch(self, **fields: Any) -> None:
        fields["updated_at"] = _now()
        try:
            # Scoped update, not a bare model write: the import id came in on a
            # request and must not address another account's run.
            #
            # NOT json_safe here — that is for untyped Dict[str, Any] payloads
            # bound for a response. These fields are typed model columns, and
            # coercing them would store every datetime as a string.
            await self.imports.update_scoped(self.account_id, self.import_id, fields)
        except Exception as ex:  # pylint: disable=broad-except
            # Progress is not the import. Losing a progress write must not lose
            # the rows.
            _logger.exception(ex)
