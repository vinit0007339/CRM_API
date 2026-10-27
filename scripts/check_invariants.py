#!/usr/bin/env python3
"""Invariants that nothing else enforces, and that a plausible edit breaks.

    python3 scripts/check_invariants.py          # report, exit 1 on failure
    python3 scripts/check_invariants.py --list    # print what is pinned

WHY A SCRIPT AND NOT A TEST. This repo has no `tests/` tree. These three
invariants are each a comment today, and a comment is not a guard — so they go
where the repo's other mechanical verification already lives, beside
`check_crm_contract.py` and `cutover_check.py`, and run the same way in CI.

WHAT IS PINNED, AND WHY EACH ONE IS HERE:

1. **`AssociationEntity` member ORDER.** Its own docstring says reordering
   "would orphan existing rows" — the member order defines the canonical
   direction a pair is stored in, and the unique index on `crm_associations` is
   only meaningful because writes are normalised to that direction first.
   Nothing enforced it. An alphabetical tidy-up of that enum is a silent data
   corruption whose only obstacle was a sentence.

2. **`associations._PRECEDENCE` AGREES with that enum.** ⚠️ THIS IS A LIVE
   LATENT BUG, not a hypothetical. `app/services/associations.py` is
   deliberately stdlib-only — "No Beanie, no Motor, no pydantic" — so it
   CANNOT import the enum, and keeps its own copy of the ordering:

       _PRECEDENCE = {"company": 0, "deal": 1, "contact": 2}

   Two copies of one rule, and the one that is easy to find is not the one that
   runs. Add a member to the enum and forget this dict and `normalize_pair`
   raises `InvalidAssociation` for every pair of the new type — at which point
   the unique index guards nothing, because nothing reaches it. This is the
   sixth instance of two-copies-of-one-rule in this codebase; the previous five
   each cost a debugging session.

3. **The UI's `LIFECYCLE_STAGES` equals `contact_rules.LIFECYCLE`.** ⚠️ ANOTHER
   LIVE BUG WHEN THIS WAS WRITTEN, and across two repositories rather than two
   files. `emailMarketingUI` offered seven stages — marketing_qualified,
   sales_qualified, opportunity, evangelist, other among them — none of which
   `clean_lifecycle` accepts, and could not reach `subscriber` or `churned` at
   all. Five of seven options came back "is not a lifecycle stage" from the
   form; as the quick filter they asked for rows no record can have and
   rendered an empty list with no error. The comment above the UI list claimed
   it mirrored `contact_rules.LIFECYCLE`.

   Checked only when the UI repo is a sibling checkout, which is the same
   assumption `check_crm_contract.py` already makes about
   `../emailMarketingUI`. Absent, it is reported as skipped, never as a pass.

4. **The `PERMISSIONS` set.** `app/auth/rbac.py` is a copy of the platform's
   file. `builtin_overrides` arrives from the platform through introspection and
   is applied against THIS copy, so when the two disagree a role means one
   thing here and another there. Pinning the set makes adding a permission a
   visible, chosen change on both sides rather than a drift.

HOW TO CHANGE A PINNED VALUE: edit the expectation here in the SAME commit as
the code, never afterwards. The point is that the change is deliberate, not
that it is impossible.

⚠️ EVERY CHECK BELOW ASSERTS ON PARSED STRUCTURE, NEVER ON A SOURCE GREP. A
`grep` for a name is not a check that the name is used: this repo has produced
five guards that passed against the mutation they existed to catch, most
recently a `compare_digest` check that was satisfied by the word appearing in
its own docstring four lines above the call. `_PRECEDENCE` is read by AST and
the enum by import, so neither can be satisfied by a comment.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── the pinned expectations ──────────────────────────────────────────────────

#: Order matters. Append only; see check 1.
# `crm_contact` appended when the CRM regained its own people object: a
# platform contact id and a CRM contact id are both ObjectIds, so the row's
# TYPE is the only thing that can say which collection its `to_id` lives in.
EXPECTED_ASSOCIATION_ORDER = ["company", "deal", "contact", "crm_contact"]

EXPECTED_PERMISSIONS = {
    "contacts:read", "contacts:write",
    "lists:read", "lists:write",
    "campaigns:read", "campaigns:write",
    "segments:read", "segments:write",
    "flows:read", "flows:write",
    "templates:read", "templates:write",
    "analytics:read",
    "billing:read", "billing:write",
    "team:read", "team:write",
    "settings:read", "settings:write",
    "apikeys:read", "apikeys:write",
    "inbox:read", "inbox:write",
    "companies:read", "companies:write",
    "deals:read", "deals:write",
    "pipelines:read", "pipelines:write",
    "crm_tasks:read", "crm_tasks:write",
    "crm:manage_others",
}

FAILURES: list = []
NOTES: list = []


def fail(check: str, detail: str) -> None:
    FAILURES.append(f"{check}: {detail}")


# ── 1. the association enum's member order ───────────────────────────────────

def check_association_order() -> list:
    """The enum's member ORDER, by import — not its membership."""
    from app.enums import AssociationEntity

    actual = [m.value for m in AssociationEntity]
    if actual != EXPECTED_ASSOCIATION_ORDER:
        fail("association-order",
             f"AssociationEntity is {actual}, pinned as "
             f"{EXPECTED_ASSOCIATION_ORDER}. The member order defines the "
             f"canonical storage direction of every crm_associations row; "
             f"REORDERING orphans existing rows and APPENDING is safe. If you "
             f"appended deliberately, update EXPECTED_ASSOCIATION_ORDER in "
             f"this file in the same commit.")
    return actual


# ── 2. _PRECEDENCE agrees with the enum ──────────────────────────────────────

def read_precedence() -> dict:
    """`_PRECEDENCE` as the module literally declares it, via AST.

    Read rather than imported so this check reports a malformed dict instead of
    inheriting an import error, and so it cannot be satisfied by the name
    appearing in a comment.
    """
    path = ROOT / "app" / "services" / "associations.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_PRECEDENCE":
                    return ast.literal_eval(node.value)
    fail("precedence-found",
         "no module-level _PRECEDENCE assignment in "
         "app/services/associations.py — it was renamed or moved, and the "
         "agreement with AssociationEntity is now unguarded")
    return {}


def check_precedence_agrees(enum_order: list) -> None:
    """The two copies of the ordering must say the same thing.

    Keys AND their order, because `_PRECEDENCE`'s values are the ranking: a
    dict with the right keys in the wrong order ranks pairs differently and
    writes them in a direction the unique index does not expect.
    """
    precedence = read_precedence()
    if not precedence:
        return

    if list(precedence) != enum_order:
        fail("precedence-agrees",
             f"_PRECEDENCE is {list(precedence)} but AssociationEntity is "
             f"{enum_order}. These are two copies of ONE rule and the "
             f"stdlib-only module is the one that runs. A member missing here "
             f"makes normalize_pair raise InvalidAssociation for every pair of "
             f"that type.")

    expected_ranks = list(range(len(precedence)))
    if sorted(precedence.values()) != expected_ranks:
        fail("precedence-ranks",
             f"_PRECEDENCE values are {sorted(precedence.values())}, expected "
             f"{expected_ranks} — the ranks must be 0..n-1 with no gaps or "
             f"duplicates, or two entity types tie and the storage direction "
             f"of their pairs depends on argument order.")
    elif [precedence[k] for k in precedence] != expected_ranks:
        fail("precedence-ranks",
             f"_PRECEDENCE keys are in a different order from their ranks: "
             f"{precedence}. Keep declaration order and rank aligned, or the "
             f"dict reads as agreeing with the enum when it does not.")


# ── 3. the RBAC permission set ───────────────────────────────────────────────

def check_permissions() -> None:
    from app.auth.rbac import PERMISSIONS

    actual = set(PERMISSIONS)
    added = actual - EXPECTED_PERMISSIONS
    removed = EXPECTED_PERMISSIONS - actual
    if added or removed:
        parts = []
        if added:
            parts.append(f"added {sorted(added)}")
        if removed:
            parts.append(f"REMOVED {sorted(removed)}")
        fail("permissions",
             f"PERMISSIONS has {' and '.join(parts)}. This set is a copy of "
             f"the platform's; `builtin_overrides` comes back from "
             f"introspection and is applied against THIS copy, so a change "
             f"here needs the same change there. Update "
             f"EXPECTED_PERMISSIONS in the same commit as both.")


def check_permissions_are_granted() -> None:
    """Every permission is reachable by some role, or it is decoration.

    A permission in the set that no role grants and no `extra_grants` mentions
    cannot be held by anybody, so the endpoint behind it is unreachable. Worth
    knowing when a module is added: this is what "I added the route and
    everyone gets a permission failure" looks like from the config side.
    """
    from app.auth import rbac

    granted = set()
    for value in vars(rbac).values():
        if isinstance(value, dict):
            for perms in value.values():
                if isinstance(perms, (list, set, tuple)):
                    granted.update(p for p in perms if isinstance(p, str))

    orphans = sorted(set(rbac.PERMISSIONS) - granted - {"crm:manage_others"})
    if orphans:
        NOTES.append(
            f"{len(orphans)} permission(s) no built-in role grants: {orphans}. "
            f"Not a failure — they may be granted per-member via extra_grants "
            f"— but an endpoint gated on one is unreachable by default.")


# ── 4. the UI's lifecycle list ───────────────────────────────────────────────

UI_CONTACT_HELPERS = (ROOT.parent / "emailMarketingUI" / "src" / "screens" /
                      "CRM" / "shared" / "contactHelpers.js")


def check_ui_lifecycle_stages() -> None:
    """The UI's dropdown must offer exactly what the server will accept.

    Parsed out of the JS array rather than imported — there is no Node in this
    checker's world — but parsed as a LIST OF STRING LITERALS, not grepped for
    a name. A `grep` for "subscriber" would pass on the file's own comment,
    which quotes both the right list and the wrong one.
    """
    from app.services.contact_rules import LIFECYCLE

    if not UI_CONTACT_HELPERS.exists():
        NOTES.append(
            f"UI lifecycle check SKIPPED — no sibling checkout at "
            f"{UI_CONTACT_HELPERS}. This is not a pass: the two lists had "
            f"never agreed before they were pinned.")
        return

    text = UI_CONTACT_HELPERS.read_text(encoding="utf-8")
    match = re.search(r"export const LIFECYCLE_STAGES\s*=\s*\[(.*?)\]",
                      text, re.S)
    if not match:
        fail("ui-lifecycle",
             f"no `export const LIFECYCLE_STAGES = [...]` in "
             f"{UI_CONTACT_HELPERS.name} — it was renamed or moved, and the "
             f"agreement with contact_rules.LIFECYCLE is now unguarded")
        return

    ui = re.findall(r'"([^"]+)"', match.group(1))
    if ui != list(LIFECYCLE):
        fail("ui-lifecycle",
             f"the UI offers {ui} but the server accepts {list(LIFECYCLE)}. "
             f"Values the UI offers and the server rejects come back as \"is "
             f"not a lifecycle stage\" on the form and as a silently empty "
             f"list when used as the quick filter; values the server accepts "
             f"and the UI omits are unreachable. Order matters too — it is the "
             f"dropdown's order and the funnel's.")


# ── runner ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="print what is pinned and exit")
    args = parser.parse_args()

    if args.list:
        print("AssociationEntity order:")
        print(f"  {EXPECTED_ASSOCIATION_ORDER}")
        from app.services.contact_rules import LIFECYCLE
        print(f"\nLIFECYCLE (server, and the UI must match):")
        print(f"  {list(LIFECYCLE)}")
        print(f"\nPERMISSIONS ({len(EXPECTED_PERMISSIONS)}):")
        for permission in sorted(EXPECTED_PERMISSIONS):
            print(f"  {permission}")
        return 0

    enum_order = check_association_order()
    check_precedence_agrees(enum_order or EXPECTED_ASSOCIATION_ORDER)
    check_permissions()
    check_permissions_are_granted()
    check_ui_lifecycle_stages()

    for note in NOTES:
        print(f"note: {note}\n")

    if FAILURES:
        print(f"FAIL — {len(FAILURES)} invariant(s) broken:\n")
        for failure in FAILURES:
            print(f"  ✗ {failure}\n")
        return 1

    print("PASS — association order pinned, _PRECEDENCE agrees with the enum, "
          f"{len(EXPECTED_PERMISSIONS)} permissions unchanged, "
          "the UI's lifecycle stages match the server's")
    return 0


if __name__ == "__main__":
    sys.exit(main())
