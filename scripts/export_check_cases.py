"""Export cases that pin the hosted validator to the Python one, word for word.

    python scripts/export_check_cases.py

For every tool of every catalogued LabMCP server: a valid call, the call with
no params, and calls that break one rule each (an unknown param, a wrong type,
each bound, enum, length and safety limit). Each case records what
rhylthyme_labmcp says: ``problems`` from ``call_problems`` (server defaults
for limits) and the planning ``estimate``. rhylthyme-server's
mcp-api/labmcp.test.js checks labmcp.js against them.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping

from rhylthyme_labmcp import load_catalog
from rhylthyme_labmcp.checks import LIMIT_TARGETS, SERIES, server_entry
from rhylthyme_labmcp.checks import RETURNS_EARLY_TOOLS, call_problems, timing_problems
from rhylthyme_labmcp.estimates import estimate_call

OUT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "rhylthyme_labmcp"
    / "catalog"
    / "labmcp-check-cases.json"
)


def _branch(prop: Mapping[str, Any]) -> Mapping[str, Any]:
    """The non-null branch of an Optional (anyOf [X, null])."""
    for option in prop.get("anyOf") or []:
        if option.get("type") != "null":
            return option
    return prop


def _valid(prop: Mapping[str, Any]) -> Any:
    p = _branch(prop)
    if "default" in prop and prop["default"] is not None:
        return prop["default"]
    if "enum" in p:
        return p["enum"][0]
    kind = p.get("type")
    if kind in ("number", "integer"):
        for key in ("minimum", "exclusiveMinimum"):
            if key in p:
                return p[key] + (1 if key == "exclusiveMinimum" else 0)
        return p.get("maximum", 1)
    if kind == "boolean":
        return True
    if kind == "string":
        return "x" * max(1, p.get("minLength", 1))
    if kind == "array":
        item = _valid(p.get("items") or {})
        return [item] * max(1, p.get("minItems", 1))
    if kind == "object":
        return {}
    return None


def _broken(prop: Mapping[str, Any]) -> Iterator[Any]:
    p = _branch(prop)
    kind = p.get("type")
    yield {"number": "fast", "integer": "fast", "string": 7, "boolean": "yes"}.get(
        kind, 3.5
    )
    if kind == "integer":
        yield 1.5
    if "minimum" in p:
        yield p["minimum"] - 1
    if "exclusiveMinimum" in p:
        yield p["exclusiveMinimum"]
    if "maximum" in p:
        yield p["maximum"] + 1
    if "exclusiveMaximum" in p:
        yield p["exclusiveMaximum"]
    if "enum" in p:
        yield "not-a-choice"
    if "minLength" in p and p["minLength"] > 0:
        yield ""
    if "maxLength" in p:
        yield "y" * (p["maxLength"] + 1)
    if "pattern" in p:
        yield "!!"
    if kind == "array":
        if p.get("minItems"):
            yield []
        if "maxItems" in p:
            yield [_valid(p.get("items") or {})] * (p["maxItems"] + 1)
        yield ["wrong", 1, None]


def cases() -> List[Dict[str, Any]]:
    out = []
    catalog = load_catalog()["packages"]
    for package in sorted(catalog):
        entry = server_entry(package) or catalog[package]
        for command in sorted(entry.get("tools") or {}):
            schema = entry["tools"][command]["inputSchema"]
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            valid = {name: _valid(props[name]) for name in required}
            calls = [valid, {}, {**valid, "no_such_param": 1}]
            for name, prop in props.items():
                for bad in _broken(prop):
                    calls.append({**valid, name: bad})
            for limit, by_tool in sorted(LIMIT_TARGETS.get(package, {}).items()):
                target = by_tool.get(command)
                default = (entry.get("limits") or {}).get(limit, {}).get("default")
                if target == SERIES and isinstance(default, (int, float)):
                    calls.append({**valid, "count": 1000, "interval_s": default})
                elif target and isinstance(default, (int, float)):
                    calls.append({**valid, target: default + 1})
            for limit, spec in sorted((entry.get("limits") or {}).items()):
                param = limit.split("_", 1)[1] if "_" in limit else ""
                if param in props and isinstance(spec.get("default"), (int, float)):
                    step = 1 if spec.get("kind", "max") == "max" else -1
                    calls.append({**valid, param: spec["default"] + step})
            seen = set()
            for params in calls:
                key = json.dumps(params, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                found = estimate_call(package, command, params)
                out.append(
                    {
                        "package": package,
                        "command": command,
                        "params": params,
                        "problems": [
                            list(p) for p in call_problems(package, command, params)
                        ],
                        "estimate": {
                            "seconds": found.seconds,
                            "source": found.source,
                            "detail": found.detail,
                        },
                    }
                )
        out.append(
            {
                "package": package,
                "command": "no_such_tool",
                "params": {},
                "problems": [
                    list(p) for p in call_problems(package, "no_such_tool", {})
                ],
                "estimate": None,
            }
        )
    return out


def timing_cases() -> List[Dict[str, Any]]:
    """Each dose tool in each phase, with steps shorter and longer than it."""
    doses = [
        ("labmcp-new-era", "infuse", {"volume_ml": 2, "rate_ml_min": 4}),
        ("labmcp-new-era", "withdraw", {"volume_ml": 0.5, "rate_ml_min": 7}),
        ("labmcp-cavro", "dispense_ul", {"volume_ul": 250, "flow_ul_s": 50}),
        ("labmcp-cavro", "aspirate_ul", {"volume_ul": 300}),
        ("labmcp-ika", "set_speed", {"speed_rpm": 300}),
    ]
    assert {(p, c) for p, c, _ in doses} >= set(RETURNS_EARLY_TOOLS)
    out = []
    for package, command, params in doses:
        for phase in ("call", "until", "start", "end"):
            for seconds in (None, 1, 5, 60):
                out.append(
                    {
                        "package": package,
                        "command": command,
                        "params": params,
                        "phase": phase,
                        "stepSeconds": seconds,
                        "problems": [
                            list(p)
                            for p in timing_problems(
                                package, command, params, phase, seconds
                            )
                        ],
                    }
                )
    return out


def main() -> int:
    data = {
        "source": load_catalog()["source"],
        "cases": cases(),
        "timing": timing_cases(),
    }
    OUT.write_text(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )
    bad = sum(1 for c in data["cases"] if c["problems"])
    print(f"{OUT}: {len(data['cases'])} cases ({bad} with problems)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
