"""The examples validate, and the titration runs in simulate mode."""

import json
import shutil
import time
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
PROGRAMS = ["tare-and-shake.json", "titration.json"]
WORKCELLS = {
    "tare-and-shake.json": "workcell-mixed.json",
    "titration.json": "workcell-titration.json",
}

runner = pytest.importorskip("rhylthyme_cli_runner")


def load(name):
    return json.loads((EXAMPLES / name).read_text())


@pytest.mark.parametrize("name", PROGRAMS)
def test_examples_validate_against_the_workcell(name):
    from rhylthyme_cli_runner.validate_program import (
        perform_additional_validations,
        validate_program,
    )
    from rhylthyme_spec import get_program_schema_path

    if name == "tare-and-shake.json":
        pytest.importorskip("rhylthyme_galago")
    schema = json.loads(Path(get_program_schema_path("0.2.0-alpha")).read_text())
    program = load(name)
    valid, errors = validate_program(program, schema)
    assert valid, errors
    assert (
        perform_additional_validations(
            program, workcell=str(EXAMPLES / WORKCELLS[name])
        )
        == []
    )


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uvx")
def test_titration_runs_to_completion_in_simulate_mode(tmp_path):
    from rhylthyme_cli_runner.history.recorder import RunRecorder
    from rhylthyme_cli_runner.history.store import validate_run
    from rhylthyme_cli_runner.instruments import attach_instruments
    from rhylthyme_cli_runner.program_runner import ProgramRunner

    program = load("titration.json")
    workcell = load("workcell-titration.json")
    run = ProgramRunner(program, time_scale=100.0)
    recorder = RunRecorder(run, source_program=program, runs_dir=str(tmp_path)).attach()
    session = attach_instruments(run, workcell)
    try:
        run.start()
        run.command_queue.put("start_program")
        deadline = time.time() + 180
        while run.is_running and time.time() < deadline:
            run.update()
            time.sleep(0.05)
        assert not run.is_running, run.status_message
        for _ in range(40):  # replies to the last end actions
            run.update()
            time.sleep(0.05)
    finally:
        assert session.shutdown() == []
    text = Path(recorder.finalize()).read_text()
    for tool in workcell["tools"]:
        assert tool["address"] not in text
    record = json.loads(text)
    assert validate_run(record) == []
    steps = {s["stepId"]: s for s in record["steps"]}
    assert {s.get("endedBy") for s in steps.values()} <= {"instrument", "timer"}
    assert steps["heat-stir"]["endedBy"] == "timer"
    heat = [
        (r["phase"], r["command"], r["code"])
        for r in steps["heat-stir"]["instrument"]["replies"]
    ]
    assert heat == [
        ("start", "set_temperature", "SUCCESS"),
        ("start", "set_speed", "SUCCESS"),
        ("start", "start_heating", "SUCCESS"),
        ("start", "start_stirring", "SUCCESS"),
        ("end", "stop_heating", "SUCCESS"),
    ]
    ph = steps["log-ph"]
    assert ph["endedBy"] == "instrument"
    until = [r for r in ph["instrument"]["replies"] if r["phase"] == "until"][0]
    assert until["metadata"]["stats"][0]["unit"] == "pH"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uvx")
def test_a_refused_setpoint_stops_the_stirrer_and_abort_stops_the_rest(tmp_path):
    """The workcell caps the stirrer at 80 °C; asking for 120 °C fails the
    step with the server's refusal, its default safety tools are sent, and
    aborting the run stops the other instruments it used."""
    from rhylthyme_cli_runner.history.recorder import RunRecorder
    from rhylthyme_cli_runner.history.store import validate_run
    from rhylthyme_cli_runner.instruments import attach_instruments
    from rhylthyme_cli_runner.program_runner import ProgramRunner, StepStatus

    program = load("titration.json")
    heat = program["tracks"][0]["steps"][2]
    heat["instrument"]["start"][0]["params"]["temperature_c"] = 120
    run = ProgramRunner(program, time_scale=100.0)
    recorder = RunRecorder(run, source_program=program, runs_dir=str(tmp_path)).attach()
    session = attach_instruments(run, load("workcell-titration.json"))
    try:
        run.start()
        run.command_queue.put("start_program")
        deadline = time.time() + 120
        while time.time() < deadline and not (
            run.steps["heat-stir"].status == StepStatus.FAILED
            and not run.safe_stops_pending
        ):
            run.update()
            time.sleep(0.05)
        failure = run.steps["heat-stir"].failure
        assert failure["command"] == "set_temperature"
        assert (
            "max_temperature_c" in failure["errorMessage"]
            or "80" in failure["errorMessage"]
        )
        assert run.status_message.startswith("Instruments stopped.")
        run.abort_program("operator")
        session.finish(run, timeout=30)
    finally:
        session.shutdown()
    record = json.loads(Path(recorder.finalize()).read_text())
    assert validate_run(record) == []
    steps = {s["stepId"]: s for s in record["steps"]}
    stops = [
        r["command"]
        for r in steps["heat-stir"]["instrument"]["replies"]
        if r["phase"] == "onAbort"
    ]
    assert stops == ["stop_heating", "stop_stirring", "stop_all"]
    balance_stops = [
        r["command"]
        for r in steps["weigh"]["instrument"]["replies"]
        if r.get("phase") == "onAbort"
    ]
    assert balance_stops == ["reset_balance"]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uvx")
def test_a_live_run_with_unreachable_instruments_is_refused(tmp_path, capsys):
    """Live servers on ports that do not exist: the pre-flight shows each
    instrument's mode and limits and every hazard action, then refuses the
    run before anything is sent, without printing an address."""
    from rhylthyme_cli_runner.program_runner import run_program

    workcell = load("workcell-titration.json")
    for i, tool in enumerate(workcell["tools"]):
        tool["address"] = f"/dev/tty.rhylthyme-missing-{i}"
    path = tmp_path / "lab.json"
    path.write_text(json.dumps(workcell))
    with pytest.raises(SystemExit):
        run_program(
            str(EXAMPLES / "titration.json"),
            validate=False,
            record=False,
            workcell=str(path),
            live=True,
            confirm_live=True,
        )
    out = capsys.readouterr().out
    assert "max_temperature_c=80 °C (workcell)" in out
    assert "heat-stir: start stirrer.start_heating()" in out
    refusal = out[out.index("Live run refused") :]
    assert refusal.count("NOT_CONNECTED") == 3
    assert "rhylthyme-missing" not in refusal
