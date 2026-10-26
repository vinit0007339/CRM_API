#!/usr/bin/env python3
"""Is this service up, and is it telling the truth about itself?

    python3 scripts/smoke.py                        # http://127.0.0.1:8100
    python3 scripts/smoke.py http://127.0.0.1:8100

This is the Phase 1 exit check, and it stays useful afterwards: run it against
the droplet after any deploy. It reads `/health` and `/health/ready` and prints
a verdict, including **how much of the extraction has landed** — routers and
endpoints mounted against the 12 and 83 the monolith serves today.

Exit code 0 when the service is live AND ready, 1 otherwise, so it drops into a
deploy script without parsing.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT = "http://127.0.0.1:8100"


def fetch(url: str, timeout: float = 10.0):
    """`(status, body, seconds)`. A connection failure is a status of None."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return (response.status, json.loads(response.read().decode()),
                    time.monotonic() - started)
    except urllib.error.HTTPError as ex:
        # 503 from readiness is an ANSWER, not a failure to reach — read it.
        try:
            return ex.code, json.loads(ex.read().decode()), time.monotonic() - started
        except Exception:                         # pylint: disable=broad-except
            return ex.code, None, time.monotonic() - started
    except Exception as ex:                       # pylint: disable=broad-except
        return None, {"error": f"{type(ex).__name__}: {ex}"}, time.monotonic() - started


def main(base: str) -> int:
    base = base.rstrip("/")
    print(f"→ {base}\n")

    status, body, secs = fetch(f"{base}/health")
    if status is None:
        print(f"✗ liveness: no answer — {body['error']}")
        print("\n  The process is not listening. Check `pm2 list`, or start it:")
        print("    poetry run uvicorn app.main:app --port 8100")
        return 1

    print(f"✓ liveness  {status} in {secs * 1000:.0f}ms"
          f"   env={body.get('environment')}"
          f"   up {body.get('uptime_seconds')}s")
    # ⚠️ READS `modules`, NOT `mounted`. `/health` renamed these when Phase 2
    # had to distinguish 12 route MODULES from the 16 MOUNTS they make, and
    # this line was not updated — so a fully extracted service reported
    # "routers 0/0 (Phase 1 skeleton)" to the one tool whose job is to say how
    # much has landed. A health check that lies cheerfully is worse than none.
    routers = body.get("routers", {})
    endpoints = body.get("endpoints", {})
    modules = routers.get("modules", routers.get("mounted", 0))
    expected = routers.get("expected_modules", routers.get("expected", 0))
    mounted_eps = endpoints.get("mounted", 0)
    expected_eps = endpoints.get("expected", 0)
    done = "✓" if modules and modules == expected and mounted_eps == expected_eps else "·"
    print(f"{done} extraction  routers {modules}/{expected}"
          f"   mounts {routers.get('mounts', '?')}"
          f"   endpoints {mounted_eps}/{expected_eps}"
          + ("   (skeleton — the routers are not mounted)"
             if not modules else ""))

    status, body, secs = fetch(f"{base}/health/ready")
    missing = (body or {}).get("missing_config") or []
    auth_blocked = [m for m in missing if "auth will refuse" in m]
    if auth_blocked:
        # ⚠️ WORTH ITS OWN LINE. A service that is live and ready but cannot
        # authenticate answers 503 to every CRM request, and the body says so
        # — but only to whoever reads it. Say it here, where someone is
        # already looking, rather than letting it be discovered by a user.
        print("✗ auth        cannot authenticate: "
              + ", ".join(m.split(" (")[0] for m in auth_blocked))
        print("              every CRM request will answer 503 until these "
              "are set (see config/app.conf.example)")
    database = (body or {}).get("database", {})
    if status == 200:
        print(f"✓ readiness {status} in {secs * 1000:.0f}ms"
              f"   database={database.get('name')} reachable")
        return 0

    print(f"✗ readiness {status} in {secs * 1000:.0f}ms")
    for key in ((body or {}).get("missing_config") or []):
        print(f"    missing config: {key}")
    if database.get("error"):
        print(f"    database: {database['error']}")
    if not database.get("configured"):
        print("\n  Fill in [MAIN_DATABASE] in config/app.conf"
              " (copy config/app.conf.example), or export MAIN_DB_URI.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT))
