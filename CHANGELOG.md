# Changelog

## 0.1.0a0 - 2026-10-05

First release: LabMCP instrument servers as Rhylthyme workcell tools
(`"driver": "labmcp"`), with rhylthyme-cli-runner 0.4.0a0 (`pip install
"rhylthyme[labmcp]"`).

- Servers are started with uvx at the catalogued version, simulated unless
  the run is live, or reached over streamable HTTP (`url`); limits and
  options from the workcell; addresses scrubbed from every message.
- A pinned catalogue of every LabMCP server's tools, input schemas and
  limits (`scripts/export_catalog.py`), and checks against it: tools,
  params, bounds, limits (including limits not named after their param, and
  series durations), server command policies (SiLA 2 allow-list, SCPI
  allow/deny lists, Modbus raw writes) and pump timing.
- Planning estimates from params: doses, series, runs, waits.
- Default safe stops from each server's safety tools, stop-type first;
  pause/resume tools for holding work while the schedule is paused.
- galago-compatible Opentrons commands (`run_program`, `pause`, `resume`,
  `cancel`) on LabMCP's Opentrons server.
- Parity cases that pin the hosted validator (rhylthyme-server
  `mcp-api/labmcp.js`) to these checks and estimates.
- Examples: titration, syringe-pump dosing, an Opentrons transfer (LabMCP or
  galago), SiLA 2 / SCPI / Modbus devices, and a mixed LabMCP + galago bench.
- A guide: `docs/guide.md`.
