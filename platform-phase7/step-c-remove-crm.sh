#!/usr/bin/env bash
#
# STEP C — remove the CRM from the platform. THE LAST STEP, AND THE ONE THAT
# ENDS THE ONE-LINE ROLLBACK.
#
#     bash scripts/step-c-remove-crm.sh          # dry run, prints what it would do
#     bash scripts/step-c-remove-crm.sh --apply  # stages the deletions and moves
#
# ⚠️ DO NOT RUN THIS UNTIL nginx ROUTES `/api/v1/crm/` TO THE CRM SERVICE AND
# YOU HAVE WATCHED IT SERVE TRAFFIC. Until then this repo IS the CRM for every
# screen in the app. Deleting these paths while nginx still points here does
# not degrade the CRM, it removes it: 83 endpoints become 404 and every CRM
# page in the UI goes blank at once.
#
# The order that is safe, from docs/cutover.md in CRM_API:
#
#   1. Step A/B deployed here (api/routes/internal_auth.py +
#      api/services/crm_client.py + the two config keys). Both additive; the
#      CRM mounts in this repo still answer.
#   2. CRM service running, `scripts/cutover_check.py` green against it.
#   3. nginx `location /api/v1/crm/` → the service. ⚠️ THIS IS THE LAST
#      MOMENT THE ROLLBACK IS ONE LINE: put the old proxy_pass back and this
#      repo serves the CRM again, because the code is still here.
#   4. Watch it. `scripts/watch_cutover.py`.
#   5. THEN this script + step-c-code.patch, as ONE commit.
#
# After step 5 a rollback is a revert and a deploy.
#
# IT COMMITS NOTHING. Everything is left staged for you to inspect.
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

[ -d .git ] || { echo "✗ run this from the repo root (the directory with .git in it)" >&2; exit 1; }
[ -f api/routes_handler.py ] || { echo "✗ this does not look like emailMarketingAPI" >&2; exit 1; }

# ── the gate ────────────────────────────────────────────────────────────────
# Not a reminder, a precondition. Step A/B must be in already: internal_auth.py
# is what the CRM service calls to authorise EVERY request, so if it is not
# here the CRM is already answering 401 to the whole app and deleting more
# code will not be the thing you want to be doing.
missing=()
[ -f api/routes/internal_auth.py ] || missing+=("api/routes/internal_auth.py")
[ -f api/services/crm_client.py ]  || missing+=("api/services/crm_client.py")
if [ ${#missing[@]} -gt 0 ]; then
  printf '✗ Step A/B is not in this tree yet. Missing:\n' >&2
  printf '    %s\n' "${missing[@]}" >&2
  printf '  Apply and deploy Step A/B first — see docs/cutover.md.\n' >&2
  exit 1
fi

# ── what goes ───────────────────────────────────────────────────────────────
# This list is also the inventory of what "the CRM" meant in this repo. It is
# the same list `CRM_API/scripts/import_history.sh` used to bring the
# history across, minus the three paths the service kept at the same name.
DELETE=(
  api/routes/crm
  api/services/engage/crm
  app/db/crud/main/crm
  app/db/models/main/crm
  api/schemas/main/crm
  app/enums/crm.py
  tests/mutation
  scripts/seed_crm_defaults.py
  scripts/check_crm_contract.py
  scripts/crm_tests.py
  scripts/benchmark_crm_board.py
  scripts/repair_crm_typed_columns.py
  scripts/reconcile_crm_task_counters.py
  scripts/migrate_crm_auto_link_default.py
  scripts/migrate_crm_contacts_removal.py
  # ⚠️ NOT NAMED `*crm*`, AND NEARLY MISSED EACH TIME. Only the CRM's merge
  # tests use it, and they are in the other repo now.
  scripts/backfill_company_name_normalised.py
)
# Every tests/test_engage_crm_*.py — expanded rather than listed, because the
# set grew four times during the CRM work and a hand-written list went stale
# twice.
while IFS= read -r f; do DELETE+=("$f"); done < <(git ls-files 'tests/test_engage_crm_*.py')

# ── what stays, and why ─────────────────────────────────────────────────────
# `tests/crm_integration/` is NOT in the delete list, because one of its 28
# files tests THIS repo. `test_p2_contact_scope.py` is 25 tests of
# `ContactCrud` and `api/services/contact_scope.py` — platform code, written
# during the CRM work, which is the only reason it ever sat in a directory
# called crm_integration. It needs `harness.py` and `shims/`, so those three
# things move to `tests/platform_integration/` and the rest of the directory
# goes. See tests/integration/MOVED_OUT.md in CRM_API.
#
# ⚠️ `git mv`, NOT `cp`. `git log --follow` on these three is how the next
# person finds out why the shims shadow four packages.
KEEP_FROM_CRM_INTEGRATION=(harness.py shims test_p2_contact_scope.py)

run() {
  if [ "$APPLY" = "1" ]; then "$@"; else printf '    would run: %s\n' "$*"; fi
}

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ "$APPLY" = "1" ] || say "DRY RUN — nothing will change. Re-run with --apply."

say "Moving the three platform things out of tests/crm_integration/"
run mkdir -p tests/platform_integration
for item in "${KEEP_FROM_CRM_INTEGRATION[@]}"; do
  if [ -e "tests/crm_integration/$item" ]; then
    run git mv "tests/crm_integration/$item" "tests/platform_integration/$item"
  else
    printf '  · tests/crm_integration/%s is not here (already moved?)\n' "$item"
  fi
done
# ⚠️ NO __init__.py. The harness is imported as a top-level `harness`, which
# only works while this directory is on sys.path and is NOT a package. Adding
# one makes the suite error with `ModuleNotFoundError: No module named
# 'harness'`, which does not point at its own cause.
if [ -e tests/platform_integration/__init__.py ]; then
  run git rm -q --ignore-unmatch tests/platform_integration/__init__.py
fi

say "Deleting what is left of tests/crm_integration/"
run git rm -r -q --ignore-unmatch tests/crm_integration

say "Deleting the CRM trees (${#DELETE[@]} paths)"
for path in "${DELETE[@]}"; do
  case "$path" in \#*) continue ;; esac
  if git ls-files --error-unmatch "$path" >/dev/null 2>&1 || [ -e "$path" ]; then
    run git rm -r -q --ignore-unmatch "$path"
  else
    printf '  · %s is not here\n' "$path"
  fi
done

say "Tidying the directories the deletions emptied"
if [ "$APPLY" = "1" ]; then
  find api/services/engage app/db/crud/main app/db/models/main api/schemas/main \
       -type d -empty -delete 2>/dev/null || true
else
  printf '    would delete any directories left empty\n'
fi

if [ "$APPLY" = "1" ]; then
  cat <<'NEXT'

Done — nothing committed. Now:

  1. Apply the code changes, if you have not already:

         git apply --3way docs/step-c-code.patch

  2. Check nothing still reaches into the CRM. This must print nothing:

         grep -rn "routes\.crm\|engage\.crm\|crud\.main\.crm\|models\.main\.crm\|schemas\.main\.crm\|enums\.crm\|crm_integration" \
              --include=*.py . | grep -v tests/platform_integration

  3. Check the app still builds its routers and has no /crm paths left:

         python3 -c "
         import inspect
         from fastapi import FastAPI
         import api.routes_handler as rh
         fn=[v for n,v in vars(rh).items() if inspect.isfunction(v) and 'rout' in n.lower()]
         app=FastAPI(); fn[0](app)
         p=sorted(app.openapi()['paths'])
         print('paths', len(p), '| crm', [x for x in p if '/crm' in x],
               '| internal', [x for x in p if '/internal/' in x])"

     Expect roughly 560 paths, `crm []`, and
     `internal ['/api/v1/internal/auth/context']`.

  4. The kept platform suite, 25 tests:

         python3 scripts/platform_integration_tests.py

  5. Commit the deletions AND the code changes TOGETHER, so a revert is one
     commit:

         git commit -m "Remove the CRM: it lives in CRM_API now"

NEXT
else
  printf '\n  Nothing changed. Re-run with --apply when the cutover is done.\n'
fi
