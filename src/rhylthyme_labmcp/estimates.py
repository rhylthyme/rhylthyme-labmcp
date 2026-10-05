"""Planning durations for LabMCP calls whose reply ends a step.

A step with a ``command`` or ``until`` and no ``duration`` ends when its call
replies. Planners still need a number, estimated from the call's params,
with the tool's own defaults (from the catalogue) for params left out, by the
first rule that applies:

1. a dose: a volume moved at a rate takes volume / rate (``volume_ml`` at
   ``rate_ml_min``, ``volume_ul`` at ``flow_ul_s``: syringe pumps);
2. a series: ``count`` (or ``timepoints``) readings ``interval_s`` apart take
   ``(count - 1) * interval_s``, as LabMCP's own series-length check counts;
3. a run length: ``duration_s`` or ``run_time_s``, plus ``equilibration_s``;
4. a wait bounded by ``timeout_s`` or ``wait_s`` (wait_for_temperature, BLE
   reads, a SiLA 2 observable command): the bound, an upper bound;
5. otherwise ``DEFAULT_SECONDS`` (tare, set a speed, read a value).

Rules 1-4 are ``source: "params"`` estimates (detail says which params, and
"defaults" when the tool's defaults supplied them); rule 5 is ``"default"``.
"""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .checks import server_entry

#: Planning seconds for a call with nothing time-like in its params.
DEFAULT_SECONDS = 10

#: (volume, rate, seconds per rate unit of time): volume / rate * factor
DOSES = (
    ("volume_ml", "rate_ml_min", 60.0),
    ("volume_ul", "flow_ul_s", 1.0),
)
SERIES_COUNTS = ("count", "timepoints")
SERIES_INTERVALS = ("interval_s",)
RUN_LENGTHS = ("duration_s", "run_time_s")
SETTLE = ("equilibration_s",)
TIMEOUTS = ("timeout_s", "wait_s")


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

    for volume_key, rate_key, factor in DOSES:
        volume = _number(merged.get(volume_key))
        rate = _number(merged.get(rate_key))
        if volume is not None and rate:
            return CallEstimate(
                volume / rate * factor, "params", detail(volume_key, rate_key)
            )
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
    from .compat import compat_command

    typical = compat_command(package, command).get("estimateSeconds")
    if typical:
        return CallEstimate(float(typical), "default")
    return CallEstimate(float(DEFAULT_SECONDS), "default")


def dose_seconds(
    package: str, command: str, params: Mapping[str, Any]
) -> Optional[float]:
    """How long a dose call pumps (volume / rate), or None if it is not one."""
    found = estimate_call(package, command, params)
    first = found.detail.split(",")[0]
    if found.source == "params" and first in {f"params.{v}" for v, _, _ in DOSES}:
        return found.seconds
    return None


__all__ = ["CallEstimate", "DEFAULT_SECONDS", "dose_seconds", "estimate_call"]
