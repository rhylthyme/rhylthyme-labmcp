"""Run LabMCP tool calls for a live program, off the runner's thread.

One server per workcell tool, started by ``prepare`` and stopped by
``shutdown``. ``submit`` calls one step's tool on a worker thread and calls
``on_reply(key, result)`` from that thread; the first outcome (reply or
timeout) wins. Results never carry a tool's address: it is replaced by
``<tool address>`` in every message.
"""

import shutil
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .client import (
    NOT_SENT,
    SUCCESS,
    TIMEOUT,
    TOOL_ERROR,
    HttpServerClient,
    ServerClient,
    ServerError,
    StdioServerClient,
    ToolResult,
    find_uvx,
)
from .compat import (
    POLL_SECONDS,
    RUN_DONE,
    RUN_ENDED,
    RUN_STUCK_PREFIX,
    compat_command,
)
from .workcell import LabMCPTool, server_args

OnResult = Callable[[str, ToolResult], None]
ClientFactory = Callable[[LabMCPTool, bool], ServerClient]

#: Extra seconds a call may run past timeoutSeconds before its worker gives up.
DEADLINE_GRACE = 5.0

#: Seconds to wait for ``get_connection_info`` and the tool list.
INFO_TIMEOUT = 60.0

#: Safety tools that are not stops.
NOT_STOPS = frozenset({"reconnect"})

#: Safety tools whose names say they stop; sent before the others.
STOP_WORDS = ("stop", "terminate", "abort", "cancel", "halt")


def launch_client(tool: LabMCPTool, simulate: bool) -> ServerClient:
    """
    Start ``tool``'s server with uvx (stdio), simulated unless live, at the
    version the catalogue was exported from unless the workcell pins one.
    """
    if tool.url:
        # Already running elsewhere: connect, launch nothing
        return HttpServerClient(tool.url, name=tool.name)
    from .checks import pinned_version

    args = server_args(tool, simulate=simulate, version=pinned_version(tool.package))
    return StdioServerClient(find_uvx(), args, name=tool.name)


@dataclass(frozen=True)
class ServerCheck:
    """One tool's server before a run: started, identified, in the right mode."""

    tool: str
    status: str  # SIMULATED | READY | WRONG_MODE | NOT_CONNECTED | OFFLINE
    ready: bool
    detail: str = ""
    info: Mapping[str, Any] = field(default_factory=dict)
    tools: Tuple[Mapping[str, Any], ...] = ()


def _looser_limits(wanted: Mapping[str, float], active: Mapping[str, Any]) -> List[str]:
    """Workcell limits a running server does not enforce at least as tightly."""
    problems = []
    for name, value in wanted.items():
        limit = active.get(name)
        if not isinstance(limit, Mapping):
            problems.append(f"{name} is not one of its limits")
            continue
        current = limit.get("value", limit.get("default"))
        kind = limit.get("kind", "max")
        ok = isinstance(current, (int, float)) and (
            current <= value if kind == "max" else current >= value
        )
        if not ok:
            problems.append(f"{name} is {current}, the workcell needs {value:g}")
    return problems


def scrubber(tools: Iterable[LabMCPTool]) -> Callable[[str], str]:
    pairs = sorted(
        (
            (value, f"<{tool.name} address>")
            for tool in tools
            for value in tool.private_values()
        ),
        key=lambda p: len(p[0]),
        reverse=True,
    )

    def scrub(text: str) -> str:
        for value, name in pairs:
            text = text.replace(value, name)
        return text

    return scrub


class LabMCPExecutor:
    def __init__(
        self,
        tools: Mapping[str, LabMCPTool],
        client_factory: ClientFactory = launch_client,
        *,
        simulated: bool = True,
        max_workers: int = 16,
    ):
        self.tools = dict(tools)
        self.simulated = simulated
        self._client_factory = client_factory
        self._clients: Dict[str, ServerClient] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="rhylthyme-labmcp"
        )
        self._in_flight: Dict[str, Tuple[object, Future, Optional[threading.Timer]]] = (
            {}
        )
        self._lock = threading.Lock()
        #: Each started server's tools, {name, kind, required, ...}
        self.server_tools: Dict[str, List[Mapping[str, Any]]] = {}
        #: Tools whose protocol run is being set up (uploaded, not started
        #: yet), and whether a pause arrived meanwhile
        self._starting: Dict[str, bool] = {}
        self.scrub = scrubber(self.tools.values())

    def tool(self, name: str) -> LabMCPTool:
        return self.tools[name]

    def started(self, name: str) -> bool:
        with self._lock:
            return name in self._clients

    # -- Before the run ----------------------------------------------------

    def _check(self, name: str) -> ServerCheck:
        """Start ``name``'s server (once; later checks reuse it) and identify it."""
        tool = self.tools[name]
        with self._lock:
            client = self._clients.get(name)
        if client is None:
            try:
                client = self._client_factory(tool, self.simulated)
                client.start(tool.start_timeout)
            except ServerError as e:
                return ServerCheck(name, "OFFLINE", False, self.scrub(str(e)))
            with self._lock:
                self._clients[name] = client
        result = client.call("get_connection_info", {}, INFO_TIMEOUT)
        if not result.ok:
            return ServerCheck(name, "OFFLINE", False, self.scrub(result.error_message))
        info = dict(result.data)
        try:
            tools = client.list_tools(INFO_TIMEOUT)
        except ServerError as e:
            return ServerCheck(name, "OFFLINE", False, self.scrub(str(e)), info)
        self.server_tools[name] = tools
        if not info.get("connected") or info.get("error"):
            detail = self.scrub(str(info.get("error") or "not connected"))
            return ServerCheck(name, "NOT_CONNECTED", False, detail, info)
        if bool(info.get("simulated")) != self.simulated:
            want = "simulated" if self.simulated else "live"
            return ServerCheck(
                name, "WRONG_MODE", False, f"the server is not {want}", info
            )
        if tool.url:
            # A running server keeps its own limits: it must enforce the
            # workcell's, or the run would rely on limits nobody set
            looser = _looser_limits(tool.limits, info.get("safety_limits") or {})
            if looser:
                return ServerCheck(
                    name,
                    "WRONG_LIMITS",
                    False,
                    "the server's limits are not the workcell's: " + "; ".join(looser),
                    info,
                    tuple(tools),
                )
        status = "SIMULATED" if self.simulated else "READY"
        return ServerCheck(name, status, True, "", info, tuple(tools))

    def tool_kind(self, name: str, command: str) -> Optional[str]:
        """The LabMCP kind of ``command`` on ``name``'s server, once started."""
        compat = compat_command(self.tools[name].package, command)
        if compat:
            return compat.get("kind")
        for t in self.server_tools.get(name, []):
            if t["name"] == command:
                return t.get("kind")
        return None

    def _no_arg_tools(self, name: str, kind: str) -> List[str]:
        return [
            t["name"]
            for t in self.server_tools.get(name, [])
            if t.get("kind") == kind and not t.get("required")
        ]

    def default_stops(self, name: str) -> List[str]:
        """
        The tools that put ``name``'s instrument in a safe state: its server's
        safety-kind tools that take no required arguments, the ones that stop
        (stop, terminate, abort, cancel) first, as later ones (switching
        modules off) may need it stopped. ``pause_*`` holds rather than stops
        and ``reconnect`` is not a stop. Empty before ``prepare``.
        """
        tools = [
            t
            for t in self._no_arg_tools(name, "safety")
            if t not in NOT_STOPS and not t.startswith("pause")
        ]
        stops_first = [t for t in tools if t.startswith(STOP_WORDS)]
        return stops_first + [t for t in tools if t not in stops_first]

    def pause_tools(self, name: str) -> List[str]:
        """Safety tools that hold ``name``'s work so it can resume (pause_run)."""
        return [t for t in self._no_arg_tools(name, "safety") if t.startswith("pause")]

    def resume_tools(self, name: str) -> List[str]:
        """The tools that continue what ``pause_tools`` held (resume_run)."""
        if not self.pause_tools(name):
            return []
        return [
            t["name"]
            for t in self.server_tools.get(name, [])
            if t["name"].startswith("resume") and not t.get("required")
        ]

    def prepare(self, tools: Iterable[str]) -> List[ServerCheck]:
        """Start each tool's server (in parallel) and ask what it is connected to."""
        names = sorted(set(tools))
        if not names:
            return []
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            return list(pool.map(self._check, names))

    # -- During the run ----------------------------------------------------

    def submit(
        self, key: str, instrument: Mapping[str, Any], on_result: OnResult
    ) -> None:
        """Call ``instrument`` ({tool, command, params, timeoutSeconds})."""
        name = instrument.get("tool")
        with self._lock:
            client = self._clients.get(name)
        if client is None:
            on_result(key, ToolResult(NOT_SENT, f"{name}: its server is not running"))
            return
        tool = self.tools[name]
        timeout = instrument.get("timeoutSeconds") or tool.timeout_seconds
        token = object()

        def deliver(result: ToolResult) -> None:
            with self._lock:
                entry = self._in_flight.get(key)
                if entry is None or entry[0] is not token:
                    return  # timed out, retried or shut down meanwhile
                del self._in_flight[key]
            if entry[2] is not None:
                entry[2].cancel()
            if result.error_message:
                result = ToolResult(
                    result.code, self.scrub(result.error_message), result.data
                )
            on_result(key, result)

        command = str(instrument["command"])
        params = instrument.get("params") or {}
        compat = compat_command(tool.package, command)
        if compat.get("calls"):
            command = compat["calls"]  # a galago name for a server tool
        elif compat:
            # Set up before any later call (a pause) can be handled
            with self._lock:
                self._starting[str(name)] = False

        def run() -> None:
            try:
                early = self._early_hold(str(name), command)
                if early is not None:
                    result = early
                elif compat and not compat.get("calls"):
                    result = self._run_compat(
                        client, command, params, token, key, str(name)
                    )
                else:
                    result = client.call(
                        command, params, timeout + DEADLINE_GRACE if timeout else None
                    )
            except Exception as e:  # a broken client must still end the step
                result = ToolResult(NOT_SENT, f"{type(e).__name__}: {e}")
            deliver(result)

        timer = None
        if timeout:
            timer = threading.Timer(
                timeout, deliver, [ToolResult(TIMEOUT, f"no reply after {timeout:g} s")]
            )
            timer.daemon = True
        with self._lock:
            self._in_flight[key] = (token, self._pool.submit(run), timer)
        if timer is not None:
            timer.start()

    def _still_wanted(self, key: str, token: object) -> bool:
        with self._lock:
            entry = self._in_flight.get(key)
            return entry is not None and entry[0] is token

    def _early_hold(self, name: str, command: str) -> Optional[ToolResult]:
        """
        A pause (or resume) for a tool whose protocol run is still being set
        up: there is no run to pause yet, so remember it; the run is paused
        the moment it starts.
        """
        with self._lock:
            if name not in self._starting:
                return None
            if command.startswith("pause"):
                self._starting[name] = True
                note = "no run yet: it will be paused as soon as it starts"
            elif command.startswith("resume"):
                self._starting[name] = False
                note = "no run yet: the pause is cancelled"
            else:
                return None
        return ToolResult(SUCCESS, data={"deferred": note})

    def _run_compat(
        self,
        client: ServerClient,
        command: str,
        params: Mapping[str, Any],
        token: object,
        key: str,
        name: str,
    ) -> ToolResult:
        """A galago command made of several server calls (see compat)."""
        if command != "run_program":
            return ToolResult(NOT_SENT, f"no way to run {command!r} here")
        script = str(params.get("script_content") or "")
        try:
            return self._run_protocol(client, script, token, key, name)
        finally:
            with self._lock:
                self._starting.pop(name, None)

    def _run_protocol(
        self, client: ServerClient, script: str, token: object, key: str, name: str
    ) -> ToolResult:
        """Upload a protocol, start a run of it, and poll until it ends."""
        workdir = Path(tempfile.mkdtemp(prefix="rhylthyme-protocol-"))
        path = workdir / "protocol.py"
        path.write_text(script)
        try:
            uploaded = client.call(
                "upload_protocol", {"path": str(path)}, INFO_TIMEOUT * 5
            )
            if not uploaded.ok:
                return uploaded
            if not uploaded.data.get("ready_to_run"):
                problems = uploaded.data.get("analysis_errors") or uploaded.data.get(
                    "problems"
                )
                return ToolResult(
                    TOOL_ERROR,
                    f"the protocol is not ready to run: {problems}",
                    uploaded.data,
                )
            protocol_id = uploaded.data.get("id") or uploaded.data.get("protocol_id")
            started = client.call(
                "start_run",
                {"protocol_id": protocol_id, "deck_confirmed": True},
                INFO_TIMEOUT,
            )
            if not started.ok:
                return started
            run_id = started.data.get("id")
            with self._lock:
                pause_now = self._starting.pop(name, False)
            if pause_now:  # paused while the run was being set up
                client.call("pause_run", {"run_id": run_id}, INFO_TIMEOUT)
            status = started
            while self._still_wanted(key, token):
                state = str(status.data.get("status") or "")
                if state in RUN_ENDED or state.startswith(RUN_STUCK_PREFIX):
                    break
                time.sleep(POLL_SECONDS)
                status = client.call("get_run_status", {"run_id": run_id}, INFO_TIMEOUT)
                if not status.ok:
                    return status
            state = str(status.data.get("status") or "")
            summary = {
                k: status.data.get(k)
                for k in (
                    "id",
                    "protocol_id",
                    "status",
                    "commands_executed",
                    "commands_expected",
                    "started_at",
                    "completed_at",
                    "errors",
                )
                if k in status.data
            }
            if state == RUN_DONE:
                return ToolResult(SUCCESS, data=summary)
            errors = status.data.get("errors") or ""
            return ToolResult(
                TOOL_ERROR,
                f"the protocol run ended {state or 'without a status'}"
                + (f": {errors}" if errors else ""),
                summary,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def in_flight(self) -> List[str]:
        with self._lock:
            return sorted(self._in_flight)

    def shutdown(self) -> List[str]:
        """Stop every server; return the keys whose calls never replied."""
        with self._lock:
            pending = sorted(self._in_flight)
            for _, _, timer in self._in_flight.values():
                if timer is not None:
                    timer.cancel()
            self._in_flight.clear()
            clients = list(self._clients.values())
            self._clients.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
        for client in clients:
            client.close()
        return pending


__all__ = [
    "LabMCPExecutor",
    "SUCCESS",
    "ServerCheck",
    "launch_client",
    "scrubber",
]
