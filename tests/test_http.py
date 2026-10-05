"""LabMCP servers already running over streamable HTTP (phase 10)."""

import shutil
import socket
import subprocess
import time

import pytest

from rhylthyme_labmcp import LabMCPExecutor, find_uvx, parse_tools, pinned_version

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uvx"),
]

PACKAGE = "labmcp-mettler-toledo"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(port, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def http_server():
    """A simulated balance started on its own, as on another machine."""
    port = _free_port()
    proc = subprocess.Popen(
        [
            find_uvx(),
            f"{PACKAGE}=={pinned_version(PACKAGE)}",
            "--simulate",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(port), "the HTTP server did not start"
        yield f"http://127.0.0.1:{port}/mcp", proc
    finally:
        proc.terminate()
        proc.wait(10)


def _call(ex, command):
    results = []
    ex.submit(
        "k", {"tool": "balance", "command": command}, lambda k, r: results.append(r)
    )
    deadline = time.time() + 30
    while not results and time.time() < deadline:
        time.sleep(0.05)
    return results[0]


def test_a_run_uses_a_server_started_separately(http_server):
    url, proc = http_server
    tools = parse_tools([{"name": "balance", "server": "mettler-toledo", "url": url}])
    ex = LabMCPExecutor(tools)
    try:
        [check] = ex.prepare(["balance"])
        assert check.ready, check.detail
        assert check.info["simulated"] is True
        assert ex.default_stops("balance") == ["reset_balance"]
        tare = _call(ex, "tare")
        assert tare.ok and tare.data["unit"] == "g"
    finally:
        ex.shutdown()
    assert proc.poll() is None, "the runner must not stop a server it did not start"


def test_the_runner_runs_a_program_on_it(http_server, tmp_path):
    import json

    runner_mod = pytest.importorskip("rhylthyme_cli_runner.program_runner")
    from rhylthyme_cli_runner.history.recorder import RunRecorder
    from rhylthyme_cli_runner.instruments import attach_instruments

    url, _ = http_server
    program = {
        "programId": "tare-over-http",
        "name": "Tare over HTTP",
        "startTrigger": {"type": "manual"},
        "tracks": [
            {
                "trackId": "t",
                "name": "T",
                "steps": [
                    {
                        "stepId": "tare",
                        "name": "Tare",
                        "instrument": {"tool": "balance", "command": "tare"},
                        "startTrigger": {"type": "programStart"},
                    },
                ],
            }
        ],
        "resourceConstraints": [],
    }
    workcell = {
        "id": "remote-bench",
        "tools": [
            {
                "name": "balance",
                "driver": "labmcp",
                "server": "mettler-toledo",
                "url": url,
            }
        ],
    }
    run = runner_mod.ProgramRunner(program, time_scale=100.0)
    recorder = RunRecorder(run, source_program=program, runs_dir=str(tmp_path)).attach()
    session = attach_instruments(run, workcell)
    try:
        run.start()
        run.command_queue.put("start_program")
        deadline = time.time() + 30
        while run.is_running and time.time() < deadline:
            run.update()
            time.sleep(0.05)
        assert not run.is_running
    finally:
        session.shutdown()
    text = open(recorder.finalize()).read()
    assert url not in text and "127.0.0.1" not in text
    assert json.loads(text)["steps"][0]["endedBy"] == "instrument"


def test_an_unreachable_server_is_not_ready_and_its_url_is_scrubbed():
    url = f"http://127.0.0.1:{_free_port()}/mcp"  # nothing listens here
    tools = parse_tools([{"name": "remote", "server": "mettler-toledo", "url": url}])
    ex = LabMCPExecutor(tools)
    try:
        [check] = ex.prepare(["remote"])
    finally:
        ex.shutdown()
    assert (check.status, check.ready) == ("OFFLINE", False)
    assert url not in check.detail and "127.0.0.1" not in check.detail
