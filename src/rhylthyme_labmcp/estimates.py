"""Planning durations for LabMCP calls whose reply ends a step.

A step with a ``command`` or ``until`` and no ``duration`` ends when its call
replies. Planners still need a number, estimated from the call's params,
with the tool's own defaults (from the catalogue) for params left out, by the
first rule that applies:

1. a series: ``count`` (or ``timepoints``) readings ``interval_s`` apart take
   ``(count - 1) * interval_s``, as LabMCP's own series-length check counts;
2. a run length: ``duration_s`` or ``run_time_s``, plus ``equilibration_s``;
3. a wait bounded by ``timeout_s`` (wait_for_temperature, BLE reads): the
   timeout, an upper bound;
4. otherwise ``DEFAULT_SECONDS`` (tare, set a speed, read a value).

Rules 1-3 are ``source: "params"`` estimates (detail says which params, and
"defaults" when the tool's defaults supplied them); rule 4 is ``"default"``.
"""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .checks import server_entry

#: Planning seconds for a call with nothing time-like in its params.
DEFAULT_SECONDS = 10

SERIES_COUNTS = ("count", "timepoints")
SERIES_INTERVALS = ("interval_s",)
RUN_LENGTHS = ("duration_s", "run_time_s")
SETTLE = ("equilibration_s",)
TIMEOUTS = ("timeout_s",)


@dataclass(frozen=True)
class CallEstimate:
    seconds: float
    source: str  # "params" | "default"
    detail: str = ""


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def _with_defaults(
    package: str, command: str, params: Mapping[str, Any]
) -> Tuple[Dict[str, Any], set]:
    """The params with the tool's schema defaults filled in, and which were."""
    merged = dict(params or {})
    defaulted = set()
    entry = server_entry(package) or {}
    spec = (entry.get("tools") or {}).get(command) or {}
    for name, prop in ((spec.get("inputSchema") or {}).get("properties") or {}).items():
        if name not in merged and isinstance(prop, Mapping) and "default" in prop:
            merged[name] = prop["default"]
            defaulted.add(name)
    return merged, defaulted


def _first(params: Mapping[str, Any], names) -> Optional[Tuple[str, float]]:
    for name in names:
        value = _number(params.get(name))
        if value is not None:
            return name, value
    return None


def estimate_call(
    package: str, command: str, params: Optional[Mapping[str, Any]] = None
) -> CallEstimate:
    merged, defaulted = _with_defaults(package, command, params or {})

    def detail(*names: str) -> str:
        used = [f"params.{n}" for n in names]
        if any(n in defaulted for n in names):
            used.append("tool defaults")
        return ", ".join(used)

    count = _first(merged, SERIES_COUNTS)
    interval = _first(merged, SERIES_INTERVALS)
    if count and interval and count[1] >= 1:
        seconds = max(0.0, count[1] - 1) * interval[1]
        return CallEstimate(seconds, "params", detail(count[0], interval[0]))
    length = _first(merged, RUN_LENGTHS)
    if length:
        settle = _first(merged, SETTLE)
        seconds = length[1] + (settle[1] if settle else 0.0)
        names = (length[0],) + ((settle[0],) if settle else ())
        return CallEstimate(seconds, "params", detail(*names))
    timeout = _first(merged, TIMEOUTS)
    if timeout and timeout[1] > 0:
        return CallEstimate(
            timeout[1], "params", detail(timeout[0]) + " (an upper bound)"
        )
    return CallEstimate(float(DEFAULT_SECONDS), "default")


__all__ = ["CallEstimate", "DEFAULT_SECONDS", "estimate_call"]
