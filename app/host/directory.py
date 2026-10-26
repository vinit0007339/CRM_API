"""Directory: the names behind ids the CRM stores but does not own.

Five lookups, and they fall into TWO CLASSES with opposite failure rules. The
class matters more than the signature, and getting it backwards is how a
cosmetic outage becomes a data one:

    EXPANSION — `users_by_ids`, `tags_by_ids`, `contacts_by_ids`
        Cosmetic. The owner's name beside a deal, a company's tag chips, the
        Contacts tab. ⚠️ A FAILURE HERE RETURNS `[]` AND IS LOGGED, NEVER
        RAISED. A directory blip must not fail a page of deals; the deals are
        the data, the names are decoration. The monolith's versions of these
        reads each carried that comment and it is preserved deliberately.

    LOOKUP — `contact_by_email`, `contact_by_id`
        Load-bearing. Resolving which person a note or an imported row belongs
        to. `None` means NOT FOUND — a real, trustworthy answer.
        ⚠️ AN INFRASTRUCTURE FAILURE MUST RAISE, NOT RETURN `None`, AND THIS
        IS THE SHARPEST EDGE IN THE FILE. `shared_db` has always behaved this
        way (there is no try/except around its `find_one`), and the reason
        shows up in `import_worker`: it reads `contact_by_email` as "does this
        person already exist?" and CREATES ONE when the answer is no. Collapse
        "the host is down" into "no such contact" and a thirty-second outage
        becomes a column of duplicate people in a customer's account, with no
        error anywhere. So a lookup answers or it raises; the callers already
        have the try/except that turns a raise into a reported row failure.

⚠️ THE TWO LOOKUPS ARE ACCOUNT-SCOPED AND THE THREE EXPANSIONS ARE NOT, AND
THAT ASYMMETRY IS NOT AN OVERSIGHT. A host's contact rows may be shared
between accounts behind a globally unique email index — the platform's are —
so an unscoped lookup attaches a note to another tenant's person. The
expansions take ids that came out of THIS account's own `crm_associations`
rows, so they have already been through a scoped read. Any adapter must
preserve both halves: scope the lookups, and do not "tidy up" the expansions
into being scoped, because that is a behaviour change that drops rows.

⚠️ AND THERE IS NO `write_*`, NOW OR EVER. A write to a host collection is a
request to the host, not a query. If the CRM ever needs to change a contact,
that is an endpoint on the host, added deliberately.

ADAPTERS

    shared_db   The default, and the platform's. The CRM and the host share
                one Mongo, so these are plain reads of `users`, `tags` and
                `contacts`. Fastest and most faithful; requires one database.
    http        One endpoint on the host: POST /internal/crm-host/directory.
                For a consumer with its own database. See
                docs/host-contract.md.
    none        Everything expands to nothing and every lookup misses.
                Companies and deals work fully; owner names and tag chips show
                as ids or blanks. A legitimate way to start a new project.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from app.config import DEFAULT_HOST_DIRECTORY, logger, settings


class DirectoryUnavailable(Exception):
    """The host could not be asked. ⚠️ NOT "the row does not exist".

    Raised by LOOKUPS only. Expansions catch it themselves and degrade to
    `[]`, because an owner's name is decoration; a lookup cannot degrade,
    because its caller treats a miss as licence to create a record.
    """


class ForeignRecord:
    """A row from a collection this service does not own.

    Carries `.id` because that is all the CRM code wants from one — see
    `import_worker._find_note_target`, which treats a company, a deal and a
    contact identically through it. `raw` is there for the rare caller that
    needs a field, and keeping it explicit means nobody mistakes one of these
    for a Beanie document they can save.
    """

    __slots__ = ("id", "raw")

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw or {}
        self.id = self.raw.get("_id")

    def get(self, key: str, default=None):
        return self.raw.get(key, default)

    def __repr__(self) -> str:                    # pragma: no cover
        return f"ForeignRecord({self.id!r})"


class Directory(Protocol):
    """What a host must answer. `db` is the CRM's own handle.

    ⚠️ `db` IS PASSED IN RATHER THAN READ FROM A GLOBAL, and every adapter
    keeps the parameter even when it ignores it. The integration suite binds a
    FAKE database and hands it to the cruds; it never sets the global. An
    adapter that reached for `Database.db` instead would be a module with no
    tests — and the signature has to stay uniform so `app/foreign.py` can hand
    its callers' handle through without knowing which adapter is configured.
    """

    async def contact_by_email(self, db: Any, account_id: Any,
                               email: str) -> Optional[ForeignRecord]: ...

    async def contact_by_id(self, db: Any, account_id: Any,
                            contact_id: Any) -> Optional[ForeignRecord]: ...

    async def contacts_by_ids(self, db: Any,
                              contact_ids: List[Any]) -> List[Dict[str, Any]]: ...

    async def users_by_ids(self, db: Any,
                           user_ids: List[Any]) -> List[Dict[str, Any]]: ...

    async def tags_by_ids(self, db: Any,
                          tag_ids: List[Any]) -> List[Dict[str, Any]]: ...


# ── the default: one database, shared with the host ─────────────────────────

class SharedDbDirectory:
    """Plain Mongo reads. The platform's adapter, and the service's default.

    ⚠️ THE BODIES OF THESE FIVE METHODS ARE THE MONOLITH'S READS, MOVED
    WITHOUT EDIT. That is the point: `emailMarketingAPI` must get exactly the
    behaviour it has today out of this refactor, and the 891 integration tests
    are the proof — they exercise this adapter and were not changed.
    """

    name = "shared_db"

    @staticmethod
    def _collection(db: Any, name: str):
        if db is None:
            return None
        return db[name]

    async def contact_by_email(self, db, account_id, email):
        """One account's contact with that address, or None.

        ⚠️ CASE-INSENSITIVE, UNLIKE A CRM LOOKUP. `Contact.email` is
        deliberately NOT lowercased on write — it carries a globally unique
        index and folding case would collide existing rows — so an exact match
        misses `Asha@Acme.com` for a file that says `asha@acme.com`.

        ⚠️ AND ALWAYS ACCOUNT-SCOPED. That unique index is global and the rows
        are shared between accounts, so an unscoped lookup attaches a note to
        another tenant's person.
        """
        import re

        from app.services import contact_scope as cs

        address = str(email or "").strip()
        collection = self._collection(db, "contacts")
        if not address or collection is None:
            return None
        pattern = {"$regex": f"^{re.escape(address)}$", "$options": "i"}
        row = await collection.find_one(cs.scoped({"email": pattern}, account_id))
        return ForeignRecord(row) if row else None

    async def contact_by_id(self, db, account_id, contact_id):
        """One account's contact by id, or None. Scoped for the same reason."""
        from app.object_id import PyObjectId
        from app.services import contact_scope as cs

        collection = self._collection(db, "contacts")
        if collection is None:
            return None
        try:
            oid = PyObjectId(str(contact_id))
        except Exception:                         # pylint: disable=broad-except
            return None                           # not an id at all
        row = await collection.find_one(cs.scoped({"_id": oid}, account_id))
        return ForeignRecord(row) if row else None

    async def contacts_by_ids(self, db, contact_ids):
        """Rows for a company's Contacts tab.

        ⚠️ NOT ACCOUNT-SCOPED, AND THAT IS THE MONOLITH'S BEHAVIOUR PRESERVED
        EXACTLY. The ids come from this account's own `crm_associations` rows,
        so they have already been through a scoped read; adding a scope here
        would be belt and braces, but it would also be a behaviour CHANGE, and
        Phase 5 diffs these two services against each other.
        """
        collection = self._collection(db, "contacts")
        if not contact_ids or collection is None:
            return []
        try:
            return await collection.find(
                {"_id": {"$in": list(contact_ids)}}).to_list(length=None)
        except Exception as ex:                   # pylint: disable=broad-except
            logger.exception(ex)
            return []

    async def users_by_ids(self, db, user_ids):
        """One query for a whole page of rows, never one per row (NFR6)."""
        from app.object_id import PyObjectId

        collection = self._collection(db, "users")
        if not user_ids or collection is None:
            return []
        try:
            return await collection.find(
                {"_id": {"$in": [PyObjectId(i) for i in user_ids]}}
            ).to_list(length=None)
        except Exception as ex:                   # pylint: disable=broad-except
            # Expansion is cosmetic — a failure here must not fail the list.
            logger.exception(ex)
            return []

    async def tags_by_ids(self, db, tag_ids):
        from app.object_id import PyObjectId

        collection = self._collection(db, "tags")
        if not tag_ids or collection is None:
            return []
        try:
            return await collection.find(
                {"_id": {"$in": [PyObjectId(i) for i in tag_ids]}}
            ).to_list(length=None)
        except Exception as ex:                   # pylint: disable=broad-except
            logger.exception(ex)
            return []


# ── for a consumer with its own database ────────────────────────────────────

class HttpDirectory:
    """One endpoint on the host: `POST /internal/crm-host/directory`.

    The request names a kind and some ids; the reply is rows. One call per
    kind per page — never one per row, which is the whole reason the interface
    takes LISTS of ids rather than single ones.

    ⚠️ THE FAILURE RULES SURVIVE THE NETWORK, AND THEY ARE NOT THE SAME RULE
    FOR BOTH CLASSES. An unreachable host degrades an EXPANSION to `[]` — a
    directory blip must not 500 every CRM list page, and a page of deals
    showing owner ids beats no page at all. A LOOKUP raises
    `DirectoryUnavailable`, matching what `shared_db` does when Mongo is down.

    The first version of this class returned `None` from a failed lookup, for
    symmetry with the expansions. That is the duplicate-people bug described
    in the module docstring: `import_worker` reads `contact_by_email` as "does
    this person exist?" and creates one when told no, so a host timeout would
    have produced duplicates silently. Symmetry was the wrong instinct — the
    two classes differ precisely in what a failure is allowed to look like.

    ⚠️ AND NO CACHING, for the same reason one layer down: a cached `None`
    would outlive the blip that caused it.
    """

    name = "http"

    def __init__(self, base_url: str, internal_key: Optional[str],
                 timeout: float = 3.0):
        self.base_url = (base_url or "").rstrip("/")
        self.internal_key = internal_key
        self.timeout = timeout

    async def _post(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Ask the host. Raises `DirectoryUnavailable` if it could not be asked.

        Callers decide what that means: `_by_ids` catches it, the lookups do
        not. Putting the decision in the callers rather than here is what
        keeps the two failure classes from quietly becoming one.
        """
        if not self.base_url:
            raise DirectoryUnavailable("host directory base_url is not configured")
        try:
            import httpx

            headers = {"Content-Type": "application/json"}
            if self.internal_key:
                headers["X-Internal-Key"] = self.internal_key
            url = f"{self.base_url}/api/v1/internal/crm-host/directory"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                # ⚠️ 404 IS A FAILURE HERE, NOT AN EMPTY RESULT. The contract
                # answers 200 with `rows: []` for "nothing matched"; a 404
                # means the endpoint is not there — a misconfigured base_url,
                # or a host that has not implemented the contract. Reading it
                # as "no such contact" would be the duplicate-people bug again,
                # arriving by a different route.
                raise DirectoryUnavailable(
                    f"host directory answered {response.status_code}: "
                    f"{response.text[:200]}")
            body = response.json()
            if not isinstance(body, dict):
                raise DirectoryUnavailable(
                    f"host directory answered {type(body).__name__}, not an "
                    f"object")
            return body
        except DirectoryUnavailable:
            raise
        except Exception as ex:                   # pylint: disable=broad-except
            raise DirectoryUnavailable(
                f"host directory {payload.get('kind')} failed: {ex}") from ex

    @staticmethod
    def _rows(body: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """`{status,message,data:{rows:[...]}}` — the service's own envelope.

        Anything else is treated as no rows rather than as a crash: a host
        that answers 200 with a surprise shape is a misconfiguration, and the
        cosmetic-failure rule applies to it too.
        """
        if not body:
            return []
        data = body.get("data")
        rows = data.get("rows") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def _by_ids(self, kind: str, ids: List[Any]) -> List[Dict[str, Any]]:
        """EXPANSION: degrade to `[]`, log, never raise."""
        if not ids:
            return []
        try:
            return self._rows(await self._post(
                {"kind": kind, "ids": [str(i) for i in ids]}))
        except DirectoryUnavailable as ex:
            logger.warning("%s expansion degraded to empty: %s", kind, ex)
            return []

    async def contact_by_email(self, db, account_id, email):
        address = str(email or "").strip()
        if not address:
            return None
        rows = self._rows(await self._post({
            "kind": "contacts", "account_id": str(account_id or ""),
            "email": address,
        }))
        return ForeignRecord(rows[0]) if rows else None

    async def contact_by_id(self, db, account_id, contact_id):
        rows = self._rows(await self._post({
            "kind": "contacts", "account_id": str(account_id or ""),
            "ids": [str(contact_id)],
        }))
        return ForeignRecord(rows[0]) if rows else None

    async def contacts_by_ids(self, db, contact_ids):
        return await self._by_ids("contacts", contact_ids)

    async def users_by_ids(self, db, user_ids):
        return await self._by_ids("users", user_ids)

    async def tags_by_ids(self, db, tag_ids):
        return await self._by_ids("tags", tag_ids)


# ── for a project that has no directory to offer ────────────────────────────

class NoDirectory:
    """No host directory. Expansions are empty, lookups miss.

    A legitimate way to run: companies, deals, pipelines, notes, tasks, files,
    attributes, saved views and the board all work completely. What degrades is
    cosmetic — an owner shows as an id, tag chips are absent, a company's
    Contacts tab is empty — plus note/task targeting by contact, which is a
    feature a project without contacts does not have anyway.

    ⚠️ IT ANSWERS, IT DOES NOT RAISE. The point of this adapter is that a new
    consumer can bring the CRM up and click through it before writing a single
    line of host integration. An adapter that raised "not configured" would
    make the first run look broken.
    """

    name = "none"

    async def contact_by_email(self, db, account_id, email):
        return None

    async def contact_by_id(self, db, account_id, contact_id):
        return None

    async def contacts_by_ids(self, db, contact_ids):
        return []

    async def users_by_ids(self, db, user_ids):
        return []

    async def tags_by_ids(self, db, tag_ids):
        return []


#: ⚠️ A DICT, NOT AN IF/ELSE CHAIN. Branching on a configured name with
#: if/elif is how a fourth adapter gets added in two places and wired in one.
_ADAPTERS = {
    SharedDbDirectory.name: SharedDbDirectory,
    HttpDirectory.name: HttpDirectory,
    NoDirectory.name: NoDirectory,
}

_INSTANCE: Optional[Directory] = None
_INSTANCE_FOR: Optional[str] = None


def get_directory() -> Directory:
    """The configured adapter, built once.

    ⚠️ REBUILT WHEN THE CONFIGURED NAME CHANGES, because `config.reload()`
    mutates the settings object in place and the test suites use it to switch
    adapters between cases. Caching on the name rather than caching
    unconditionally is what makes that work without a reload hook.

    An UNKNOWN name falls back to `shared_db` and says so loudly rather than
    raising: this is read on the request path, and a typo in `app.conf` must
    not take the service down.
    """
    global _INSTANCE, _INSTANCE_FOR          # pylint: disable=global-statement

    name = (getattr(settings, "host_directory", None)
            or DEFAULT_HOST_DIRECTORY).strip().lower()
    if name not in _ADAPTERS:
        logger.error("unknown [HOST] directory=%r — falling back to shared_db "
                     "(valid: %s)", name, ", ".join(sorted(_ADAPTERS)))
        name = DEFAULT_HOST_DIRECTORY
    if _INSTANCE is not None and _INSTANCE_FOR == name:
        return _INSTANCE

    if name == HttpDirectory.name:
        _INSTANCE = HttpDirectory(
            base_url=getattr(settings, "host_base_url", None)
            or getattr(settings, "platform_base_url", None) or "",
            internal_key=getattr(settings, "host_internal_key", None)
            or getattr(settings, "internal_key", None),
            timeout=float(getattr(settings, "introspect_timeout", 3.0) or 3.0),
        )
    else:
        _INSTANCE = _ADAPTERS[name]()
    _INSTANCE_FOR = name
    return _INSTANCE


def reset() -> None:
    """Drop the cached adapter. For tests, and for a config reload."""
    global _INSTANCE, _INSTANCE_FOR          # pylint: disable=global-statement

    _INSTANCE = None
    _INSTANCE_FOR = None
