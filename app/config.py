"""Configuration and the logger. The whole of what `core_app` was to the CRM.

WHY THIS FILE IS SO SMALL. The CRM code referenced the monolith's `core_app`
object 85 times, and **every single one of them was `core_app.logger`**. Not
one config read, no app state, no service locator. So the thing that looked
like the deepest coupling in the extraction was a logger handle.

What the service genuinely needs from configuration is the database, the JWT
public key (Phase 4) and the S3 bucket (Phase 2, for `crm_files`). Each is read
once, here, and nowhere else.

THE FILE FORMAT IS THE MONOLITH'S, UNCHANGED — `config/app.conf`, ConfigParser,
the same section and option names, the same `MAIN_DB_URI` / `MAIN_DB_NAME`
environment overrides. Not because INI is a good choice in 2026, but because
the droplet already has that file, the deploy already writes it, and anyone
debugging at 2am already knows where it lives. A new config format is a change
to make on purpose, later, not as a side effect of moving code.
"""
from __future__ import annotations

import logging
import os
from configparser import ConfigParser
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional

#: Where `config/app.conf` lives, relative to the repo root. `CRM_CONFIG_PATH`
#: overrides it, which is how the test suite and a one-off script point at
#: something else without editing anything.
DEFAULT_CONFIG_PATH = os.environ.get(
    "CRM_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "config", "app.conf"),
)


#: The platform's allow-list, verbatim from `api/main.py`. A literal here
#: rather than an import — there is nothing to import across a repo boundary —
#: so `tests/test_cors.py` pins it and says where to look when it drifts.
PLATFORM_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://167.71.19.118:8005",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "https://stage-engage.nevtan.com",
    "https://engage.nevtan.com",
    "https://main.d2eih31zb4hi4c.amplifyapp.com",
    "https://main.d16nzx6a9di3av.amplifyapp.com",
    "https://marketing.nevtan.com",
    "https://stage-marketing.nevtan.com",
)


#: ⚠️ THE DEFAULT HOST ADAPTERS, DEFINED ONCE. These two names are the
#: platform's behaviour, and they were written out FOUR times in the first
#: version of this code: the dataclass fields below, the `or` fallbacks in
#: `_load`, and again in each of `get_identity()` / `get_directory()`. Mutating
#: either dataclass field changed nothing — the loader always overwrote it — so
#: two mutation tests aimed straight at "did the platform's defaults change?"
#: both SURVIVED. Four copies of one rule, and the only one that was load
#: bearing was the one hardest to find.
#:
#: Same class of bug as the two coercion maps that produced the
#: `expected_close_date='cylonsign.com'` 500, and the two copies of the
#: system-attribute set before that. If you add a third adapter, its name goes
#: in the adapter module and these constants stay the only defaults.
DEFAULT_HOST_IDENTITY = "introspect"
DEFAULT_HOST_DIRECTORY = "shared_db"


@dataclass
class Settings:
    """Everything this service reads from its environment, in one place."""

    # ── database ────────────────────────────────────────────────────────────
    db_uri: str = ""
    db_name: str = ""

    # ── auth (Phase 4) ──────────────────────────────────────────────────────
    #: PEM public key for RS256 verification. The tokens are ASYMMETRIC, so
    #: this service verifies them without a shared secret and without asking
    #: the platform — which matters, because `require()` runs on all 83
    #: endpoints and a per-request round trip would be the service's slowest
    #: component by far.
    jwt_public_key: Optional[str] = None
    jwt_algorithm: str = "RS256"
    #: Where the platform API lives, for `/internal/auth/context`. The CRM
    #: verifies tokens itself but asks the platform WHO the caller is — see
    #: `app/auth/introspect.py` for why that is not a copy of the monolith's
    #: identity code.
    platform_base_url: Optional[str] = None
    #: Shared secret for the internal endpoint, sent as `X-Internal-Key`. The
    #: second of two locks; the first is an nginx rule that refuses
    #: /api/v1/internal/ from off-box.
    internal_key: Optional[str] = None
    #: Seconds. Short on purpose: this call sits in front of every request, so
    #: a slow platform must fail fast and visibly rather than holding workers.
    introspect_timeout: float = 3.0

    #: Whether THIS process runs the CRM task-reminder sweep.
    #:
    #: ⚠️ DEFAULT OFF, AND THAT IS THE SAFE DIRECTION. Exactly one process may
    #: own it: the monolith's `api/main.py` starts its copy until Phase 7
    #: deletes it. Two owners is not a data problem — each reminder is claimed
    #: with a conditional update on `reminder.sent_at` — but during the dual
    #: run both would poll every minute, and if that claim ever regressed the
    #: symptom is a customer receiving two reminder emails and telling you.
    #:
    #: It is a CONFIG FLAG rather than a code change so the cutover does not
    #: need an edit at the moment of highest risk: set it true here in the same
    #: deploy that removes the scheduler from the platform. `/health` reports
    #: which side owns it, so "did I remember?" is one curl.
    task_reminders: bool = False

    # ── the host boundary (see app/host/) ───────────────────────────────────
    #: Which identity adapter: "introspect" (ask the host per request) or
    #: "jwt_claims" (read the verified token's claims).
    #:
    #: ⚠️ THE DEFAULTS ARE THE PLATFORM'S BEHAVIOUR, and that is load-bearing,
    #: not polite. `emailMarketingAPI` must get out of the host refactor
    #: exactly what it had going in — the 891 integration tests and the Phase 5
    #: dual-run diff are only meaningful while that holds — so an existing
    #: deployment needs NO config change at all.
    #: How much of the CRM to serve: "full" (Engage, and the default), "core"
    #: or "minimal". See app/profiles.py — including why this is a profile
    #: rather than a second, smaller repository.
    profile: str = "full"
    host_identity: str = DEFAULT_HOST_IDENTITY
    #: Which directory adapter: "shared_db" (one Mongo with the host),
    #: "http" (one endpoint on the host), or "none" (degrade gracefully).
    host_directory: str = DEFAULT_HOST_DIRECTORY
    #: Where the host lives, for the http adapters. Falls back to
    #: `platform_base_url` so the existing config keeps working.
    host_base_url: Optional[str] = None
    #: Shared secret for the host's internal endpoints. Falls back to
    #: `internal_key` for the same reason.
    host_internal_key: Optional[str] = None
    #: For `jwt_claims` only: override which claim carries which field, because
    #: no two auth servers agree. `{"account_id": "org", "role": "roles.0"}`.
    host_claims_map: Dict[str, str] = field(default_factory=dict)

    # ── file storage (Phase 2) ──────────────────────────────────────────────
    #: ⚠️ THESE TWO WERE MISSED IN THE EXTRACTION, and `file_storage.py` has
    #: been reading them off a `core_app` that does not exist in this service
    #: — a NameError on the first file upload or CSV export. They are the
    #: platform's own names because the droplet's app.conf is the file this
    #: service is pointed at.
    aws_access_key: Optional[str] = None
    aws_secret_key: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_region: Optional[str] = None
    #: Where `crm_files` are written when S3 is not configured. The dev
    #: fallback the file routes already have.
    local_file_dir: str = "data/crm_files"

    # ── process ─────────────────────────────────────────────────────────────
    log_dir: str = "logs"
    port: int = 8100
    #: Free-form, for `/health` to report. "stage" | "production" | "dev".
    environment: str = "dev"

    # ── CORS ────────────────────────────────────────────────────────────────
    #: ⚠️ THIS LIST IS THE CUTOVER'S SHARPEST EDGE, AND IT IS INVISIBLE TO
    #: EVERY OTHER CHECK. The UI is served from a DIFFERENT ORIGIN than the
    #: API (`https://stage-engage.nevtan.com` calling the API host), so every
    #: CRM request from a browser is cross-origin. The platform answers them
    #: with `Access-Control-Allow-Origin` for exactly these origins.
    #:
    #: The moment nginx routes /api/v1/crm/ here, THIS service has to send
    #: those headers. If it does not, every CRM screen breaks the instant the
    #: rule is reloaded — while `curl` keeps working perfectly, and so does
    #: `scripts/diff_against_platform.py`, because neither enforces CORS. The
    #: dual run would have been green all the way to the outage.
    #:
    #: Copied from `api/main.py`'s middleware and pinned by
    #: `tests/test_cors.py`. Override with [CRM] cors_origins (comma-separated)
    #: when the platform's list changes.
    cors_origins: List[str] = field(default_factory=lambda: list(PLATFORM_CORS_ORIGINS))
    cors_origin_regex: str = r"http://localhost:\d+"

    missing: list = field(default_factory=list)


def _logger(log_dir: str) -> logging.Logger:
    """A logger whose output is byte-for-byte the monolith's.

    ⚠️ THE FORMAT IS COPIED ON PURPOSE, down to the trailing dashes:

        ERROR-2026-10-23 14:25:19 :'CrmAttributeCrud' object has no attribute…
        ---------

    Every runbook, every grep the team has in its shell history, and every log
    line anyone pastes into a chat looks like that. A service that formats its
    logs differently makes the first five minutes of every incident about the
    logs. Rotating at 5MB × 10 files, also as the monolith does.
    """
    os.makedirs(log_dir, exist_ok=True)
    log = logging.getLogger("crm_service")
    if log.handlers:                      # idempotent: uvicorn --reload re-imports
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    formatter = logging.Formatter(
        "%(levelname)s-%(asctime)s :%(message)s\n---------", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "crm_service.log"),
        maxBytes=5 * 1024 * 1024, backupCount=10)
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    # Also to stdout, which is what PM2 captures — the monolith's file-only
    # logger means `pm2 logs` shows nothing useful, and that is not worth
    # reproducing faithfully.
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    log.addHandler(stream)
    return log


def _section_items(parser: ConfigParser, section: str) -> Dict[str, str]:
    """Every key in one optional section, or `{}`. Never raises.

    A whole section is the right shape for a claim map: one mistyped line
    costs one claim name, where a JSON blob in a single option would cost the
    entire host configuration to a stray brace.
    """
    if not parser.has_section(section):
        return {}
    return {k: (v or "").strip() for k, v in parser.items(section)
            if (v or "").strip()}


def _reset_host_adapters() -> None:
    """Drop the cached host adapters so a reload actually takes effect.

    ⚠️ IMPORTED LATE AND FAILURES SWALLOWED, DELIBERATELY. `app.host` imports
    `app.config` for `settings`, so importing it at module scope here is a
    circular import — and config is the module everything else imports first,
    so the cycle would surface as an unrelated ImportError at startup. During
    the very first `_load()` at import time the host package does not exist
    yet either; there is nothing cached, so there is nothing to reset.
    """
    try:
        from app.host import directory, identity

        directory.reset()
        identity.reset()
    except Exception:                             # pylint: disable=broad-except
        pass


def _load(path: str) -> Settings:
    """Read the config file. A missing option is recorded, never fatal.

    ⚠️ A MISSING VALUE MUST NOT KILL THE PROCESS AT IMPORT TIME. The monolith
    reads config in `App.__init__`, so a stale `app.conf` on the droplet turns
    into an import-time crash with a ConfigParser traceback and no running
    service to ask. Here, what is missing is collected and reported by
    `/health`, so a misconfigured deploy answers "ready: false, missing:
    [db_uri]" instead of refusing to start.
    """
    settings = Settings()
    parser = ConfigParser(interpolation=None)
    read_ok = parser.read(path)

    def get(section: str, option: str, default=None):
        try:
            return parser.get(section, option)
        except Exception:                 # pylint: disable=broad-except
            return default

    if not read_ok:
        settings.missing.append(f"config file not found at {path}")

    settings.db_uri = get("MAIN_DATABASE", "db_uri", "") or ""
    settings.db_name = get("MAIN_DATABASE", "db_name", "") or ""
    # The same environment overrides the monolith honours, so a droplet that
    # sets MAIN_DB_URI keeps working when the CRM lands beside it.
    settings.db_uri = os.environ.get("MAIN_DB_URI", "").strip() or settings.db_uri
    settings.db_name = os.environ.get("MAIN_DB_NAME", "").strip() or settings.db_name

    settings.log_dir = get("CRM", "log_dir", None) or get("MAIN", "log_dir", None) \
        or settings.log_dir
    settings.environment = (os.environ.get("CRM_ENV", "").strip()
                            or get("CRM", "environment", None)
                            or get("MAIN", "environment", None)
                            or settings.environment)
    try:
        settings.port = int(os.environ.get("CRM_PORT", "").strip()
                            or get("CRM", "port", None)
                            or settings.port)
    except (TypeError, ValueError):
        settings.missing.append("port is not a number; using the default")

    # Phase 2/4 read these; absent is fine until then.
    configured_origins = get("CRM", "cors_origins", None)
    if configured_origins:
        settings.cors_origins = [o.strip() for o in configured_origins.split(",")
                                 if o.strip()]
    settings.cors_origin_regex = (get("CRM", "cors_origin_regex", None)
                                  or settings.cors_origin_regex)

    # Credentials from [AWS] if present, else the platform's [MAIN] spelling —
    # the droplet points this service at the platform's own app.conf.
    settings.aws_access_key = (get("AWS", "access_key", None)
                               or get("MAIN", "aws_access_key", None))
    settings.aws_secret_key = (get("AWS", "secret_key", None)
                               or get("MAIN", "aws_secret_key", None))
    # ⚠️ `[MAIN] s3_bucket` IS IN THE FALLBACK CHAIN BECAUSE THAT IS WHERE THE
    # PLATFORM KEEPS IT. The credentials and region above already fall back to
    # `[MAIN]`; the bucket did not, so pasting the platform's `[MAIN]` block
    # into this file gave you keys and a region and NO bucket — and a missing
    # bucket means `s3_available()` is False, which silently routes every
    # attachment to local disk instead of erroring.
    settings.s3_bucket = (get("AWS", "s3_bucket", None) or get("S3", "bucket", None)
                          or get("MAIN", "s3_bucket", None))
    settings.s3_region = (get("AWS", "region", None) or get("S3", "region", None)
                          or get("MAIN", "aws_region", None))
    # ⚠️ THE DEFAULT PATH IS THE MONOLITH'S OWN LAYOUT:
    # `<config_dir>/jwt/jwt_rs256.key.pub`, which is where
    # `RsJwtCreator.get_public_key` reads it from. A deploy that drops this
    # service beside the platform on the droplet therefore needs no new file —
    # point `jwt_public_key_path` at the platform's copy. The PUBLIC half
    # only: this service never needs to mint a token and must not be able to.
    key_path = (os.environ.get("CRM_JWT_PUBLIC_KEY_PATH", "").strip()
                or get("CRM", "jwt_public_key_path", None)
                or get("MAIN", "jwt_public_key_path", None))
    if key_path and os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as handle:
            settings.jwt_public_key = handle.read()
    elif key_path:
        settings.missing.append(f"jwt public key not found at {key_path}")

    settings.platform_base_url = (
        os.environ.get("CRM_PLATFORM_BASE_URL", "").strip()
        or get("CRM", "platform_base_url", None))
    settings.internal_key = (os.environ.get("CRM_INTERNAL_KEY", "").strip()
                             or get("CRM", "internal_key", None))
    #: Anything but an explicit truthy word is OFF — a typo must not silently
    #: start a second scheduler.
    _reminders = (os.environ.get("CRM_TASK_REMINDERS", "").strip()
                  or get("CRM", "task_reminders", "") or "")
    settings.task_reminders = str(_reminders).strip().lower() in (
        "1", "true", "yes", "on")

    # ⚠️ DEFAULTS TO `full`. A deployment whose app.conf predates profiles
    # must get every endpoint it had before; anything else turns a file nobody
    # edited into a broken CRM.
    settings.profile = (os.environ.get("CRM_PROFILE", "").strip()
                        or get("CRM", "profile", None) or "full").strip().lower()

    # ── the host boundary ───────────────────────────────────────────────────
    # Read from [HOST], falling back to the [CRM] keys an existing deployment
    # already has. ⚠️ AN EMPTY OR MISSING VALUE MUST LAND ON THE PLATFORM'S
    # BEHAVIOUR, never on a stricter or looser one, because this file is also
    # read by a droplet whose app.conf predates the [HOST] section entirely.
    settings.host_identity = (
        os.environ.get("CRM_HOST_IDENTITY", "").strip()
        or get("HOST", "identity", None)
        or DEFAULT_HOST_IDENTITY).strip().lower()
    settings.host_directory = (
        os.environ.get("CRM_HOST_DIRECTORY", "").strip()
        or get("HOST", "directory", None)
        or DEFAULT_HOST_DIRECTORY).strip().lower()
    settings.host_base_url = (
        os.environ.get("CRM_HOST_BASE_URL", "").strip()
        or get("HOST", "base_url", None) or settings.platform_base_url)
    settings.host_internal_key = (
        os.environ.get("CRM_HOST_INTERNAL_KEY", "").strip()
        or get("HOST", "internal_key", None) or settings.internal_key)

    # `account_id = org`, one per line, in a [HOST_CLAIMS] section. A section
    # rather than a JSON blob in one value: a mistyped brace in app.conf would
    # otherwise take out the whole host config rather than one claim name.
    settings.host_claims_map = dict(_section_items(parser, "HOST_CLAIMS"))

    # The adapters cache the instance they built, so a reload has to drop it or
    # `reload()` would change the config and leave the old adapter running —
    # which is exactly the config-singleton trap this module already carries a
    # warning about, one level further out.
    _reset_host_adapters()
    try:
        settings.introspect_timeout = float(
            get("CRM", "introspect_timeout", None) or settings.introspect_timeout)
    except (TypeError, ValueError):
        settings.missing.append("introspect_timeout is not a number")

    # ⚠️ REPORTED, NOT FATAL — and reported as what they BLOCK. Without these
    # the service still starts and still answers health; it refuses CRM
    # requests with a sentence naming the missing setting, which is what you
    # want from a half-configured deploy during a cutover.
    for name, label in (("jwt_public_key", "jwt_public_key_path"),
                        ("platform_base_url", "platform_base_url")):
        if not getattr(settings, name):
            settings.missing.append(f"{label} (auth will refuse every request)")

    for name in ("db_uri", "db_name"):
        if not getattr(settings, name):
            settings.missing.append(name)
    return settings


#: ⚠️ ONE OBJECT FOR THE LIFE OF THE PROCESS, REBOUND NEVER, MUTATED BY
#: `reload()`. Modules do `from app.config import settings` and keep that
#: reference, so replacing this object would orphan every captured one:
#: configure the new, read the old, and nothing says why. That is not
#: hypothetical — rebuilding the config by popping this module out of
#: `sys.modules` made six auth tests fail while each file passed alone.
settings = _load(DEFAULT_CONFIG_PATH)
logger = _logger(settings.log_dir)


def reload(path: Optional[str] = None) -> Settings:
    """Re-read the config INTO the existing `settings` object.

    The only supported way to change configuration after import, and the only
    one tests should use. Mutating in place keeps every `from app.config
    import settings` reference valid — which is the whole reason this function
    exists rather than a module reload.
    """
    fresh = _load(path or os.environ.get("CRM_CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    for field_name in vars(fresh):
        setattr(settings, field_name, getattr(fresh, field_name))
    return settings
