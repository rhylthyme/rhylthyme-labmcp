import shutil
import sys
from pathlib import Path

import pytest

from rhylthyme_labmcp import (
    check_calls,
    check_workcell,
    effective_limits,
    load_catalog,
    parse_tools,
    pinned_version,
    server_args,
)

TOOLS = parse_tools(
    [
        {
            "name": "stirrer",
            "server": "ika",
            "address": "COM1",
            "limits": {"max_temperature_c": 80},
        },
        {"name": "balance", "server": "mettler-toledo", "address": "COM2"},
    ]
)


def program(*instruments):
    return {
        "tracks": [
            {
                "trackId": "t",
                "steps": [
                    {"stepId": f"s{i}", "instrument": inst}
                    for i, inst in enumerate(instruments)
                ],
            }
        ]
    }


def messages(issues):
    return [(i.code, i.step_id, i.message) for i in issues]


def test_the_catalogue_is_pinned_and_attributed():
    catalog = load_catalog()
    assert catalog["source"]["license"] == "Apache-2.0"
    assert "K-Dense" in catalog["source"]["attribution"]
    assert catalog["source"]["labmcp"]
    packages = catalog["packages"]
    assert len(packages) >= 30
    for name, entry in packages.items():
        assert name.startswith("labmcp-") and entry["version"], name
        for tool, spec in entry.get("tools", {}).items():
            assert spec["kind"] in ("read", "control", "hazard", "safety"), (name, tool)
            assert spec["inputSchema"].get("type") == "object", (name, tool)


def test_servers_launch_at_the_catalogued_version_unless_pinned():
    [t] = parse_tools([{"name": "b", "server": "ika"}]).values()
    version = pinned_version("labmcp-ika")
    assert server_args(t, simulate=True, version=version)[0] == f"labmcp-ika=={version}"
    [t] = parse_tools([{"name": "b", "server": "ika==0.0.9"}]).values()
    assert server_args(t, simulate=True, version=version)[0] == "labmcp-ika==0.0.9"


def test_good_calls_pass():
    issues = check_calls(
        program(
            {
                "tool": "stirrer",
                "command": "set_temperature",
                "params": {"temperature_c": 60},
            },
            {"tool": "balance", "command": "tare"},
            {"tool": "balance", "command": "read_weight", "params": {"stable": False}},
        ),
        TOOLS,
    )
    assert issues == []


def test_unknown_tools_params_types_and_bounds():
    issues = messages(
        check_calls(
            program(
                {"tool": "balance", "command": "weigh"},
                {"tool": "balance", "command": "tare", "params": {"now": True}},
                {
                    "tool": "stirrer",
                    "command": "set_speed",
                    "params": {"speed_rpm": "fast"},
                },
                {
                    "tool": "stirrer",
                    "command": "set_temperature",
                    "params": {"temperature_c": 600},
                },
                {"tool": "stirrer", "command": "set_temperature"},
            ),
            TOOLS,
        )
    )
    codes = [(c, s) for c, s, _ in issues]
    assert ("instrument_invalid_command", "s0") in codes
    assert any("has no tool 'weigh'" in m for *_, m in issues)
    assert any("'now' was unexpected" in m for *_, m in issues)
    assert any("speed_rpm: 'fast' is not of type 'number'" in m for *_, m in issues)
    assert any("600 is greater than the maximum of 500" in m for *_, m in issues)
    assert any("'temperature_c' is a required property" in m for *_, m in issues)


def test_limits_from_the_workcell_and_the_server_defaults():
    issues = messages(
        check_calls(
            program(
                {
                    "tool": "stirrer",
                    "command": "set_temperature",
                    "params": {"temperature_c": 90},
                },
                {
                    "tool": "stirrer",
                    "command": "set_speed",
                    "params": {"speed_rpm": 1500},
                },
            ),
            TOOLS,
        )
    )
    over = [m for c, _, m in issues if c == "instrument_over_limit"]
    assert over == [
        "Step 's0': stirrer (labmcp-ika) set_temperature: temperature_c=90 is above "
        "the workcell's limit max_temperature_c=80 °C",
        "Step 's1': stirrer (labmcp-ika) set_speed: speed_rpm=1500 is above the "
        "server's default limit max_speed_rpm=1000 rpm",
    ]
    assert effective_limits("labmcp-ika", {"max_temperature_c": 80})[
        "max_temperature_c"
    ][1:] == (80, "°C", True)


def test_without_a_workcell_the_step_names_its_server():
    issues = check_calls(
        program(
            {
                "tool": "x",
                "toolType": "labmcp-ika",
                "command": "set_temperature",
                "params": {"temperature_c": 160},
            },
            {"tool": "y", "toolType": "labmcp-nope", "command": "go"},
        )
    )
    assert [(i.code, i.severity) for i in issues] == [
        ("instrument_over_limit", "error"),  # the server's own 150 °C default
        ("instrument_unknown_tool_type", "warning"),
    ]


def test_workcell_entries_are_checked():
    tools = parse_tools(
        [
            {"name": "a", "server": "ika", "limits": {"max_temp": 50}},
            {"name": "b", "server": "nope"},
            {"name": "c", "server": "mettler-toledo==0.0.1"},
        ]
    )
    issues = check_workcell(tools)
    assert [(i.code, i.severity) for i in issues] == [
        ("workcell_unknown_limit", "error"),
        ("instrument_unknown_tool_type", "warning"),
        ("workcell_version_mismatch", "warning"),
    ]
    assert "max_speed_rpm, max_temperature_c, max_wait_s" in issues[0].message


def test_url_tools_without_a_server_are_not_checked_and_say_so():
    tools = parse_tools([{"name": "remote", "url": "http://lab:8000/mcp"}])
    [issue] = check_workcell(tools)
    assert (issue.code, issue.severity) == ("workcell_url_unchecked", "warning")
    issues = check_calls(program({"tool": "remote", "command": "anything"}), tools)
    assert issues == []


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uvx")
def test_the_vendored_catalogue_matches_a_fresh_export():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from export_catalog import export_server

    package = "labmcp-mettler-toledo"
    entry = load_catalog()["packages"][package]
    assert export_server(package, entry["version"]) == entry


def test_the_check_cases_are_fresh():
    """labmcp-check-cases.json (mirrored to the hosted validator) is what the
    catalogue and this package say now: re-run export_check_cases.py if not."""
    import json

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from export_check_cases import OUT, cases

    vendored = json.loads(OUT.read_text())
    assert vendored["source"] == load_catalog()["source"]
    assert vendored["cases"] == json.loads(json.dumps(cases()))
    from export_check_cases import timing_cases

    assert vendored["timing"] == json.loads(json.dumps(timing_cases()))


def test_galago_opentrons_commands_are_checked_on_labmcp_opentrons():
    from rhylthyme_labmcp.checks import call_problems
    from rhylthyme_labmcp.estimates import estimate_call

    assert (
        call_problems("labmcp-opentrons", "run_program", {"script_content": "x"}) == []
    )
    assert call_problems("labmcp-opentrons", "cancel", {}) == []
    [(code, problem)] = call_problems("labmcp-opentrons", "run_program", {})
    assert problem == "'script_content' is a required property"
    [(code, problem)] = call_problems(
        "labmcp-opentrons",
        "run_program",
        {"script_content": "x", "variables": {"a": 1}},
    )
    assert problem == "variables: {'a': 1} is expected to be empty"
    assert estimate_call("labmcp-opentrons", "run_program", {}).seconds == 600


# --- Protocol-bridge policies (phase 13) ---------------------------------------

POLICY_TOOLS = parse_tools(
    [
        {
            "name": "block",
            "server": "sila2",
            "options": {"command_allowlist": "Shaker.*, Lid.Open"},
        },
        {
            "name": "psu",
            "server": "scpi",
            "options": {
                "write_denylist": r"^OUTP\d*(:STAT)? (?!OFF\b)",
                "query_denylist": "^MEAS",
            },
        },
        {"name": "plc", "server": "modbus", "options": {"raw_writes": "false"}},
        {"name": "plc2", "server": "modbus"},
    ]
)


@pytest.mark.parametrize(
    "tool, command, params, refused",
    [
        ("block", "call_command", {"feature": "Shaker", "command": "Shake"}, False),
        ("block", "call_command", {"feature": "lid", "command": "open"}, False),
        ("block", "call_command", {"feature": "Lid", "command": "Close"}, True),
        ("psu", "scpi_write", {"command": "OUTP OFF"}, False),
        ("psu", "scpi_write", {"command": ":OUTPut1:STATe ON"}, True),
        ("psu", "scpi_write", {"command": "VOLT 2;OUTP 1"}, True),
        ("psu", "scpi_batch", {"steps": ["VOLT 2", "OUTP ON"]}, True),
        ("psu", "scpi_query", {"command": "MEAS:VOLT?"}, True),
        ("psu", "scpi_query", {"command": "*TST?"}, True),
        ("psu", "scpi_query", {"command": "FETC?"}, False),
        ("plc", "write_register", {"address": 1, "value": 2}, True),
        ("plc", "write_point", {"name": "setpoint", "value": 2}, False),
        ("plc2", "write_register", {"address": 1, "value": 2}, False),
    ],
)
def test_server_policies_are_applied_before_the_run(tool, command, params, refused):
    issues = check_calls(
        program({"tool": tool, "command": command, "params": params}), POLICY_TOOLS
    )
    found = [i for i in issues if i.code == "instrument_refused_by_policy"]
    assert bool(found) is refused, [i.message for i in issues]


def test_bad_policy_options_are_workcell_errors():
    tools = parse_tools(
        [
            {"name": "a", "server": "scpi", "options": {"write_denylist": "(unclosed"}},
            {"name": "b", "server": "sila2", "options": {"command_allowlist": "NoDot"}},
        ]
    )
    codes = [(i.code, i.severity) for i in check_workcell(tools)]
    assert codes == [("workcell_invalid_policy", "error")] * 2


def test_misnamed_limits_bound_their_params():
    tools = parse_tools(
        [{"name": "block", "server": "sila2", "limits": {"max_command_wait_s": 60}}]
    )
    [issue] = check_calls(
        program(
            {
                "tool": "block",
                "command": "call_command",
                "params": {"feature": "A", "command": "B", "wait_s": 90},
            }
        ),
        tools,
    )
    assert issue.message.endswith(
        "wait_s=90 is above the workcell's limit max_command_wait_s=60 s"
    )
    # A series' duration, with the tool's defaults for what is left out
    [issue] = check_calls(
        program(
            {
                "tool": "b",
                "toolType": "labmcp-mettler-toledo",
                "command": "log_weight_series",
                "params": {"count": 1000, "interval_s": 1},
            }
        )
    )
    assert (
        "series (count - 1) × interval_s=999 is above the server's default limit"
        in issue.message
    )
