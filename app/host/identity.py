"""Identity: who is this caller, and what may they do?

The CRM verifies the token's SIGNATURE itself (`app/auth/tokens.py`, RS256
against a public key). This file answers the other half — account, role,
grants, denials, custom roles, API-key narrowing — and the two adapters below
differ entirely in WHERE that answer comes from.

    introspect  Ask the host, over HTTP, per request (cached 30s).
                ⚠️ THE DEFAULT, AND THE RIGHT CHOICE FOR A HOST THAT ALREADY
                HAS ROLES. The platform's answer stays the ONLY copy of its
                account, team, invite and key rules — roughly 800 lines that
                the extraction plan originally costed as "copy context.py +
                rbac.py, 461 lines". Copying a tenancy decision twice is the
                most expensive duplication this project has available, because
                the copies drift and the direction they drift is one tenant
                seeing another's records. Every future change to roles, teams,
                invites or keys reaches the CRM for free.

    jwt_claims  Read it out of the verified token. No endpoint for the
                consumer to implement, no per-request call.
                ⚠️ IT MOVES THE TENANCY DECISION INTO WHOEVER MINTS THE
                TOKEN. That is a real trade and not a shortcut: the issuer now
                owns "which account is this, and what may they do", and a
                token stays valid until it expires, so a revoked grant is
                honoured until then. Right for a consumer whose auth server
                already carries these claims; wrong for one that would have to
                invent them to fill the fields.

Both produce the same `ResolvedContext`, which is the actual contract — nine
fields, defined in `app/auth/introspect.py` beside the HTTP shape it parses.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from app.config import DEFAULT_HOST_IDENTITY, logger, settings


class Identity(Protocol):
    """What a host must answer, however it answers it.

    Raises `IntrospectionDenied` for "this caller may not" and
    `IntrospectionUnavailable` for "I could not find out" — the distinction
    matters more than it looks: see the note in `app/auth/context.py` about
    why an unreachable host is a 503 and not a 401.
    """

    async def resolve(self, token: str, account_header: Optional[str] = None,
                      authorization: Optional[str] = None) -> Any: ...


class IntrospectIdentity:
    """Ask the host. Today's behaviour, unchanged, and the default.

    A thin pass-through to `app/auth/introspect.py` rather than a reworking of
    it: that module carries the cache (keyed on a HASH of the credential,
    never the credential), the denied/unavailable split, and a long docstring
    explaining why plan §4.2 was reversed. None of that should move just to
    make this file look symmetrical.
    """

    name = "introspect"

    #: An API key can be resolved here, because the host is asked directly and
    #: `get_request_context` has nothing to verify locally. The platform's
    #: `/internal/auth/context` accepts either credential and reports which it
    #: was (`is_api_key`).
    ACCEPTS_OPAQUE_CREDENTIAL = True

    async def resolve(self, token, account_header=None, authorization=None):
        from app.auth import introspect

        return await introspect.resolve(token, account_header,
                                        authorization=authorization)


class JwtClaimsIdentity:
    """Read it out of the token the service has already verified.

    ⚠️ THE SIGNATURE IS CHECKED BEFORE THIS RUNS, in
    `app/auth/context.get_request_context`, and this adapter must never be the
    thing that decides a token is good. It receives claims from a verified
    token and maps them; if it ever starts decoding one itself, the
    verification can be skipped by accident and every field below becomes
    attacker-controlled.

    CLAIM NAMES are configurable because no two auth servers agree on them,
    but the DEFAULTS are the obvious ones:

        account_id   ← `account_id`, else `org_id`, else `tid`
        role         ← `role`
        permissions  ← `permissions` (a list) → `extra_grants`
        is_owner     ← `is_owner`, else role == "owner"

    ⚠️ NO ACCOUNT CLAIM IS A DENIAL, NOT A DEFAULT. An absent account id with
    a tenancy model of `created_by == account_id` would otherwise resolve to
    `None` and match every row whose `created_by` is unset — a cross-tenant
    read produced by a missing claim. So it refuses.
    """

    name = "jwt_claims"

    #: ⚠️ `X-Account-Id` IS IGNORED BY THIS ADAPTER. Under introspection the
    #: host decides whether a caller may act for the account in that header;
    #: here there is nobody to ask, so honouring it would let any valid token
    #: name any account. The token's own claim is the only source.
    HONOURS_ACCOUNT_HEADER = False

    #: ⚠️ AN API KEY CANNOT BE RESOLVED BY THIS ADAPTER, AND MUST BE REFUSED
    #: RATHER THAN ATTEMPTED. This adapter's whole method is `tokens.verify`
    #: followed by reading claims; an opaque key has no signature it can check
    #: and no claims to read, so `resolve` would raise `TokenError` — which
    #: `get_request_context` does not catch, because it is not part of the
    #: adapter interface's vocabulary. The caller would get a 500 on a
    #: credential that is merely wrong for this deployment.
    #:
    #: A consumer running this adapter has no host endpoint to ask, so there is
    #: nothing to fix here: the right answer is a 401 with a clear log line.
    ACCEPTS_OPAQUE_CREDENTIAL = False

    def __init__(self, claims_map: Optional[Dict[str, str]] = None):
        self.claims_map = claims_map or {}

    def _pick(self, claims: Dict[str, Any], field: str,
              fallbacks: List[str]) -> Any:
        configured = self.claims_map.get(field)
        for key in ([configured] if configured else []) + fallbacks:
            if key and claims.get(key) is not None:
                return claims[key]
        return None

    async def resolve(self, token, account_header=None, authorization=None):
        from app.auth import introspect, tokens

        claims = tokens.verify(token)
        if not isinstance(claims, dict):
            raise introspect.IntrospectionUnavailable(
                "the verified token carried no claims")

        account_id = self._pick(claims, "account_id",
                                ["account_id", "org_id", "tid"])
        if not account_id:
            raise introspect.IntrospectionDenied(
                "This token carries no account. The CRM scopes every record "
                "by account, so it cannot serve a caller without one.", 403)

        role = str(self._pick(claims, "role", ["role"]) or "owner")
        grants = self._pick(claims, "permissions", ["permissions", "scope"])
        if isinstance(grants, str):
            # A space- or comma-separated `scope` string, which is what OAuth
            # servers emit. Split rather than treat the whole string as one
            # permission, which would silently grant nothing.
            grants = [g for g in grants.replace(",", " ").split() if g]
        is_owner = self._pick(claims, "is_owner", ["is_owner"])

        if account_header and str(account_header) != str(account_id):
            # Not an error — the UI sends this header on every request — but
            # worth a line, because a mismatch means the caller believes they
            # are acting for an account this adapter will not give them.
            logger.info("jwt_claims: ignoring X-Account-Id=%r; the token says "
                        "%r", account_header, account_id)

        return introspect.ResolvedContext(
            account_id=str(account_id),
            role=role,
            is_owner=bool(is_owner) if is_owner is not None else role == "owner",
            extra_grants=list(grants or []),
            denials=list(self._pick(claims, "denials", ["denials"]) or []),
            custom_roles=dict(self._pick(claims, "custom_roles",
                                         ["custom_roles"]) or {}),
            builtin_overrides={},
            user={
                "id": str(self._pick(claims, "user_id", ["sub", "user_id"]) or "")
                or None,
                "email": self._pick(claims, "email", ["email"]),
                "first_name": self._pick(claims, "first_name",
                                         ["first_name", "given_name"]),
                "last_name": self._pick(claims, "last_name",
                                        ["family_name", "last_name"]),
                "username": self._pick(claims, "username",
                                       ["preferred_username", "username"]),
                "picture_url": self._pick(claims, "picture_url", ["picture"]),
            },
            is_api_key=bool(self._pick(claims, "is_api_key", ["is_api_key"])),
        )


#: A dict, not an if/else chain — see the note in `directory.py`.
_ADAPTERS = {
    IntrospectIdentity.name: IntrospectIdentity,
    JwtClaimsIdentity.name: JwtClaimsIdentity,
}

_INSTANCE: Optional[Identity] = None
_INSTANCE_FOR: Optional[str] = None


def get_identity() -> Identity:
    """The configured adapter, built once and rebuilt when the name changes.

    An UNKNOWN name falls back to `introspect` and logs loudly. ⚠️ THE
    FALLBACK DIRECTION MATTERS: falling back to `jwt_claims` on a typo would
    quietly stop asking the host anything, which is a weakening of
    authorisation produced by a misspelling. Falling back to `introspect` can
    only fail closed — if the host is not reachable, nobody gets in.
    """
    global _INSTANCE, _INSTANCE_FOR          # pylint: disable=global-statement

    name = (getattr(settings, "host_identity", None)
            or DEFAULT_HOST_IDENTITY).strip().lower()
    if name not in _ADAPTERS:
        logger.error("unknown [HOST] identity=%r — falling back to introspect "
                     "(valid: %s)", name, ", ".join(sorted(_ADAPTERS)))
        name = DEFAULT_HOST_IDENTITY
    if _INSTANCE is not None and _INSTANCE_FOR == name:
        return _INSTANCE

    if name == JwtClaimsIdentity.name:
        _INSTANCE = JwtClaimsIdentity(
            claims_map=dict(getattr(settings, "host_claims_map", None) or {}))
    else:
        _INSTANCE = _ADAPTERS[name]()
    _INSTANCE_FOR = name
    return _INSTANCE


def reset() -> None:
    """Drop the cached adapter. For tests, and for a config reload."""
    global _INSTANCE, _INSTANCE_FOR          # pylint: disable=global-statement

    _INSTANCE = None
    _INSTANCE_FOR = None
