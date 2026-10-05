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
RETURNS_EARLY = "instrument_returns_early"
DOSE_OUTLASTS_STEP = "instrument_dose_outlasts_step"

#: Tools that reply as soon as they start acting, before the work is done:
#: as a step's command or until, the step would end while the pump still
#: runs. Their descriptions say so ("Returns as soon as pumping has started").
RETURNS_EARLY_TOOLS = {
    ("labmcp-new-era", "infuse"),
    ("labmcp-new-era", "withdraw"),
}
UNCHECKED_URL = "workcell_url_unchecked"

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
    """A catalogued server, with the galago commands it can stand in for
    (compat) among its tools."""
    from .compat import COMPAT

    entry = load_catalog()["packages"].get(package)
    if not entry or "error" in entry:
        return None
    extra = COMPAT.get(package)
    if not extra:
        return entry
    tools = dict(entry.get("tools") or {})
    for command, spec in extra.items():
        tools[command] = {
            k: spec[k] for k in ("kind", "description", "inputSchema") if k in spec
        }
        tools[command]["compat"] = True
    return {**entry, "tools": tools}


def pinned_version(package: str) -> str:
    """The version the catalogue was exported from ('' if not catalogued)."""
    entry = load_catalog()["packages"].get(package)
    return entry["version"] if entry else ""


def _steps(
    program: Mapping[str, Any],
) -> Iterator[Tuple[str, Mapping[str, Any], Optional[float]]]:
    """(stepId, instrument, seconds the step may run or None) per step."""
    for track in program.get("tracks") or []:
        for step in track.get("steps") or []:
            instrument = step.get("instrument")
            if isinstance(instrument, Mapping):
                duration = step.get("duration")
                seconds = (
                    duration.get("seconds") if isinstance(duration, Mapping) else None
                )
                yield str(step.get("stepId", "?")), instrument, seconds


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


#: Limits not named after the param they bound: limit -> tool -> param, or
#: SERIES for a series duration, (count - 1) * interval_s. Also written into
#: the catalogue (``limitTargets``) for the hosted validator.
SERIES = "@series"
LIMIT_TARGETS: Dict[str, Dict[str, Dict[str, str]]] = {
    "labmcp-sila2": {
        "max_command_wait_s": {"call_command": "wait_s"},
        "max_discovery_s": {"discover_servers": "timeout_s"},
        "max_subscription_duration_s": {"subscribe_property": "duration_s"},
    },
    "labmcp-scpi": {"max_operation_wait_s": {"wait_operation_complete": "timeout_s"}},
    "labmcp-ika": {"max_wait_s": {"wait_for_temperature": "timeout_s"}},
    "labmcp-julabo": {"max_wait_s": {"wait_for_temperature": "timeout_s"}},
    "labmcp-lakeshore": {"max_wait_s": {"wait_for_stable_temperature": "timeout_s"}},
    "labmcp-ble-health": {
        "max_record_duration_s": {
            "record_heart_rate": "duration_s",
            "read_pulse_oximetry": "duration_s",
        },
        "max_wait_s": {"read_temperature": "timeout_s", "read_weight": "timeout_s"},
    },
    "labmcp-palmsens": {"max_duration_s": {"run_chronoamperometry": "run_time_s"}},
    "labmcp-mettler-toledo": {"max_series_duration_s": {"log_weight_series": SERIES}},
    "labmcp-sartorius": {"max_series_duration_s": {"log_weight_series": SERIES}},
    "labmcp-atlas-ezo": {"max_series_duration_s": {"log_series": SERIES}},
    "labmcp-alicat": {"max_series_duration_s": {"log_flow_series": SERIES}},
    "labmcp-thorlabs-pm": {"max_series_duration_s": {"log_power_series": SERIES}},
}


def _limited_values(
    package: str, command: Any, params: Mapping[str, Any]
) -> List[Tuple[str, str, float]]:
    """(limit, what, value) for every limit this call's params meet."""
    from .estimates import _with_defaults

    out = []
    for name in (server_entry(package) or {}).get("limits") or {}:
        match = _LIMIT_RE.match(name)
        if match and match.group(2) in params:
            value = params[match.group(2)]
            if not isinstance(value, bool) and isinstance(value, (int, float)):
                out.append((name, match.group(2), value))
    targets = LIMIT_TARGETS.get(package, {})
    if any(str(command) in by_tool for by_tool in targets.values()):
        merged, _ = _with_defaults(package, str(command), params)
        for name, by_tool in targets.items():
            target = by_tool.get(str(command))
            if target == SERIES:
                count, interval = merged.get("count"), merged.get("interval_s")
                if all(
                    isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in (count, interval)
                ):
                    out.append(
                        (
                            name,
                            "series (count - 1) × interval_s",
                            (count - 1) * interval,
                        )
                    )
            elif target is not None:
                value = merged.get(target)
                if not isinstance(value, bool) and isinstance(value, (int, float)):
                    out.append((name, target, value))
    return out


def _limit_problems(
    package: str,
    command: Any,
    params: Mapping[str, Any],
    overrides: Optional[Mapping[str, float]],
) -> List[str]:
    problems = []
    limits = effective_limits(package, overrides)
    for name, what, value in _limited_values(package, command, params):
        if name not in limits:
            continue
        kind, limit, unit, set_here = limits[name]
        over = value > limit if kind == "max" else value < limit
        if over:
            whose = "the workcell's" if set_here else "the server's default"
            word = "above" if kind == "max" else "below"
            problems.append(
                f"{what}={value:g} is {word} {whose} limit "
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
            (OVER_LIMIT, p)
            for p in _limit_problems(package, command, params, overrides)
        ]
    return problems


def timing_problems(
    package: str,
    command: Any,
    params: Any,
    phase: Optional[str],
    step_seconds: Optional[float],
) -> List[Tuple[str, str, str]]:
    """
    (code, problem, severity) about when a call's work ends: a tool that
    replies before its work is done cannot end a step, and a dose sent as a
    start action needs a step that lasts at least as long as the dose.
    """
    from .estimates import dose_seconds

    if not isinstance(params, Mapping):
        return []
    dose = dose_seconds(package, str(command), params)
    takes = f" (it takes {dose:g} s)" if dose is not None else ""
    if (package, command) in RETURNS_EARLY_TOOLS and phase in (None, "call", "until"):
        return [
            (
                RETURNS_EARLY,
                f"replies as soon as it starts, so the step would end before "
                f"the work is done{takes}; send it as a start action and give "
                "the step a duration",
                "error",
            )
        ]
    if phase == "start" and dose is not None and step_seconds is not None:
        if step_seconds < dose:
            return [
                (
                    DOSE_OUTLASTS_STEP,
                    f"the dose takes {dose:g} s but the step lasts "
                    f"{step_seconds:g} s; its end actions and next steps would "
                    "start while the pump still runs",
                    "warning",
                )
            ]
    return []


def check_calls(
    program: Mapping[str, Any], tools: Optional[Mapping[str, LabMCPTool]] = None
) -> List[Issue]:
    """Issues for ``program``'s instrument steps (one call each)."""
    issues: List[Issue] = []
    version = load_catalog()["source"].get("labmcp", "")
    for step_id, instrument, step_seconds in _steps(program):
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
        issues += [
            Issue(code, step_id, f"{about}: {problem}", severity)
            for code, problem, severity in timing_problems(
                package, command, params, instrument.get("phase"), step_seconds
            )
        ]
        if tool is not None:
            # The command policy the lab set on this server (options)
            from . import policies

            issues += [
                Issue(policies.POLICY_REFUSED, step_id, f"{about}: {problem}")
                for problem in policies.call_problems(tool, str(command), params)
            ]
    return issues


def check_workcell(tools: Mapping[str, LabMCPTool]) -> List[Issue]:
    issues: List[Issue] = []
    version = load_catalog()["source"].get("labmcp", "")
    for tool in tools.values():
        if not tool.package:
            issues.append(
                Issue(
                    UNCHECKED_URL,
                    "",
                    f"Tool {tool.name!r} is reached by url and names no server; "
                    'its steps are not checked (add "server" to name its package)',
                    severity="warning",
                )
            )
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
        from . import policies

        for problem in policies.workcell_problems(tool):
            issues.append(
                Issue(policies.POLICY_INVALID, "", f"Tool {tool.name!r}: {problem}")
            )
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
        if tool.version and not tool.url and tool.version != entry["version"]:
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
