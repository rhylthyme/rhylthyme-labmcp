import threading
import time

from rhylthyme_labmcp import (
    FakeServerClient,
    LabMCPExecutor,
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


def test_url_tools_are_connected_to_not_launched():
    from rhylthyme_labmcp import HttpServerClient, launch_client

    [tool] = parse_tools([{"name": "b", "url": "http://lab:8000/mcp"}]).values()
    client = launch_client(tool, True)
    assert isinstance(client, HttpServerClient) and client.url == "http://lab:8000/mcp"


def _url_executor(workcell_limit, server_value):
    tools = parse_tools(
        [
            {
                "name": "stirrer",
                "server": "ika",
                "url": "http://lab:8000/mcp",
                "limits": {"max_temperature_c": workcell_limit},
            }
        ]
    )
    info = {
        "simulated": True,
        "connected": True,
        "instrument": {"model": "RCT"},
        "safety_limits": {
            "max_temperature_c": {"value": server_value, "kind": "max", "default": 150}
        },
    }
    server = FakeServerClient({"get_connection_info": info})
    return LabMCPExecutor(tools, lambda tool, simulate: server)


def test_a_url_server_must_enforce_the_workcells_limits():
    [check] = _url_executor(80, 150).prepare(["stirrer"])
    assert (check.status, check.ready) == ("WRONG_LIMITS", False)
    assert "max_temperature_c is 150, the workcell needs 80" in check.detail
    [check] = _url_executor(80, 60).prepare(["stirrer"])  # tighter is fine
    assert check.ready


# --- Opentrons: galago commands, stop order, holding a run (phase 12) --------

OT_TOOLS = [
    {"name": "deactivate_modules", "kind": "safety", "required": []},
    {"name": "pause_run", "kind": "safety", "required": []},
    {"name": "resume_run", "kind": "hazard", "required": []},
    {"name": "stop_run", "kind": "safety", "required": []},
]


def _ot(server):
    tools = parse_tools(
        [{"name": "ot2", "server": "opentrons", "address": "http://ot2"}]
    )
    return LabMCPExecutor(tools, lambda tool, simulate: server)


def test_default_stops_stop_first_and_never_just_pause():
    ex = _ot(FakeServerClient(tools=OT_TOOLS))
    ex.prepare(["ot2"])
    assert ex.default_stops("ot2") == ["stop_run", "deactivate_modules"]
    assert ex.pause_tools("ot2") == ["pause_run"]
    assert ex.resume_tools("ot2") == ["resume_run"]
    ex.shutdown()


def test_galago_names_for_simple_calls_reach_the_server_tools():
    server = FakeServerClient(tools=OT_TOOLS)
    ex = _ot(server)
    ex.prepare(["ot2"])
    results = []
    for command in ("pause", "resume", "cancel"):
        ex.submit(
            command, {"tool": "ot2", "command": command}, lambda k, r: results.append(r)
        )
    wait_for(results, 3)
    ex.shutdown()
    # Submitted together: they reach the server in any order
    assert sorted(c["tool"] for c in server.calls[1:]) == [
        "pause_run",
        "resume_run",
        "stop_run",
    ]
    assert ex.tool_kind("ot2", "run_program") == "hazard"
    assert ex.tool_kind("ot2", "cancel") == "safety"


def _protocol_server(final="succeeded", gate=None):
    statuses = iter(["running", final])

    def status(args):
        return {"id": "r1", "status": next(statuses, final)}

    def upload(args):
        if gate is not None:
            gate.wait(5)
        assert open(args["path"]).read() == "def run(protocol): pass\n"
        return {"id": "p1", "ready_to_run": True}

    return FakeServerClient(
        {
            "upload_protocol": upload,
            "start_run": {"id": "r1", "status": "running"},
            "get_run_status": status,
        },
        tools=OT_TOOLS,
    )


def test_run_program_uploads_starts_and_waits_for_the_run(monkeypatch):
    import rhylthyme_labmcp.executor as E

    monkeypatch.setattr(E, "POLL_SECONDS", 0.01)
    server = _protocol_server()
    ex = _ot(server)
    ex.prepare(["ot2"])
    results = []
    ex.submit(
        "s",
        {
            "tool": "ot2",
            "command": "run_program",
            "params": {"script_content": "def run(protocol): pass\n"},
        },
        lambda k, r: results.append(r),
    )
    [result] = wait_for(results)
    ex.shutdown()
    assert result.ok and result.data["status"] == "succeeded"
    calls = [c["tool"] for c in server.calls[1:]]
    assert (
        calls[:2] == ["upload_protocol", "start_run"]
        and calls[2:] == ["get_run_status"] * 2
    )
    assert server.calls[2]["arguments"] == {"protocol_id": "p1", "deck_confirmed": True}


def test_a_run_that_fails_fails_the_call(monkeypatch):
    import rhylthyme_labmcp.executor as E

    monkeypatch.setattr(E, "POLL_SECONDS", 0.01)
    ex = _ot(_protocol_server(final="failed"))
    ex.prepare(["ot2"])
    results = []
    ex.submit(
        "s",
        {
            "tool": "ot2",
            "command": "run_program",
            "params": {"script_content": "def run(protocol): pass\n"},
        },
        lambda k, r: results.append(r),
    )
    [result] = wait_for(results)
    ex.shutdown()
    assert result.code == "TOOL_ERROR" and "ended failed" in result.error_message


def test_a_pause_before_the_run_exists_pauses_it_when_it_starts(monkeypatch):
    import rhylthyme_labmcp.executor as E

    monkeypatch.setattr(E, "POLL_SECONDS", 0.01)
    gate = threading.Event()
    server = _protocol_server(gate=gate)
    ex = _ot(server)
    ex.prepare(["ot2"])
    results, paused = [], []
    ex.submit(
        "s",
        {
            "tool": "ot2",
            "command": "run_program",
            "params": {"script_content": "def run(protocol): pass\n"},
        },
        lambda k, r: results.append(r),
    )
    ex.submit(
        "p", {"tool": "ot2", "command": "pause_run"}, lambda k, r: paused.append(r)
    )
    [early] = wait_for(paused)
    assert early.ok and "paused as soon as it starts" in early.data["deferred"]
    gate.set()
    wait_for(results)
    ex.shutdown()
    calls = [c["tool"] for c in server.calls[1:]]
    assert calls.index("pause_run") == calls.index("start_run") + 1
