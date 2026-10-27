#!/usr/bin/env python3
"""Compare `api-contract.md` against the real CRM surface. CRM-P0-003.

    python3 scripts/check_crm_contract.py            # report drift
    python3 scripts/check_crm_contract.py --list     # print the real surface

WHY A SCRIPT AND NOT A CAREFUL READ. The contract was frozen at Phase 1 and six
phases landed on top of it. It has drifted in BOTH directions — endpoints that
exist and are undocumented, and endpoints documented that never shipped — and
every reconciliation by hand has lasted exactly until the next phase. A
frontend engineer building against a wrong contract loses more time than this
script took to write.

So the document stays hand-written where judgement matters (envelopes, error
semantics, the rules with teeth) and this checks the one thing that is
mechanical: does every endpoint the routers expose appear in the tables, with
the right method and permission, and does every documented endpoint exist?

HOW IT READS THE ROUTERS. By AST, not regex: the decorators carry the method
and path, the handler signature carries the permission via
`Depends(require(...))` / `require_any(...)`, and the mount prefix comes from
`routes_handler.py`. A route whose permission cannot be determined is REPORTED,
not assumed — an endpoint whose gate nobody can state is worth looking at.

Exit 1 on drift, so it can go in CI.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES = ROOT / "app" / "routes"
#: The mount table moved from the monolith's `api/routes_handler.py` into the
#: service's own `app/main.py`. Same `include_router(...)` calls, same
#: prefixes, read the same way — by AST, so the four modules that publish a
#: second router under a different prefix are still keyed correctly.
HANDLER = ROOT / "app" / "main.py"
CONTRACT_CANDIDATES = [
    # This repo first: the contract documents THIS service's API, and the
    # checker that verifies it lives here now.
    ROOT / "docs" / "api-contract.md",
    # The UI repo, where it lived while the CRM was part of the monolith.
    # Kept so the checker works before Phase 8 moves the document, and so a
    # developer with both repos checked out gets the same answer either way.
    ROOT.parent / "emailMarketingUI" / "docs" / "CRM" / "api-contract.md",
    ROOT.parent / "emailMarketingAPI" / "docs" / "CRM" / "api-contract.md",
]

METHODS = {"get", "post", "patch", "put", "delete"}

#: Route modules that are deliberately NOT part of the CRM's public contract.
#:
#: ⚠️ `internal.py` IS NOT AN OVERSIGHT AND MUST NOT BE DOCUMENTED. It lives
#: under `/api/v1/internal/`, behind an nginx deny rule and a shared secret,
#: and it takes an account id with no end-user token in the request — the
#: platform calls it after its own permission check. Putting it in the
#: document the UI team reads as "the API" is an invitation to call it.
#:
#: Named explicitly rather than inferred from the mount prefix, because the
#: prefix table is built by parsing `app/main.py` and a module it fails to
#: parse would fall back to `/crm/<module>` and be reported as an
#: undocumented CRM endpoint — which is exactly what happened: the checker
#: announced `POST /crm/internal/crm/auto-link` and pushed whoever read it
#: towards documenting an internal endpoint. An explicit list cannot do that.
NON_CRM_MODULES = {"internal"}


def find_contract() -> Path | None:
    for path in CONTRACT_CANDIDATES:
        if path.exists():
            return path
    return None


def mount_prefixes() -> dict[tuple[str, str], str]:
    """`{(module, router_variable): mount_prefix}` from the include_router calls.

    ⚠️ KEYED ON THE ROUTER VARIABLE, NOT THE MODULE. Four CRM modules declare
    more than one router and mount them at different paths — `deals.board_router`
    under /crm/pipelines, `exports.company_router` and `exports.deal_router`
    under the company and deal prefixes, `tasks.types_router` under
    /crm/task-types. Keyed on the module, every one of those is reported at its
    sibling's path: the first version of this script put `GET /crm/task-types`
    at `/crm/tasks` and produced a phantom duplicate.

    Read from the source rather than hard-coded, so a router mounted somewhere
    new is picked up instead of quietly misfiled.
    """
    text = HANDLER.read_text()

    # ⚠️ TWO IMPORT SHAPES, BECAUSE THE MOUNT TABLE MOVED REPOS. The
    # monolith's `routes_handler.py` imports every CRM route module under a
    # `crm_`-prefixed alias (`deals as crm_deals`) because it also imports
    # thirty other modules and `deals` alone would collide. The service's
    # `app/main.py` has no such problem and imports them plainly — only
    # `settings` needs an alias, since `app.config.settings` is already a name
    # here.
    #
    # The first port of this script read aliases only, found none, and
    # reported ten endpoints undocumented and twelve documented-but-missing —
    # every one a phantom (`/crm/saved_views` guessed from the module name
    # instead of the real `/crm/saved-views`). A checker that fails loudly for
    # the wrong reason gets switched off, so it reads both shapes.
    # ⚠️ BY AST, NOT REGEX — AND THE REGEX IT REPLACED WAS SILENTLY WRONG.
    # The old pattern `^\s+(\w+) as (\w+),?$` required the alias to be the
    # ONLY name on its line. `app/main.py` writes it inline:
    #
    #     pipelines, saved_views, settings as crm_settings, tasks,
    #
    # so the alias never matched; the plain-name pass then skipped
    # `settings as crm_settings` because it is not an identifier, and
    # `settings` ended up with no mount prefix at all. That stayed invisible
    # because `real_surface` fell back to a GUESSED `/crm/<module>` prefix,
    # which for this one module happens to be the right answer — so the
    # checker passed on a lookup that had failed. It only surfaced when the
    # fallback was removed, and then it read as "settings is not mounted",
    # which is the opposite of true.
    #
    # Two lessons the rest of this file already states: a prefix recovered by
    # guessing is not a prefix that was read, and an import is a parse tree,
    # not a line of text.
    aliases: dict[str, str] = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "routes"):
            for name in node.names:
                aliases[name.name] = name.asname or name.name

    by_alias_router: dict[tuple[str, str], str] = {}
    for alias, router, prefix in re.findall(
        r"(\w+)\.(\w*router),.*?prefix=f\"\{API_ROOT_PATH\}([^\"]+)\"",
        text, re.S,
    ):
        by_alias_router.setdefault((alias, router), prefix)

    out: dict[tuple[str, str], str] = {}
    for module, alias in aliases.items():
        for (a, router), prefix in by_alias_router.items():
            if a == alias:
                out[(module, router)] = prefix
    return out


def module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = require(...)` / `require_any(...)` gates.

    `saved_views.py` writes `READ = require_any("companies:read", "deals:read")`
    once and reuses it on four handlers. Reading only the inline form reported
    all four as having no permission — which looks like four ungated endpoints
    and is not true. A checker's own blind spot is worse than the drift it
    finds, because it sends somebody looking for a security hole that is not
    there.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        name = getattr(node.value.func, "id", None)
        if name not in ("require", "require_any"):
            continue
        perms = [a.value for a in node.value.args if isinstance(a, ast.Constant)]
        if not perms:
            continue
        label = ANY_OF.join(perms) if name == "require_any" else perms[0]
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = label
    return out


# How a `require_any` gate is written in the generated table.
#
# ⚠️ NOT " | ". A raw pipe inside a markdown table cell ENDS THE CELL, so every
# `require_any` row in the emitted block rendered as a six-column mess — the
# notes endpoints had looked like that since this script was written, and the
# checker never noticed because it compares method and path, not this column.
ANY_OF = " · "


def permission_of(node, constants: dict[str, str]) -> str | None:
    """The permission gating this handler, inline or via a module constant."""
    defaults = list(node.args.defaults) + [
        d for d in node.args.kw_defaults if d is not None]
    for default in defaults:
        if not isinstance(default, ast.Call):
            continue
        if getattr(default.func, "id", None) != "Depends":
            continue
        if not default.args:
            continue
        arg = default.args[0]
        # Depends(READ) — a module-level constant.
        if isinstance(arg, ast.Name):
            if arg.id in constants:
                return constants[arg.id]
            continue
        # Depends(require("x")) / Depends(require_any("x", "y"))
        if not isinstance(arg, ast.Call):
            continue
        name = getattr(arg.func, "id", None)
        if name not in ("require", "require_any"):
            continue
        perms = [a.value for a in arg.args if isinstance(a, ast.Constant)]
        if not perms:
            return None
        return ANY_OF.join(perms) if name == "require_any" else perms[0]
    return None


def real_surface() -> list[dict]:
    prefixes = mount_prefixes()
    out: list[dict] = []
    for path in sorted(ROUTES.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = path.stem
        tree = ast.parse(path.read_text())
        constants = module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                attr = getattr(deco.func, "attr", None)
                router = getattr(deco.func, "value", None)
                router_name = getattr(router, "id", "")
                if attr not in METHODS or "router" not in router_name:
                    continue
                route = deco.args[0].value if deco.args else ""
                response = next(
                    (ast.unparse(kw.value) for kw in deco.keywords
                     if kw.arg == "response_model"), None)
                mount = prefixes.get((module, router_name))
                if module in NON_CRM_MODULES:
                    continue
                out.append({
                    "module": module,
                    "method": attr.upper(),
                    "path": f"{mount or f'/crm/{module}'}{route}",
                    # ⚠️ A ROUTER WITH NO `include_router` CALL EXPOSES
                    # NOTHING, and the line above invents `/crm/<module>` for
                    # it. That fallback used to make an unmounted router look
                    # shipped: its routes were compared against the contract,
                    # reported as undocumented drift, and the run failed —
                    # while the endpoints were unreachable and documenting
                    # them would have made the contract lie. Recorded here and
                    # reported separately in `main`, so "defined but not
                    # mounted" is a stated fact rather than either a silent
                    # omission or a false failure.
                    "mounted": mount is not None,
                    "permission": permission_of(node, constants),
                    "response": response,
                    "handler": node.name,
                })
    return out


# Path parameters are spelled differently in prose than in code
# (`{id}` vs `{company_id}`), and that is not drift.
def canonical(path: str) -> str:
    # ⚠️ STRIP THE QUERY STRING. The prose documents endpoints as
    # `GET /crm/view-preferences?object_type=` and `GET /crm/deals?pipeline_id=all`,
    # which are the same endpoint as the table row. The first version of this
    # script reported eight of those as "documented but does not exist" — a
    # checker that cries wolf is one that gets switched off, and this is the
    # only kind of bug it can have that costs more than it saves.
    path = path.split("?")[0]
    path = re.sub(r"\{[^}]*\}", "{}", path)
    path = re.sub(r":path", "", path)
    return path.rstrip("/") or "/"


def documented(contract: Path) -> set[tuple[str, str]]:
    """`{(METHOD, canonical path)}` from every table row and heading.

    A prose mention is a CLAIM that the endpoint exists, which is why it
    counts: `POST /crm/deals/{id}/close` written in a paragraph is as much a
    promise to a client as a table row.

    ⚠️ EXCEPT A STRUCK-THROUGH ONE. A contract has to be able to say "this used
    to exist and is gone" — that note is the most useful line in the document
    for whoever is holding a client that still calls it. Written
    `~~`POST /crm/contacts/{id}/link`~~`, it renders as struck text and is
    subtracted here. Without this the checker made documenting a removal
    impossible: naming the endpoint in order to say it was removed re-added it
    to the contract, and the only way to pass was to say nothing.
    """
    text = contract.read_text()
    found: set[tuple[str, str]] = set()
    # table rows: | `GET` | `/crm/companies` | ...
    for method, path in re.findall(
        r"^\|\s*`(GET|POST|PATCH|PUT|DELETE)`\s*\|\s*`([^`]+)`", text, re.M):
        found.add((method, canonical(path)))
    # headings and prose: `GET /crm/deals/{id}/stage`
    for method, path in re.findall(
        r"`(GET|POST|PATCH|PUT|DELETE)\s+(/crm/[^`\s]+)`", text):
        found.add((method, canonical(path)))
    # ...minus the ones marked removed: ~~`POST /crm/contacts/{id}/link`~~
    for method, path in re.findall(
        r"~~`(GET|POST|PATCH|PUT|DELETE)\s+(/crm/[^`\s]+)`~~", text):
        found.discard((method, canonical(path)))
    return found


ROUTER_TITLES = {
    "companies": "Companies", "contacts": "Contacts", "deals": "Deals",
    "pipelines": "Pipelines", "tasks": "Tasks", "notes": "Notes",
    "files": "Files", "attributes": "Attributes", "settings": "Settings",
    "imports": "Imports", "exports": "Exports",
    "saved_views": "Saved views", "view_preferences": "Column layouts",
}


def emit_markdown(surface: list[dict]) -> str:
    """The surface as a markdown block, grouped by router.

    Written back into `api-contract.md` between the GENERATED markers so the
    complete endpoint list is never a transcription. The prose around it stays
    hand-written — judgement does not generate.
    """
    lines = [
        "<!-- GENERATED: scripts/check_crm_contract.py --emit — do not edit by hand -->",
        "",
        "Every CRM endpoint the routers expose, with the permission that gates it",
        "and the response model it declares. Regenerate with",
        "`python3 scripts/check_crm_contract.py --emit`; verify with the same",
        "script and no flag, which fails on drift in either direction.",
        "",
        f"**{len(surface)} endpoints across "
        f"{len({r['module'] for r in surface})} routers.**",
        "",
    ]
    for module in sorted({r["module"] for r in surface},
                         key=lambda m: ROUTER_TITLES.get(m, m)):
        rows = sorted((r for r in surface if r["module"] == module),
                      key=lambda r: (r["path"], r["method"]))
        lines.append(f"#### {ROUTER_TITLES.get(module, module)}")
        lines.append("")
        lines.append("| Method | Path | Permission | Response model |")
        lines.append("|---|---|---|---|")
        for row in rows:
            perm = row["permission"] or "**⚠️ none found**"
            lines.append(
                f"| `{row['method']}` | `{row['path']}` | `{perm}` | "
                f"`{row['response'] or '—'}` |")
        lines.append("")
    lines.append("<!-- /GENERATED -->")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true",
                        help="Print the real surface and exit")
    parser.add_argument("--emit", action="store_true",
                        help="Print the surface as the markdown block that "
                             "belongs between the GENERATED markers in "
                             "api-contract.md")
    args = parser.parse_args()

    surface = real_surface()

    if args.list:
        for row in sorted(surface, key=lambda r: (r["path"], r["method"])):
            perm = row["permission"] or "⚠️ UNKNOWN"
            flag = "" if row["mounted"] else "  [NOT MOUNTED]"
            print(f"{row['method']:<7} {row['path']:<46} {perm:<34} "
                  f"{row['response'] or '—'}{flag}")
        live = [r for r in surface if r["mounted"]]
        print(f"\n{len(live)} endpoints across "
              f"{len({r['module'] for r in live})} routers")
        if len(live) != len(surface):
            print(f"{len(surface) - len(live)} further route(s) are defined "
                  f"but not mounted, and expose nothing")
        return 0

    if args.emit:
        # Only the live API belongs in the contract.
        print(emit_markdown([r for r in surface if r["mounted"]]))
        return 0

    contract = find_contract()
    if contract is None:
        print("✗ api-contract.md not found. Looked in:", file=sys.stderr)
        for path in CONTRACT_CANDIDATES:
            print(f"    {path}", file=sys.stderr)
        return 1

    docs = documented(contract)
    # The contract documents the LIVE API, so only mounted routes are compared
    # against it. Unmounted ones are reported below instead.
    mounted = [r for r in surface if r["mounted"]]
    unmounted = [r for r in surface if not r["mounted"]]
    real = {(r["method"], canonical(r["path"])) for r in mounted}

    undocumented = sorted(real - docs)
    # A documented path that no longer exists. Worse than an undocumented one:
    # somebody will write a client against it.
    phantom = sorted(docs - real)
    ungated = [r for r in mounted if not r["permission"]]

    print(f"contract: {contract}")
    print(f"real surface: {len(real)} endpoints across "
          f"{len({r['module'] for r in mounted})} routers")
    print(f"documented:   {len(docs)}")
    print()

    if undocumented:
        print(f"✗ {len(undocumented)} endpoint(s) exist and are NOT documented:")
        for method, path in undocumented:
            match = next(r for r in surface
                         if r["method"] == method and canonical(r["path"]) == path)
            print(f"    {method:<7} {match['path']:<44} "
                  f"({match['permission'] or '⚠️ no permission found'})")
        print()

    if unmounted:
        modules = sorted({r["module"] for r in unmounted})
        print(f"ℹ️ {len(unmounted)} route(s) DEFINED BUT NOT MOUNTED "
              f"({', '.join(modules)}):")
        print("    They expose nothing and are not compared against the")
        print("    contract. Either mount them in app/main.py's _MOUNT_TABLE")
        print("    and document them, or delete them — a route module nobody")
        print("    mounts is the shape a half-finished wiring job leaves.")
        for row in sorted(unmounted, key=lambda r: (r["path"], r["method"])):
            print(f"    {row['method']:<7} {row['path']:<44} "
                  f"({row['module']}.{row['handler']})")
        print()

    if phantom:
        print(f"✗ {len(phantom)} endpoint(s) documented that DO NOT exist:")
        print("    (worse than the above — somebody will write a client "
              "against these)")
        for method, path in phantom:
            print(f"    {method:<7} {path}")
        print()

    if ungated:
        print(f"⚠️ {len(ungated)} route(s) whose permission could not be read:")
        for row in ungated:
            print(f"    {row['method']:<7} {row['path']:<44} "
                  f"({row['module']}.{row['handler']})")
        print()

    ok = not undocumented and not phantom
    print("PASS — the contract matches the routers" if ok else "FAIL — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
