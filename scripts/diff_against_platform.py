#!/usr/bin/env python3
"""Ask both services the same questions and compare every byte of the answers.

    export CRM_DIFF_TOKEN="<a real access token>"
    python3 scripts/diff_against_platform.py \
        --platform http://127.0.0.1:8000 \
        --crm      http://127.0.0.1:8100 \
        --account  6a80479c2ee98febd40f58de

This is the Phase 5 exit check, and it is the only evidence that 38,000 moved
lines behave identically. The 1,735 tests in this repo prove the code is
self-consistent; they cannot prove it matches the monolith, because they ARE
the monolith's tests — moved. Only the running pair can show that.

⚠️ READ-ONLY. BY DESIGN, AND NOT NEGOTIABLE BY ACCIDENT.
Both services share one database during the dual-run. A POST diffed across
them would create TWO records, a PATCH would apply twice, and a DELETE would
succeed once and 404 the other — which would then look like a behaviour
difference. So only GET is ever issued. There is no `--allow-writes` flag;
adding one would need a second database first.

WHAT COUNTS AS A DIFFERENCE, AND WHAT DOES NOT

* **Order counts.** A list whose order depends on which row the driver
  returned first reads as random to a user, and `GET /deals/{id}/companies`
  sorts deliberately (primary first, then alphabetical). An order-only
  difference is reported separately from a content one, because the triage is
  different.
* **Timestamps count.** Both services read the same documents, so a timestamp
  that differs means the two SERIALISE it differently — exactly the
  naive-vs-aware divergence that `tz_aware` would have caused, and precisely
  what this check exists to catch.
* **Failures count.** A 401's envelope, a business failure's
  `{"status": "failure", "message"}`, a validation 422 — the UI branches on
  all three shapes, so they are compared like any other response.
* **The clock does not count.** Nothing in a CRM payload should be "now", but
  `/health` is not compared and neither is any header.

⚠️ AND A DIFFERENCE THAT DOES NOT REPEAT IS NOT A DIFFERENCE. These are two
sequential requests against live data: somebody editing a deal between them
produces a difference that is real, correct and not a porting bug. Every
difference is therefore re-checked once (`--retries`), and one that fails to
reproduce is reported as FLAKY and does not fail the run. A tool that cries
wolf on live data gets ignored, and then the one real difference is ignored
with it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT = 30.0

#: Response keys that are allowed to differ, with the reason. EMPTY, and it
#: should stay that way: every entry here is a hole in the only check that
#: compares the two services. If you add one, say why in the comment, not just
#: in a commit message.
IGNORED_PATHS: Dict[str, str] = {}


# ── HTTP ────────────────────────────────────────────────────────────────────

def get(base: str, path: str, token: str, account: Optional[str]
        ) -> Tuple[int, Any]:
    """`(status, body)`. A non-200 is an ANSWER and is compared like any other."""
    url = f"{base.rstrip('/')}{path}"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    if account:
        request.add_header("X-Account-Id", account)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as ex:
        return ex.code, _json(ex.read())
    except Exception as ex:                      # pylint: disable=broad-except
        # A transport failure is not a response. Marked so it can never be
        # mistaken for one service legitimately answering differently.
        return 0, {"__transport_error__": f"{type(ex).__name__}: {ex}"}


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:                            # pylint: disable=broad-except
        return {"__unparseable__": raw[:400].decode("utf-8", "replace")}


# ── comparison ──────────────────────────────────────────────────────────────

def compare(left: Any, right: Any, path: str = "") -> List[Dict[str, Any]]:
    """Every difference between two JSON values, deepest first.

    Reports the SMALLEST differing node rather than "the two bodies differ":
    a report that says `data[3].expected_close_date` sends you to one line of
    code, and a report that dumps two 40KB bodies sends you to a diff tool.
    """
    if path in IGNORED_PATHS:
        return []

    if type(left) is not type(right):
        # ⚠️ TYPE FIRST, AND `type(...) is not`. `1 == 1.0` and `True == 1`
        # in Python, and both of those are real wire differences: a client
        # reading `amount` as an int where it used to be a float, or a
        # boolean arriving as a number. This is the same discipline the
        # suite uses for `type(x) is str` on serialised ids.
        return [{"path": path or "(root)", "kind": "type",
                 "platform": _brief(left), "crm": _brief(right)}]

    if isinstance(left, dict):
        out: List[Dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            where = f"{path}.{key}" if path else key
            if key not in left:
                out.append({"path": where, "kind": "only-in-crm",
                            "platform": None, "crm": _brief(right[key])})
            elif key not in right:
                out.append({"path": where, "kind": "missing-in-crm",
                            "platform": _brief(left[key]), "crm": None})
            else:
                out += compare(left[key], right[key], where)
        return out

    if isinstance(left, list):
        if len(left) != len(right):
            return [{"path": path or "(root)", "kind": "length",
                     "platform": f"{len(left)} item(s)",
                     "crm": f"{len(right)} item(s)"}]
        out = []
        for index, (a, b) in enumerate(zip(left, right)):
            out += compare(a, b, f"{path}[{index}]")
        if out and _same_set(left, right):
            # ⚠️ ORDER-ONLY, FLAGGED AS SUCH. The elements match as a set, so
            # nothing is missing — the two services sorted differently. Still
            # a difference (a list whose order moves reads as random, and the
            # CRM sorts some lists deliberately), but the fix is a sort key,
            # not a serialiser, so it must not be buried among content
            # differences.
            return [{"path": path or "(root)", "kind": "order",
                     "platform": _brief([_key_of(x) for x in left]),
                     "crm": _brief([_key_of(x) for x in right])}]
        return out

    if left != right:
        return [{"path": path or "(root)", "kind": "value",
                 "platform": _brief(left), "crm": _brief(right)}]
    return []


def _key_of(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("id") or item.get("_id") or item.get("name") or "?"
    return item


def _same_set(left: List[Any], right: List[Any]) -> bool:
    try:
        return sorted(json.dumps(x, sort_keys=True, default=str) for x in left) == \
               sorted(json.dumps(x, sort_keys=True, default=str) for x in right)
    except Exception:                            # pylint: disable=broad-except
        return False


def _brief(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


# ── the endpoints to ask about ──────────────────────────────────────────────

def plan(base: str, token: str, account: Optional[str]) -> List[str]:
    """Every GET worth comparing, with real ids discovered from the platform.

    Discovery runs against the PLATFORM, deliberately: it is the service still
    serving traffic, so its ids are the ones a user would be looking at. If
    the CRM cannot produce the same record for the same id, that is the
    difference we are hunting.
    """
    paths: List[str] = [
        "/api/v1/crm/pipelines",
        "/api/v1/crm/settings",
        "/api/v1/crm/attributes",
        "/api/v1/crm/attributes?object_type=deal",
        "/api/v1/crm/attributes?object_type=company",
        "/api/v1/crm/companies?limit=25",
        "/api/v1/crm/companies?limit=25&skip=25",
        "/api/v1/crm/companies/filter-fields",
        "/api/v1/crm/companies/duplicates",
        "/api/v1/crm/deals?limit=25",
        "/api/v1/crm/deals/filter-fields",
        "/api/v1/crm/tasks?limit=25",
        "/api/v1/crm/task-types",
        "/api/v1/crm/notes?entity_type=company&entity_id=000000000000000000000000",
        "/api/v1/crm/saved-views?object_type=company",
        "/api/v1/crm/saved-views?object_type=deal",
        "/api/v1/crm/view-preferences?object_type=company",
        "/api/v1/crm/view-preferences?object_type=deal",
        "/api/v1/crm/imports?limit=10",
        "/api/v1/crm/exports?limit=10",
        # ⚠️ A DELIBERATELY BAD ID. The two services must agree about FAILURE
        # as well as success: this is the "no longer exists" business failure,
        # at HTTP 200 with `status: "failure"`, which the UI reads to show a
        # message. A service that 404ed here instead would look fine in every
        # happy-path check and break every error path in the screens.
        "/api/v1/crm/companies/000000000000000000000000",
        "/api/v1/crm/deals/000000000000000000000000",
    ]

    status, body = get(base, "/api/v1/crm/pipelines", token, account)
    for pipeline in _rows(body)[:3]:
        pid = pipeline.get("id")
        if not pid:
            continue
        paths += [
            f"/api/v1/crm/pipelines/{pid}",
            f"/api/v1/crm/pipelines/{pid}/board",
            f"/api/v1/crm/deals?pipeline_id={pid}&limit=25",
            f"/api/v1/crm/deals?pipeline_id={pid}&sort_dir=desc&skip=0&limit=25",
            # The forecast and list views read the same rows through different
            # code; both were rewritten by the move.
            f"/api/v1/crm/deals?pipeline_id={pid}&status=open&limit=25",
        ]

    status, body = get(base, "/api/v1/crm/companies?limit=5", token, account)
    for company in _rows(body)[:5]:
        cid = company.get("id")
        if not cid:
            continue
        paths += [
            f"/api/v1/crm/companies/{cid}",
            f"/api/v1/crm/companies/{cid}/deals",
            f"/api/v1/crm/companies/{cid}/contacts",
            f"/api/v1/crm/companies/{cid}/activities",
            f"/api/v1/crm/notes?entity_type=company&entity_id={cid}",
            f"/api/v1/crm/tasks?entity_type=company&entity_id={cid}",
            f"/api/v1/crm/files?entity_type=company&entity_id={cid}",
        ]

    status, body = get(base, "/api/v1/crm/deals?limit=5", token, account)
    for deal in _rows(body)[:5]:
        did = deal.get("id")
        if not did:
            continue
        paths += [
            f"/api/v1/crm/deals/{did}",
            f"/api/v1/crm/deals/{did}/activities",
            # New in the extraction, so it has no monolith counterpart to
            # match until the platform has the commit that added it. Compared
            # anyway: if the platform 404s, that is reported and explains
            # itself at a glance.
            f"/api/v1/crm/deals/{did}/companies",
            f"/api/v1/crm/notes?entity_type=deal&entity_id={did}",
            f"/api/v1/crm/tasks?entity_type=deal&entity_id={did}",
            f"/api/v1/crm/files?entity_type=deal&entity_id={did}",
        ]

    return list(dict.fromkeys(paths))             # de-duped, order kept


def _rows(body: Any) -> List[Dict[str, Any]]:
    """The list out of either envelope — flat `data`, or nested `data.data`."""
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [r for r in data["data"] if isinstance(r, dict)]
    return []


# ── the run ─────────────────────────────────────────────────────────────────

def check(path: str, args, token: str, account: Optional[str]) -> Dict[str, Any]:
    p_status, p_body = get(args.platform, path, token, account)
    c_status, c_body = get(args.crm, path, token, account)

    result: Dict[str, Any] = {"path": path, "platform_status": p_status,
                              "crm_status": c_status, "differences": []}
    if p_status != c_status:
        result["differences"].append(
            {"path": "(status)", "kind": "status",
             "platform": p_status, "crm": c_status})
    result["differences"] += compare(p_body, c_body)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform", default="http://127.0.0.1:8000")
    parser.add_argument("--crm", default="http://127.0.0.1:8100")
    parser.add_argument("--token", default=os.environ.get("CRM_DIFF_TOKEN", ""))
    parser.add_argument("--account", default=None,
                        help="X-Account-Id. Omit to use the token's default.")
    parser.add_argument("--retries", type=int, default=1,
                        help="Re-check each difference this many times before "
                             "reporting it. 0 reports everything, flakes "
                             "included.")
    parser.add_argument("--only", default=None,
                        help="Substring filter on the path, to re-check one "
                             "endpoint after a fix.")
    parser.add_argument("--report", default=None, help="Write the JSON report here.")
    args = parser.parse_args()

    if not args.token:
        print("✗ no token. export CRM_DIFF_TOKEN=<an access token>, or pass "
              "--token.\n  Use a token for a REAL account with CRM data: an "
              "empty account makes every comparison pass.")
        return 2

    print(f"platform  {args.platform}\ncrm       {args.crm}")
    print(f"account   {args.account or '(the token default)'}\n")

    paths = plan(args.platform, args.token, args.account)
    if args.only:
        paths = [p for p in paths if args.only in p]
    print(f"{len(paths)} endpoint(s) to compare\n")

    identical, flaky, differing = [], [], []
    for path in paths:
        result = check(path, args, args.token, args.account)
        if not result["differences"]:
            identical.append(result)
            print(f"  ✓ {path}")
            continue

        # Re-check. A difference that will not reproduce is live data moving
        # under us, not a porting bug.
        persisted = result
        for _ in range(args.retries):
            again = check(path, args, args.token, args.account)
            if not again["differences"]:
                persisted = None
                break
            persisted = again
        if persisted is None:
            flaky.append(result)
            print(f"  ~ {path}   (differed once, then matched — live data moved)")
        else:
            differing.append(persisted)
            print(f"  ✗ {path}")
            for difference in persisted["differences"][:6]:
                print(f"       {difference['kind']:14} {difference['path']}")
                print(f"         platform: {difference['platform']}")
                print(f"         crm:      {difference['crm']}")
            if len(persisted["differences"]) > 6:
                print(f"       … and {len(persisted['differences']) - 6} more")

    print(f"\nidentical {len(identical)}   differing {len(differing)}"
          f"   flaky {len(flaky)}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump({"identical": [r["path"] for r in identical],
                       "flaky": [r["path"] for r in flaky],
                       "differing": differing}, handle, indent=2, default=str)
        print(f"report    {args.report}")

    if differing:
        print("\n✗ the two services do not agree. Each difference above is "
              "either a porting bug or a deliberate change nobody wrote down.")
        return 1
    if not (identical or flaky):
        # ⚠️ `identical or flaky`, NOT `identical`. A run whose only result was
        # a flake HAD compared something, and reporting "nothing was compared"
        # for it was wrong — it sent you to check a token that was fine.
        # Caught by testing the flaky path against a service that differs once.
        print("\n✗ nothing was compared. Check the token and the account — an "
              "account with no CRM data proves nothing.")
        return 1
    if flaky and not identical:
        print(f"\n· {len(flaky)} endpoint(s) differed once and then matched, "
              f"and nothing else was compared. Live data is moving; re-run "
              f"with --retries 3 to be sure.")
        return 0
    print("\n✓ identical on every endpoint compared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
