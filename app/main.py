"""The CRM service. FastAPI app, health, and (from Phase 2) the 12 CRM routers.

PHASE 1 — THIS IS THE SKELETON. It boots, connects, and answers health. No CRM
routers are mounted yet; `EXPECTED_ROUTERS` below records what Phase 2 must
reach, so "did the move finish?" is a question the service answers itself
rather than something somebody counts by hand.

THE PATHS DO NOT MOVE. Every endpoint stays at `/api/v1/crm/...`, exactly where
it is in the monolith today, because the cutover is an nginx rule — not a UI
change. The UI has one base URL and 83 CRM calls behind it; if the paths were
renamed here, every one of those would have to change and the rollback would
stop being "reload the old nginx config".
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import logger, settings
from app import db as db_module
from app.db import close_db, init_db, ping
from app.errors import (
    AuthError, AuthNotConfiguredError, PermissionDeniedError,
)
from app.profiles import DEFAULT_PROFILE, modules_for

#: The monolith's prefix, kept verbatim. See the module docstring.
API_ROOT_PATH = "/api/v1"

#: What Phase 2 has to arrive at: 83 endpoints across 12 routers, which is
#: what `scripts/check_crm_contract.py` counts in the monolith today. The
#: health endpoint reports the live numbers beside these, so a half-finished
#: move is visible rather than assumed.
EXPECTED_ROUTERS = 12
EXPECTED_ENDPOINTS = 83

#: ⚠️ FOR COUNTING ONLY. The mounts themselves are the sixteen literal
#: `include_router` calls in `_mount()`; this table must not grow into a loop
#: that replaces them (see the note there). It exists so `/health` can report
#: how many mounts a profile produces without anybody maintaining a second
#: hard-coded integer.
_MOUNT_TABLE = (
    ("pipelines.router", "pipelines"), ("companies.router", "companies"),
    ("deals.router", "deals"), ("crm_settings.router", "settings"),
    ("attributes.router", "attributes"),
    ("view_preferences.router", "view_preferences"),
    ("saved_views.router", "saved_views"), ("files.router", "files"),
    ("notes.router", "notes"), ("exports.router", "exports"),
    ("exports.company_router", "exports"), ("exports.deal_router", "exports"),
    ("imports.router", "imports"), ("tasks.router", "tasks"),
    ("tasks.types_router", "tasks"), ("deals.board_router", "deals"),
)

STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Connect on the way up, close on the way down.

    ⚠️ A FAILED CONNECTION DOES NOT STOP THE PROCESS. `init_db` logs and
    returns; the service starts and reports itself not ready. The alternative —
    raising here — gives you a PM2 restart loop and no `/health` to ask, which
    is the worst possible state to debug a bad `app.conf` from.
    """
    await init_db()

    # ⚠️ THE TASK-REMINDER SCHEDULER HAS EXACTLY ONE OWNER, AND CONFIG SAYS
    # WHICH. `app/services/task_reminder_scheduler.py` moved here with the
    # rest of the code, but the monolith's `api/main.py` keeps starting its
    # copy until Phase 7 deletes it. Run both and each polls every minute for
    # the same reminders — the DATA survives, because every reminder is claimed
    # with a conditional update on `reminder.sent_at`, but if that claim ever
    # regressed the symptom is a customer receiving two reminder emails and
    # telling you about it.
    #
    # So it is OFF by default and turned on by `[CRM] task_reminders = true`,
    # in the same deploy that removes the scheduler from the platform. A flag
    # rather than a code edit, because the cutover is the worst moment to be
    # changing code — and `/health` reports `task_reminders`, so "which side
    # owns it right now?" is one curl rather than two greps.
    app.state.task_reminder_scheduler = None
    app.state.task_reminder_task = None
    if settings.task_reminders:
        if db_module.INIT_ERROR:
            # Started against a database that never connected, the sweep would
            # log a failure a minute for as long as the process lives. The
            # readiness endpoint already reports INIT_ERROR; this just declines
            # to add noise on top of it.
            logger.error("task reminders are enabled but the database did not "
                         "connect (%s) — the scheduler is not started",
                         db_module.INIT_ERROR)
        else:
            import asyncio  # pylint: disable=import-outside-toplevel

            from app.crud import CrmTaskCrud  # pylint: disable=import-outside-toplevel
            from app.services.task_reminder_scheduler import (  # pylint: disable=import-outside-toplevel
                TaskReminderScheduler,
            )

            scheduler = TaskReminderScheduler(
                task_crud=CrmTaskCrud(db_session=db_module.Database.db),
                interval_seconds=60,
            )
            app.state.task_reminder_scheduler = scheduler
            app.state.task_reminder_task = asyncio.create_task(scheduler.start())
            logger.info("TaskReminderScheduler started (this process owns "
                        "CRM task reminders)")
    else:
        logger.info("task reminders are OFF in this process — the platform "
                    "still owns them (set [CRM] task_reminders = true in the "
                    "same deploy that removes it from api/main.py)")
    logger.info("CRM service up: env=%s port=%s routers=%d/%d endpoints=%d/%d",
                settings.environment, settings.port,
                MOUNTED_MODULES, EXPECTED_ROUTERS,
                _endpoint_count(app), EXPECTED_ENDPOINTS)
    try:
        yield
    finally:
        scheduler = getattr(app.state, "task_reminder_scheduler", None)
        task = getattr(app.state, "task_reminder_task", None)
        if scheduler is not None:
            # Ask it to stop, then cancel: `stop()` only clears the loop flag,
            # and the loop may be inside its one-minute sleep, which would hold
            # shutdown open for up to a minute on every restart.
            try:
                # ⚠️ `stop()` IS A COROUTINE. Calling it without awaiting
                # builds an object, clears nothing, and emits a
                # "coroutine was never awaited" warning that is easy to miss
                # in a shutdown log.
                await scheduler.stop()
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning("TaskReminderScheduler.stop() failed: %s", ex)
        if task is not None:
            task.cancel()
        await close_db()
        logger.info("CRM service down")


app = FastAPI(
    title="Engage CRM",
    version="0.1.0",
    description="The CRM module, extracted from emailMarketingAPI.",
    lifespan=lifespan,
)

# ⚠️ CORS IS ON IN EVERY ENVIRONMENT, AND IT HAS TO BE.
#
# The first version of this file enabled it only when `environment == "dev"`,
# on the reasoning that the edge would handle it. The edge does not: the
# platform's own `api/main.py` adds `CORSMiddleware` with an eleven-origin
# allow-list, because the UI is served from a different host than the API.
# Every CRM request from a browser is cross-origin.
#
# So at the moment nginx routes /api/v1/crm/ here, this service becomes
# responsible for those headers. Without them every CRM screen breaks on the
# reload — and nothing in the dual run would have predicted it, because
# neither `curl` nor `scripts/diff_against_platform.py` enforces CORS. The
# diff would have been green right up to the outage.
#
# The settings mirror the platform's exactly: same origins, same regex,
# credentials allowed, every method, every header, `expose_headers=["*"]` (the
# file download needs `Content-Disposition` readable) and the same max_age.
# `tests/test_cors.py` pins all of it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)


@app.middleware("http")
async def catch_exceptions(request, call_next):
    """One 500 handler, copied in spirit from the monolith's.

    The monolith's `catch_exceptions_middleware` logs the exception and answers
    `{"messages": ["Internal Server Error"], "success": false}`. Keeping both
    halves identical matters more than it looks: the UI's `unwrap()` reads that
    exact shape to decide `ok: false`, and Phase 5 diffs the two services'
    responses — including their failures.
    """
    try:
        return await call_next(request)
    except Exception as ex:                       # pylint: disable=broad-except
        logger.exception(ex)
        return JSONResponse(
            status_code=500,
            content={"messages": ["Internal Server Error"], "success": False},
        )


#: Set by `mount_crm_routers`. The modules mounted, and the mount count — 12
#: modules in 16 mounts, because four routers are published under a second
#: prefix (see the note on the mount table).
MOUNTED_MODULES: int = 0
MOUNTED_ROUTERS: int = 0


def _endpoint_count(application: FastAPI) -> int:
    """Method/path pairs under `/crm`, read from the OpenAPI schema.

    ⚠️ COUNTED FROM `openapi()`, NOT BY WALKING `app.routes`. Measured: on
    fastapi 0.143 each `include_router` call appears in `app.routes` as a
    single `_IncludedRouter` with no `.routes` to descend into, so a walker
    reported **0 endpoints on a fully mounted service** — the health endpoint
    lying in the one direction that matters, claiming the move had not landed
    when it had. The OpenAPI schema is the surface this service actually
    declares, and it reads the same on every version.
    """
    paths = application.openapi().get("paths", {})
    total = 0
    for path, operations in paths.items():
        if not path.startswith(f"{API_ROOT_PATH}/crm"):
            continue
        total += len([m for m in operations
                      if m.lower() not in ("head", "options", "parameters")])
    return total


# ── health ───────────────────────────────────────────────────────────────────
# TWO ENDPOINTS, AND THE SPLIT IS THE WHOLE POINT.
#
#   /health        liveness. Touches nothing. "Is the process answering?"
#   /health/ready  readiness. Pings Mongo. "Should traffic come here?"
#
# Collapsing them into one endpoint that pings the database is the classic
# mistake: Mongo blips, liveness fails, the supervisor kills a perfectly
# healthy process, and the restart cannot fix the thing that was broken. PM2
# and any uptime check watch `/health`; nginx and the Phase 6 cutover watch
# `/health/ready`.

@app.get("/health", tags=["health"])
async def health():
    """Liveness. Deliberately does not touch the database."""
    return {
        "status": "ok",
        "service": "crm",
        "version": app.version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        # How much of the move has landed. 0/12 while this is the skeleton.
        # Which profile, and what it actually mounted. ⚠️ `expected_*` ARE THE
        # `full` NUMBERS, so a `core` deployment reads 7/12 and 57/83 — that
        # is not a half-finished move, it is the profile doing its job, and
        # `/health/ready` knows the difference.
        "profile": getattr(settings, "profile", "full"),
        "routers": {"modules": MOUNTED_MODULES, "mounts": MOUNTED_ROUTERS,
                    "expected_modules": EXPECTED_ROUTERS},
        "endpoints": {"mounted": _endpoint_count(app),
                      "expected": EXPECTED_ENDPOINTS},
        # Which process owns the CRM task-reminder sweep. During the dual run
        # this is false here and the platform owns it; from Phase 7 it is true
        # here and nowhere else. `configured` is what config asked for,
        # `running` is what actually happened — they differ when the database
        # did not connect.
        "task_reminders": {
            "configured": bool(settings.task_reminders),
            "running": getattr(app.state, "task_reminder_task", None) is not None,
        },
    }


@app.get("/health/ready", tags=["health"])
async def ready():
    """Readiness: can this process serve a request that reads the database?

    Answers 503 when it cannot, so nginx and any orchestrator can act on the
    status code rather than parsing the body. `missing` names the config keys
    that are absent — a bad deploy says *which* value it needs instead of
    leaving you to guess from a connection error.
    """
    reachable, error = await ping()
    body = {
        "status": "ready" if reachable else "not_ready",
        "database": {
            "configured": bool(settings.db_uri and settings.db_name),
            "name": settings.db_name or None,
            "reachable": reachable,
            "error": error,
        },
        "missing_config": settings.missing,
        # ⚠️ READ THROUGH THE MODULE, not a `from app.db import MODELS`.
        # `init_db` REBINDS that global at startup, and a value imported
        # by name here would stay the empty list it was at import time —
        # so health would report 0 models against a fully registered
        # service. Same shape as the stubbing trap in tests/test_health.py.
        # ⚠️ "DECLARED" AND "REGISTERED" ARE DIFFERENT. `MODELS` is filled
        # before `init_beanie` runs, so reporting its length alone said
        # "models_registered: 14" on a service whose registration had just
        # FAILED against a dead Mongo. Readiness must not flatter itself.
        "models": {"declared": len(db_module.MODELS),
                   "registered": db_module.INIT_ERROR is None},
    }
    return JSONResponse(status_code=200 if reachable else 503, content=body)


# ── the CRM routers ──────────────────────────────────────────────────────────
# SIXTEEN MOUNTS ACROSS TWELVE MODULES, and the extra four are not a mistake.
# `exports` publishes a `company_router` and a `deal_router` so that
# `POST /crm/companies/export` sits under companies where the UI expects it;
# `tasks` publishes `types_router` for `/crm/task-types`; `deals` publishes
# `board_router` for `/crm/pipelines/{id}/board`. Every prefix below is copied
# from `api/routes_handler.py` verbatim — a renamed path here would mean a
# change in the UI and a cutover that cannot be rolled back with one nginx
# reload.
#
# Imported inside the function, the way the monolith's routes_handler does it,
# so that app creation cannot be broken by an import cycle in a moved module.
def mount_crm_routers() -> None:
    from app.routes import (                     # pylint: disable=import-outside-toplevel
        attributes, companies, deals, exports, files, imports, notes,
        pipelines, saved_views, settings as crm_settings, tasks,
        view_preferences,
    )

    # ⚠️ SIXTEEN LITERAL CALLS, NOT A LOOP OVER A TABLE. The first version of
    # this function looped over `(router, prefix, tag)` tuples, which reads
    # better and broke `scripts/check_crm_contract.py`: the checker reads these
    # mounts BY AST, and a loop gives it `include_router(router, prefix=f"…
    # {prefix}")` — no literal module, no literal path. It fell back to
    # guessing the prefix from the module name and reported ten endpoints
    # missing and twelve documented-but-absent, all of them phantoms
    # (`/crm/saved_views` for `/crm/saved-views`).
    #
    # The contract check is worth more than the tidier loop. Same calls, same
    # order, same prefixes as `api/routes_handler.py`.
    #
    # ⚠️ EACH ONE IS NOW GATED ON THE PROFILE (`app/profiles.py`), AND THE
    # GATE IS AN `if` AROUND A LITERAL CALL RATHER THAN A FILTERED LOOP — for
    # exactly the reason above. The checker walks this file's AST and needs a
    # literal module name and a literal prefix to find; an `if` leaves both
    # where they were.
    modules = modules_for(getattr(settings, "profile", DEFAULT_PROFILE))

    if "pipelines" in modules:
        app.include_router(
            pipelines.router,
            prefix=f"{API_ROOT_PATH}/crm/pipelines",
            tags=["CRM Pipelines"],
        )

    if "companies" in modules:
        app.include_router(
            companies.router,
            prefix=f"{API_ROOT_PATH}/crm/companies",
            tags=["CRM Companies"],
        )

    if "deals" in modules:
        app.include_router(
            deals.router,
            prefix=f"{API_ROOT_PATH}/crm/deals",
            tags=["CRM Deals"],
        )

    if "settings" in modules:
        app.include_router(
            crm_settings.router,
            prefix=f"{API_ROOT_PATH}/crm/settings",
            tags=["CRM Settings"],
        )

    if "attributes" in modules:
        app.include_router(
            attributes.router,
            prefix=f"{API_ROOT_PATH}/crm/attributes",
            tags=["CRM Attributes"],
        )

    if "view_preferences" in modules:
        app.include_router(
            view_preferences.router,
            prefix=f"{API_ROOT_PATH}/crm/view-preferences",
            tags=["CRM View Preferences"],
        )

    if "saved_views" in modules:
        app.include_router(
            saved_views.router,
            prefix=f"{API_ROOT_PATH}/crm/saved-views",
            tags=["CRM Saved Views"],
        )

    if "files" in modules:
        app.include_router(
            files.router,
            prefix=f"{API_ROOT_PATH}/crm/files",
            tags=["CRM Files"],
        )

    if "notes" in modules:
        app.include_router(
            notes.router,
            prefix=f"{API_ROOT_PATH}/crm/notes",
            tags=["CRM Notes"],
        )

    if "exports" in modules:
        app.include_router(
            exports.router,
            prefix=f"{API_ROOT_PATH}/crm/exports",
            tags=["CRM Exports"],
        )

    if "exports" in modules:
        app.include_router(
            exports.company_router,
            prefix=f"{API_ROOT_PATH}/crm/companies",
            tags=["CRM Exports"],
        )

    if "exports" in modules:
        app.include_router(
            exports.deal_router,
            prefix=f"{API_ROOT_PATH}/crm/deals",
            tags=["CRM Exports"],
        )

    if "imports" in modules:
        app.include_router(
            imports.router,
            prefix=f"{API_ROOT_PATH}/crm/imports",
            tags=["CRM Imports"],
        )

    if "tasks" in modules:
        app.include_router(
            tasks.router,
            prefix=f"{API_ROOT_PATH}/crm/tasks",
            tags=["CRM Tasks"],
        )

    if "tasks" in modules:
        app.include_router(
            tasks.types_router,
            prefix=f"{API_ROOT_PATH}/crm/task-types",
            tags=["CRM Tasks"],
        )

    if "deals" in modules:
        app.include_router(
            deals.board_router,
            prefix=f"{API_ROOT_PATH}/crm/pipelines",
            tags=["CRM Deals"],
        )


    # ⚠️ THE INTERNAL ROUTER IS MOUNTED OUTSIDE `/crm`, ON PURPOSE.
    #
    # nginx sends `/api/v1/crm/` here from the public hostname. This endpoint
    # takes an account id and acts on it with no end-user token in the
    # request — it is called by the platform, after the platform's own
    # permission check — so it must sit behind the same `/api/v1/internal/`
    # deny rule as the platform's auth endpoint, not in the public CRM path.
    #
    # It is also why the documented surface is unchanged:
    # `scripts/check_crm_contract.py` counts endpoints under `/crm` only, so
    # this one is deliberately invisible to it. An internal endpoint in the
    # public contract is an invitation.
    from app.routes import internal           # pylint: disable=import-outside-toplevel

    app.include_router(internal.router,
                       prefix=f"{API_ROOT_PATH}/internal",
                       tags=["Internal"])

    global MOUNTED_MODULES, MOUNTED_ROUTERS    # pylint: disable=global-statement
    # ⚠️ COUNTED, NOT HARD-CODED. These were the literals 16 and 12, which was
    # correct for exactly one profile. A number that is right for `full` and
    # silently wrong for `core` is worse than no number, because `/health`
    # reports it and the readiness check compares against it.
    MOUNTED_ROUTERS = sum(1 for _r, m in _MOUNT_TABLE if m in modules)
    MOUNTED_MODULES = len(modules)


mount_crm_routers()


# ── errors the CRM raises that the framework renders ─────────────────────────
@app.exception_handler(PermissionDeniedError)
async def permission_denied_handler(_, ex: PermissionDeniedError):
    """⚠️ HTTP 200, NOT 403 — the monolith's shape, kept exactly.

    This codebase has two error shapes: a BUSINESS failure is
    `{"status": "failure", "message": …, "data": null}` at 200, and a
    validation failure is a 422. The UI's `unwrap()` decides `ok` from
    `status === "success"`, so a 403 here would reach the screen as a
    network-shaped error with no message — "Something went wrong. Please try
    again." instead of "You don't have permission…".
    """
    return JSONResponse(
        status_code=200,
        content={"status": "failure", "message": ex.message, "data": None},
    )


@app.exception_handler(AuthError)
async def auth_error_handler(_, ex: AuthError):
    """⚠️ THE PLATFORM'S `ApiError` ENVELOPE, NOT THE BUSINESS-FAILURE ONE.

    `{"message", "messages", "success": false}` at the real status code —
    exactly what the monolith's handler emits. The UI's `unwrap()` branches on
    `body.success === false` to find the message, so a differently-shaped 401
    reaches the screen as "Something went wrong. Please try again." with the
    actual reason nowhere. Both halves are compared in Phase 5.
    """
    return JSONResponse(
        status_code=ex.status_code,
        content={"message": ex.message, "messages": [ex.message],
                 "success": False},
    )


@app.exception_handler(AuthNotConfiguredError)
async def auth_not_configured_handler(_, ex: AuthNotConfiguredError):
    """503: this deployment cannot authenticate, which is not the caller's fault.

    A missing public key or platform URL is a deploy problem. Answering 401
    would tell every user their credentials are bad and send them to reset a
    password that is fine.
    """
    return JSONResponse(
        status_code=503,
        content={"message": ex.message, "messages": [ex.message],
                 "success": False},
    )
