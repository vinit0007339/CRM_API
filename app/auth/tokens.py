"""Verifying a session token, locally.

⚠️ RS256 IS WHY THIS NEEDS NO COORDINATION. The platform signs with a private
key and this service verifies with the PUBLIC half, so a forged, expired or
replayed token is rejected here — no call to the platform, no shared secret,
and nothing this service holds could mint a token even if it were breached.

Everything in this file is a copy of `app/security/jwt.py` +
`api/dependencies/authentication.py`'s decode step in the monolith, and it has
to stay a faithful one:

* the same algorithm (RS256) and the same key file;
* `{"require": ["exp"]}` — a token with no expiry is refused, which is the
  monolith's `decode` default and not PyJWT's;
* `type == "access_token"` enforced. The platform signs password-reset and
  email-verification tokens with the SAME key, so without this check a 24-hour
  `forgot_password` token could be presented here as a session bearer. The
  monolith's comment cites its own security review for this; dropping it
  during a move would reintroduce the hole in a service nobody was auditing.

What this file does NOT do is decide who the caller is — a verified token says
only that the platform issued it. Account, role, grants and denials come from
`app/auth/introspect.py`, and the reasons are there.
"""
from __future__ import annotations

import functools
from typing import Any, Dict

from app.config import logger, settings

#: The monolith's `TOKEN_TYPE_ACCESS`. A literal here rather than an import —
#: there is nothing to import — so it is pinned by a test instead.
TOKEN_TYPE_ACCESS = "access_token"


class TokenError(Exception):
    """The token is absent, malformed, expired, or not a session token.

    One exception for every rejection reason on purpose: the caller turns it
    into a single "Invalid Credentials" 401, exactly as the monolith does.
    Telling an unauthenticated caller WHICH check failed is free help for
    someone probing.
    """


@functools.lru_cache(maxsize=1)
def _public_key() -> bytes:
    """The PEM public key, read once.

    Cached because `require()` runs on all 83 endpoints and re-reading a file
    per request is a syscall per request for a value that cannot change
    without a restart. `settings.jwt_public_key` is read at import; this is the
    file fallback for a path configured later.
    """
    if settings.jwt_public_key:
        return settings.jwt_public_key.encode("utf-8")
    raise TokenError("no JWT public key configured")


def verify(token: str) -> Dict[str, Any]:
    """The token's claims, or `TokenError`.

    ⚠️ EVERY FAILURE IS THE SAME FAILURE TO THE CALLER. `jwt.decode` raises a
    different exception for an expired token, a bad signature and a malformed
    one; all three mean "not authenticated" to a client and the difference is
    only useful to an attacker. The specifics are logged, not returned.
    """
    if not token:
        raise TokenError("no token")

    import jwt                                  # pylint: disable=import-outside-toplevel

    try:
        claims = jwt.decode(
            token,
            _public_key(),
            algorithms=["RS256"],
            # The monolith's `RsJwtCreator.decode` default. PyJWT does NOT
            # require `exp` on its own: without this, a token minted with no
            # expiry would be accepted for ever.
            options={"require": ["exp"]},
        )
    except TokenError:
        raise
    except Exception as ex:                      # pylint: disable=broad-except
        logger.info("CRM auth: token rejected (%s)", type(ex).__name__)
        raise TokenError(str(ex)) from ex

    actual = claims.get("type")
    if actual != TOKEN_TYPE_ACCESS:
        # The reason this check exists is in the module docstring. Logged with
        # the type it saw, because a legitimate client hitting this means a
        # bug in whatever minted the token.
        logger.warning("CRM auth: token type %r is not %r",
                       actual, TOKEN_TYPE_ACCESS)
        raise TokenError(f"token type {actual!r} is not a session token")

    return claims


def bearer(authorization: str | None) -> str:
    """The token out of an `Authorization: Bearer …` header.

    Case-insensitive on the scheme, which `OAuth2PasswordBearer` is too — a
    client sending `bearer abc` is not making a mistake worth a 401 over.
    """
    value = (authorization or "").strip()
    if not value:
        raise TokenError("no Authorization header")
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise TokenError("Authorization header is not a Bearer token")
    return parts[1].strip()
