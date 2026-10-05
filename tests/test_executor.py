import threading
import time

from rhylthyme_labmcp import (
    FakeServerClient,
    LabMCPExecutor,
    ServerError,
    ToolResult,
    parse_tools,
)

TOOLS = parse_tools(
    [
        {
            "name": "balance",
            "server": "mettler-toledo",
            "address": "/dev/ttyUSB7",
        }
    ]
)


def executor(client, **kwargs):
    made = []

    def factory(tool, simulate):
        made.append((tool.name, simulate))
        return client

    ex = LabMCPExecutor(TOOLS, factory, **kwargs)
    ex.made = made
    return ex


def wait_for(results, n=1, timeout=5.0):
    deadline = time.time() + timeout
    while len(results) < n and time.time() < deadline:
        time.sleep(0.01)
    return results


def test_prepare_starts_a_simulated_server_and_identifies_it():
    client = FakeServerClient()
    ex = executor(client)
    [check] = ex.prepare(["balance"])
    assert ex.made == [("balance", True)]
    assert (check.status, check.ready) == ("SIMULATED", True)
    assert check.info["instrument"] == {"model": "Fake"}
    assert ex.shutdown() == [] and client.closed


def test_live_runs_need_a_live_server():
    ex = executor(FakeServerClient(simulated=True), simulated=False)
    [check] = ex.prepare(["balance"])
    assert (check.status, check.ready) == ("WRONG_MODE", False)
    ex = executor(FakeServerClient(simulated=False), simulated=False)
    assert ex.prepare(["balance"])[0].status == "READY"


def test_unconnected_or_unstartable_servers_are_not_ready_and_addresses_scrubbed():
    ex = executor(FakeServerClient(connected=False))
    [check] = ex.prepare(["balance"])
    assert (check.status, check.ready) == ("NOT_CONNECTED", False)
    ex = executor(FakeServerClient(start_error="cannot open /dev/ttyUSB7"))
    [check] = ex.prepare(["balance"])
    assert check.status == "OFFLINE" and check.detail == "cannot open <balance address>"


def test_a_call_replies_with_the_tools_data():
    client = FakeServerClient({"tare": {"value": 0.0, "unit": "g"}})
    ex = executor(client)
    ex.prepare(["balance"])
    results = []
    ex.submit(
        "s1",
        {"tool": "balance", "command": "tare"},
        lambda k, r: results.append((k, r)),
    )
    [(key, result)] = wait_for(results)
    assert key == "s1" and result.ok and result.data == {"value": 0.0, "unit": "g"}
    assert client.calls[-1] == {"tool": "tare", "arguments": {}}
    ex.shutdown()


def test_tool_errors_carry_the_servers_message_without_the_address():
    client = FakeServerClient(
        {"tare": ToolResult("TOOL_ERROR", "balance at /dev/ttyUSB7 is overloaded")}
    )
    ex = executor(client)
    ex.prepare(["balance"])
    results = []
    ex.submit(
        "s1", {"tool": "balance", "command": "tare"}, lambda k, r: results.append(r)
    )
    [result] = wait_for(results)
    assert result.code == "TOOL_ERROR"
    assert result.error_message == "balance at <balance address> is overloaded"
    ex.shutdown()


def test_timeout_wins_and_the_late_reply_is_dropped():
    gate = threading.Event()
    ex = executor(FakeServerClient(gate=gate))
    ex.prepare(["balance"])
    results = []
    ex.submit(
        "s1",
        {"tool": "balance", "command": "tare", "timeoutSeconds": 0.1},
        lambda k, r: results.append(r),
    )
    [result] = wait_for(results)
    assert result.code == "TIMEOUT"
    gate.set()
    time.sleep(0.1)
    assert len(results) == 1
    ex.shutdown()


def test_calls_before_prepare_are_not_sent():
    ex = executor(FakeServerClient())
    results = []
    ex.submit(
        "s1", {"tool": "balance", "command": "tare"}, lambda k, r: results.append(r)
    )
    assert results[0].code == "NOT_SENT"


def test_shutdown_reports_calls_in_flight():
    gate = threading.Event()
    ex = executor(FakeServerClient(gate=gate))
    ex.prepare(["balance"])
    ex.submit("s1", {"tool": "balance", "command": "tare"}, lambda k, r: None)
    time.sleep(0.05)
    assert ex.shutdown() == ["s1"]
    gate.set()


def test_url_tools_are_not_launched_yet():
    from rhylthyme_labmcp import launch_client

    [tool] = parse_tools([{"name": "b", "url": "http://lab:8000/mcp"}]).values()
    try:
        launch_client(tool, True)
    except ServerError as e:
        assert "not supported yet" in str(e)
    else:
        raise AssertionError("expected ServerError")
