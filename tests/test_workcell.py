import pytest

from rhylthyme_labmcp import WorkcellError, parse_tools, server_args


def tool(**extra):
    return {"name": "balance", "driver": "labmcp", "server": "mettler-toledo", **extra}


def test_server_names_get_the_labmcp_prefix_and_an_optional_pin():
    [t] = parse_tools([tool()]).values()
    assert (t.package, t.version, t.kind) == ("labmcp-mettler-toledo", "", t.package)
    [t] = parse_tools([tool(server="labmcp-mettler-toledo==0.1.3")]).values()
    assert (t.package, t.version) == ("labmcp-mettler-toledo", "0.1.3")


def test_launch_arguments():
    [t] = parse_tools(
        [
            tool(
                address="/dev/ttyUSB0",
                limits={"max_series_duration_s": 300},
                options={"baudrate": 9600},
            )
        ]
    ).values()
    assert server_args(t, simulate=True) == [
        "labmcp-mettler-toledo",
        "--address",
        "/dev/ttyUSB0",
        "--limit",
        "max_series_duration_s=300",
        "--option",
        "baudrate=9600",
        "--simulate",
    ]
    assert "--simulate" not in server_args(t, simulate=False)


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"name": "b", "driver": "labmcp"}, "needs a server \\(to launch\\) or a url"),
        (
            {"name": "b", "url": "http://lab:8000/mcp", "options": {"a": "1"}},
            "options cannot be passed to a server reached by url",
        ),
        ({"name": "b", "url": "ftp://x"}, "url must start with http"),
        (tool(server="Bad Name!"), "is not a package name"),
        (tool(limits={"x": -1}), "limits.x must be a positive number"),
        (tool(limits=[1]), "limits must be an object"),
        (tool(options={"x": {}}), "options.x must be a string or number"),
        (tool(timeoutSeconds=0), "timeoutSeconds must be a positive number"),
        ({"server": "x"}, "is missing name"),
    ],
)
def test_bad_tools_are_refused(raw, message):
    with pytest.raises(WorkcellError, match=message):
        parse_tools([raw])


def test_private_values_cover_address_parts_and_options():
    [t] = parse_tools(
        [tool(address="tcp://10.1.2.3:4001", options={"k": "secret"})]
    ).values()
    assert t.private_values() == [
        "tcp://10.1.2.3:4001",
        "10.1.2.3:4001",
        "('10.1.2.3', 4001)",
        "10.1.2.3",
        "secret",
    ]


@pytest.mark.parametrize(
    "address, hidden",
    [
        ("serial:///dev/ttyUSB0?baudrate=9600", ["/dev/ttyUSB0", "ttyUSB0"]),
        ("/dev/tty.usbserial-A1B2", ["tty.usbserial-A1B2"]),
        ("COM7", ["COM7"]),
        ("TCPIP0::10.1.2.9::inst0::INSTR", ["10.1.2.9"]),
        ("USB0::0x1AB1::0x04CE::DS1ZA2023::INSTR", ["DS1ZA2023"]),
    ],
)
def test_address_parts_that_name_the_instrument_are_private(address, hidden):
    [t] = parse_tools([tool(address=address)]).values()
    for value in hidden:
        assert value in t.private_values()
    assert "INSTR" not in t.private_values() and "inst0" not in t.private_values()


def test_a_url_tool_may_name_its_server_for_checks():
    [t] = parse_tools([tool(url="http://lab-pc:8000/mcp")]).values()
    assert (t.package, t.url, t.kind) == (
        "labmcp-mettler-toledo",
        "http://lab-pc:8000/mcp",
        "labmcp-mettler-toledo (http)",
    )
    [t] = parse_tools([{"name": "b", "url": "https://lab-pc/mcp"}]).values()
    assert (t.package, t.kind) == ("", "labmcp (http)")
