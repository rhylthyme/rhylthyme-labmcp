"""Talking to one LabMCP server: launched over stdio, or a scripted fake in tests.

The MCP client is asynchronous; the runner is not. Each ``StdioServerClient``
owns an event loop on its own thread, holds the server's session open there
for the whole run, and offers blocking calls to any thread.
"""

import asyncio
import concurrent.futures
import contextlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

#: Reply codes. SUCCESS and TOOL_ERROR come from the server; the others mean
#: the call never got an answer.
SUCCESS = "SUCCESS"
TOOL_ERROR = "TOOL_ERROR"
NOT_SENT = "NOT_SENT"
TIMEOUT = "TIMEOUT"
UNREACHABLE = "UNREACHABLE"


class ServerError(RuntimeError):
    """A LabMCP server could not be started or reached."""


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one MCP tool call."""

    code: str
    error_message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == SUCCESS


#: LabMCP tool kinds, from each tool's FastMCP tags.
KINDS = ("safety", "hazard", "control", "read")


class ServerClient(Protocol):
    def start(self, timeout: float) -> None: ...

    def list_tools(self, timeout: Optional[float] = None) -> List[Dict[str, Any]]: ...

    def call(
        self, tool: str, arguments: Mapping[str, Any], timeout: Optional[float] = None
    ) -> ToolResult: ...

    def close(self) -> None: ...


def _field(obj: Any, snake: str, camel: str, default: Any = None) -> Any:
    """mcp 2.x names fields in snake_case, 1.x in camelCase."""
    value = getattr(obj, snake, None)
    return getattr(obj, camel, default) if value is None else value


def result_from_mcp(result: Any) -> ToolResult:
    texts = [
        c.text for c in (getattr(result, "content", None) or []) if hasattr(c, "text")
    ]
    if _field(result, "is_error", "isError", False):
        return ToolResult(TOOL_ERROR, "\n".join(texts).strip() or "tool error")
    data = _field(result, "structured_content", "structuredContent")
    if data is None and texts:
        try:
            data = json.loads(texts[0]) if len(texts) == 1 else None
        except ValueError:
            data = None
        if data is None:
            data = {"text": "\n".join(texts)}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        data = {"result": data}
    return ToolResult(SUCCESS, data=data)


def tool_info(tool: Any) -> Dict[str, Any]:
    """{name, kind, required, inputSchema, description} of one MCP tool.

    LabMCP tags every tool read, control, hazard (control + hazard) or
    safety; servers without tags fall back to the MCP annotations.
    """
    meta = _field(tool, "meta", "_meta") or {}
    fastmcp = (meta.get("fastmcp") or {}) if isinstance(meta, dict) else {}
    tags = set(fastmcp.get("tags") or [])
    kind = next((k for k in KINDS if k in tags), None)
    if kind is None:
        notes = getattr(tool, "annotations", None)
        if _field(notes, "read_only_hint", "readOnlyHint", False):
            kind = "read"
        elif _field(notes, "destructive_hint", "destructiveHint", False):
            kind = "hazard"
        else:
            kind = "control"
    schema = _field(tool, "input_schema", "inputSchema") or {}
    return {
        "name": tool.name,
        "kind": kind,
        "required": list(schema.get("required") or []),
        "inputSchema": schema,
        "description": getattr(tool, "description", "") or "",
    }


def find_uvx() -> str:
    """The ``uvx`` that launches LabMCP servers (``RHYLTHYME_UVX`` overrides)."""
    found = os.environ.get("RHYLTHYME_UVX") or shutil.which("uvx")
    if not found:
        raise ServerError(
            "LabMCP servers are launched with uvx, which was not found. Install uv: "
            "https://docs.astral.sh/uv/getting-started/installation/"
        )
    return found


def _error_text(error: BaseException) -> str:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


class StdioServerClient:
    """One LabMCP server process, spoken to over stdio for one run.

    The server's own output (banner, logs) goes to ``log_path``, never to the
    terminal the runner draws on.
    """

    def __init__(
        self,
        command: str,
        args: List[str],
        *,
        name: str = "labmcp",
        env: Optional[Mapping[str, str]] = None,
        log_dir: Optional[Path] = None,
    ):
        self.command = command
        self.args = list(args)
        self.name = name
        self.env = dict(env) if env is not None else None
        fd, path = tempfile.mkstemp(
            prefix=f"labmcp-{name}-", suffix=".log", dir=log_dir
        )
        os.close(fd)
        self.log_path: Optional[Path] = Path(path)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[concurrent.futures.Future] = None
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self._session: Any = None
        self._stop: Optional[asyncio.Event] = None

    def start(self, timeout: float) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"labmcp-{self.name}", daemon=True
        )
        self._thread.start()
        self._task = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        try:
            self._ready.result(timeout)
        except concurrent.futures.TimeoutError:
            self.close()
            raise ServerError(
                f"the server did not answer within {timeout:g} s" + self._log_tail()
            ) from None
        except Exception as e:
            self.close()
            # _serve already formatted it ("ConnectTimeout: ...")
            raise ServerError(self._failed(str(e)) + self._log_tail()) from None

    def _failed(self, reason: str) -> str:
        return reason

    @contextlib.asynccontextmanager
    async def _streams(self) -> AsyncIterator[Tuple[Any, Any]]:
        """The server's read and write streams: a launched process's stdio."""
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.command, args=self.args, env=self.env
        )
        with open(self.log_path, "a") as errlog:
            async with stdio_client(params, errlog=errlog) as streams:
                yield streams[0], streams[1]

    async def _serve(self) -> None:
        from mcp import ClientSession

        self._stop = asyncio.Event()
        try:
            async with self._streams() as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set_result(True)
                    await self._stop.wait()
        except BaseException as e:  # noqa: BLE001 - reported through _ready
            if not self._ready.done():
                self._ready.set_exception(Exception(_error_text(e)))
        finally:
            self._session = None

    def _log_tail(self, lines: int = 5) -> str:
        if self.log_path is None:
            return ""
        try:
            tail = self.log_path.read_text(errors="replace").strip().splitlines()
        except OSError:
            return ""
        tail = [line.rstrip() for line in tail if line.strip()][-lines:]
        return ("\n    " + "\n    ".join(tail)) if tail else ""

    def list_tools(self, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """Every tool the server offers, with its LabMCP kind."""
        session, loop = self._session, self._loop
        if session is None or loop is None:
            raise ServerError("the server is not running")

        async def collect() -> List[Any]:
            tools: List[Any] = []
            cursor = None
            while True:
                page = await (
                    session.list_tools(cursor=cursor)
                    if cursor
                    else session.list_tools()
                )
                tools += list(page.tools)
                cursor = _field(page, "next_cursor", "nextCursor")
                if not cursor:
                    return tools

        try:
            found = asyncio.run_coroutine_threadsafe(collect(), loop).result(timeout)
        except concurrent.futures.TimeoutError:
            raise ServerError("the server did not list its tools in time") from None
        except Exception as e:  # noqa: BLE001
            raise ServerError(_error_text(e)) from None
        return [tool_info(t) for t in found]

    def call(
        self, tool: str, arguments: Mapping[str, Any], timeout: Optional[float] = None
    ) -> ToolResult:
        session, loop = self._session, self._loop
        if session is None or loop is None:
            return ToolResult(UNREACHABLE, "the server is not running")
        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(tool, dict(arguments)), loop
        )
        try:
            return result_from_mcp(future.result(timeout))
        except concurrent.futures.TimeoutError:
            future.cancel()
            return ToolResult(TIMEOUT, f"no reply after {timeout:g} s")
        except Exception as e:  # noqa: BLE001 - a broken pipe still ends the step
            return ToolResult(UNREACHABLE, _error_text(e))

    def close(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if self._stop is not None:
            loop.call_soon_threadsafe(self._stop.set)
        if self._task is not None:
            try:
                self._task.result(10)
            except Exception:  # noqa: BLE001 - best effort
                self._task.cancel()
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)
        if not loop.is_running():
            loop.close()
        self._loop = None


class HttpServerClient(StdioServerClient):
    """
    A LabMCP server already running elsewhere (``--transport http``), spoken
    to over MCP streamable HTTP for one run. Nothing is launched or stopped:
    ``close`` only ends this client's session. The server runs in whatever
    mode and with whatever limits it was started with; the executor checks
    both against the run before it starts.
    """

    def __init__(
        self,
        url: str,
        *,
        name: str = "labmcp",
        headers: Optional[Mapping[str, str]] = None,
    ):
        self.url = url
        self.headers = dict(headers or {})
        self.command, self.args, self.env = "", [], None
        self.name = name
        self.log_path = None
        self._loop = None
        self._thread = None
        self._task = None
        self._ready = concurrent.futures.Future()
        self._session = None
        self._stop = None

    def _failed(self, reason: str) -> str:
        return f"could not reach the server at {self.url} ({reason})"

    @contextlib.asynccontextmanager
    async def _streams(self) -> AsyncIterator[Tuple[Any, Any]]:
        try:  # mcp 2.x
            from mcp.client.streamable_http import streamable_http_client as connect
        except ImportError:  # mcp 1.x
            from mcp.client.streamable_http import streamablehttp_client as connect

        if self.headers:
            from mcp.client.streamable_http import create_mcp_http_client

            http = create_mcp_http_client(headers=self.headers)
            async with http, connect(self.url, http_client=http) as streams:
                yield streams[0], streams[1]
        else:
            async with connect(self.url) as streams:
                yield streams[0], streams[1]


class FakeServerClient:
    """In-memory LabMCP server for tests.

    ``replies`` maps a tool name to a ``ToolResult``, a dict of data, or a
    callable ``(arguments) -> ToolResult | dict``; unscripted tools succeed
    with no data. ``get_connection_info`` answers like a LabMCP server.
    ``tools`` are the tools it lists beside LabMCP's three built-in ones,
    ``{name, kind, required}`` each. Set ``gate`` to hold every call until
    the test releases it.
    """

    BUILT_IN = (
        {"name": "get_connection_info", "kind": "read", "required": []},
        {"name": "get_command_log", "kind": "read", "required": []},
        {"name": "reconnect", "kind": "safety", "required": []},
    )

    def __init__(
        self,
        replies: Optional[Mapping[str, Any]] = None,
        *,
        simulated: bool = True,
        connected: bool = True,
        identity: Optional[Mapping[str, Any]] = None,
        start_error: Optional[str] = None,
        gate: Optional[threading.Event] = None,
        tools: Optional[List[Mapping[str, Any]]] = None,
    ):
        self.replies: Dict[str, Any] = dict(replies or {})
        self.tools = [dict(t) for t in self.BUILT_IN] + [dict(t) for t in tools or []]
        self.simulated = simulated
        self.connected = connected
        self.identity = dict(identity or {"model": "Fake"})
        self.start_error = start_error
        self.gate = gate
        self.started = False
        self.closed = False
        self.calls: List[Dict[str, Any]] = []

    def start(self, timeout: float) -> None:
        if self.start_error:
            raise ServerError(self.start_error)
        self.started = True

    def list_tools(self, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        return [dict(t) for t in self.tools]

    def call(
        self, tool: str, arguments: Mapping[str, Any], timeout: Optional[float] = None
    ) -> ToolResult:
        self.calls.append({"tool": tool, "arguments": dict(arguments)})
        if tool == "get_connection_info" and tool not in self.replies:
            info: Dict[str, Any] = {
                "simulated": self.simulated,
                "connected": self.connected,
                "safety_limits": {},
            }
            if self.connected:
                info["instrument"] = self.identity
            else:
                info["error"] = "no instrument at the configured address"
            return ToolResult(SUCCESS, data=info)
        if self.gate is not None:
            self.gate.wait()
        reply = self.replies.get(tool, {})
        if callable(reply):
            reply = reply(dict(arguments))
        return (
            reply if isinstance(reply, ToolResult) else ToolResult(SUCCESS, data=reply)
        )

    def close(self) -> None:
        self.closed = True


ClientFactory = Callable[..., ServerClient]
