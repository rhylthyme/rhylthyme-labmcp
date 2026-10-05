# rhylthyme-labmcp guide

Schedule real lab instruments with [Rhylthyme](https://rhylthyme.com): a
program says *what* happens and *when*; each instrument step is carried out
by a **[LabMCP](https://github.com/K-Dense-AI/lab-instrument-mcps)** server,
K-Dense's open-source MCP servers for balances, stirrers, pumps, sensors,
spectrometers, liquid handlers and generic SiLA 2 / SCPI / Modbus devices.
Everything runs **simulated** unless you ask for a live run.

- [Install](#install)
- [Quick start (simulated)](#quick-start-simulated)
- [The workcell](#the-workcell)
- [Writing instrument steps](#writing-instrument-steps)
- [Checking a program before it runs](#checking-a-program-before-it-runs)
- [Running](#running)
- [Limits, safe stops and pausing](#limits-safe-stops-and-pausing)
- [Live runs](#live-runs)
- [Watching and steering from rhylthyme.com](#watching-and-steering-from-rhylthymecom)
- [Servers on another machine](#servers-on-another-machine)
- [Troubleshooting](#troubleshooting)
- [Examples](#examples)
- [The catalogue](#the-catalogue)

## Install

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/) (it downloads and
starts each LabMCP server for you):

```bash
# uv, once (or: brew install uv)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Rhylthyme with LabMCP support
pip install "rhylthyme[labmcp]"
```

`rhylthyme[labmcp]` installs the `rhylthyme` command and this package. You
do not install LabMCP servers yourself: the runner starts the version each
tool needs with `uvx` (the first start of a server downloads it, which can
take a minute). galago-tools instruments can share the same workcell
(`pip install "rhylthyme[galago]"`).

## Quick start (simulated)

```bash
curl -sO https://raw.githubusercontent.com/rhylthyme/rhylthyme-labmcp/main/examples/titration.json
curl -sO https://raw.githubusercontent.com/rhylthyme/rhylthyme-labmcp/main/examples/workcell-titration.json

rhylthyme validate titration.json --workcell workcell-titration.json
rhylthyme run titration.json --workcell workcell-titration.json
```

The runner starts three simulated servers (a Mettler Toledo balance, an IKA
hotplate stirrer and an Atlas Scientific pH probe), checks each one, then runs
the schedule: tare, weigh, heat and stir for two minutes, log pH until the
series is done, stop. Each instrument step shows a `[tool]` badge and the tool
it is waiting on; `p` pauses, `q` quits. The run record (`rhylthyme runs`)
keeps every call and reply, with the data each instrument returned.

## The workcell

A **workcell** is a local JSON file that maps the tool names a program uses
to real instruments. It never leaves the machine: programs are shared, the
workcell is not, and Rhylthyme scrubs its addresses from everything it
publishes.

```json
{
  "id": "titration-bench",
  "name": "Titration bench",
  "tools": [
    { "name": "balance", "driver": "labmcp", "server": "mettler-toledo", "address": "/dev/ttyUSB0" },
    { "name": "stirrer", "driver": "labmcp", "server": "ika", "address": "/dev/ttyUSB1",
      "limits": { "max_temperature_c": 80 } },
    { "name": "ph", "driver": "labmcp", "server": "atlas-ezo", "address": "/dev/ttyUSB2" }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | The tool name programs use (`"tool": "balance"`) |
| `driver` | `labmcp` for LabMCP servers; galago-tools tools leave it out |
| `server` | The LabMCP package, with or without `labmcp-` (`ika`, `labmcp-ika`); pin a version with `ika==0.1.1`. `uvx --from labmcp labmcp list` lists them all |
| `address` | Where the instrument is, as LabMCP expects it: `/dev/ttyUSB0`, `COM3`, `serial:///dev/ttyUSB0?baudrate=9600`, `tcp://192.168.1.50:5025`, a VISA resource, … (`uvx --from labmcp labmcp ports` lists serial ports and VISA instruments) |
| `limits` | Safety limits, `{name: number}`: passed to the server as `--limit`; it refuses anything beyond them |
| `options` | Server options, `{name: value}`: passed as `--option` (e.g. a syringe size, an SCPI denylist) |
| `timeoutSeconds` | Fail a call that has not replied after this long |
| `startTimeoutSeconds` | How long a server may take to start (default 180 s) |
| `url` | A server already running over HTTP, instead of starting one (see [Servers on another machine](#servers-on-another-machine)) |
| `description` | For people |

Each server's limits and options are in its LabMCP page; `rhylthyme validate
--workcell` checks the names you use. Without `server` pinned to a version,
the runner starts the version the [catalogue](#the-catalogue) was made from,
so what you validated is what runs.

## Writing instrument steps

A step's `instrument` names a workcell tool and what to send it.

**One call.** The step starts the call and ends when the instrument replies:

```json
{ "stepId": "tare", "name": "Tare the balance",
  "instrument": { "tool": "balance", "command": "tare" },
  "startTrigger": { "type": "programStart" } }
```

`command` is one of the server's tools and `params` its arguments, exactly as
the server describes them. Without a `duration`, the step is planned with an
estimate and ends on the reply.

**Phase actions.** A step can also send actions around its own timer, each
`{command, params, tool?}` (`tool` defaults to the step's):

| Phase | When |
|---|---|
| `start` | In order, when the step starts (and again on retry) |
| `until` | One call whose reply ends the step, instead of `command` |
| `end` | In order, when the step ends: by timer, by the operator, or on a reply |
| `onAbort` | If the step fails or the run is aborted, instead of the default safe stops |

```json
{ "stepId": "heat-stir", "name": "Heat to 40 °C and stir for 2 minutes",
  "duration": { "type": "fixed", "seconds": 120 },
  "instrument": { "tool": "stirrer",
    "start": [ { "command": "set_temperature", "params": { "temperature_c": 40 } },
               { "command": "start_heating" }, { "command": "start_stirring" } ],
    "end":   [ { "command": "stop_heating" } ] } }
```

```json
{ "stepId": "log-ph", "name": "Stir gently and log pH",
  "instrument": { "tool": "ph",
    "start": [ { "tool": "stirrer", "command": "set_speed", "params": { "speed_rpm": 150 } } ],
    "until": { "command": "log_series", "params": { "count": 10, "interval_s": 3 } } } }
```

A step holds every tool its actions name, so no other step commands them
meanwhile. A step with only `start`/`end` actions runs on its own `duration`
(without one, until the operator ends it; `validate` warns).

**Without a workcell.** Give the step `"toolType": "labmcp-ika"` (the server
package) and `rhylthyme validate`, `plan` and the hosted MCP server check and
time it without knowing your lab.

**Syringe pumps.** Some tools reply as soon as they *start* acting. A New Era
pump's `infuse` and `withdraw` return once pumping begins, so as a `command`
the step would end with the pump still running: send them as a `start` action
and give the step a duration at least as long as the dose (volume ÷ rate).
`validate` catches both mistakes. A Cavro pump's `aspirate_ul` and
`dispense_ul` reply when the move is done and can be a step's `command`.

**Opentrons.** A program written for galago-tools' Opentrons tool runs on
LabMCP's Opentrons server by changing only the workcell. `run_program`
(`params.script_content`, the protocol's Python) uploads the protocol, starts
a run and waits until it ends; the step fails unless the run succeeds.
`pause`, `resume` and `cancel` are `pause_run`, `resume_run` and `stop_run`.
The deck must already match the protocol.

**SiLA 2, SCPI and Modbus devices.** LabMCP's bridge servers let a step send
generic commands (`call_command` on a SiLA 2 feature, `scpi_write`,
`write_point`), so narrow them in the workcell's `options`, and the server
refuses everything else with nothing sent: SiLA 2 `command_allowlist`
(`"TemperatureController.*"`), SCPI `write_denylist` / `write_allowlist` /
`query_denylist` (regular expressions) and `safe_state`, Modbus
`raw_writes: "false"` (named points only). SCPI patterns are matched against
each command as sent and in its short form: `:SOURce:VOLTage 24` is
`SOUR:VOLT 24`, so write `(^|:)VOLT` rather than `^VOLT` to catch it under any
subsystem.

## Checking a program before it runs

```bash
rhylthyme validate program.json --workcell lab.json
```

checks every call each step can send, in every phase, against the
[catalogue](#the-catalogue) of the server its tool runs:

- the server has the tool, and the params fit its input schema (unknown or
  missing params, types, bounds, choices);
- values stay within the safety limits: the workcell's, else the server's
  defaults (`temperature_c=120` against `max_temperature_c=80`; a series'
  `(count - 1) × interval_s` against `max_series_duration_s`);
- the server's command policy (allow and deny lists) would accept it;
- doses outlast their steps, and tools that reply early do not end a step;
- the workcell itself: known servers, known limits, valid policies.

```
[instrument_over_limit] Step 'heat-stir': stirrer (labmcp-ika) set_temperature: temperature_c=120 is above the workcell's limit max_temperature_c=80 °C
[instrument_refused_by_policy] Step 'psu-load': psu (labmcp-scpi) scpi_write: Refused: 'VOLTage 24' matches the command denylist ...
```

Servers enforce all of this again at run time; validation only moves the
refusal before the run.

**Planning.** A step that ends on a reply and has no duration is timed by an
estimate from its params, flagged in `metadata.durationEstimate` (the
timeline draws it with `≈`):

| Call | Estimate |
|---|---|
| a dose (`volume_ml` at `rate_ml_min`, `volume_ul` at `flow_ul_s`) | volume ÷ rate |
| a series (`count` or `timepoints`, `interval_s`) | `(count - 1) × interval_s` |
| a run (`duration_s`, `run_time_s`) | that, plus `equilibration_s` |
| a wait bounded by `timeout_s` or `wait_s` | the bound (an upper bound) |
| an Opentrons `run_program` | 600 s (give the step a duration) |
| anything else | 10 s |

`rhylthyme plan program.json planned.json --workcell lab.json` writes them
into a copy of the program. The hosted MCP server (`validate_program`,
`analyze_schedule`, `visualize_schedule`) applies the same checks and
estimates to steps that name their `toolType`, with the servers' default
limits.

## Running

```bash
rhylthyme run program.json --workcell lab.json
```

Before the clock starts, the runner starts one server per tool the program
uses (`uvx labmcp-<server> --address … --simulate`), asks each what it is
connected to, and refuses to start unless every one is ready. Each server's
own output goes to a log file (`$TMPDIR/labmcp-<tool>-*.log`), not your
terminal. When the run ends, the servers are stopped.

The run record keeps, per step, every call and reply: its phase, tool,
command, code, any error, and the data the instrument returned. Addresses are
replaced by `<tool address>` in every message.

Instruments run in real time. `--time-scale` speeds up the schedule's clock,
not the hardware: a timed step (a pump dose, a heating hold) ends early on a
faster clock while the instrument keeps going.

## Limits, safe stops and pausing

**Limits.** Set them in the workcell; the server refuses any request beyond
them before it reaches the instrument, and the step fails with its message.

**When a step fails** (an error reply, a refusal, a timeout), the runner sends
its `onAbort` actions, or else every touched instrument's **default stops**:
its server's safety tools that need no arguments, the ones that stop first
(`stop_all`, `stop_pump`, `stop_run`, `terminate`, then switching modules
off), never `pause` and never `reconnect`. Then it offers retry (`r`, which
waits until the stops have replied), mark done (`x`) or abort (`A` twice).

**Aborting** (here or from rhylthyme.com), and quitting a run part-way, stops
every instrument the run has used since it was last stopped. Every stop and
its reply is in the run record.

**Pausing** the schedule (`p`) also pauses instruments that can hold their
work and resume it (an Opentrons run, with `pause_run`/`resume_run`);
resuming continues them. Others carry on while the schedule's clock is
stopped.

## Live runs

```bash
rhylthyme run program.json --workcell lab.json --live
```

A live run starts the servers without `--simulate` and shows a pre-flight
before anything is sent: each instrument's identity (make, model, serial),
mode and active limits, and every hazard-kind action the program can send
(heating, moving, dispensing, energising) with its parameters. It refuses to
start if any instrument is unreachable, not connected, in the wrong mode or
not enforcing the workcell's limits; otherwise it asks you to type `live`
(`--confirm-live` answers in scripts). For an Opentrons protocol, typing
`live` also confirms that the deck matches it.

Before the first live run of a program: run it simulated, check
`rhylthyme validate --workcell`, set limits for anything that heats, moves or
pumps, and read the pre-flight's hazard list.

## Watching and steering from rhylthyme.com

```bash
rhylthyme login
rhylthyme bridge program.json --workcell lab.json      # one run, shown live
rhylthyme bridge --workcell lab.json [--allow-live]    # wait for runs started from the web
```

The Bridges page shows every step, the tool it waits on and any failure; you
can pause, resume, retry or skip a failed step, and abort (which sends the
safe stops). Tools are listed by name, server and status; addresses, URLs and
options never leave the machine. Live runs from the web need both
`--allow-live` here and the typed `live` in the browser.

## Servers on another machine

A tool may point at a LabMCP server that is already running over streamable
HTTP, on another computer or in a container:

```bash
# on the instrument's computer
uvx labmcp-mettler-toledo --address /dev/ttyUSB0 --transport http --host 0.0.0.0 --port 8000
```

```json
{ "name": "balance", "driver": "labmcp", "server": "mettler-toledo",
  "url": "http://lab-pc.local:8000/mcp", "limits": { "max_series_duration_s": 300 } }
```

The runner connects instead of starting anything, and never stops that
server. `server` is optional with `url` and only names the package, for
checks and estimates. The server keeps the mode and limits it was started
with, so the pre-flight checks them: a simulated run needs a simulated
server, a live run a live one, and every workcell limit must be enforced at
least as tightly (else `WRONG_LIMITS`). `options` cannot be applied to a
running server; set them where it starts.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `LabMCP servers are launched with uvx, which was not found` | Install uv (above), or set `RHYLTHYME_UVX` to its path |
| The first run waits a long time at start-up | `uvx` is downloading the server; later starts are quick. Raise `startTimeoutSeconds` on a slow network |
| `OFFLINE: the server did not answer within 180 s` | See the log lines in the message, and `$TMPDIR/labmcp-<tool>-*.log` |
| `NOT_CONNECTED: Could not open serial port '<tool address>'` (live) | Wrong port, cable, or another program has it open; `uvx --from labmcp labmcp ports` lists ports |
| `WRONG_MODE: the server is not live` | A `url` server was started with `--simulate` (or the reverse) |
| `WRONG_LIMITS: the server's limits are not the workcell's` | Start the `url` server with the workcell's `--limit` values |
| `could not reach the server at <tool address>` | The `url` server is not running, or a firewall blocks it |
| A step fails with `Refused: …` | A limit or the server's policy refused it, with nothing sent: change the step, or the workcell if the request is intended |
| `instrument_returns_early` | Send that call as a `start` action and give the step a duration |
| A dose or hold ends early | You ran with `--time-scale`: instruments run in real time |
| `Instrument steps on labmcp tools need rhylthyme-labmcp` | `pip install "rhylthyme[labmcp]"` |
| `… is not in the LabMCP catalogue` | A server newer than this package's catalogue: its steps are not checked before the run (the server still checks them). Upgrade rhylthyme-labmcp |
| galago's Opentrons fails with `unexpected keyword argument 'simulated'` | A galago-tools 0.19.9 bug in its simulated mode; LabMCP's Opentrons server simulates fine |

## Examples

All in [`examples/`](../examples), each with its workcell, all simulated:

| Program | Workcell | Shows |
|---|---|---|
| [`titration.json`](../examples/titration.json) | `workcell-titration.json` | Balance, hotplate stirrer and pH probe; `start`/`end` around a timer, an `until` on another tool |
| [`dosing.json`](../examples/dosing.json) | `workcell-dosing.json` | New Era (`infuse` as a start action) and Cavro (`dispense_ul` as a command) syringe pumps |
| [`opentrons-transfer.json`](../examples/opentrons-transfer.json) | `workcell-opentrons-labmcp.json` or `workcell-opentrons-galago.json` | One Opentrons program, either backend |
| [`protocol-bridges.json`](../examples/protocol-bridges.json) | `workcell-protocol-bridges.json` | SiLA 2, SCPI and Modbus devices, each with a command policy |
| [`tare-and-shake.json`](../examples/tare-and-shake.json) | `workcell-mixed.json` | A LabMCP balance and a galago-tools shaker in one workcell |

## The catalogue

`rhylthyme validate`, the estimates and the hosted MCP server read a
catalogue of every LabMCP server's tools, input schemas and safety limits,
exported from each server run with `--simulate` and pinned to their versions
(`src/rhylthyme_labmcp/catalog/labmcp-catalog.json`, with LabMCP's
attribution). To refresh it:

```bash
python scripts/export_catalog.py --refresh   # newest LabMCP, re-pinned
python scripts/export_check_cases.py         # the cases that pin the hosted validator
```

then copy both files to `rhylthyme-server/mcp-api/` (`tools/check_mirrors.sh`
in the monorepo fails until they match).

LabMCP is by [K-Dense](https://www.k-dense.ai) (Apache-2.0); this package is
independent of it. See [NOTICE](../NOTICE).
