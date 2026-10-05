"""LabMCP's Opentrons server in place of galago's (phase 12), on its simulator."""

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


def program(**transfer):
    p = load("opentrons-transfer.json")
    for step in p["tracks"][0]["steps"]:
        if "duration" in step:
            step["duration"] = {"type": "fixed", "seconds": 1}
    p["tracks"][0]["steps"][1]["instrument"].update(transfer)
    return p


def run(prog, workcell, tmp_path, during=None, until=None, timeout=120):
    from rhylthyme_cli_runner.history.recorder import RunRecorder
    from rhylthyme_cli_runner.history.store import validate_run
    from rhylthyme_cli_runner.instruments import attach_instruments
    from rhylthyme_cli_runner.program_runner import ProgramRunner

    runner = ProgramRunner(copy.deepcopy(prog), time_scale=1.0)
    recorder = RunRecorder(runner, source_program=prog, runs_dir=str(tmp_path)).attach()
    session = attach_instruments(runner, workcell)
    try:
        runner.start()
        runner.command_queue.put("start_program")
        stop = until or (lambda r: not r.is_running)
        deadline = time.time() + timeout
        while time.time() < deadline and not stop(runner):
            runner.update()
            if during:
                during(runner)
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


def by_phase(step, phase):
    return [
        (r.get("command"), r["code"])
        for r in step["instrument"]["replies"]
        if r.get("phase") == phase
    ]


def test_a_protocol_runs_to_completion_through_labmcp(tmp_path):
    runner, steps = run(program(), load("workcell-opentrons-labmcp.json"), tmp_path)
    transfer = steps["transfer"]
    assert transfer["endedBy"] == "instrument"
    [reply] = transfer["instrument"]["replies"]
    assert reply["code"] == "SUCCESS"
    assert reply["metadata"]["status"] == "succeeded"
    assert (
        reply["metadata"]["commands_executed"] == reply["metadata"]["commands_expected"]
    )


def test_a_failed_run_triggers_the_opentrons_safety_stop(tmp_path):
    # The run takes ~5 s: a 1 s timeout fails the step while the robot moves
    runner, steps = run(
        program(timeoutSeconds=1),
        load("workcell-opentrons-labmcp.json"),
        tmp_path,
        until=lambda r: r.failed_steps and not r.safe_stops_pending,
    )
    assert runner.steps["transfer"].failure["code"] == "TIMEOUT"
    # Stop the run first, then switch the modules off; never just pause
    assert by_phase(steps["transfer"], "onAbort") == [
        ("stop_run", "SUCCESS"),
        ("deactivate_modules", "SUCCESS"),
    ]


@pytest.mark.parametrize("after", [0.0, 2.0], ids=["while-starting", "mid-run"])
def test_pausing_the_schedule_pauses_the_robot(tmp_path, after):
    """Pause as the step starts (the run is still being set up: it is paused
    the moment it starts) or mid-run; resume 3 s later."""
    state = {"started": None, "paused_at": None, "resumed": False}

    def pause_then_resume(runner):
        transfer = runner.steps["transfer"]
        if transfer.status.value != "RUNNING":
            return
        now = time.time()
        state["started"] = state["started"] or now
        if state["paused_at"] is None and now - state["started"] >= after:
            runner.toggle_pause()
            state["paused_at"] = now
        elif (
            state["paused_at"] and not state["resumed"] and now - state["paused_at"] > 3
        ):
            runner.toggle_pause()
            state["resumed"] = True

    runner, steps = run(
        program(),
        load("workcell-opentrons-labmcp.json"),
        tmp_path,
        during=pause_then_resume,
    )
    assert by_phase(steps["transfer"], "pause") == [("pause_run", "SUCCESS")]
    assert by_phase(steps["transfer"], "resume") == [("resume_run", "SUCCESS")]
    assert steps["transfer"]["endedBy"] == "instrument"
    calls = [
        r
        for r in steps["transfer"]["instrument"]["replies"]
        if r.get("phase") in (None, "call")
    ]
    assert calls[-1]["metadata"]["status"] == "succeeded"
    # The robot held while paused: the run took ~5 s plus the ~3 s pause
    outcome = calls[-1]["metadata"]
    from datetime import datetime

    took = (
        datetime.fromisoformat(outcome["completed_at"])
        - datetime.fromisoformat(outcome["started_at"])
    ).total_seconds()
    assert took >= 6.5, took
