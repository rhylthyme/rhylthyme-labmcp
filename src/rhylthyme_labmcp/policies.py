"""The command policies protocol-bridge servers enforce, checked before a run.

LabMCP's generic servers let a step send arbitrary device commands, so the
lab narrows them with ``options`` in the workcell, which the server enforces
at run time (refusing, with nothing sent). ``rhylthyme validate --workcell``
applies the same policies to every step, so a refusal shows up before the
run instead of halfway through it:

- ``labmcp-scpi``: ``write_denylist`` (alias ``denylist``),
  ``write_allowlist`` and ``query_denylist`` regexes, and the built-in list of
  queries with side effects, matched exactly as the server does (its policy
  module, vendored in ``_vendor/scpi_policy.py``);
- ``labmcp-sila2``: ``command_allowlist`` (``Feature.Command``,
  ``Feature.*``) on ``call_command``;
- ``labmcp-modbus``: ``raw_writes=false`` (no ``write_register``,
  ``write_registers``, ``write_coil``: named points only).
"""

import re
from typing import Any, List, Mapping, Optional, Tuple

from .workcell import LabMCPTool

POLICY_REFUSED = "instrument_refused_by_policy"
POLICY_INVALID = "workcell_invalid_policy"

_FALSE = {"0", "false", "no", "off"}
_MODBUS_RAW_WRITES = ("write_register", "write_registers", "write_coil")


def _scpi_policy(options: Mapping[str, str]) -> Any:
    from ._vendor.scpi_policy import CommandPolicy

    return CommandPolicy.from_options(dict(options))


def _sila_allowlist(spec: Optional[str]) -> Optional[List[Tuple[str, str]]]:
    """As labmcp_sila2.driver.parse_allowlist: lower-case (feature, command)."""
    if spec is None or not spec.strip():
        return None
    out = []
    for item in re.split(r"[,;\s]+", spec.strip()):
        if not item:
            continue
        feat, sep, cmd = item.rpartition(".")
        if not sep or not feat or not cmd:
            raise ValueError(
                "command_allowlist entries must look like Feature.Command or "
                f"Feature.*, got {item!r}"
            )
        out.append((feat.lower(), cmd.lower()))
    return out


def workcell_problems(tool: LabMCPTool) -> List[str]:
    """Policy options a server would reject at start-up."""
    try:
        if tool.package == "labmcp-scpi":
            _scpi_policy(tool.options)
        elif tool.package == "labmcp-sila2":
            _sila_allowlist(tool.options.get("command_allowlist"))
    except ValueError as e:
        return [str(e)]
    return []


def _scpi_texts(command: str, params: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """(check, text) for each piece of SCPI a tool call would send."""
    if command in ("scpi_query", "query_binary_block"):
        return [("query", str(params.get("command") or ""))]
    if command == "scpi_write":
        return [("command", str(params.get("command") or ""))]
    if command == "scpi_batch":
        return [("command", str(s)) for s in params.get("steps") or []]
    if command == "reset_instrument":
        return [("command", "*RST")]
    return []


def call_problems(
    tool: LabMCPTool, command: str, params: Mapping[str, Any]
) -> List[str]:
    """What the server's policy would refuse in this call (empty: nothing)."""
    if not isinstance(params, Mapping):
        return []
    try:
        if tool.package == "labmcp-scpi":
            from ._vendor.scpi_policy import CommandRefused

            policy = _scpi_policy(tool.options)
            for check, text in _scpi_texts(command, params):
                try:
                    if check == "query":
                        policy.check_query(text)
                    else:
                        policy.check_command(text)
                except CommandRefused as e:
                    return [str(e)]
        elif tool.package == "labmcp-sila2" and command == "call_command":
            allowlist = _sila_allowlist(tool.options.get("command_allowlist"))
            feature = str(params.get("feature") or "")
            name = str(params.get("command") or "")
            if allowlist is not None and not any(
                f == feature.lower() and c in {name.lower(), "*"} for f, c in allowlist
            ):
                allowed = ", ".join(f"{f}.{c}" for f, c in allowlist)
                return [
                    f"Refused: {feature}.{name} is not in the command allow-list "
                    f"({allowed}). Nothing was sent."
                ]
        elif tool.package == "labmcp-modbus" and command in _MODBUS_RAW_WRITES:
            if str(tool.options.get("raw_writes", "true")).strip().lower() in _FALSE:
                return [
                    f"Refused: {command} is off on this tool (raw_writes=false): "
                    "write named points from the register map with write_point"
                ]
    except ValueError:
        return []  # a bad option: reported once, by workcell_problems
    return []


__all__ = [
    "POLICY_INVALID",
    "POLICY_REFUSED",
    "call_problems",
    "workcell_problems",
]
