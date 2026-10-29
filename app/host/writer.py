"""Writing to host collections. Phase 5 of docs/contact-unification-plan.md.

⚠️ **A SEPARATE MODULE FROM `directory.py`, AND A SEPARATE DOOR FROM
`foreign.py`, ON PURPOSE.** `foreign.py` says plainly: *"There is no `write_*`
function here, and there should never be one. A write to a host collection is
a request to the host, not a query."* That sentence is honoured, not
overruled. Reads stay read-only in that file; writes get their own named door
(`app/host_writes.py`) over this interface, so the two can never be confused
at a call site and `foreign.py`'s count of host reads stays meaningful.

⚠️ **WHY A WRITE EXISTS AT ALL NOW.** Until the contact unification there was
nothing to write: the CRM owned its own people. With one person shared between
marketing and sales, creating a contact in the CRM *is* creating a marketing
contact. There is no version of that which does not write to `contacts`.

⚠️ **THE POLICY IS NOT HERE.** This interface is mechanical: insert a row,
set fields on a row, add an owner to a row. Whether an address may be created
(the global unique index), whether this account may modify a row it did not
create (`contact_scope.may_write`), and what a contact is allowed to be
created *as* all live in the route. An adapter that made those decisions would
make them three times, differently.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

from app.config import logger, settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ContactWriter(Protocol):
    """What a host must accept. `db` is the CRM's own handle, as in Directory."""

    async def create_contact(self, db: Any, account_id: Any,
                             fields: Dict[str, Any]) -> Optional[Dict[str, Any]]: ...

    async def update_contact(self, db: Any, account_id: Any, contact_id: Any,
                             fields: Dict[str, Any]) -> bool: ...

    async def attach_owner(self, db: Any, account_id: Any,
                           contact_id: Any) -> bool: ...

    #: ⚠️ ONE CALL, BECAUSE THE INDEX FORCES IT. `Contact.email` is globally
    #: unique, so "create unless it exists" cannot be a scoped read followed
    #: by an insert: the read misses another account's row, the insert then
    #: violates the index, and a routine repeat import becomes a hard failure
    #: in a customer's face. `app/services/contact_scope.py` documents this at
    #: length. The global read is a mechanical consequence of the index, not a
    #: policy choice, which is why it lives in the adapter rather than the
    #: route. Returns (row, created).
    async def find_or_create_contact(
            self, db: Any, account_id: Any, email: Optional[str],
            fields: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]: ...


# ── the default: one database, shared with the host ─────────────────────────

class SharedDbWriter:
    """Writes straight into `contacts`, for a CRM deployed beside its host."""

    name = "shared_db"

    @staticmethod
    def _collection(db: Any):
        return None if db is None else db["contacts"]

    async def create_contact(self, db, account_id, fields):
        """Insert a person and return the stored row.

        ⚠️ `created_by` AND `owner_ids` ARE SET HERE AND NOT BY THE CALLER.
        They are this collection's tenancy, and `contact_scope` reads BOTH —
        `created_by` is who made the row, `owner_ids` is everyone since
        granted it. A row written with only the first is invisible to the
        predicate's second arm the moment anybody shares it.
        """
        collection = self._collection(db)
        if collection is None:
            return None
        from app.object_id import PyObjectId

        try:
            acct = PyObjectId(str(account_id))
        except Exception:                         # pylint: disable=broad-except
            return None

        doc = dict(fields or {})
        doc["created_by"] = acct
        doc["owner_ids"] = [acct]
        doc.setdefault("created_at", _utcnow())
        try:
            result = await collection.insert_one(doc)
            doc["_id"] = result.inserted_id
            return doc
        except Exception as ex:                   # pylint: disable=broad-except
            # Re-raised, NOT swallowed: a duplicate address is the caller's to
            # interpret (it means "attach to the existing person"), and a
            # swallowed insert would read as a silent no-op.
            logger.exception(ex)
            raise

    async def update_contact(self, db, account_id, contact_id, fields):
        """Set fields on a contact THIS ACCOUNT MAY WRITE.

        ⚠️ THE SCOPE IS IN THE FILTER, NOT IN A PRIOR READ. A check-then-write
        is a race, and the row being raced for belongs to another tenant. The
        update matches on the scope predicate so a row this account does not
        hold simply does not match, and the caller sees `False`.
        """
        collection = self._collection(db)
        if collection is None or not fields:
            return False
        from app.object_id import PyObjectId
        from app.services import contact_scope as cs

        try:
            oid = PyObjectId(str(contact_id))
        except Exception:                         # pylint: disable=broad-except
            return False
        try:
            res = await collection.update_one(
                cs.scoped({"_id": oid}, account_id), {"$set": dict(fields)})
            return bool(getattr(res, "modified_count", 0))
        except Exception as ex:                   # pylint: disable=broad-except
            logger.exception(ex)
            return False

    async def find_or_create_contact(self, db, account_id, email, fields):
        """Create the person, or adopt the one that already holds the address.

        ⚠️ THE GLOBAL READ IS DELIBERATE AND IS THE ONLY ONE IN THIS FILE.
        Scoping it would miss another account's row and the insert below would
        then duplicate-key. `contact_scope` spells out why these paths cannot
        be scoped while the unique index stands; the mitigation is that
        nothing here WRITES to a row it found globally beyond adding this
        account to `owner_ids`, which is how the collection is designed to be
        shared.

        ⚠️ AND THE INSERT IS STILL GUARDED. Two requests can both miss and
        both insert; the index lets one win, and the loser re-reads instead of
        failing. The read-then-write is a race whichever way round it is done,
        so the index is the arbiter rather than the check.
        """
        collection = self._collection(db)
        if collection is None:
            return None, False

        address = str(email or "").strip()
        if address:
            import re
            pattern = {"$regex": f"^{re.escape(address)}$", "$options": "i"}
            found = await collection.find_one({"email": pattern})
            if found is not None:
                await self.attach_owner(db, account_id, found.get("_id"))
                return found, False

        try:
            return await self.create_contact(db, account_id, fields), True
        except Exception as ex:                   # pylint: disable=broad-except
            if ex.__class__.__name__ != "DuplicateKeyError":
                raise
            if not address:
                raise
            import re
            pattern = {"$regex": f"^{re.escape(address)}$", "$options": "i"}
            found = await collection.find_one({"email": pattern})
            if found is None:
                raise
            await self.attach_owner(db, account_id, found.get("_id"))
            return found, False

    async def attach_owner(self, db, account_id, contact_id):
        """Grant this account a contact that already exists.

        ⚠️ THE ANSWER TO THE GLOBAL UNIQUE INDEX. Two accounts cannot each
        have a row for `priya@acme.com`; `owner_ids` is how they share one.
        `$addToSet`, so attaching twice is not two entries — this runs on
        every create that collides, which is every repeat import.
        """
        collection = self._collection(db)
        if collection is None:
            return False
        from app.object_id import PyObjectId

        try:
            oid = PyObjectId(str(contact_id))
            acct = PyObjectId(str(account_id))
        except Exception:                         # pylint: disable=broad-except
            return False
        try:
            res = await collection.update_one(
                {"_id": oid}, {"$addToSet": {"owner_ids": acct}})
            return bool(getattr(res, "modified_count", 0))
        except Exception as ex:                   # pylint: disable=broad-except
            logger.exception(ex)
            return False


# ── the host over HTTP ──────────────────────────────────────────────────────

class HttpWriter:
    """Posts the write to the host as a request, which is what it is.

    ⚠️ **THE HOST ENDPOINT DOES NOT EXIST YET.** The client half is written so
    the adapter satisfies the Protocol and a host that implements it works
    immediately; the platform side is tracked with `contacts_page` from phase
    4b.

    ⚠️ AND UNLIKE A READ, A FAILED WRITE RAISES. `HttpDirectory` degrades a
    read to empty because a missing owner name must not fail a list. A write
    that quietly did nothing would tell the user their contact was saved.
    """

    name = "http"

    def __init__(self, base_url: str, internal_key: Optional[str],
                 timeout: float = 5.0):
        self.base_url = (base_url or "").rstrip("/")
        self.internal_key = internal_key
        self.timeout = timeout

    async def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        import httpx

        url = f"{self.base_url}/api/v1/internal/directory/write"
        headers = {"X-Internal-Key": self.internal_key or ""}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json() or {}

    async def create_contact(self, db, account_id, fields):
        body = await self._post({"kind": "contact_create",
                                 "account_id": str(account_id or ""),
                                 "fields": dict(fields or {})})
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else None

    async def update_contact(self, db, account_id, contact_id, fields):
        body = await self._post({"kind": "contact_update",
                                 "account_id": str(account_id or ""),
                                 "contact_id": str(contact_id),
                                 "fields": dict(fields or {})})
        return bool((body or {}).get("status") == "success")

    async def attach_owner(self, db, account_id, contact_id):
        body = await self._post({"kind": "contact_attach_owner",
                                 "account_id": str(account_id or ""),
                                 "contact_id": str(contact_id)})
        return bool((body or {}).get("status") == "success")

    async def find_or_create_contact(self, db, account_id, email, fields):
        # One request, because the host owns the index and must arbitrate.
        body = await self._post({"kind": "contact_find_or_create",
                                 "account_id": str(account_id or ""),
                                 "email": str(email or ""),
                                 "fields": dict(fields or {})})
        data = body.get("data") if isinstance(body, dict) else None
        row = data.get("contact") if isinstance(data, dict) else None
        created = bool(data.get("created")) if isinstance(data, dict) else False
        return (row if isinstance(row, dict) else None), created


# ── no host ─────────────────────────────────────────────────────────────────

class NoWriter:
    """No host, so no person to write.

    ⚠️ IT REFUSES RATHER THAN PRETENDING, which is the opposite of
    `NoDirectory`. A read that answers empty degrades a screen; a write that
    answers "fine" and stores nothing loses a user's data and tells them it
    was saved.
    """

    name = "none"

    async def create_contact(self, db, account_id, fields):
        raise RuntimeError(
            "This CRM is not configured with a host, so it cannot create a "
            "marketing contact. Set [CRM] host_directory to shared_db or http.")

    async def update_contact(self, db, account_id, contact_id, fields):
        raise RuntimeError(
            "This CRM is not configured with a host, so it cannot edit a "
            "marketing contact.")

    async def attach_owner(self, db, account_id, contact_id):
        raise RuntimeError(
            "This CRM is not configured with a host, so it cannot attach a "
            "marketing contact.")

    async def find_or_create_contact(self, db, account_id, email, fields):
        raise RuntimeError(
            "This CRM is not configured with a host, so it cannot create a "
            "marketing contact. Set [CRM] host_directory to shared_db or http.")


#: A dict, not an if/elif chain — the same reason `directory._ADAPTERS` is one.
_WRITERS = {
    SharedDbWriter.name: SharedDbWriter,
    HttpWriter.name: HttpWriter,
    NoWriter.name: NoWriter,
}

_INSTANCE: Optional[ContactWriter] = None
_INSTANCE_FOR: Optional[str] = None


def get_writer() -> ContactWriter:
    """The configured writer, built once.

    ⚠️ KEYED ON `host_directory`, NOT A SETTING OF ITS OWN. A deployment that
    reads its host over HTTP and writes to it directly would be a
    misconfiguration nobody could see, and a second setting is how that
    happens. One host, one transport.
    """
    global _INSTANCE, _INSTANCE_FOR                # pylint: disable=global-statement

    configured = (getattr(settings, "host_directory", "") or "shared_db").strip()
    if _INSTANCE is not None and _INSTANCE_FOR == configured:
        return _INSTANCE

    cls = _WRITERS.get(configured, SharedDbWriter)
    if cls is HttpWriter:
        _INSTANCE = HttpWriter(
            getattr(settings, "host_base_url", "") or "",
            getattr(settings, "host_internal_key", None),
        )
    else:
        _INSTANCE = cls()
    _INSTANCE_FOR = configured
    return _INSTANCE
