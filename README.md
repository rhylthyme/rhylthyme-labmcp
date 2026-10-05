# rhylthyme-labmcp

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/rhylthyme-labmcp)](https://pypi.org/project/rhylthyme-labmcp/)

Run lab instruments served by **[LabMCP](https://github.com/K-Dense-AI/lab-instrument-mcps)**,
K-Dense's open-source (Apache-2.0) MCP servers for balances, stirrers, syringe
pumps, sensors, spectrometers, Opentrons robots and generic SiLA 2, SCPI and
Modbus devices, from [Rhylthyme](https://rhylthyme.com) programs. Rhylthyme's
planner and runner schedule the work; each workcell tool is one LabMCP server,
started by the runner and spoken to over MCP. It is an independent project,
not affiliated with or endorsed by K-Dense.

Status: alpha. Everything runs **simulated** unless you ask for a live run.

```bash
pip install "rhylthyme[labmcp]"     # and uv: https://docs.astral.sh/uv/
E=https://raw.githubusercontent.com/rhylthyme/rhylthyme-labmcp/main/examples
curl -sO $E/titration.json -sO $E/workcell-titration.json
rhylthyme run titration.json --workcell workcell-titration.json
```

A step names a tool of the server; the step can send actions when it starts,
end on a reply, and send actions when it ends:

```json
{ "stepId": "heat-stir", "name": "Heat to 40 °C and stir for 2 minutes",
  "duration": { "type": "fixed", "seconds": 120 },
  "instrument": { "tool": "stirrer",
    "start": [ { "command": "set_temperature", "params": { "temperature_c": 40 } },
               { "command": "start_heating" }, { "command": "start_stirring" } ],
    "end":   [ { "command": "stop_heating" } ] } }
```

A local **workcell** says which LabMCP server each tool is, where the
instrument is, and its safety limits; it never leaves the lab machine, and
LabMCP and galago-tools tools can share one:

```json
{ "id": "titration-bench",
  "tools": [
    { "name": "stirrer", "driver": "labmcp", "server": "ika",
      "address": "/dev/ttyUSB1", "limits": { "max_temperature_c": 80 } } ] }
```

## What you get

- **Checks before the run**: `rhylthyme validate --workcell` checks every
  call against a catalogue of every LabMCP server's tools, input schemas and
  limits, the servers' command policies, and pump timing.
- **Planning**: durations estimated from params (doses, series, runs, waits)
  for `rhylthyme plan`, timelines and the hosted MCP server.
- **Safety**: workcell limits passed to the servers; safe stops when a step
  fails or the run is aborted; pausing the schedule pauses instruments that
  can hold; a live pre-flight with every instrument's identity, mode and
  limits and every hazardous action, behind a typed `live`.
- **Remote servers** over HTTP, and the **web bridge** (`rhylthyme bridge`)
  to watch and steer runs from rhylthyme.com, with addresses never published.

**Read the [guide](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/docs/guide.md)**: setup and the workcell reference,
writing steps, checking and planning, running, limits and safe stops, live
runs, the web bridge, and troubleshooting.

## Examples

| Program | Shows |
|---|---|
| [`titration.json`](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/examples/titration.json) | Balance, hotplate stirrer and pH probe; actions around a timer |
| [`dosing.json`](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/examples/dosing.json) | New Era and Tecan Cavro syringe pumps |
| [`opentrons-transfer.json`](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/examples/opentrons-transfer.json) | One Opentrons protocol on LabMCP or galago-tools: only the workcell changes |
| [`protocol-bridges.json`](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/examples/protocol-bridges.json) | SiLA 2, SCPI and Modbus devices, each with a command policy |
| [`tare-and-shake.json`](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/examples/tare-and-shake.json) | A LabMCP balance and a galago-tools shaker in one workcell |

Each has its workcell in [`examples/`](https://github.com/rhylthyme/rhylthyme-labmcp/tree/main/examples).

## Development

```bash
pip install -e ".[dev]" -e ../rhylthyme-cli-runner
pytest -m "not integration"   # fakes only
pytest -m integration         # real LabMCP servers via uvx, simulated
```

## License

Apache-2.0. LabMCP is by [K-Dense](https://www.k-dense.ai), Apache-2.0; the
catalogue is exported from its servers, and one module is vendored from it.
See [NOTICE](https://github.com/rhylthyme/rhylthyme-labmcp/blob/main/NOTICE).
