"""SiLA 2, SCPI and Modbus devices through LabMCP's bridge servers (phase 13)."""

import copy
import json
import shutil
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uvx"),
]
pytest.importorskip("rhylthyme_cli_runner")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text())


def run(prog, tmp_path, until=None, timeout=120):
    from rhylthyme_cli_runner.history.recorder import RunRecorder
    from rhylthyme_cli_runner.history.store import validate_run
    from rhylthyme_cli_runner.instruments import attach_instruments
    from rhylthyme_cli_runner.program_runner import ProgramRunner

    runner = ProgramRunner(copy.deepcopy(prog), time_scale=5.0)
    recorder = RunRecorder(runner, source_program=prog, runs_dir=str(tmp_path)).attach()
    session = attach_instruments(runner, load("workcell-protocol-bridges.json"))
    try:
        runner.start()
        runner.command_queue.put("start_program")
        stop = until or (lambda r: not r.is_running)
        deadline = time.time() + timeout
        while time.time() < deadline and not stop(runner):
            runner.update()
            time.sleep(0.05)
        assert stop(runner), runner.status_message
        if until is not None:
            runner.abort_program("test over")
        session.finish(runner, timeout=30)
    finally:
        session.shutdown()
    record = json.loads(Path(recorder.finalize()).read_text())
    assert validate_run(record) == []
    return runner, {s["stepId"]: s for s in record["steps"]}


def test_each_device_runs_a_scheduled_step(tmp_path):
    runner, steps = run(load("protocol-bridges.json"), tmp_path)
    assert all(s["endedBy"] in ("instrument", "timer") for s in steps.values())
    ramp = steps["block-ramp"]["instrument"]["replies"][0]["metadata"]
    assert ramp["status"] == "finishedSuccessfully"
    assert ramp["responses"]["FinalTemperature"] == pytest.approx(37, abs=0.5)
    psu = [
        r for r in steps["psu-load"]["instrument"]["replies"] if r.get("phase") == "end"
    ]
    assert [r["command"] for r in psu] == ["scpi_query", "scpi_write"]
    assert float(psu[0]["metadata"]["response"]) == pytest.approx(5, abs=0.5)
    point = steps["pid-read"]["instrument"]["replies"][0]["metadata"]
    assert "setpoint" in json.dumps(point)


def _only(track_id, **change):
    prog = load("protocol-bridges.json")
    prog["tracks"] = [t for t in prog["tracks"] if t["trackId"] == track_id]
    return prog


def _failed(tmp_path, prog, step_id):
    runner, steps = run(
        prog, tmp_path, until=lambda r: r.failed_steps and not r.safe_stops_pending
    )
    failure = runner.steps[step_id].failure
    stops = [
        (r["command"], r["code"])
        for r in steps[step_id]["instrument"]["replies"]
        if r.get("phase") == "onAbort"
    ]
    return failure, stops


def test_the_scpi_denylist_is_respected_and_the_supply_made_safe(tmp_path):
    prog = _only("scpi")
    prog["tracks"][0]["steps"][0]["instrument"]["start"][0]["params"][
        "command"
    ] = "VOLTage 24"
    failure, stops = _failed(tmp_path, prog, "psu-load")
    assert failure["command"] == "scpi_write"
    assert failure["errorMessage"].startswith(
        "Refused: 'VOLTage 24' matches the command denylist"
    )
    assert stops == [("device_clear", "SUCCESS"), ("apply_safe_state", "SUCCESS")]


def test_the_sila_allowlist_is_respected(tmp_path):
    prog = _only("sila")
    prog["tracks"][0]["steps"][0]["instrument"]["params"].update(
        feature="SiLAService", command="SetServerName", parameters={"ServerName": "x"}
    )
    failure, stops = _failed(tmp_path, prog, "block-ramp")
    assert "is not in the command allow-list" in failure["errorMessage"]
    assert stops == [("cancel_command", "SUCCESS")]


def test_modbus_raw_writes_are_refused_when_off(tmp_path):
    prog = _only("modbus")
    prog["tracks"][0]["steps"][0]["instrument"] = {
        "tool": "pid",
        "command": "write_register",
        "params": {"address": 2, "value": 400},
    }
    failure, stops = _failed(tmp_path, prog, "pid-setpoint")
    assert (
        failure["code"] == "TOOL_ERROR" and "write_register" in failure["errorMessage"]
    )
    assert stops == [("apply_safe_state", "SUCCESS")]


def test_a_limit_is_respected_at_run_time(tmp_path):
    prog = _only("sila")
    prog["tracks"][0]["steps"][0]["instrument"]["params"][
        "wait_s"
    ] = 240  # workcell: 180
    failure, _ = _failed(tmp_path, prog, "block-ramp")
    assert "max_command_wait_s" in failure["errorMessage"]
