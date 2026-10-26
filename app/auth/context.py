"""The request context and the RBAC gate.

`RequestContext`, `has()`/`can()` and the `rbac` module beneath them are copied
from the monolith unchanged — they are pure logic, and `require()` runs on all
83 endpoints, so no round trip may stand between a request and a permission
check.

HOW A TOKEN BECOMES ONE OF THESE, in two steps that are deliberately split:

1. **verified locally** — `app/auth/tokens.py`, RS256 against the public key.
   A forged, expired, malformed or wrong-type token is rejected here with no
   network call at all.
2. **resolved by the platform** — `app/auth/introspect.py`, cached for 30
   seconds. Account, role, grants, denials, custom roles and key narrowing all
   stay in the one implementation that owns them.

⚠️ STEP 2 IS NOT WHAT THE PLAN SAID, and `introspect.py` documents why at
length: §4.2 costed it as "copy context.py + rbac.py, 461 lines", and the
transitive closure is nearer 800 lines of the platform's account, team, invite
and API-key rules. Copying a tenancy decision twice is the most expensive
duplication available to this project.

WHAT THIS MEANS FOR THE TESTS, which is why 891 of them did not change: the
integration harness builds a `RequestContext` directly and calls the handlers
as functions. Only the handful of tests in `tests/test_auth.py` exercise the
dependency, and they do it offline with a generated keypair and a stubbed
transport.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header, Request

from app.auth import introspect, rbac, tokens
from app.config import settings
from app.host.identity import get_identity
from app.errors import (
    AuthError, AuthNotConfiguredError, PermissionDeniedError,
)


@dataclass
class RequestContext:
    """Everything a route needs to authorize a request.

    ⚠️ `user` IS TYPED `Any`, WHERE THE MONOLITH SAYS `UserModel`. The user
    document belongs to the platform, and registering its Beanie model here
    would mean this service managing a collection it does not own (see
    `app/foreign.py` for that line). Nothing in the CRM reads more than
    `user.id` and the name fields, and a dataclass does not validate its
    annotations anyway — so the annotation would have been decoration while
    the import would have been real coupling.
    """

    user: Any
    account_id: Optional[str]
    role: str = "owner"
    is_owner: bool = True
    extra_grants: List[str] = field(default_factory=list)
    denials: List[str] = field(default_factory=list)
    custom_roles: Dict[str, List[str]] = field(default_factory=dict)
    # Admin-edited built-in role definitions (global overrides).
    builtin_overrides: Dict[str, List[str]] = field(default_factory=dict)
    # The API key this request was authenticated with, when it was a key rather
    # than a signed-in person. Routes that must behave differently for a key
    # (or want it in an audit line) read this; most never look.
    api_key: Optional[object] = None
    #: Whether the reply Inbox is switched on for this account. Carried because
    #: the dataclass is constructed by name in a hundred places and dropping a
    #: field would break them; no CRM route reads it.
    inbox_enabled: bool = False

    @property
    def is_api_key(self) -> bool:
        return self.api_key is not None

    # ── Authorization helpers ────────────────────────────────────────────────
    def can(self, permission: str) -> bool:
        """Does the caller hold ``permission``? (alias of ``has``)."""
        return self.has(permission)

    def has(self, permission: str) -> bool:
        """Role-level permission check (role + grants/denials + admin overrides)."""
        return rbac.has_permission(
            self.role,
            permission,
            custom_roles=self.custom_roles,
            extra_grants=self.extra_grants,
            denials=self.denials,
            builtin_overrides=self.builtin_overrides,
        )


async def get_request_context(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> RequestContext:
    """Resolve the caller. Verify locally, then ask the platform.

    ⚠️ IT REFUSES RATHER THAN DEFAULTING, in every failure mode. No token, a
    bad token, an unconfigured service, an unreachable platform — each one
    raises. There is no branch here that produces a `RequestContext` with a
    guessed account id, because every CRM query is scoped by that value and a
    guess serves one tenant's records to another.
    """
    if not settings.jwt_public_key or not settings.platform_base_url:
        raise AuthNotConfiguredError()

    try:
        token = tokens.bearer(authorization)
        tokens.verify(token)
    except tokens.TokenError as ex:
        # ⚠️ ONE MESSAGE FOR EVERY TOKEN FAILURE, matching the monolith's
        # "Invalid Credentials". Which check failed is in the log, not in the
        # answer: the difference between "expired" and "bad signature" is only
        # useful to someone probing.
        raise AuthError("Invalid Credentials", 401) from ex

    account_header = request.headers.get(introspect.ACCOUNT_ID_HEADER)
    try:
        # ⚠️ THE CONFIGURED HOST ADAPTER, NOT `introspect.resolve` DIRECTLY.
        # `introspect` is one of two implementations (`app/host/identity.py`);
        # the other reads the claims out of the token this function has just
        # verified, for a consumer with no endpoint to offer. The exception
        # TYPES still come from `introspect` because they are the interface's
        # vocabulary — "denied" and "could not find out" — and the difference
        # between them is the 401/503 split below.
        resolved = await get_identity().resolve(
            token, account_header, authorization=authorization)
    except introspect.IntrospectionDenied as ex:
        raise AuthError(ex.message, ex.status_code) from ex
    except introspect.IntrospectionUnavailable as ex:
        # 503, NOT 401. An unreachable platform is not a credential problem,
        # and answering 401 would sign every user out of the CRM during a
        # platform blip — the monolith makes the same distinction for a
        # database outage, for the same reason.
        raise AuthError(
            "Service temporarily unavailable. Please try again shortly.",
            503) from ex

    return RequestContext(
        user=_User(resolved.user),
        account_id=resolved.account_id,
        role=resolved.role,
        is_owner=resolved.is_owner,
        extra_grants=resolved.extra_grants,
        denials=resolved.denials,
        custom_roles=resolved.custom_roles,
        builtin_overrides=resolved.builtin_overrides,
        # Truthy when the platform says the credential was a key. The CRM
        # reads `ctx.is_api_key` in a couple of places; it needs a marker, not
        # the key document.
        api_key=object() if resolved.is_api_key else None,
    )


class _User:
    """The caller, as much of them as the CRM actually touches.

    ⚠️ NOT THE PLATFORM'S `UserModel`. The CRM reads `user.id` (it stamps
    `created_by_user_id` and `owner_id` with it) and the name fields for an
    activity line — nothing else. Importing the real document would mean
    registering a Beanie model for a collection this service does not own, the
    same line `app/foreign.py` draws.
    """

    __slots__ = ("id", "email", "first_name", "last_name", "username",
                 "picture_url", "raw")

    def __init__(self, data: Dict[str, Any]):
        self.raw = data or {}
        self.id = self.raw.get("id")
        self.email = self.raw.get("email")
        self.first_name = self.raw.get("first_name")
        self.last_name = self.raw.get("last_name")
        self.username = self.raw.get("username")
        self.picture_url = self.raw.get("picture_url")

    def __repr__(self) -> str:                   # pragma: no cover
        return f"_User({self.id!r})"


def require(permission: str):
    """Dependency factory: resolve context AND enforce ``permission``.

    Byte-for-byte the monolith's behaviour: a denial raises
    `PermissionDeniedError`, which renders as the business-failure envelope at
    HTTP 200 — not a 403. See `app/errors.py` for why that is not a mistake.
    """

    async def _dep(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
        if not ctx.has(permission):
            raise PermissionDeniedError(
                "You don't have permission to perform this action. "
                "Please contact your administrator."
            )
        return ctx

    return _dep


def require_any(*permissions: str):
    """Resolve context and require **any** of these.

    For resources whose access is inherited rather than owned. A CRM note is
    not a thing you have permission to independently — it belongs to a deal or
    a company, and the permission that matters is the parent's. A role holding
    only ``deals:read`` must still reach the notes endpoint; the handler then
    checks the permission for the specific parent it was asked about.

    This is the route-level gate only. It keeps out a user with no CRM access
    at all; it does NOT decide which records they may see.

    ⚠️ IT EXISTS BECAUSE `require("companies:write")` ON THE IMPORT ROUTES
    REFUSED EVERY DEALS-ONLY ROLE. The dependency runs before the handler, so
    no per-entity check inside could see it — the whole endpoint was shut to a
    role that should have reached it.
    """

    async def _dep(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
        if not any(ctx.has(p) for p in permissions):
            raise PermissionDeniedError(
                "You don't have permission to perform this action. "
                "Please contact your administrator."
            )
        return ctx

    return _dep
