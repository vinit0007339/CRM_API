"""The host boundary: everything this service needs from the app around it.

⚠️ READ THIS BEFORE INTEGRATING THE CRM INTO A NEW PROJECT. It is the whole
contract, and it is deliberately small: two questions and five lookups.

    IDENTITY   "who is this caller, and what may they do?"   → identity.py
    DIRECTORY  "what is this user/tag/contact called?"        → directory.py

Nothing else. The CRM owns its 14 collections and asks the host for exactly
these. `docs/host-contract.md` is the spec a consumer implements; this package
is the service's side of it.

WHY THIS EXISTS AS A SEAM AT ALL. The CRM was extracted from
`emailMarketingAPI`, and the first version of the extraction simply called
that monolith: one hardcoded URL for identity, and direct Mongo reads into its
`users`, `tags` and `contacts` collections. That works, and it still runs —
it is the `introspect` + `shared_db` pair below. But it made the service
consumable by exactly one project, and the thing that made it one project was
not the CRM logic. It was these two files.

⚠️ THE DEFAULTS ARE THE OLD BEHAVIOUR, ON PURPOSE AND NOT AS A COURTESY.
`identity=introspect` and `directory=shared_db` run the SAME CODE PATHS the
platform has been running, which is what lets the 891 integration tests and
the Phase 5 dual-run diff stay meaningful: a new consumer gets new adapters,
and `emailMarketingAPI` gets no change at all. A refactor that improves the
abstraction and alters the tenancy behaviour in passing is not a refactor,
it is an incident with good intentions.

CHOOSING ADAPTERS. `config/app.conf`:

    [HOST]
    identity  = introspect        ; or jwt_claims
    directory = shared_db         ; or http, or none
    base_url  = http://127.0.0.1:8000
    internal_key = <shared secret>

`base_url` and `internal_key` fall back to `[CRM] platform_base_url` and
`[CRM] internal_key`, so an existing deployment needs no config change
whatsoever to keep working.
"""
from __future__ import annotations

from app.host.directory import Directory, get_directory
from app.host.identity import Identity, get_identity

__all__ = ["Directory", "Identity", "get_directory", "get_identity"]
