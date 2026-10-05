"""Workcell tools served by LabMCP: which server, and where the instrument is.

A LabMCP tool in a (local, never shared) workcell file::

    {"name": "balance", "driver": "labmcp",
     "server": "mettler-toledo",          # or "labmcp-mettler-toledo"
     "address": "/dev/ttyUSB0",           # a LabMCP address URI
     "limits": {"max_series_duration_s": 300},   # passed as --limit
     "options": {"baudrate": "9600"},             # passed as --option
     "timeoutSeconds": 30}

``url`` points at a server already running over streamable HTTP
(``labmcp-<package> --transport http``, on another machine or in a
container): nothing is launched, and ``server`` (optional with ``url``) only
names the package, so steps are checked against its catalogue entry. Such a
server keeps the limits it was started with; the workcell's ``limits`` are
then a requirement the pre-flight checks rather than settings it passes.
Programs refer to tools only by ``name``.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

PACKAGE_PREFIX = "labmcp-"

#: Seconds a launched server may take to answer; the first ``uvx`` run of a
#: package downloads it.
DEFAULT_START_TIMEOUT = 180.0

_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+!_-]+$")


class WorkcellError(ValueError):
    """A LabMCP workcell tool that cannot be used."""


@dataclass(frozen=True)
class LabMCPTool:
    name: str
    package: str = ""
    version: str = ""
    url: str = ""
    address: str = ""
    limits: Mapping[str, float] = field(default_factory=dict)
    options: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: Optional[float] = None
    start_timeout: float = DEFAULT_START_TIMEOUT
    description: str = ""

    @property
    def kind(self) -> str:
        """The server, as shown next to the tool name."""
        if self.url:
            return f"{self.package} (http)" if self.package else "labmcp (http)"
        return self.package

    @property
    def location(self) -> str:
        """Where the instrument is reached, for local output only."""
        return self.url or self.address or "no address"

    def private_values(self) -> List[str]:
        """
        Every local value that must never leave the machine, in each form it
        may appear in an error message: the address and its parts (serial
        device, TCP host and port, VISA resource parts), the server URL and
        its host, and every option value.
        """
        values = address_variants(self.address) + address_variants(self.url)
        values += [v for v in self.options.values() if isinstance(v, str) and v]
        seen: Dict[str, None] = {}
        for v in values:
            seen.setdefault(v, None)
        return list(seen)


#: VISA resource words that name no particular instrument.
_VISA_WORDS = re.compile(
    r"^(?:(?:TCPIP|GPIB|USB|ASRL|PXI|VXI|GPIB-VXI)\d*|INSTR|SOCKET|INTFC|BACKPLANE"
    r"|inst\d+|hislip\d+)$",
    re.IGNORECASE,
)


def address_variants(address: str) -> List[str]:
    """
    A LabMCP address (or server URL) and the parts of it that identify the
    instrument: ``tcp://10.1.2.3:4001`` -> the address, ``10.1.2.3:4001``,
    ``('10.1.2.3', 4001)``, ``10.1.2.3``; ``serial:///dev/ttyUSB0?baudrate=9600``
    -> the address, ``/dev/ttyUSB0``, ``ttyUSB0``; ``TCPIP0::10.1.2.9::INSTR`` ->
    the resource and ``10.1.2.9``.
    """
    from urllib.parse import unquote, urlsplit

    address = (address or "").strip()
    if not address:
        return []
    out = [address]
    bare = address.split("?", 1)[0]
    out.append(bare)
    scheme, sep, rest = bare.partition("://")
    target = unquote(rest) if sep else bare
    if sep and scheme.lower() in ("tcp", "http", "https"):
        parts = urlsplit(f"//{rest}")
        if parts.hostname:
            if parts.port:
                out += [
                    f"{parts.hostname}:{parts.port}",
                    f"('{parts.hostname}', {parts.port})",
                ]
            out.append(parts.hostname)
        return [v for v in dict.fromkeys(out) if v]
    out.append(target)
    if "::" in target:  # VISA: TCPIP0::10.1.2.9::inst0::INSTR
        out += [
            p for p in target.split("::") if len(p) >= 4 and not _VISA_WORDS.match(p)
        ]
    elif "/" in target:  # serial device path
        name = target.rstrip("/").rsplit("/", 1)[-1]
        if len(name) >= 5:
            out.append(name)
    return [v for v in dict.fromkeys(out) if v]


def _number(where: str, key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WorkcellError(f"{where} {key} must be a positive number")
    return float(value)


def _string_map(where: str, key: str, raw: Any) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WorkcellError(f"{where} {key} must be an object")
    out = {}
    for k, v in raw.items():
        if isinstance(v, (dict, list)) or v is None:
            raise WorkcellError(f"{where} {key}.{k} must be a string or number")
        out[str(k)] = v if isinstance(v, str) else _plain(v)
    return out


def _plain(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _package(where: str, raw: Any) -> Tuple[str, str]:
    """``mettler-toledo``, ``labmcp-mettler-toledo`` or ``…==0.1.3``."""
    text = str(raw).strip().lower()
    version = ""
    if "==" in text:
        text, version = (part.strip() for part in text.split("==", 1))
        if not _VERSION_RE.match(version):
            raise WorkcellError(f"{where} server version {version!r} is not valid")
    if not text.startswith(PACKAGE_PREFIX):
        text = PACKAGE_PREFIX + text
    if not _PACKAGE_RE.match(text):
        raise WorkcellError(f"{where} server {raw!r} is not a package name")
    return text, version


def parse_tool(index: int, raw: Any) -> LabMCPTool:
    where = f"tools[{index}]"
    if not isinstance(raw, dict):
        raise WorkcellError(f"{where} must be an object")
    name = raw.get("name")
    if name in (None, ""):
        raise WorkcellError(f"{where} is missing name")
    where = f"{where} ({name!r})"
    server, url = raw.get("server"), raw.get("url")
    if not server and not url:
        raise WorkcellError(f"{where} needs a server (to launch) or a url")
    package = version = ""
    if server:
        package, version = _package(where, server)
        if raw.get("version"):
            version = str(raw["version"])
    if url and not str(url).startswith(("http://", "https://")):
        raise WorkcellError(f"{where} url must start with http:// or https://")
    if url and raw.get("options"):
        raise WorkcellError(
            f"{where} options cannot be passed to a server reached by url; "
            "set them where it is started"
        )
    limits_raw = raw.get("limits") or {}
    if not isinstance(limits_raw, dict):
        raise WorkcellError(f"{where} limits must be an object")
    limits = {str(k): _number(where, f"limits.{k}", v) for k, v in limits_raw.items()}
    timeout = raw.get("timeoutSeconds")
    start_timeout = raw.get("startTimeoutSeconds")
    return LabMCPTool(
        name=str(name),
        package=package,
        version=version,
        url=str(url or ""),
        address=str(raw.get("address") or ""),
        limits=limits,
        options=_string_map(where, "options", raw.get("options")),
        timeout_seconds=(
            _number(where, "timeoutSeconds", timeout) if timeout is not None else None
        ),
        start_timeout=(
            _number(where, "startTimeoutSeconds", start_timeout)
            if start_timeout is not None
            else DEFAULT_START_TIMEOUT
        ),
        description=str(raw.get("description", "")),
    )


def parse_tools(raw_tools: Sequence[Any]) -> Dict[str, LabMCPTool]:
    tools: Dict[str, LabMCPTool] = {}
    for i, raw in enumerate(raw_tools):
        tool = parse_tool(i, raw)
        if tool.name in tools:
            raise WorkcellError(f"Duplicate tool name {tool.name!r}")
        tools[tool.name] = tool
    return tools


def server_args(
    tool: LabMCPTool, *, simulate: bool, version: Optional[str] = None
) -> List[str]:
    """
    The ``uvx`` arguments that start ``tool``'s server over stdio: the
    workcell's version pin, else ``version`` (the catalogued one), else the
    newest release.
    """
    pin = tool.version or version
    spec = f"{tool.package}=={pin}" if pin else tool.package
    args = [spec]
    if tool.address:
        args += ["--address", tool.address]
    for name, value in tool.limits.items():
        args += ["--limit", f"{name}={_plain(value)}"]
    for name, value in tool.options.items():
        args += ["--option", f"{name}={value}"]
    if simulate:
        args.append("--simulate")
    return args
