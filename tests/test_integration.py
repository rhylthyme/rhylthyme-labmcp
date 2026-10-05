"""Real LabMCP servers, launched with uvx in --simulate (downloads on first run)."""

import shutil
import time

import pytest

from rhylthyme_labmcp import LabMCPExecutor, parse_tools

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uvx") is None, reason="needs uvx"),
]

TOOLS = parse_tools(
    [
        {
            "name": "balance",
            "server": "mettler-toledo",
            "address": "/dev/tty.rhylthyme-test-0",
        }
    ]
)


def call(ex, command, params=None):
    results = []
    ex.submit(
        "k",
        {"tool": "balance", "command": command, "params": params or {}},
        lambda k, r: results.append(r),
    )
    deadline = time.time() + 60
    while not results and time.time() < deadline:
        time.sleep(0.05)
    return results[0]


def test_simulated_balance_tares_and_refuses_bad_calls():
    ex = LabMCPExecutor(TOOLS)
    try:
        [check] = ex.prepare(["balance"])
        assert check.ready, check.detail
        assert check.info["simulated"] is True
        assert "model" in check.info["instrument"]
        tare = call(ex, "tare")
        assert tare.ok and tare.data["unit"] == "g"
        bad = call(ex, "set_draft_shield", {"position": "sideways"})
        assert bad.code == "TOOL_ERROR" and "position" in bad.error_message
        missing = call(ex, "no_such_tool")
        assert missing.code == "TOOL_ERROR"
    finally:
        ex.shutdown()
