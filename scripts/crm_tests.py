#!/usr/bin/env python3
"""Reproduce the CRM test baseline. CRM-P0-004.

    python3 scripts/crm_tests.py            # integration + unit, with counts
    python3 scripts/crm_tests.py --mutation # also the mutation harnesses
    python3 scripts/crm_tests.py --quiet    # counts only

WHY THIS EXISTS. Reproducing the CRM suites used to require knowing two things
that are written down nowhere, each of which has cost real time:

1. **The integration suite needs its shims on `PYTHONPATH` and must run from
   its own directory.** Point `unittest discover` at it from the repo root and
   most modules fail to import — 39 collected, 28 errors, none of them about
   the real problem. This script sets the cwd and the three path entries so
   that cannot happen.

2. **It needs `config/app.conf`, which is GITIGNORED.** A fresh clone or a git
   worktree has only tracked files, so the app bootstrap dies with

       configparser.NoSectionError: No section: 'MAIN'

   which says nothing about the real problem. This script checks for the file
   FIRST and says what is missing.

The expected counts below are the point of the script: a drop from 875 to 873
is a deleted test, and without a number to compare against nobody notices.
Update them in the same commit that changes them, never afterwards.

⚠️ FEWER TESTS THAN EXPECTED IS A FAILURE, not a warning. A warning that still
prints PASS is a warning that gets rationalised — "oh, that count is probably
stale" is exactly how a deleted test goes unnoticed. MORE tests than expected
only warns, because that is what adding tests looks like; it asks you to update
the constant.
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── the baseline ─────────────────────────────────────────────────────────────
# ⚠️ 891, NOT THE MONOLITH'S 916. `test_p2_contact_scope.py` (25 tests) did not
# move: it tests the PLATFORM's `app/db/crud/main/contacts.py`, and a copy here
# would test a copy of `contact_scope` and pass while the real one rotted. See
# `tests/integration/MOVED_OUT.md`. One test is skipped, not missing — the
# cross-service assertion about the platform's contact schema, which cannot be
# made from a repo that does not import the platform.
# Update these in the SAME commit that changes them. A count that drifts
# silently is a test somebody deleted to make a suite pass.
# The count of the COMMITTED tree, which is now the whole tree.
# `test_p8_card_settings.py` (22 tests, CRM-409) was deliberately held back
# from commits, so this number was kept 22 BELOW what a developer machine runs
# and the script reported a permanent +22. That file is committed as of
# `0490a70`, so the gap is closed and the number is simply the count again. If
# you ever hold a test file back from a commit, subtract it here and say so —
# otherwise the one machine without it reports missing tests.
EXPECTED_INTEGRATION = 891
# 798, up from the monolith's 794: `DerivedFromTheCatalogueTests` adds four.
# They kill three mutations that survived in BOTH repos — see the class
# docstring in tests/unit/test_engage_crm_defaults.py.
EXPECTED_UNIT = 798
# 96, up from 80. `tests/test_internal.py` adds 12 for the platform's way in
# (`POST /api/v1/internal/crm/auto-link`) — its locks are tested harder than
# anywhere else in the service, because it is the one endpoint that acts on an
# account id with no end-user token in the request. `test_health.py` adds 4
# for task-reminder ownership: exactly one process may run that sweep, and the
# failure if two do is a customer receiving two reminder emails.
# 135: `tests/test_host.py` adds 25 for the host boundary and
# `tests/test_profiles.py` 14 for the serving profiles. Most of both
# files defends the DEFAULTS — `introspect`+`shared_db`, and `full` —
# because the risk in either change was never the new code paths
# (`app/host/`) — the interface that makes this service consumable by a
# project other than emailMarketingAPI. Most of those 18 defend the DEFAULT
# adapters, because the risk in that refactor was never the new code; it was
# the old behaviour changing while nothing noticed.
EXPECTED_SERVICE = 135

# None left. `tests/test_engage_crm_attribute_routing.py` used to IMPORT
# `import_worker` to compare its copy of the system-field sets, which pulled in
# motor and errored outside the poetry venv. The worker no longer has a copy —
# it aliases `attribute_routing.SYSTEM_FIELDS` — and the test now reads the
# worker as source instead, so it runs everywhere. Any error here is real.
KNOWN_UNIT_ERRORS = 0

CONFIG = ROOT / "config" / "app.conf"
INTEGRATION_DIR = ROOT / "tests" / "integration"
UNIT_DIR = ROOT / "tests" / "unit"
SERVICE_DIR = ROOT / "tests"

# Packages the suites cannot run without. Checked by import BEFORE anything
# else, because without them unittest still "runs": it collects the pure-Python
# modules, errors on every module that touches the app, and reports a count.
# The first version of this script was run in an environment without pydantic
# and announced "837 test(s) have gone missing" — the exact wrong-diagnosis
# failure it was written to prevent.
#
# ⚠️ `motor`, `beanie`, `pymongo` and `bson` are NOT in this list and must not
# be. `tests/crm_integration/shims/` deliberately provides fakes for all four —
# that is how the integration suite runs without a database at all. Requiring
# them here would refuse to run in the one environment the suite was designed
# for. (The first version of this check did exactly that.)
REQUIRED_IMPORTS = ("pydantic", "fastapi")


def fail(message: str, hint: str = "") -> None:
    print(f"\n✗ {message}", file=sys.stderr)
    if hint:
        print(f"  {hint}", file=sys.stderr)
    sys.exit(1)


def check_environment() -> None:
    """Refuse to report counts from an interpreter that cannot run the suites."""
    import importlib.util

    missing = [name for name in REQUIRED_IMPORTS
               if importlib.util.find_spec(name) is None]
    if not missing:
        return
    fail(
        "this interpreter cannot run the CRM suites — missing: "
        + ", ".join(missing),
        "Run it where the app's dependencies are installed (the poetry venv, "
        "or CI). Counts from here would be meaningless: unittest collects the "
        "pure modules, errors on everything that imports `app`, and reports a "
        "number that looks like deleted tests.\n"
        f"  interpreter: {sys.executable}",
    )


def check_config() -> None:
    """A missing config is a NOTE here, not a failure. That changed with the move.

    ⚠️ THIS FUNCTION USED TO ABORT THE RUN. In the monolith it had to: the app
    reads `config/app.conf` in `App.__init__`, so importing anything without it
    died with `configparser.NoSectionError: No section: 'MAIN'` — forty frames
    deep and never mentioning the file. The check existed to name the real
    cause.

    `app/config.py` in this service does not do that. A missing file or option
    is collected into `settings.missing` and reported by `/health/ready`; the
    process starts either way. And none of these suites needs a database at
    all — the integration tests run against a fake Mongo. So the only thing
    worth saying is that the file is absent, in case you were about to run the
    service itself.
    """
    if not CONFIG.exists():
        print(f"· {CONFIG.relative_to(ROOT)} is absent — fine for the tests "
              f"(they use a fake database). Copy config/app.conf.example "
              f"before running the service itself.")
        return
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(CONFIG)
    except configparser.Error as ex:
        fail(f"{CONFIG.relative_to(ROOT)} could not be parsed: {ex}")
    if not parser.has_section("MAIN_DATABASE"):
        print(f"· {CONFIG.relative_to(ROOT)} has no [MAIN_DATABASE] section — "
              f"the tests do not care, the service will report itself not ready.")


def check_suites_exist() -> None:
    """Say the suites are not here, rather than dying forty frames in.

    ⚠️ THIS SCRIPT'S WHOLE PURPOSE IS TO NOT MISDIAGNOSE A TEST RUN, and
    without this check it misdiagnosed its own. With `tests/` deleted,
    `subprocess.run(cwd=tests/integration)` raises a bare

        FileNotFoundError: [Errno 2] No such file or directory:
        PosixPath('.../tests/integration')

    which reads like a broken script rather than a missing directory — the
    same shape as the two failures the module docstring above describes, where
    the traceback names a path and not the cause.

    ⚠️ AND THIS GUARD HAS ALREADY BEEN LOST ONCE. It was first written into a
    side copy of this file while the suites were deleted, and when they came
    back, later edits to the ORIGINAL copy were transferred over the top and
    silently removed it. If you are about to edit this file in two places,
    don't.
    """
    missing = [d for d in (INTEGRATION_DIR, ROOT / "tests" / "unit")
               if not d.is_dir()]
    if not missing:
        return
    fail(
        "the test suites are not in this repo: "
        + ", ".join(str(d.relative_to(ROOT)) for d in missing),
        "`tests/` was deleted. Nothing in `app/` depends on it — the service "
        "imports, mounts its 83 endpoints and passes "
        "`scripts/check_crm_contract.py` without it — but this script and "
        "`tests/mutation/` have nothing to run. Restore `tests/` to use them "
        "again, and keep the EXPECTED_* counts above in step with it.",
    )


def run(args: list[str], cwd: Path, extra_path: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = extra_path or str(ROOT)
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


def parse_counts(output: str) -> tuple[int, int, int, int]:
    """`(ran, failures, errors, skipped)` from unittest's own summary."""
    ran = int((re.search(r"^Ran (\d+) tests?", output, re.M) or [0, 0])[1])
    tail = output[-400:]
    def count(label: str) -> int:
        match = re.search(rf"{label}=(\d+)", tail)
        return int(match.group(1)) if match else 0
    return ran, count("failures"), count("errors"), count("skipped")


def report(name: str, result: subprocess.CompletedProcess, expected: int,
           allowed_errors: int, quiet: bool) -> bool:
    text = (result.stdout or "") + (result.stderr or "")
    ran, failures, errors, skipped = parse_counts(text)

    drift = ran - expected
    # A DROP fails; a rise only warns. See the note in the module docstring.
    lost_tests = drift < 0
    ok = failures == 0 and errors <= allowed_errors and not lost_tests

    mark = "✓" if ok else "✗"
    line = f"{mark} {name}: ran {ran}"
    if failures:
        line += f", {failures} FAILED"
    if errors:
        line += f", {errors} error(s)"
        if errors <= allowed_errors:
            line += " (known)"
    if skipped:
        line += f", {skipped} skipped"
    if lost_tests:
        line += (f"   ✗ expected {expected} ({drift:+d}) — "
                 f"{abs(drift)} test(s) have gone missing")
    elif drift:
        line += f"   ⚠️  expected {expected} ({drift:+d}) — update the constant"
    print(line)

    if not ok and not quiet:
        for block in re.findall(r"^(?:FAIL|ERROR): .*$", text, re.M)[:15]:
            print(f"    {block}")
        print(f"    (full output: rerun without --quiet, or see {name} directly)")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mutation", action="store_true",
                        help="Also run the mutation harnesses (slow)")
    parser.add_argument("--quiet", action="store_true",
                        help="Counts only, no failure detail")
    args = parser.parse_args()

    check_environment()
    check_config()
    check_suites_exist()
    ok = True

    # ⚠️ cwd AND PYTHONPATH both matter. Run from the repo root this discovers
    # nothing and exits 0 — a green run that tested nothing.
    integration = run(
        [sys.executable, "-m", "unittest", "discover", "-p", "test_*.py"],
        cwd=INTEGRATION_DIR,
        extra_path=os.pathsep.join([
            str(INTEGRATION_DIR / "shims"), str(INTEGRATION_DIR), str(ROOT),
        ]),
    )
    ok &= report("crm_integration", integration, EXPECTED_INTEGRATION, 0, args.quiet)

    unit = run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(UNIT_DIR),
         "-p", "test_engage_crm*.py"],
        cwd=ROOT,
    )
    ok &= report("engage_crm unit", unit, EXPECTED_UNIT, KNOWN_UNIT_ERRORS,
                 args.quiet)

    service = run(
        # ⚠️ NAMED, NOT DISCOVERED. `discover -s tests` walks INTO
        # tests/unit and tests/integration, so it re-ran both suites and
        # reported 815 "service" tests. The service's own tests are the two
        # modules at the top of tests/; list them.
        [sys.executable, "-m", "unittest",
         "tests.test_health", "tests.test_suite_integrity", "tests.test_auth",
         "tests.test_diff", "tests.test_cors", "tests.test_internal",
         "tests.test_host", "tests.test_profiles"],
        cwd=ROOT,
    )
    ok &= report("service", service, EXPECTED_SERVICE, 0, args.quiet)

    if args.mutation:
        harnesses = sorted((ROOT / "tests" / "mutation").glob("mutate_*.py"))
        if not harnesses:
            print("· no mutation harnesses checked in yet")
        for harness in harnesses:
            res = run([sys.executable, str(harness)], cwd=ROOT)
            text = (res.stdout or "") + (res.stderr or "")
            survived = re.search(r"SURVIVED (\d+)", text)
            killed = re.search(r"killed\s+(\d+)", text)
            count = int(survived.group(1)) if survived else -1
            mark = "✓" if res.returncode == 0 and count == 0 else "✗"
            print(f"{mark} {harness.name}: "
                  f"killed {killed.group(1) if killed else '?'}, survived {count}")
            if count != 0 and not args.quiet:
                for line in text.splitlines():
                    if line.strip().startswith("!!"):
                        print(f"    {line.strip()}")
            ok &= res.returncode == 0 and count == 0

    print()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
