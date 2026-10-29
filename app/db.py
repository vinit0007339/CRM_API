"""The Mongo connection and Beanie registration.

ONE DATABASE, TWO OWNERS. This service connects to the SAME database as the
platform. It owns 14 collections and writes to no others:

    companies   deals   pipelines   crm_activities   crm_associations
    crm_attributes   crm_exports   crm_files   crm_imports   crm_notes
    crm_saved_views   crm_settings   crm_tasks   crm_view_preferences

It READS three it does not own — `users` (owner expansion), `tags` (a
company's tag chips) and `contacts` (a company's Contacts tab, and resolving a
note's or task's target to a platform contact). Those three reads live in
`app/foreign.py` and nowhere else, so that splitting the databases later is a
change to one file instead of a search through 38,000 lines.

⚠️ `companies`, `deals` and `pipelines` ARE UNPREFIXED COLLECTION NAMES in a
shared database. Do not rename them to `crm_*`: every document would have to
be rewritten, and `scripts/repair_crm_typed_columns.py` already records what a
wrong collection name costs — it reported "nothing to repair" on exactly the
corrupt data it was written to find. Reserve the three names with whoever owns
the platform schema instead.
"""
from __future__ import annotations

from typing import Any, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import logger, settings


class Database:
    """Process-wide handles. Same shape as the monolith's, so moved code that
    says `Database.db` keeps working without a rewrite."""

    db_client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None


def _models() -> List[Any]:
    """The 15 CRM documents, in the monolith's registration order.

    Imported inside a function, not at module scope: `app/foreign.py` and the
    models both import `app.config`, and a model importing `app.db` at import
    time (several do, transitively, through the CRUD layer) would close the
    cycle. The monolith's registry has the same note beside the same import.

    ⚠️ THE ORDER IS COPIED FROM `app/db/models/registry.py`. Beanie does not
    document an ordering requirement and almost certainly does not have one —
    but "almost certainly" is not a thing to discover during a cutover, and
    index creation walks this list. Keeping the order identical removes the
    question.
    """
    from app.models.activity import CrmActivityModel
    from app.models.association import CrmAssociationModel
    from app.models.attribute import CrmAttributeModel
    from app.models.company import CompanyModel
    from app.models.contact import CrmContactModel
    from app.models.contact_profile import CrmContactProfileModel
    from app.models.deal import DealModel
    from app.models.export_run import CrmExportModel
    from app.models.file import CrmFileModel
    from app.models.import_run import CrmImportModel
    from app.models.note import CrmNoteModel
    from app.models.pipeline import PipelineModel
    from app.models.saved_view import CrmSavedViewModel
    from app.models.settings import CrmSettingsModel
    from app.models.task import CrmTaskModel
    from app.models.view_preference import CrmViewPreferenceModel

    return [
        CompanyModel, PipelineModel, DealModel, CrmAssociationModel,
        CrmActivityModel, CrmSettingsModel, CrmAttributeModel, CrmNoteModel,
        CrmFileModel, CrmTaskModel, CrmImportModel, CrmExportModel,
        CrmViewPreferenceModel, CrmSavedViewModel,
        # Phase 1 of docs/contact-unification-plan.md. Registered ahead of any
        # reader so the collection and its indexes exist before the read path
        # lands. Purely additive: no existing index is touched, so unlike
        # CrmContactModel below this one cannot conflict on deploy.
        CrmContactProfileModel,
        # ⚠️ LAST, as `app/db/models/registry.py` had it before the object
        # was removed — see the ordering note above. Restored in the same
        # position rather than tucked in beside CompanyModel, so a diff
        # against the monolith's registry still reads as identical.
        #
        # ⚠️ REGISTERING THIS CREATES ITS INDEXES, AND ONE OF THEM MAY
        # CONFLICT. `crm_contacts` was never dropped — the removal migration
        # deliberately destroyed nothing — so a database that carried the
        # object still has the OLD `uniq_crm_contact_email`, built with
        # `sparse=True`. `models/contact.py` now declares it partial, which
        # is the same name with different options, and `create_indexes`
        # answers that with IndexOptionsConflict. `allow_index_dropping` is
        # False here (see below), so Beanie will not resolve it for us.
        #
        # The failure is survivable by design — `init_beanie` errors are
        # logged and surface through /health/ready rather than crashing the
        # process — but readiness stays red until the stale index is
        # dropped. Run `scripts/check_crm_contact_index.py` BEFORE deploying
        # this to any environment that ever had the collection.
        CrmContactModel,
    ]


#: Filled at startup by `init_db`, and read by `/health` so a half-finished
#: registration is visible rather than assumed.
MODELS: List[Any] = []

#: Why model registration failed, or None. Readiness reports it.
INIT_ERROR: Optional[str] = None


def _client(uri: str) -> AsyncIOMotorClient:
    """A Motor client configured exactly as the monolith's.

    ⚠️ NO `tz_aware=True`, AND THAT IS NOT AN OVERSIGHT. The monolith builds
    its client without it, so PyMongo returns **naive** datetimes and every CRM
    timestamp goes onto the wire without an offset. The UI knows this and
    compensates (`Utility/functions/timestampZone.js` exists for it; a bug
    where `toIST` read those values as local time is what made a task created
    a moment ago read "5h ago").

    Turning tz_aware on here would change every timestamp this service emits,
    which would (a) make the Phase 5 dual-run diff light up everywhere and (b)
    silently fix and re-break the UI's compensation at the same time. If the
    naive-timestamp decision is to be revisited, it is one deliberate change
    across both services and the UI, not a flag flipped during a move.
    """
    uri = (uri or "").strip()
    kwargs: dict = {}
    # Atlas SRV needs TLS, and system cert discovery fails on some macOS
    # Python builds — the monolith carries the same certifi fallback.
    if uri.lower().startswith("mongodb+srv://"):
        try:
            import certifi                       # pylint: disable=import-outside-toplevel

            kwargs["tlsCAFile"] = certifi.where()
        except Exception:                         # pylint: disable=broad-except
            pass
    return AsyncIOMotorClient(uri, **kwargs)


async def init_db() -> None:
    """Connect, and register the models once there are any.

    Deliberately does NOT ping. Connecting is lazy in Motor, so a database that
    is down must not stop this process from starting: PM2 would restart-loop it
    and the `/health` endpoint — the one thing that could tell you the database
    is down — would never answer. Readiness is `/health/ready`, which pings.
    """
    if not settings.db_uri or not settings.db_name:
        logger.error(
            "CRM: no database configured (missing: "
            f"{', '.join(settings.missing) or 'db_uri/db_name'}). "
            "The service will start and report itself NOT ready.")
        return

    Database.db_client = _client(settings.db_uri)
    Database.db = Database.db_client[settings.db_name]

    global MODELS                              # pylint: disable=global-statement
    MODELS = _models()

    from beanie import init_beanie              # pylint: disable=import-outside-toplevel

    # ⚠️ `allow_index_dropping` IS FALSE, where the monolith sets it True.
    #
    # Beanie drops indexes it finds on a registered collection that the models
    # no longer declare. In the monolith that is survivable: it registers every
    # model, so "not declared" really does mean retired. Here it would mean
    # "not declared BY THIS SERVICE" — and during the dual-run of Phase 5 two
    # processes initialise Beanie against the same collections. A service
    # starting with an incomplete model list could drop the board's compound
    # index, whose definition in `models/deal.py` carries the line "Do not
    # reorder these keys". Losing it would not fail anything; the board would
    # just get slow, on production, with no error to find.
    #
    # Index changes are a migration, run deliberately. See the monolith's
    # `_reconcile_changed_indexes` for what a changed index actually costs.
    # ⚠️ `init_beanie` TALKS TO THE SERVER — it creates the declared indexes —
    # so it raises when Mongo is unreachable. Letting that propagate undoes the
    # whole point of the lifespan's design: startup would fail, PM2 would
    # restart-loop, and `/health`, the one thing that could tell you the
    # database is down, would never answer. Found by booting the service
    # against a dead Mongo after the models arrived in Phase 2 — Phase 1's
    # skeleton had no models, so it could not have shown this.
    #
    # So: log it loudly, record it, and let READINESS fail. A service that is
    # up and says "not ready, here is why" is debuggable; a crash loop is not.
    global INIT_ERROR                          # pylint: disable=global-statement
    try:
        await init_beanie(database=Database.db, document_models=MODELS,
                          allow_index_dropping=False)
    except Exception as ex:                    # pylint: disable=broad-except
        INIT_ERROR = f"{type(ex).__name__}: {ex}".replace("\n", " ")[:200]
        logger.exception(ex)
        logger.error("CRM: Beanie registration FAILED — the service is up but "
                     "NOT READY. No request that touches a model will work "
                     "until this is fixed. See /health/ready.")
        return
    INIT_ERROR = None
    logger.info("CRM: Beanie registered %d model(s) against %s",
                len(MODELS), settings.db_name)


async def close_db() -> None:
    """Drop the connection on the way down.

    ⚠️ SHUTDOWN MUST NOT RAISE. If closing fails, the traceback from this line
    replaces whatever was actually bringing the process down — so the thing you
    need to read is buried under a secondary failure in the cleanup. Log it and
    let the process go.
    """
    client = Database.db_client
    Database.db_client = None
    Database.db = None
    if client is None:
        return
    try:
        client.close()
    except Exception as ex:                       # pylint: disable=broad-except
        logger.warning("CRM: closing the database client failed: %s", ex)


async def ping(timeout_s: float = 2.0) -> tuple[bool, Optional[str]]:
    """`(reachable, error)` — used by readiness, never by a request path.

    ⚠️ THE TIMEOUT IS ENFORCED WITH `wait_for`, NOT `maxTimeMS`. Measured: with
    a configured-but-dead Mongo, `maxTimeMS` alone made this endpoint take
    **30 seconds** to answer, because the budget applies to the command and
    PyMongo spends its 30s default on SERVER SELECTION before any command is
    sent. A readiness probe that answers slower than the thing polling it is
    worse than no probe — nginx records a timeout, and the body that would
    have told you "connection refused" never arrives.

    The alternative, lowering `serverSelectionTimeoutMS` on the client, would
    also change every real query's behaviour and make this service diverge
    from the monolith it is being diffed against in Phase 5. So the client
    stays identical and readiness gets its own budget.
    """
    if Database.db is None:
        return False, "no database configured"
    if INIT_ERROR:
        # The ping might well succeed now — Mongo could have come back — but
        # the models are not registered in THIS process, so a request would
        # still fail. Report the real blocker, not the ping.
        return False, f"models not registered: {INIT_ERROR}"
    try:
        import asyncio                           # pylint: disable=import-outside-toplevel

        await asyncio.wait_for(
            Database.db.command({"ping": 1}), timeout=timeout_s)
        return True, None
    except asyncio.TimeoutError:
        # ⚠️ SAY SOMETHING USEFUL HERE. A bare `TimeoutError:` with an empty
        # message is what `wait_for` gives you, and it is indistinguishable
        # from a dozen other faults. The budget is ours, so name it, and name
        # the two things it actually means.
        return False, (
            f"no response within {timeout_s}s — the database is unreachable, "
            f"still starting, or the URI points somewhere that is not Mongo")
    except Exception as ex:                       # pylint: disable=broad-except
        # The message, not the traceback: readiness is polled, and a traceback
        # per poll turns the log into noise that hides the real failure.
        #
        # Truncated, because PyMongo's ServerSelectionTimeoutError carries a
        # full topology dump — ~900 characters of it — and a health body is
        # read by a human in a hurry.
        detail = f"{type(ex).__name__}: {ex}".replace("\n", " ")
        return False, detail[:200] + ("…" if len(detail) > 200 else "")


async def get_db() -> AsyncIOMotorDatabase:
    """FastAPI dependency. Mirrors the monolith's `get_main_db`."""
    if Database.db is None:
        raise RuntimeError("Database not initialized")
    return Database.db
