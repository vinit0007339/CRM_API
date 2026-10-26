"""How much of the CRM to serve. One config line, three answers.

    [CRM]
    profile = full        ; or core, or minimal

⚠️ WHY THIS IS A PROFILE AND NOT A SECOND, SMALLER REPOSITORY. The obvious way
to ship "a small CRM another project can consume" is to copy this service and
delete two thirds of it. That produces a genuinely small codebase, and it is
the wrong trade, because the copies then drift — and this project has paid for
that mistake three times in as many weeks:

  · two numbers-only coercion maps, one of which never learned about dates,
    which is what made `expected_close_date='cylonsign.com'` a 500;
  · two copies of the system-attribute set, which is why a committed tree
    reported `17 != 2` and eight tests failed on a clean checkout;
  · the default host-adapter name written out in FOUR places, only one of
    them load bearing, which is why two mutation tests aimed straight at
    "did the platform's defaults change?" both survived.

A forked CRM is that failure with a two-year fuse: the fork gets the bug fix
or the tenancy correction, and the other one does not. So there is ONE
codebase, and a profile decides how much of it is mounted.

⚠️ WHAT A PROFILE TRIMS IS THE API SURFACE, NOT THE SOURCE TREE. A consumer
running `core` gets a smaller thing to LEARN — fewer endpoints, a shorter
contract, less to secure — but the repository is the same size. If what you
need is a small repository rather than a small API, that is a fork, and the
paragraph above is the argument against it; read it before deciding, not
after.

⚠️ `full` IS THE DEFAULT AND MUST STAY THE DEFAULT. Engage's UI makes calls
against all 83 endpoints. A deployment whose `app.conf` predates this file
gets every one of them, exactly as before; anything else would turn a config
file nobody edited into a broken CRM.
"""
from __future__ import annotations

from typing import Dict, FrozenSet

#: Every route module, and the only list of them. `app/main.py` mounts from
#: this and `scripts/check_crm_contract.py` reads it, so a fourteenth module
#: cannot be half-wired.
ALL_MODULES: FrozenSet[str] = frozenset({
    "attributes", "companies", "deals", "exports", "files", "imports",
    "notes", "pipelines", "saved_views", "settings", "tasks",
    "view_preferences",
})

#: ⚠️ `attributes` AND `settings` ARE IN EVERY PROFILE, AND NOT BECAUSE THEY
#: ARE SMALL. They are load bearing for the modules around them:
#:
#:   · `deals.py` routes every custom field value through
#:     `attribute_routing.route_for`, which reads attribute DEFINITIONS. Mount
#:     deals without attributes and a consumer can create a deal but cannot
#:     define, see or validate a single custom field — and the 15 system
#:     attributes the routes self-provision on read would have no endpoint to
#:     be read from.
#:   · `settings.py` carries `lost_reasons`, which the close-as-lost dialog
#:     cannot render without. Closing a deal as lost is core CRM, not a
#:     setting.
PROFILES: Dict[str, FrozenSet[str]] = {
    # Engage. Everything.
    "full": ALL_MODULES,

    # A CRM another project can adopt: companies, deals, pipelines and the
    # board, plus notes and tasks on the records. What is left out is the
    # machinery Engage grew around it — saved views, per-user column layouts,
    # the import wizard, export jobs, and files with signed downloads — each
    # of which is a feature a new consumer can ask for later rather than a
    # thing it must understand on day one.
    "core": frozenset({
        "companies", "deals", "pipelines", "notes", "tasks",
        "settings", "attributes",
    }),

    # The smallest thing that is still a CRM: records, pipelines and a board.
    # No notes, no tasks. For a project that has its own activity model and
    # wants the CRM only for companies and deals.
    "minimal": frozenset({
        "companies", "deals", "pipelines", "settings", "attributes",
    }),
}

#: The profile a config file that has never heard of profiles gets.
DEFAULT_PROFILE = "full"


def modules_for(profile: str) -> FrozenSet[str]:
    """The modules a profile mounts.

    An UNKNOWN name falls back to `full` rather than raising, and that
    direction is deliberate: this is read once at startup, and a typo in
    `app.conf` must not be the reason a CRM serves nothing. Serving too much
    on a misspelling is visible and harmless; serving a third of the API is
    neither.
    """
    from app.config import logger

    name = (profile or "").strip().lower()
    if name not in PROFILES:
        if name:
            logger.error("unknown [CRM] profile=%r — serving %r instead "
                         "(valid: %s)", profile, DEFAULT_PROFILE,
                         ", ".join(sorted(PROFILES)))
        return PROFILES[DEFAULT_PROFILE]
    return PROFILES[name]
