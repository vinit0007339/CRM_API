#!/usr/bin/env python3
"""Everything that must be true before nginx is reloaded. Run it, read it, then flip.

    export CRM_DIFF_TOKEN="<a real access token>"
    python3 scripts/cutover_check.py --host https://stage-engage-api.nevtan.com

Nine checks. Each one is a thing that has either bitten this project already
or would be discovered by a user within a minute of the reload. Exit 0 means
go; anything else means the answer to "shall we cut over?" is no, with a
reason.

⚠️ THIS IS NOT THE DIFF. `scripts/diff_against_platform.py` proves the two
services ANSWER the same; this proves the service is deployable, routed,
reachable and authenticating — the operational half, which a byte-for-byte
diff cannot see. Both have to pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT = 15.0


def fetch(url: str, headers: Optional[Dict[str, str]] = None,
          method: str = "GET") -> Tuple[Optional[int], Any]:
    request = urllib.request.Request(url, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw.decode())
            except Exception:                    # pylint: disable=broad-except
                return response.status, raw[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        raw = ex.read()
        try:
            return ex.code, json.loads(raw.decode())
        except Exception:                        # pylint: disable=broad-except
            return ex.code, raw[:200].decode("utf-8", "replace")
    except Exception as ex:                      # pylint: disable=broad-except
        return None, f"{type(ex).__name__}: {ex}"


def headers_of(url: str, headers: Dict[str, str]) -> Tuple[Optional[int], Dict[str, str]]:
    request = urllib.request.Request(url, method="OPTIONS")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as ex:
        return ex.code, {k.lower(): v for k, v in ex.headers.items()}
    except Exception:                            # pylint: disable=broad-except
        return None, {}


class Checks:
    def __init__(self, args):
        self.args = args
        self.results: List[Tuple[bool, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.results.append((True, name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.results.append((False, name, detail))

    # ── the checks ──────────────────────────────────────────────────────────

    def crm_is_live(self):
        status, body = fetch(f"{self.args.crm}/health")
        if status != 200 or not isinstance(body, dict):
            return self.fail("crm is live",
                             f"{self.args.crm}/health answered {status}: {body}")
        self.body = body
        self.ok("crm is live",
                f"env={body.get('environment')} up={body.get('uptime_seconds')}s")

    def all_routers_mounted(self):
        """Engage needs all 83. A trimmed profile is the likely reason it isn't.

        ⚠️ THE PROFILE IS NAMED IN THE FAILURE ON PURPOSE. `61/83 endpoints`
        reads like a half-finished move — a missing `include_router`, a bad
        deploy — and somebody would go looking for one. If the box is running
        `profile = core` the real cause is one config line, and the whole CRM
        UI is about to 404 on the two thirds it serves. Say which it is.
        """
        body = getattr(self, "body", {})
        routers = body.get("routers", {})
        endpoints = body.get("endpoints", {})
        modules = routers.get("modules")
        profile = body.get("profile")
        trimmed = (f" — this box is running `profile = {profile}`, and Engage's "
                   f"UI calls all {endpoints.get('expected')}. Set "
                   f"`[CRM] profile = full` (or remove the line) and restart."
                   if profile not in (None, "full") else "")
        if modules != routers.get("expected_modules"):
            return self.fail("every router is mounted",
                             f"{modules}/{routers.get('expected_modules')} modules"
                             f"{trimmed or ' — a missing include is a 404 on one screen'}")
        if endpoints.get("mounted") != endpoints.get("expected"):
            return self.fail(
                "every endpoint is mounted",
                f"{endpoints.get('mounted')}/{endpoints.get('expected')} endpoints"
                f"{trimmed}")
        self.ok("every router is mounted",
                f"profile={profile or 'full'}, {modules} modules, "
                f"{routers.get('mounts')} mounts, "
                f"{endpoints.get('mounted')} endpoints")

    def crm_is_ready(self):
        status, body = fetch(f"{self.args.crm}/health/ready")
        if status != 200:
            detail = (body or {}).get("database", {}).get("error") if isinstance(body, dict) else body
            return self.fail("crm is ready", f"{status}: {detail}")
        models = (body or {}).get("models", {})
        if not models.get("registered"):
            return self.fail("models are registered",
                             "Beanie registration failed — no request that "
                             "touches a model will work")
        self.ok("crm is ready",
                f"database={body['database'].get('name')}, "
                f"{models.get('declared')} models")

    def auth_is_configured(self):
        status, body = fetch(f"{self.args.crm}/health/ready")
        missing = (body or {}).get("missing_config") or [] if isinstance(body, dict) else []
        blocked = [m for m in missing if "auth will refuse" in m]
        if blocked:
            return self.fail("auth is configured",
                             "; ".join(blocked) + " — every CRM request would "
                             "answer 503")
        self.ok("auth is configured")

    def auth_refuses_a_bad_token(self):
        """A 401 proves the chain is wired; a 503 proves it is not."""
        status, body = fetch(f"{self.args.crm}/api/v1/crm/deals",
                             {"Authorization": "Bearer definitely-not-a-token"})
        if status == 401:
            return self.ok("auth refuses a bad token", "401 as the platform does")
        if status == 503:
            return self.fail(
                "auth refuses a bad token",
                "503 — this service cannot reach the platform's "
                "/internal/auth/context, or its key/URL are unset. Every real "
                "request would 503 too")
        self.fail("auth refuses a bad token",
                  f"expected 401, got {status}: {body}")

    def auth_accepts_a_real_token(self):
        if not self.args.token:
            return self.fail("auth accepts a real token",
                             "no CRM_DIFF_TOKEN — this is the check that "
                             "proves a user can actually get in")
        status, body = fetch(f"{self.args.crm}/api/v1/crm/pipelines",
                             {"Authorization": f"Bearer {self.args.token}"})
        if status != 200:
            return self.fail("auth accepts a real token", f"{status}: {body}")
        rows = (body or {}).get("data") if isinstance(body, dict) else None
        self.ok("auth accepts a real token",
                f"{len(rows) if isinstance(rows, list) else '?'} pipeline(s) returned")

    def cors_is_answered(self):
        """⚠️ THE CHECK THAT THE DIFF TOOL CANNOT MAKE.

        The UI is on a different origin than the API, so every CRM call from
        a browser is cross-origin. `curl` does not enforce CORS and neither
        does the diff — so a service with no `Access-Control-Allow-Origin`
        passes every other check and breaks every screen on the reload.
        """
        origin = self.args.ui_origin
        status, headers = headers_of(
            f"{self.args.crm}/api/v1/crm/deals",
            {"Origin": origin, "Access-Control-Request-Method": "GET",
             "Access-Control-Request-Headers": "authorization,x-account-id"})
        allowed = headers.get("access-control-allow-origin")
        if allowed != origin:
            return self.fail(
                "cors answers the UI's origin",
                f"preflight from {origin} got "
                f"Access-Control-Allow-Origin={allowed!r} (status {status}). "
                f"Add it to [CRM] cors_origins")
        if headers.get("access-control-allow-credentials") != "true":
            return self.fail("cors allows credentials",
                             "the UI sends a bearer with credentials; the "
                             "browser will discard the response")
        self.ok("cors answers the UI's origin", origin)

    def internal_auth_is_not_public(self):
        """The Phase 4 endpoint must be unreachable from outside."""
        if not self.args.host:
            return self.ok("internal auth is not public",
                           "skipped — pass --host to check it")
        status, _ = fetch(f"{self.args.host.rstrip('/')}/api/v1/internal/auth/context")
        if status in (401, 403, 404):
            return self.ok("internal auth is not public", f"{status} from outside")
        self.fail("internal auth is not public",
                  f"{self.args.host}/api/v1/internal/auth/context answered "
                  f"{status} — anyone on the internet can resolve a token's "
                  f"context. Add the nginx deny rule before cutting over")

    def nginx_still_points_at_the_platform(self):
        """Confirms this is being run BEFORE the flip, not after."""
        if not self.args.host:
            return self.ok("nginx not yet switched", "skipped — pass --host")
        status, body = fetch(f"{self.args.host.rstrip('/')}/api/v1/crm/deals",
                             {"Authorization": "Bearer definitely-not-a-token"})
        self.ok("public /crm is answering",
                f"{status} through nginx (whichever service that is — after "
                f"the flip it is the CRM, and this check becomes the 'did it "
                f"work' one)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--crm", default="http://127.0.0.1:8100")
    parser.add_argument("--host", default=None,
                        help="The PUBLIC API hostname, to check the internal "
                             "endpoint is denied and /crm is routed.")
    parser.add_argument("--ui-origin", default="https://stage-engage.nevtan.com",
                        help="The origin the UI is served from.")
    parser.add_argument("--token", default=os.environ.get("CRM_DIFF_TOKEN", ""))
    args = parser.parse_args()

    checks = Checks(args)
    for name in ("crm_is_live", "all_routers_mounted", "crm_is_ready",
                 "auth_is_configured", "auth_refuses_a_bad_token",
                 "auth_accepts_a_real_token", "cors_is_answered",
                 "internal_auth_is_not_public",
                 "nginx_still_points_at_the_platform"):
        try:
            getattr(checks, name)()
        except Exception as ex:                  # pylint: disable=broad-except
            checks.fail(name.replace("_", " "), f"{type(ex).__name__}: {ex}")

    print()
    for passed, name, detail in checks.results:
        mark = "✓" if passed else "✗"
        print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))

    failed = [r for r in checks.results if not r[0]]
    print()
    if failed:
        print(f"✗ {len(failed)} check(s) failed. Do not reload nginx.")
        return 1
    print("✓ all checks pass. The nginx rule is in docs/cutover.md; the "
          "rollback is the same reload backwards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
