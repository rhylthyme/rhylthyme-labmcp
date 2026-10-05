"""Check LabMCP calls before a run, against the vendored LabMCP catalogue.

``check_calls(program, tools)`` checks every instrument step's call: that its
server has the tool, and that the params fit the tool's input schema (unknown
or missing params, types, bounds, enums) and the safety limits. A limit named
``max_<param>`` or ``min_<param>`` bounds the param of that name; the
workcell's ``limits`` override the server's defaults, as they do at run time.

With a workcell, each step's server comes from its tool; without one, from
the step's ``toolType`` (a LabMCP package name, e.g. ``labmcp-ika``).
``check_workcell(tools)`` checks the workcell's own entries: known servers,
known limit names, and version pins.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from .workcell import PACKAGE_PREFIX, LabMCPTool

UNKNOWN_SERVER = "instrument_unknown_tool_type"
INVALID_COMMAND = "instrument_invalid_command"
OVER_LIMIT = "instrument_over_limit"
UNKNOWN_LIMIT = "workcell_unknown_limit"
VERSION_MISMATCH = "workcell_version_mismatch"

_LIMIT_RE = re.compile(r"^(max|min)_(.+)$")


@dataclass(frozen=True)
class Issue:
    code: str
    step_id: str  # "" for the workcell itself
    message: str
    severity: str = "error"  # "error" | "warning"


def _ints(value: Any) -> Any:
    """2.0 -> 2, all the way down: JSON cannot tell them apart in JavaScript,
    so messages print whole numbers the same way in both validators."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_ints(v) for v in value]
    if isinstance(value, dict):
        return {k: _ints(v) for k, v in value.items()}
    return value


@lru_cache(maxsize=1)
def load_catalog() -> Mapping[str, Any]:
    """The vendored catalogue (see scripts/export_catalog.py)."""
    text = (
        resources.files("rhylthyme_labmcp")
        .joinpath("catalog/labmcp-catalog.json")
        .read_text(encoding="utf-8")
    )
    return _ints(json.loads(text))


def server_entry(package: str) -> Optional[Mapping[str, Any]]:
    entry = load_catalog()["packages"].get(package)
    return entry if entry and "error" not in entry else None


def pinned_version(package: str) -> str:
    """The version the catalogue was exported from ('' if not catalogued)."""
    entry = load_catalog()["packages"].get(package)
    return entry["version"] if entry else ""


def _steps(program: Mapping[str, Any]) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    for track in program.get("tracks") or []:
        for step in track.get("steps") or []:
            instrument = step.get("instrument")
            if isinstance(instrument, Mapping):
                yield str(step.get("stepId", "?")), instrument


def _schema_problems(schema: Mapping[str, Any], params: Any) -> List[str]:
    import jsonschema

    cls = jsonschema.validators.validator_for(schema, jsonschema.Draft202012Validator)
    problems = []
    for error in sorted(cls(schema).iter_errors(params), key=lambda e: list(e.path)):
        where = ".".join(str(p) for p in error.path)
        problems.append(f"{where}: {error.message}" if where else error.message)
    return problems


def effective_limits(
    package: str, overrides: Optional[Mapping[str, float]] = None
) -> Dict[str, Tuple[str, float, str, bool]]:
    """limit name -> (kind, value, unit, set by the workcell)."""
    entry = server_entry(package) or {}
    limits = {}
    for name, limit in (entry.get("limits") or {}).items():
        if "default" not in limit:
            continue
        set_here = name in (overrides or {})
        value = (overrides or {}).get(name, limit["default"])
        limits[name] = (
            limit.get("kind", "max"),
            value,
            limit.get("unit", ""),
            set_here,
        )
    return limits


def _limit_problems(
    package: str, params: Mapping[str, Any], overrides: Optional[Mapping[str, float]]
) -> List[str]:
    problems = []
    for name, (kind, limit, unit, set_here) in effective_limits(
        package, overrides
    ).items():
        match = _LIMIT_RE.match(name)
        if not match or match.group(2) not in params:
            continue
        value = params[match.group(2)]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        over = value > limit if kind == "max" else value < limit
        if over:
            whose = "the workcell's" if set_here else "the server's default"
            word = "above" if kind == "max" else "below"
            problems.append(
                f"{match.group(2)}={value:g} is {word} {whose} limit "
                f"{name}={limit:g}{(' ' + unit) if unit else ''}"
            )
    return problems


def call_problems(
    package: str,
    command: Any,
    params: Any,
    overrides: Optional[Mapping[str, float]] = None,
) -> List[Tuple[str, str]]:
    """
    (code, problem) for one call to a catalogued server: an unknown tool,
    then its params against the tool's input schema, then the limits. The
    hosted validator (rhylthyme-server mcp-api/labmcp.js) says the same,
    word for word (scripts/export_check_cases.py).
    """
    entry = server_entry(package) or {"tools": {}}
    spec = entry["tools"].get(command)
    if spec is None:
        known = ", ".join(sorted(entry["tools"]))
        return [
            (INVALID_COMMAND, f"{package} has no tool {command!r} (tools: {known})")
        ]
    params = _ints(params)
    problems = [
        (INVALID_COMMAND, p)
        for p in _schema_problems(spec.get("inputSchema") or {}, params)
    ]
    if isinstance(params, Mapping):
        problems += [
            (OVER_LIMIT, p) for p in _limit_problems(package, params, overrides)
        ]
    return problems


def check_calls(
    program: Mapping[str, Any], tools: Optional[Mapping[str, LabMCPTool]] = None
) -> List[Issue]:
    """Issues for ``program``'s instrument steps (one call each)."""
    issues: List[Issue] = []
    version = load_catalog()["source"].get("labmcp", "")
    for step_id, instrument in _steps(program):
        tool_name = instrument.get("tool")
        command = instrument.get("command")
        prefix = f"Step '{step_id}'"
        tool = (tools or {}).get(tool_name)
        if tool is not None:
            package, overrides = tool.package, tool.limits
            if not package:
                continue  # an http server: its tools are not catalogued
        else:
            package, overrides = str(instrument.get("toolType") or ""), None
        entry = server_entry(package)
        if entry is None:
            issues.append(
                Issue(
                    UNKNOWN_SERVER,
                    step_id,
                    f"{prefix}: {package!r} is not in the LabMCP catalogue "
                    f"(LabMCP {version}); {tool_name}.{command} not checked",
                    severity="warning",
                )
            )
            continue
        about = f"{prefix}: {tool_name} ({package}) {command}"
        params = instrument.get("params") or {}
        for code, problem in call_problems(package, command, params, overrides):
            issues.append(Issue(code, step_id, f"{about}: {problem}"))
    return issues


def check_workcell(tools: Mapping[str, LabMCPTool]) -> List[Issue]:
    issues: List[Issue] = []
    version = load_catalog()["source"].get("labmcp", "")
    for tool in tools.values():
        if not tool.package:
            continue
        entry = server_entry(tool.package)
        if entry is None:
            issues.append(
                Issue(
                    UNKNOWN_SERVER,
                    "",
                    f"Tool {tool.name!r}: {tool.package!r} is not in the LabMCP "
                    f"catalogue (LabMCP {version}); its steps are not checked",
                    severity="warning",
                )
            )
            continue
        known = entry.get("limits") or {}
        for name in tool.limits:
            if name not in known:
                issues.append(
                    Issue(
                        UNKNOWN_LIMIT,
                        "",
                        f"Tool {tool.name!r}: {tool.package} has no limit {name!r} "
                        f"(limits: {', '.join(sorted(known)) or 'none'})",
                    )
                )
        if tool.version and tool.version != entry["version"]:
            issues.append(
                Issue(
                    VERSION_MISMATCH,
                    "",
                    f"Tool {tool.name!r} pins {tool.package}=={tool.version}; steps "
                    f"are checked against {entry['version']}, the catalogued version",
                    severity="warning",
                )
            )
    return issues


__all__ = [
    "INVALID_COMMAND",
    "call_problems",
    "Issue",
    "OVER_LIMIT",
    "PACKAGE_PREFIX",
    "UNKNOWN_LIMIT",
    "UNKNOWN_SERVER",
    "VERSION_MISMATCH",
    "check_calls",
    "check_workcell",
    "effective_limits",
    "load_catalog",
    "pinned_version",
    "server_entry",
]
