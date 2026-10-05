import pytest

from rhylthyme_labmcp import estimate_call
from rhylthyme_labmcp.estimates import DEFAULT_SECONDS


@pytest.mark.parametrize(
    "package, command, params, seconds, detail",
    [
        # A series: (count - 1) * interval, the tool's defaults when left out
        (
            "labmcp-atlas-ezo",
            "log_series",
            {"count": 10, "interval_s": 3},
            27,
            "params.count, params.interval_s",
        ),
        (
            "labmcp-atlas-ezo",
            "log_series",
            {},
            18,
            "params.count, params.interval_s, tool defaults",
        ),
        (
            "labmcp-mettler-toledo",
            "log_weight_series",
            {"count": 61, "interval_s": 1},
            60,
            "params.count, params.interval_s",
        ),
        (
            "labmcp-micro-manager",
            "acquire_time_lapse",
            {"timepoints": 5, "interval_s": 30},
            120,
            "params.timepoints, params.interval_s",
        ),
        # A run length, plus equilibration
        (
            "labmcp-palmsens",
            "run_chronoamperometry",
            {"potential_v": 0.2, "run_time_s": 90, "equilibration_s": 10},
            100,
            "params.run_time_s, params.equilibration_s",
        ),
        (
            "labmcp-ble-health",
            "read_pulse_oximetry",
            {},
            10,
            "params.duration_s, tool defaults",
        ),
        # A wait: its timeout, an upper bound
        (
            "labmcp-ika",
            "wait_for_temperature",
            {"target_c": 60},
            600,
            "params.timeout_s, tool defaults (an upper bound)",
        ),
        (
            "labmcp-ika",
            "wait_for_temperature",
            {"target_c": 60, "timeout_s": 900},
            900,
            "params.timeout_s (an upper bound)",
        ),
    ],
)
def test_blocking_calls_are_estimated_from_their_params(
    package, command, params, seconds, detail
):
    found = estimate_call(package, command, params)
    assert (found.seconds, found.source, found.detail) == (seconds, "params", detail)


@pytest.mark.parametrize(
    "package, command",
    [
        ("labmcp-mettler-toledo", "tare"),
        ("labmcp-ika", "set_temperature"),
        ("labmcp-unknown", "anything"),
    ],
)
def test_other_calls_get_the_default(package, command):
    found = estimate_call(package, command, {"temperature_c": 40})
    assert (found.seconds, found.source) == (DEFAULT_SECONDS, "default")


def test_uncatalogued_servers_still_read_explicit_params():
    found = estimate_call("labmcp-unknown", "record", {"duration_s": 45})
    assert (found.seconds, found.detail) == (45, "params.duration_s")
