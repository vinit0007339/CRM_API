#!/usr/bin/env bash
#
# Bring the CRM's git HISTORY into this repo, then lay the new layout over it.
#
#     bash scripts/import_history.sh ../emailMarketingAPI
#
# WHY NOT JUST COPY THE FILES. 38,000 lines of this codebase are comments
# explaining which bug a guard is standing in front of — the str-Enum trap, the
# if/else-on-object-type class, naive UTC timestamps, pydantic not validating
# on assignment. `git log` and `git blame` are how the next person finds the
# ones the comments do not name. A `cp` throws that away, and it cannot be
# recovered later.
#
# WHAT THIS DOES, in order:
#   1. saves this repo's current working tree — the service — to a scratch dir;
#   2. clones the monolith to another scratch dir (never touches your copy);
#   3. filters that clone down to the CRM paths only, keeping every commit
#      that touched them;
#   4. brings that history in, so the files land at their ORIGINAL monolith
#      paths with their full log;
#   5. restores the saved tree over the top and deletes the old paths.
#
# Step 5 as ONE commit is what makes `git log --follow app/routes/deals.py`
# walk back through `api/routes/crm/deals.py`: git detects the rename from the
# content, so the delete and the add must be in the same commit.
#
# IT COMMITS NOTHING. Everything is left staged for you to inspect and commit.
#
# ⚠️ YOU DO NOT HAVE TO RUN THIS. If you would rather start this repo with a
# clean single commit, `git add . && git commit` right now and skip the whole
# thing — the tree is ready. What you give up is `git blame`: 38,000 lines of
# this codebase are comments explaining which bug a guard stands in front of,
# and the log is how the next person finds the ones the comments do not name.
# That is recoverable only by running this BEFORE the first commit.
set -euo pipefail

MONOLITH="${1:-../emailMarketingAPI}"
SCRATCH="${TMPDIR:-/tmp}/crm-history-$$"
BRANCH="$(git -C "$MONOLITH" rev-parse --abbrev-ref HEAD 2>/dev/null || echo crm)"

# The CRM's paths in the monolith. Anything not listed here does not come over
# — which is the point: this is also the inventory of what "the CRM" means.
PATHS=(
  api/routes/crm
  api/services/engage/crm
  app/db/crud/main/crm
  app/db/models/main/crm
  api/schemas/main/crm
  app/enums/crm.py
  tests/crm_integration
  tests/test_engage_crm_activity.py
  tests/test_engage_crm_activity_filters.py
  tests/test_engage_crm_associations.py
  tests/test_engage_crm_attribute_routing.py
  tests/test_engage_crm_attributes.py
  tests/test_engage_crm_dedupe.py
  tests/test_engage_crm_defaults.py
  tests/test_engage_crm_files.py
  tests/test_engage_crm_filters.py
  tests/test_engage_crm_forecast.py
  tests/test_engage_crm_lost_reasons.py
  tests/test_engage_crm_permissions.py
  tests/test_engage_crm_position.py
  tests/test_engage_crm_queries.py
  tests/test_engage_crm_sanitise.py
  tests/test_engage_crm_saved_views.py
  tests/test_engage_crm_serializers.py
  tests/test_engage_crm_stages.py
  tests/test_engage_crm_tasks.py
  tests/mutation
  scripts/seed_crm_defaults.py
  scripts/check_crm_contract.py
  scripts/crm_tests.py
  scripts/benchmark_crm_board.py
  scripts/repair_crm_typed_columns.py
  scripts/reconcile_crm_task_counters.py
  scripts/migrate_crm_auto_link_default.py
  scripts/migrate_crm_contacts_removal.py
  # ⚠️ NOT NAMED `*crm*`, AND NEARLY MISSED. The CRM's merge tests import it.
  scripts/backfill_company_name_normalised.py
)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

# ── preconditions ───────────────────────────────────────────────────────────
command -v git >/dev/null || die "git is not on PATH"
[ -d "$MONOLITH/.git" ] || die "$MONOLITH is not a git repository (pass the path as \$1)"
[ -d .git ] || die "run this from the repo root (the directory with .git in it)"
[ -f pyproject.toml ] && [ -d app ] || die "this does not look like the CRM_API tree: expected pyproject.toml and app/"

# Two shapes are supported, and the difference is whether this repo has any
# commits yet.
if git rev-parse --verify HEAD >/dev/null 2>&1; then
  FRESH=0
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "your working tree has uncommitted changes. Commit or stash them first — bringing in a second history needs a clean tree."
  fi
else
  FRESH=1
  say "This repo has no commits yet — the imported history becomes its first."
fi

if ! command -v git-filter-repo >/dev/null && ! python3 -c "import git_filter_repo" 2>/dev/null; then
  die "git-filter-repo is not installed.
    brew install git-filter-repo        # or
    pip3 install git-filter-repo
  It is the supported tool for this; \`git filter-branch\` would work but is
  slow enough on a repo this size to look broken, and is deprecated upstream."
fi

# Only the paths that actually exist, so a renamed tree does not abort the run.
ARGS=()
for path in "${PATHS[@]}"; do
  case "$path" in \#*) continue ;; esac
  if git -C "$MONOLITH" cat-file -e "$BRANCH:$path" 2>/dev/null; then
    ARGS+=(--path "$path")
  else
    printf '  · skipping %s (not in %s)\n' "$path" "$BRANCH"
  fi
done
[ ${#ARGS[@]} -gt 0 ] || die "none of the CRM paths exist in $MONOLITH@$BRANCH"

# ── 1 & 2: clone and filter ─────────────────────────────────────────────────
# ── 1: save the working tree ────────────────────────────────────────────────
# Everything except .git, because the next step rewrites the worktree. Saved
# rather than stashed: `git stash` leaves untracked files behind by default,
# and in a fresh repo EVERYTHING is untracked — the one case where the
# familiar command silently does nothing.
SAVED="${TMPDIR:-/tmp}/crm-worktree-$$"
mkdir -p "$SAVED"
say "Saving the current tree → $SAVED"
tar -cf - --exclude=./.git --exclude=./.git/* . | tar -xf - -C "$SAVED"
printf '  %s file(s) saved\n' "$(find "$SAVED" -type f | wc -l | tr -d ' ')"

say "Cloning $MONOLITH@$BRANCH → $SCRATCH"
git clone --no-local --quiet --branch "$BRANCH" "$MONOLITH" "$SCRATCH"

say "Filtering to the CRM paths (${#ARGS[@]} / 2 paths)"
( cd "$SCRATCH" && git filter-repo --quiet "${ARGS[@]}" )
printf '  %s commits kept\n' "$(git -C "$SCRATCH" rev-list --count HEAD)"

# ── 4: bring the filtered history in at the original paths ─────────────────
say "Bringing that history into this repo"
git remote remove crm-history 2>/dev/null || true
git remote add crm-history "$SCRATCH"
git fetch --quiet crm-history "$BRANCH"

if [ "$FRESH" = "1" ]; then
  # ⚠️ NO MERGE, BECAUSE THERE IS NOTHING TO MERGE INTO. `git merge
  # --allow-unrelated-histories` needs a HEAD, and a repo with no commits has
  # none. Point the branch straight at the fetched history instead: its
  # commits become this repo's first commits, which is exactly what is wanted.
  git reset --hard "crm-history/$BRANCH" >/dev/null
else
  git merge --allow-unrelated-histories --no-edit -m \
    "Import the CRM's history from emailMarketingAPI

Files land at their monolith paths; the next commit moves them into the
service layout, so git detects the rename and \`git log --follow\` works
through it." "crm-history/$BRANCH"
fi

# ── 5: restore the service tree over it, and delete the old paths ───────────
say "Restoring the service tree"
tar -cf - -C "$SAVED" . | tar -xf - -C .
# ⚠️ ONLY THE PATHS THE NEW LAYOUT DOES NOT ALSO USE.
#
# Several paths are the SAME in both trees — `scripts/seed_crm_defaults.py`,
# `scripts/crm_tests.py`, `tests/mutation/` — because the service kept them
# where they were. Deleting every path in the list removed those files
# immediately after restoring them, and the commit came out MISSING them:
# caught by running this on a synthetic pair and noticing
# `scripts/seed_crm_defaults.py` had vanished from the final tree.
#
# So: a path goes only if the saved service tree has nothing there.
for path in "${PATHS[@]}"; do
  case "$path" in \#*) continue ;; esac
  [ -e "$path" ] || continue
  if [ -e "$SAVED/$path" ]; then
    printf '  · keeping %s (the service uses the same path)\n' "$path"
    continue
  fi
  git rm -r -q --ignore-unmatch "$path"
done
# The empty parents the deletions leave behind.
find api app/db app/enums -type d -empty -delete 2>/dev/null || true
rmdir api 2>/dev/null || true

git add -A
git remote remove crm-history
printf '\n  the saved tree is still at %s until you are happy\n' "$SAVED"

say "Done — nothing committed."
cat <<'NEXT'
  Review, then commit as ONE commit so the rename is detected:

      git status
      git diff --cached --stat -M
      git commit -m "Move the CRM into the service layout"
      git push -u origin main

  Then check the history survived:

      git log --follow --oneline app/routes/deals.py | tail -5
      git log --follow --oneline app/services/stage_rules.py | wc -l

  If `--follow` stops at the move commit, the rename was not detected: check
  that the delete and the add are in the same commit (`git show --stat -M`).
NEXT
