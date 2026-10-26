"""Local dev entry point, mirroring `emailMarketingAPI/dev_api.py`.

    python3 -m poetry run python dev_api.py

Production does NOT use this file — PM2 runs uvicorn directly, see
`ecosystem.config.js`. Two differences from the platform's copy, both
deliberate:

⚠️ THE PORT COMES FROM CONFIG, NOT A LITERAL. The platform hardcodes 8197
because that is its only port. This service already has `[CRM] port` (and
`CRM_PORT`), and `/health` reports it — so hardcoding a second copy here is
the "two copies of one rule" bug this codebase keeps paying for: change the
config, restart, and wonder why nginx is talking to nothing.

⚠️ IT BINDS 127.0.0.1, WHERE THE PLATFORM'S BINDS 0.0.0.0. This is the one
place I did not copy the platform, and the reason is `/api/v1/internal/`.
Those endpoints take an account id and act on it with no end-user token; what
protects them is a shared secret AND an nginx rule that refuses the prefix
from off-box. Bind 0.0.0.0 and the nginx rule is bypassed entirely, because
traffic no longer has to go through nginx to reach the port. PM2 uses
`--host 127.0.0.1` in production for exactly this reason.

Need it reachable from another device on your network (a phone testing the
UI)? One env var, and it says what you are turning off:

    CRM_DEV_BIND_ALL=1 python3 -m poetry run python dev_api.py
"""
import os

import uvicorn

# Keeps `__pycache__/` out of the tree, same as the platform's copy. This repo
# accumulated four of those directories from compile checks alone.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

if __name__ == "__main__":
    # Imported here rather than at module scope: with `reload=True` uvicorn
    # re-executes this file in a child process, and loading config twice logs
    # the whole startup block twice.
    from app.config import settings

    bind_all = os.environ.get("CRM_DEV_BIND_ALL", "").strip().lower() in (
        "1", "true", "yes", "on")
    host = "0.0.0.0" if bind_all else "127.0.0.1"
    port = int(getattr(settings, "port", None) or 8100)

    if bind_all:
        print(f"\n⚠️  CRM_DEV_BIND_ALL is set — binding {host}:{port}, which "
              f"exposes\n    /api/v1/internal/ to your whole network. Dev "
              f"machines only.\n")
    print(f"CRM service → http://{'localhost' if not bind_all else host}:{port}"
          f"/docs    (health: /health, readiness: /health/ready)\n")

    # ⚠️ THE RELOADER IS OFF OUTSIDE `dev`, AND THAT IS WHY PM2 CAN RUN THIS
    # FILE. `ecosystem.config.js` invokes exactly this command on the droplet,
    # mirroring the platform's own PM2 entry — and the platform's runs its
    # dev entry point with `reload=True` in production, which costs a watcher
    # process polling the tree for changes that never come.
    #
    # Keyed on `[CRM] environment`, so one command is correct in both places:
    # `dev` locally gets the reloader, `stage`/`production` do not. Override
    # either way with CRM_DEV_RELOAD=1 / =0.
    env_reload = os.environ.get("CRM_DEV_RELOAD", "").strip().lower()
    if env_reload in ("1", "true", "yes", "on"):
        reload = True
    elif env_reload in ("0", "false", "no", "off"):
        reload = False
    else:
        reload = str(getattr(settings, "environment", "dev")).lower() == "dev"

    print(f"    reload: {'on' if reload else 'off'} "
          f"(environment={getattr(settings, 'environment', '?')})\n")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        # Only `app/` — the default watches the whole tree, so `logs/`,
        # `data/crm_files/` and `config/` churn would all trigger restarts.
        reload_dirs=["app"] if reload else None,
    )
