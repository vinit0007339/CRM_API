"""Who is the caller, and what may they do? Asked of the platform, once, cached.

THE DECISION THIS FILE RECORDS, AND WHY IT CHANGED.

`EXTRACTION_PLAN.md` §4.2 recommended copying the monolith's
`api/dependencies/context.py` (283 lines) and `rbac.py` (178 lines) and reading
the membership collections directly. **That estimate was wrong, and I only
found out by reading the transitive closure.** `get_request_context` depends
on:

    account_context.py          198 lines  (resolve_membership,
                                            list_accessible_accounts,
                                            user_has_owned_account,
                                            pick_default_account_id)
    team_users.py               parts      (is_session_token_stale,
                                            auto_verify_invite_user,
                                            is_standalone_account_owner,
                                            deactivated_account_message)
    auth_or_key.py + api_keys/  parts      (API-key callers, scope clamping)
    teams.py                    TeamMember + CustomRole models
    admin_roles.py              builtin role overrides
    users.py                    UserModel + four status checks

Copying that is copying the platform's identity system — 800-odd lines of
account, team, invite and API-key rules, none of it CRM. Two copies of a
tenancy decision is the single most expensive kind of duplication this project
has: `§8` of the plan lists "membership logic drifts after being copied" as a
risk, and this would have been it, with a straight face.

SO THE PLATFORM KEEPS ANSWERING THE QUESTION. `GET /internal/auth/context`
there declares `Depends(get_request_context)` — the EXISTING dependency — and
serialises the result. No new identity logic anywhere, and every future change
to roles, teams, invites or keys reaches this service for free. API keys work
on day one for the same reason.

WHAT ABOUT THE NETWORK HOP. Three things make it a non-issue:

* the token is verified LOCALLY first (`app/auth/tokens.py`), so a forged or
  expired token never causes a request — which is the volume an attacker
  controls;
* answers are cached for `CACHE_TTL_SECONDS` against a hash of the
  credential, so a browser session doing twenty CRM calls makes one hop;
* if the platform is unreachable the CRM answers **503, not 500 and not 200** —
  and if the platform is down, the platform is down anyway.

⚠️ AND IT IS A TEMPORARY SHAPE, DELIBERATELY. When the databases split (a
separate project), the right answer is a signed internal token carrying role
and grants, minted by the platform at sign-in. That is a change on both sides
and a new token type to get wrong; doing it during a code move would have been
two risky changes at once. This file is where it goes when the time comes.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.config import logger, settings

#: How long a resolved context is reused. Short, because it carries ROLE and
#: GRANTS: revoking someone's access must take effect in seconds, not minutes.
#: The monolith resolves per request, so this is the one place where the two
#: services can legitimately disagree, and the window is how long for.
CACHE_TTL_SECONDS = 30

#: The platform's own header for "act in this workspace". Forwarded verbatim —
#: the platform validates it against the caller's memberships, and a service
#: that second-guessed it would be re-implementing the rule it is trying not
#: to copy.
ACCOUNT_ID_HEADER = "X-Account-Id"


class IntrospectionUnavailable(Exception):
    """The platform could not be reached or answered unusably → 503."""


class IntrospectionDenied(Exception):
    """The platform says this caller has no context → the caller's 401/403.

    Carries the platform's own status code and message so the CRM answers what
    the monolith would have answered, which is what Phase 5's diff compares.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


@dataclass
class ResolvedContext:
    """Exactly the fields `RequestContext` needs, and nothing else."""

    account_id: Optional[str]
    role: str = "owner"
    is_owner: bool = True
    extra_grants: List[str] = field(default_factory=list)
    denials: List[str] = field(default_factory=list)
    custom_roles: Dict[str, List[str]] = field(default_factory=dict)
    builtin_overrides: Dict[str, List[str]] = field(default_factory=dict)
    user: Dict[str, Any] = field(default_factory=dict)
    is_api_key: bool = False


# ── cache ───────────────────────────────────────────────────────────────────
# Keyed on a HASH of the credential, never the credential. A dict of live
# bearer tokens is a credential store nobody signed up for: it would show up
# in a heap dump, in a debugger session, and in anything that prints the
# module's state.
_CACHE: Dict[str, Tuple[float, ResolvedContext]] = {}


def _key(token: str, account_header: Optional[str]) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{digest}:{account_header or ''}"


def _cached(key: str) -> Optional[ResolvedContext]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, context = entry
    if expires_at < time.monotonic():
        _CACHE.pop(key, None)
        return None
    return context


def _store(key: str, context: ResolvedContext) -> None:
    # A cap, because the key space is "every token anyone presents" and an
    # unbounded dict in a long-lived process is a slow memory leak. Dropping
    # the whole cache when it fills is crude and correct: the cost is one hop
    # per caller, and the alternative (an LRU with ordering) is machinery for
    # a dict that holds seconds of data.
    if len(_CACHE) > 10_000:
        _CACHE.clear()
        logger.info("CRM auth: context cache cleared at the size cap")
    _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, context)


def clear_cache() -> None:
    """For tests, and for a `/internal/auth/flush` if one is ever wanted."""
    _CACHE.clear()


# ── the call ────────────────────────────────────────────────────────────────

async def resolve(token: str, account_header: Optional[str] = None,
                  *, authorization: Optional[str] = None) -> ResolvedContext:
    """The caller's context, from cache or from the platform."""
    key = _key(token, account_header)
    hit = _cached(key)
    if hit is not None:
        return hit

    if not settings.platform_base_url:
        raise IntrospectionUnavailable(
            "platform_base_url is not configured — see config/app.conf.example")

    import httpx                                # pylint: disable=import-outside-toplevel

    url = f"{settings.platform_base_url.rstrip('/')}/api/v1/internal/auth/context"
    headers = {"Authorization": authorization or f"Bearer {token}"}
    if account_header:
        headers[ACCOUNT_ID_HEADER] = account_header
    if settings.internal_key:
        # ⚠️ THE ENDPOINT MUST NOT BE REACHABLE FROM THE INTERNET. This header
        # is the second lock; the first is an nginx rule that refuses
        # /api/v1/internal/ from anywhere but localhost. Either alone is
        # thinner than it looks — the header can leak into a log, and an nginx
        # config can be edited by someone who does not know why the rule is
        # there — so both.
        headers["X-Internal-Key"] = settings.internal_key

    try:
        async with httpx.AsyncClient(timeout=settings.introspect_timeout) as client:
            response = await client.get(url, headers=headers)
    except Exception as ex:                      # pylint: disable=broad-except
        logger.warning("CRM auth: introspection call failed: %s: %s",
                       type(ex).__name__, ex)
        raise IntrospectionUnavailable(str(ex)) from ex

    if response.status_code in (401, 403):
        # The platform's own refusal, passed through unchanged. Re-deciding it
        # here would be the duplication this whole design avoids.
        raise IntrospectionDenied(response.status_code,
                                  _message(response) or "Invalid Credentials")
    if response.status_code >= 500:
        raise IntrospectionUnavailable(
            f"platform answered {response.status_code}")
    if response.status_code != 200:
        # ⚠️ ANYTHING ELSE IS NOT A SUCCESS, and must not be treated as one.
        # A 302 to a login page parses as neither JSON nor a context, and
        # guessing here would mean serving a request with an empty context —
        # which, in a system whose tenancy rule is `created_by == account_id`,
        # is the worst possible failure.
        raise IntrospectionUnavailable(
            f"platform answered {response.status_code}")

    try:
        body = response.json()
    except Exception as ex:                      # pylint: disable=broad-except
        raise IntrospectionUnavailable("platform answer was not JSON") from ex

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise IntrospectionUnavailable("platform answer had no context")

    context = ResolvedContext(
        account_id=data.get("account_id"),
        role=data.get("role") or "none",
        is_owner=bool(data.get("is_owner")),
        extra_grants=list(data.get("extra_grants") or []),
        denials=list(data.get("denials") or []),
        custom_roles=dict(data.get("custom_roles") or {}),
        builtin_overrides=dict(data.get("builtin_overrides") or {}),
        user=dict(data.get("user") or {}),
        is_api_key=bool(data.get("is_api_key")),
    )
    if not context.account_id:
        # No account means no tenancy key, and every CRM query is scoped by
        # it. Refusing is the only safe answer.
        raise IntrospectionDenied(403, "You do not have access to an account.")

    _store(key, context)
    return context


def _message(response: Any) -> Optional[str]:
    """The platform's message, whichever envelope it used."""
    try:
        body = response.json()
    except Exception:                            # pylint: disable=broad-except
        return None
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("message"), str):
        return body["message"]
    messages = body.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], str):
        return messages[0]
    return None
