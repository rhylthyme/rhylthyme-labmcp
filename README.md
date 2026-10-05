# rhylthyme-labmcp

Run lab instruments served by **[LabMCP](https://github.com/K-Dense-AI/lab-instrument-mcps)**
(K-Dense's open-source, Apache-2.0 MCP servers for balances, stirrers, pumps,
sensors, spectrometers and more) from [Rhylthyme](https://rhylthyme.com)
programs. Rhylthyme's planner and runner schedule the work; each workcell tool
is one LabMCP server, launched by the runner and spoken to over MCP. It is an
independent project, not affiliated with or endorsed by K-Dense.

Status: pre-alpha ([plan](https://github.com/rhylthyme/rhylthyme-labmcp/issues/1)).
Servers run in LabMCP's `--simulate` mode unless the run is live.

A step names a tool of the server; the runner calls it when the step starts,
and the step ends on the reply:

```json
{ "stepId": "tare", "name": "Tare the balance",
  "instrument": { "tool": "balance", "command": "tare" },
  "startTrigger": { "type": "programStart" } }
```

A step can also send **phase actions**, each `{command, params, tool?}`
(`tool` defaults to the step's): `start` actions in order when the step starts,
then its `command` or an `until` call whose reply ends the step, and `end`
actions in order when it ends (by timer, operator or reply). Heat and stir for
a fixed time, then log pH with the stirrer slowed down:

```json
{ "stepId": "heat-stir", "duration": { "type": "fixed", "seconds": 120 },
  "instrument": { "tool": "stirrer",
    "start": [ { "command": "set_temperature", "params": { "temperature_c": 40 } },
               { "command": "start_heating" }, { "command": "start_stirring" } ],
    "end":   [ { "command": "stop_heating" } ] } },
{ "stepId": "log-ph",
  "instrument": { "tool": "ph",
    "start": [ { "tool": "stirrer", "command": "set_speed", "params": { "speed_rpm": 150 } } ],
    "until": { "command": "log_series", "params": { "count": 10, "interval_s": 3 } } } }
```

The whole program is [`examples/titration.json`](examples/titration.json)
(balance, IKA stirrer and Atlas EZO pH probe), with
[`examples/workcell-titration.json`](examples/workcell-titration.json).

The local workcell says which server each tool is and where the instrument
is; it never leaves the lab machine. LabMCP and galago tools can share one
workcell:

```json
{ "id": "mixed-bench",
  "tools": [
    { "name": "balance", "driver": "labmcp", "server": "mettler-toledo",
      "address": "/dev/ttyUSB0", "limits": { "max_series_duration_s": 300 } },
    { "name": "shaker", "type": "bioshake", "host": "localhost", "port": 50710 } ] }
```

| Field | Meaning |
|---|---|
| `server` | LabMCP package, with or without `labmcp-`; pin with `mettler-toledo==0.1.3` |
| `address` | LabMCP address URI (serial port, `tcp://…`, VISA), passed as `--address` |
| `limits` | Safety limits, passed as `--limit name=value` |
| `options` | Driver options, passed as `--option name=value` |
| `timeoutSeconds` | Fail a call that has not replied after this long |
| `startTimeoutSeconds` | How long a server may take to start (default 180 s; the first `uvx` run downloads it) |

## Checking steps before a run

`rhylthyme validate PROGRAM --workcell lab.json` checks every LabMCP call
against a vendored catalogue of LabMCP's servers: that the server has the
tool, that the params fit its input schema (unknown or missing params, types,
bounds, enums), and that values stay within the safety limits (the workcell's
`limits`, else the server's defaults; `max_temperature_c` bounds
`temperature_c`). Without a workcell, a step that names its server as
`toolType` (`"labmcp-ika"`) is checked the same way.

The hosted Rhylthyme MCP server (`validate_program`, `analyze_schedule`,
`visualize_schedule` at mcp.rhylthyme.com) checks and times steps that name
their server the same way, word for word, from a mirrored copy of the
catalogue; it never sees a workcell, so it applies the servers' default
limits.

The catalogue is exported from every LabMCP server run with `--simulate`
and pinned to their versions; the runner launches those versions unless a
workcell pins its own (`"server": "ika==0.1.1"`). To refresh it:

```bash
python scripts/export_catalog.py --refresh   # newest LabMCP, re-pinned
python scripts/export_catalog.py             # re-export the pinned versions
python scripts/export_check_cases.py         # cases pinning the hosted validator
```

Then copy both files in `src/rhylthyme_labmcp/catalog/` to
`rhylthyme-server/mcp-api/` (`tools/check_mirrors.sh` in the monorepo fails
until they match).

## Quick start (simulated, no hardware)

```bash
brew install uv            # or: curl -LsSf https://astral.sh/uv/install.sh | sh
pip install "rhylthyme[labmcp]"
rhylthyme run examples/tare-and-shake.json --workcell examples/workcell-mixed.json
```

The example's shaker is a galago-tools Bioshake (`pip install "rhylthyme[galago]"`
and `uvx --python 3.9 --from galago-tools galago-serve --tool bioshake --port 50710`).

The runner starts each server with `uvx labmcp-<server> --address … --simulate`
before the clock starts, checks it with `get_connection_info`, and stops it
when the run ends. A server's own output goes to a log file, not the terminal.
The run record (`rhylthyme runs`) keeps each call's reply and data; tool
addresses are replaced by `<tool address>` in every message.

## Planning durations

A step that ends on a reply may leave out `duration`. `rhylthyme plan` and
`rhylthyme analyze` then estimate one from the call's params, using the tool's
own defaults for params left out, and flag it in `metadata.durationEstimate`
(the timeline draws it as an estimate):

| Call | Estimate |
|---|---|
| a series (`count` or `timepoints`, `interval_s`) | `(count - 1) × interval_s` |
| a run (`duration_s`, `run_time_s`) | that, plus `equilibration_s` |
| a wait bounded by `timeout_s` | the timeout (an upper bound) |
| anything else (tare, set a speed) | 10 s |

## Live runs

`rhylthyme run … --live` starts each server without `--simulate` and shows a
pre-flight before anything is sent: each instrument's identity, mode and
active limits, and every hazard-kind action (heat, move, dispense, energise)
the run can send, with its params. The run starts only if every instrument
reports ready, and only after you type `live` (or pass `--confirm-live`).

## Development

```bash
pip install -e ".[dev]" -e ../rhylthyme-cli-runner
pytest -m "not integration"   # fakes only
pytest -m integration         # real LabMCP servers via uvx, simulated
```

## License

Apache-2.0. See [NOTICE](NOTICE) for LabMCP attribution.
