"""Syringe pumps end to end on LabMCP's simulators (phase 11)."""

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


def run(program, workcell, tmp_path, until=None, timeout=120):
    """Run on the real simulated servers, at real time; return the record."""
    from rhylthyme_cli_runner.history.recorder import RunRecorder
    from rhylthyme_cli_runner.history.store import validate_run
    from rhylthyme_cli_runner.instruments import attach_instruments
    from rhylthyme_cli_runner.program_runner import ProgramRunner

    runner = ProgramRunner(copy.deepcopy(program), time_scale=1.0)
    recorder = RunRecorder(
        runner, source_program=program, runs_dir=str(tmp_path)
    ).attach()
    session = attach_instruments(runner, workcell)
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


def replies(step, phase=None):
    return [
        (r.get("phase", "call"), r.get("command"), r["code"])
        for r in step["instrument"]["replies"]
        if phase is None or r.get("phase") == phase
    ]


def test_the_dosing_example_runs_on_both_pumps(tmp_path):
    program = load("dosing.json")
    infuse = program["tracks"][0]["steps"][1]
    # 0.5 mL at 5 mL/min = 6 s, so the test stays short
    infuse["instrument"]["start"][0]["params"] = {"volume_ml": 0.5, "rate_ml_min": 5}
    infuse["duration"] = {"type": "fixed", "seconds": 8}
    runner, steps = run(program, load("workcell-dosing.json"), tmp_path)
    assert all(s["endedBy"] in ("instrument", "timer") for s in steps.values())
    # The step outlasted the dose: the read-back holds the whole volume
    read = [
        r
        for r in steps["ne-infuse"]["instrument"]["replies"]
        if r.get("phase") == "end"
    ]
    assert read[0]["metadata"]["infused_ml"] == pytest.approx(0.5, abs=0.02)
    moved = [
        steps[s]["instrument"]["replies"][0]["metadata"]["moved_ul"]
        for s in ("cv-draw", "cv-dose-1", "cv-dose-2")
    ]
    assert moved == [500.0, 250.0, 250.0]


def test_a_failed_cavro_dose_stops_the_pump(tmp_path):
    program = load("dosing.json")
    program["tracks"] = [program["tracks"][1]]
    # 500 µL drawn; the second dose asks for more than is left
    program["tracks"][0]["steps"][3]["instrument"]["params"]["volume_ul"] = 400
    runner, steps = run(
        program,
        load("workcell-dosing.json"),
        tmp_path,
        until=lambda r: r.failed_steps and not r.safe_stops_pending,
    )
    failure = runner.steps["cv-dose-2"].failure
    assert failure["code"] == "TOOL_ERROR" and failure["command"] == "dispense_ul"
    assert replies(steps["cv-dose-2"], "onAbort") == [
        ("onAbort", "terminate", "SUCCESS")
    ]


def test_a_refused_new_era_dose_stops_the_pump(tmp_path):
    program = load("dosing.json")
    program["tracks"] = [program["tracks"][0]]
    infuse = program["tracks"][0]["steps"][1]
    infuse["instrument"]["start"][0]["params"]["volume_ml"] = 15  # workcell: 10 mL
    runner, steps = run(
        program,
        load("workcell-dosing.json"),
        tmp_path,
        until=lambda r: r.failed_steps and not r.safe_stops_pending,
    )
    failure = runner.steps["ne-infuse"].failure
    assert failure["command"] == "infuse" and "max_volume_ml" in failure["errorMessage"]
    assert replies(steps["ne-infuse"], "onAbort") == [
        ("onAbort", "stop_pump", "SUCCESS")
    ]
