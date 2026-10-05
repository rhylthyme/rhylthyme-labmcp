"""Run LabMCP tool calls for a live program, off the runner's thread.

One server per workcell tool, started by ``prepare`` and stopped by
``shutdown``. ``submit`` calls one step's tool on a worker thread and calls
``on_reply(key, result)`` from that thread; the first outcome (reply or
timeout) wins. Results never carry a tool's address: it is replaced by
``<tool address>`` in every message.
"""

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .client import (
    NOT_SENT,
    SUCCESS,
    TIMEOUT,
    ServerClient,
    ServerError,
    StdioServerClient,
    ToolResult,
    find_uvx,
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


def launch_client(tool: LabMCPTool, simulate: bool) -> ServerClient:
    """
    Start ``tool``'s server with uvx (stdio), simulated unless live, at the
    version the catalogue was exported from unless the workcell pins one.
    """
    if tool.url:
        raise ServerError(
            f"{tool.name}: servers reached by url are not supported yet; "
            "use server and address"
        )
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
        status = "SIMULATED" if self.simulated else "READY"
        return ServerCheck(name, status, True, "", info, tuple(tools))

    def tool_kind(self, name: str, command: str) -> Optional[str]:
        """The LabMCP kind of ``command`` on ``name``'s server, once started."""
        for t in self.server_tools.get(name, []):
            if t["name"] == command:
                return t.get("kind")
        return None

    def default_stops(self, name: str) -> List[str]:
        """
        The tools that put ``name``'s instrument in a safe state: its server's
        safety-kind tools that take no required arguments (stop, abort,
        outputs off), in the server's order. Empty before ``prepare``.
        """
        return [
            t["name"]
            for t in self.server_tools.get(name, [])
            if t.get("kind") == "safety"
            and not t.get("required")
            and t["name"] not in NOT_STOPS
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

        def run() -> None:
            try:
                result = client.call(
                    str(instrument["command"]),
                    instrument.get("params") or {},
                    timeout + DEADLINE_GRACE if timeout else None,
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
