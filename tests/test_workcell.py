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
        ({"name": "b", "driver": "labmcp"}, "exactly one of server or url"),
        (tool(url="http://x"), "exactly one of server or url"),
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


def test_private_values_cover_address_url_and_options():
    [t] = parse_tools(
        [tool(address="tcp://10.1.2.3:4001", options={"k": "secret"})]
    ).values()
    assert t.private_values() == ["tcp://10.1.2.3:4001", "secret"]
