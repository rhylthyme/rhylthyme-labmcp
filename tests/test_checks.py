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
