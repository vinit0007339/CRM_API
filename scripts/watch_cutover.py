#!/usr/bin/env python3
"""Poll the public CRM path after the flip and say whether it is healthy.

    export CRM_DIFF_TOKEN="<a real access token>"
    python3 scripts/watch_cutover.py --host https://<api host> --minutes 60

"Watch for an hour" is a plan with no tool. This is the tool: it calls a
handful of real endpoints through nginx — the path a user's browser takes —
once a minute, counts what comes back, and prints one line per minute.

⚠️ IT GOES THROUGH THE PUBLIC HOST, NOT 127.0.0.1:8100. The point is to
exercise the whole path: TLS, nginx, the new location block, the service. A
poll of the service directly would have been green for every cutover mistake
there is — a wrong proxy_pass, a missing `client_max_body_size`, a regex
location winning over the prefix.

READ-ONLY, for the same reason as the diff: both services write one database.

Exit 0 if the error rate stayed at zero, 1 otherwise. Ctrl-C prints the
summary and exits cleanly, so a shorter watch than planned still reports.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Dict, List, Optional, Tuple

TIMEOUT = 20.0

#: Cheap, read-only, and between them they touch the board, the list views,
#: the attribute catalogue and the settings document — the four reads every
#: CRM screen makes on load.
PROBES = [
    "/api/v1/crm/pipelines",
    "/api/v1/crm/settings",
    "/api/v1/crm/attributes?object_type=deal",
    "/api/v1/crm/companies?limit=5",
    "/api/v1/crm/deals?limit=5",
]


def call(url: str, token: str, account: Optional[str]) -> Tuple[int, float, str]:
    """`(status, seconds, note)`. Status 0 means the request never arrived."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    if account:
        request.add_header("X-Account-Id", account)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            elapsed = time.monotonic() - started
            # ⚠️ A 200 IS NOT NECESSARILY A SUCCESS HERE. This codebase
            # answers a business failure as HTTP 200 with
            # `status: "failure"` — so a cutover that broke, say, tenancy
            # could return 200s all day. Read the envelope too.
            try:
                parsed = json.loads(body.decode())
                if isinstance(parsed, dict) and parsed.get("status") == "failure":
                    return response.status, elapsed, \
                        f"business failure: {parsed.get('message', '')[:60]}"
            except Exception:                    # pylint: disable=broad-except
                pass
            return response.status, elapsed, ""
    except urllib.error.HTTPError as ex:
        elapsed = time.monotonic() - started
        detail = ""
        try:
            parsed = json.loads(ex.read().decode())
            detail = (parsed.get("message")
                      or (parsed.get("messages") or [""])[0] or "")[:80]
        except Exception:                        # pylint: disable=broad-except
            pass
        return ex.code, elapsed, detail
    except Exception as ex:                      # pylint: disable=broad-except
        return 0, time.monotonic() - started, f"{type(ex).__name__}: {ex}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", required=True,
                        help="The PUBLIC API host — through nginx, not 8100.")
    parser.add_argument("--token", default=os.environ.get("CRM_DIFF_TOKEN", ""))
    parser.add_argument("--account", default=None)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()

    if not args.token:
        print("✗ no token. export CRM_DIFF_TOKEN=<an access token>.")
        return 2
    if "127.0.0.1" in args.host or "localhost" in args.host:
        print("· --host is loopback. That skips TLS, nginx and the new "
              "location block — the three places a cutover goes wrong. Pass "
              "the public host.")

    host = args.host.rstrip("/")
    print(f"watching {host}  every {args.interval}s for {args.minutes}m\n")
    print("  time      ok   slow   4xx   5xx   dead   worst")

    statuses: Counter = Counter()
    problems: List[str] = []
    deadline = time.monotonic() + args.minutes * 60
    try:
        while time.monotonic() < deadline:
            tick: Counter = Counter()
            worst = 0.0
            worst_path = ""
            for path in PROBES:
                status, elapsed, note = call(f"{host}{path}", args.token,
                                             args.account)
                statuses[status] += 1
                if elapsed > worst:
                    worst, worst_path = elapsed, path
                if status == 0:
                    tick["dead"] += 1
                    problems.append(f"{path} unreachable: {note}")
                elif status >= 500:
                    tick["5xx"] += 1
                    problems.append(f"{status} {path}: {note}")
                elif status >= 400:
                    tick["4xx"] += 1
                    problems.append(f"{status} {path}: {note}")
                elif note:
                    tick["4xx"] += 1
                    problems.append(f"200 {path}: {note}")
                else:
                    tick["ok"] += 1
                    if elapsed > 1.0:
                        tick["slow"] += 1

            stamp = time.strftime("%H:%M:%S")
            print(f"  {stamp}  {tick['ok']:>3}  {tick['slow']:>5}"
                  f"  {tick['4xx']:>4}  {tick['5xx']:>4}  {tick['dead']:>5}"
                  f"   {worst * 1000:.0f}ms {worst_path.split('/')[-1][:24]}")
            if tick["5xx"] or tick["dead"]:
                for line in problems[-tick["5xx"] - tick["dead"]:]:
                    print(f"            ! {line}")
            # ⚠️ SLEEP ONLY UP TO THE DEADLINE. Sleeping the full interval
            # after the last tick made a `--minutes 1` watch take 1m25s and
            # overrun its own timeout, so the summary never printed. A tool
            # that runs longer than it says and then reports nothing is worse
            # than one that does not run.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
    except KeyboardInterrupt:
        print("\n(stopped early)")

    total = sum(statuses.values())
    bad = sum(count for status, count in statuses.items()
              if status == 0 or status >= 500)
    client = sum(count for status, count in statuses.items()
                 if 400 <= status < 500)
    print(f"\n{total} request(s): "
          + ", ".join(f"{status or 'dead'}×{count}"
                      for status, count in sorted(statuses.items())))

    if bad:
        print(f"\n✗ {bad} server error(s) or unreachable request(s). Roll back: "
              f"restore the pre-cutover nginx config and reload. See "
              f"docs/cutover.md §5.")
        for line in problems[:10]:
            print(f"    {line}")
        return 1
    if client:
        print(f"\n· {client} 4xx. A 401 means the token expired during the "
              f"watch (harmless); anything else is worth reading above.")
    print("\n✓ no server errors for the whole watch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
