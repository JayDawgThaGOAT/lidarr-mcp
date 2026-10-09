#!/usr/bin/env python3
"""Sync `_TOOL_REGISTRY` query-param defaults against the vendored OpenAPI spec.

The registry is generated from `tests/data/lidarr_openapi.json`, but the
authoring script is not in this repo, so defaults drift silently. This script
only rewrites the `'qp': [...]` line of each registry entry to match the spec,
leaving names, paths, docs, bodies and grouping untouched. That keeps
regeneration-style edits scripted instead of hand-edited (see AGENTS.md).

Rules for boolean query params:
  * spec declares a default  -> use it (`True` / `False`).
  * spec declares no default -> use `False`. ASP.NET binds an absent
    non-nullable `bool` to false, so this matches the API's own behaviour for
    params that are plain filters. It also matters for params that belong to an
    undocumented any-one-of group (`trackfile`'s `unmapped`): omitting those is
    a 400, while `false` is accepted, so omitting would break the call.
  Only the spec-declared defaults are load-bearing; a hardcoded `False` for a
  param the spec declares `true` silently inverts behaviour, which is the bug
  this script exists to prevent.

Non-boolean params are left alone: their defaults are structural, not
semantic (e.g. `sortKey='""'` vs no spec default both serialise to omitted).

Usage:
    python3 scripts/sync_registry_defaults.py          # rewrite lidarr_mcp.py
    python3 scripts/sync_registry_defaults.py --check   # exit 1 if out of sync
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "lidarr_mcp.py"
SPEC = ROOT / "tests" / "data" / "lidarr_openapi.json"

REGISTRY_NAME = "_TOOL_REGISTRY"


def normalize(path: str) -> str:
    """Collapse path templates so `{id}` matches `{albumId}` for lookup."""
    return "/".join("{}" if seg.startswith("{") else seg for seg in path.split("/"))


def load_registry(source: str) -> list[dict]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == REGISTRY_NAME:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == REGISTRY_NAME:
                    return ast.literal_eval(node.value)
    raise SystemExit(f"{REGISTRY_NAME} not found in {SOURCE}")


def spec_query_defaults(spec: dict) -> dict[tuple[str, str], dict[str, object]]:
    """(METHOD, normalized path) -> {wire_name: default_or_None}."""
    out: dict[tuple[str, str], dict[str, object]] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            params: dict[str, object] = {}
            for param in op.get("parameters", []):
                if param.get("in") != "query":
                    continue
                schema = param.get("schema") or {}
                params[param["name"]] = {
                    "type": schema.get("type"),
                    "default": schema.get("default"),
                    "has_default": "default" in schema,
                }
            out[(method.upper(), normalize(path))] = params
    return out


BOOL_TYPES = ("bool", "bool | None")


def synced_param(param: dict, spec_meta: dict | None) -> dict:
    """Return `param` with any drifted default/type corrected."""
    if spec_meta is None or param.get("type") not in BOOL_TYPES:
        return param

    param = dict(param)
    param["type"] = "bool"
    if spec_meta["has_default"]:
        param["default"] = "True" if spec_meta["default"] else "False"
    else:
        # No spec default. Keep False rather than omitting: it matches the
        # API's binding for plain filters, and params in an undocumented
        # any-one-of group (trackfile's `unmapped`) reject omission with 400.
        param["default"] = "False"
    return param


def render_qp_line(params: list[dict]) -> str:
    if not params:
        return "  'qp': [],"
    inner = ", ".join(
        "{'name': '%s', 'wire': '%s', 'type': '%s', 'default': '%s'}"
        % (p["name"], p["wire"], p["type"], p["default"])
        for p in params
    )
    return "  'qp': [" + inner + "],"


def find_qp_line(lines: list[str], op_name: str, start: int) -> int | None:
    for i in range(start, len(lines)):
        if lines[i].strip() == "{'name': '%s'," % op_name:
            for j in range(i + 1, min(i + 80, len(lines))):
                if lines[j].startswith("  'qp': "):
                    return j
                if lines[j].startswith(" {'name': "):
                    break
            return None
    return None


def main() -> int:
    check = "--check" in sys.argv[1:]
    source = SOURCE.read_text()
    lines = source.split("\n")
    registry = load_registry(source)
    spec_defaults = spec_query_defaults(json.loads(SPEC.read_text()))

    changes: list[tuple[str, str, str]] = []
    missing: list[str] = []
    cursor = 0
    for entry in registry:
        key = (entry["method"].upper(), normalize(entry["path"]))
        spec_params = spec_defaults.get(key, {})
        new_params = [
            synced_param(p, spec_params.get(p["wire"])) for p in entry.get("qp", [])
        ]
        new_line = render_qp_line(new_params)
        idx = find_qp_line(lines, entry["name"], cursor)
        if idx is None:
            missing.append(entry["name"])
            continue
        cursor = idx
        if lines[idx] != new_line:
            changes.append((entry["name"], lines[idx], new_line))
            lines[idx] = new_line

    if missing:
        print(
            f"error: no qp line found for {len(missing)} entries: "
            + ", ".join(missing[:5]),
            file=sys.stderr,
        )
        return 2

    if not changes:
        print("registry defaults already in sync with spec")
        return 0

    if check:
        print(f"{len(changes)} entr{'y' if len(changes) == 1 else 'ies'} out of sync:")
        for name, old, new in changes:
            print(f"  {name}")
        return 1

    SOURCE.write_text("\n".join(lines))
    print(f"updated {len(changes)} entr{'y' if len(changes) == 1 else 'ies'}:")
    for name, _old, _new in changes:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
