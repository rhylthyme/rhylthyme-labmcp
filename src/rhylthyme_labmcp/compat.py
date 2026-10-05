"""galago-tools commands a LabMCP server can stand in for.

A program written for galago's Opentrons tool (``opentrons2``: run_program,
pause, resume, cancel) runs unchanged on LabMCP's Opentrons server when the
workcell points the tool at ``labmcp-opentrons`` instead: only the workcell
changes. Each command here has an input schema, a kind and a planning
estimate, like a catalogued tool, so validation, the live pre-flight and
planning treat it as one; the executor carries it out with the server's own
tools:

- ``run_program`` uploads ``script_content`` as a protocol
  (``upload_protocol``), starts a run of it (``start_run``) and polls
  ``get_run_status`` until the run ends. The step succeeds only if the run
  succeeds. The deck must already match the protocol: in a live run the
  typed ``live`` after the pre-flight is that confirmation.
- ``pause``, ``resume`` and ``cancel`` are ``pause_run``, ``resume_run`` and
  ``stop_run``.

The table is also written into the catalogue (``compat``) so the hosted
validator knows it too.
"""

from typing import Any, Dict, Mapping

_NO_PARAMS: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: package -> command -> {kind, description, inputSchema, estimateSeconds?,
#: calls?: the server tool it is (simple renames)}
COMPAT: Dict[str, Dict[str, Dict[str, Any]]] = {
    "labmcp-opentrons": {
        "run_program": {
            "kind": "hazard",
            "description": (
                "galago-compatible: upload script_content as a protocol, start "
                "a run of it and wait until it ends (upload_protocol, "
                "start_run, get_run_status). The deck must match the protocol."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_content": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The Opentrons Python protocol",
                    },
                    "variables": {
                        "type": "object",
                        "maxProperties": 0,
                        "description": "Not supported by LabMCP: leave empty",
                    },
                },
                "required": ["script_content"],
                "additionalProperties": False,
            },
            # A protocol takes minutes; give the step a duration to plan it
            "estimateSeconds": 600,
        },
        "pause": {
            "kind": "safety",
            "description": "galago-compatible: pause_run",
            "inputSchema": _NO_PARAMS,
            "calls": "pause_run",
        },
        "resume": {
            "kind": "hazard",
            "description": "galago-compatible: resume_run",
            "inputSchema": _NO_PARAMS,
            "calls": "resume_run",
        },
        "cancel": {
            "kind": "safety",
            "description": "galago-compatible: stop_run",
            "inputSchema": _NO_PARAMS,
            "calls": "stop_run",
        },
    },
}

#: Run statuses that end a run, and which of them is success.
RUN_DONE = "succeeded"
RUN_ENDED = {"succeeded", "failed", "stopped"}
#: A run waiting for a person to recover it cannot finish on its own.
RUN_STUCK_PREFIX = "awaiting-recovery"

#: Seconds between get_run_status polls.
POLL_SECONDS = 2.0


def compat_command(package: str, command: Any) -> Mapping[str, Any]:
    """The compat entry for ``command`` on ``package``'s server, or {}."""
    return COMPAT.get(package, {}).get(str(command), {})


__all__ = [
    "COMPAT",
    "POLL_SECONDS",
    "RUN_DONE",
    "RUN_ENDED",
    "RUN_STUCK_PREFIX",
    "compat_command",
]
