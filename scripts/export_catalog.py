"""Export the LabMCP tool catalogue that rhylthyme validate checks steps against.

    python scripts/export_catalog.py            # re-export the pinned versions
    python scripts/export_catalog.py --refresh  # newest LabMCP: re-pin, re-export

Every LabMCP server in the catalogue is launched with ``uvx <package>==<version>
--simulate`` and asked for its tools (name, kind, input schema, description)
and its safety limits (``get_connection_info``). Nothing touches hardware.

Without --refresh the package list and versions come from the vendored
catalogue, so the output is reproducible. With --refresh the package list
comes from the newest ``labmcp`` core release and each package's version from
PyPI. The output has no timestamps and sorted keys.
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

from rhylthyme_labmcp import ServerError, StdioServerClient, find_uvx

CATALOG = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "rhylthyme_labmcp"
    / "catalog"
    / "labmcp-catalog.json"
)
REPOSITORY = "https://github.com/K-Dense-AI/lab-instrument-mcps"
ATTRIBUTION = (
    "Tool names, kinds, descriptions, input schemas and safety limits are "
    "exported from LabMCP (https://github.com/K-Dense-AI/lab-instrument-mcps), "
    "by K-Dense, licensed under the Apache License, Version 2.0. Exported by "
    "rhylthyme-labmcp/scripts/export_catalog.py from each server run with "
    "--simulate; not modified except for selection and layout."
)
START_TIMEOUT = 300.0


def labmcp_servers() -> Tuple[str, List[str]]:
    """The newest labmcp core release and the server packages it lists."""
    code = (
        "import importlib.metadata as m, json\n"
        "from importlib import resources\n"
        "c = json.loads(resources.files('labmcp').joinpath('catalog.json').read_text())\n"
        "print(json.dumps({'version': m.version('labmcp'),"
        " 'packages': [s['package'] for s in c['servers']]}))\n"
    )
    out = subprocess.run(
        [
            find_uvx(),
            "--refresh-package",
            "labmcp",
            "--from",
            "labmcp",
            "python",
            "-c",
            code,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data = json.loads(out.strip().splitlines()[-1])
    return data["version"], sorted(data["packages"])


def pypi_version(package: str) -> str:
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/{package}/json", timeout=30
    ) as r:
        return json.load(r)["info"]["version"]


def export_server(package: str, version: str) -> Dict[str, Any]:
    client = StdioServerClient(
        find_uvx(), [f"{package}=={version}", "--simulate"], name=package
    )
    try:
        client.start(START_TIMEOUT)
        tools = client.list_tools(120)
        info = client.call("get_connection_info", {}, 120)
    except ServerError as e:
        return {"version": version, "error": str(e).splitlines()[0]}
    finally:
        client.close()
    limits = {}
    for name, limit in sorted((info.data.get("safety_limits") or {}).items()):
        limits[name] = {
            k: limit[k]
            for k in ("default", "unit", "kind", "description")
            if k in limit
        }
    return {
        "version": version,
        "server": info.data.get("server", ""),
        "limits": limits,
        "tools": {
            t["name"]: {
                "kind": t["kind"],
                "description": t["description"].strip(),
                "inputSchema": t["inputSchema"],
            }
            for t in sorted(tools, key=lambda t: t["name"])
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--refresh", action="store_true", help="re-pin to the newest LabMCP"
    )
    parser.add_argument("--output", type=Path, default=CATALOG)
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args(argv)

    if args.refresh or not args.output.exists():
        core, packages = labmcp_servers()
        pins = {p: pypi_version(p) for p in packages}
    else:
        old = json.loads(args.output.read_text())
        core = old["source"]["labmcp"]
        pins = {p: entry["version"] for p, entry in old["packages"].items()}

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = dict(
            zip(pins, pool.map(lambda p: export_server(p, pins[p]), list(pins)))
        )
    catalog = {
        "schema": 1,
        "source": {
            "name": "LabMCP",
            "repository": REPOSITORY,
            "license": "Apache-2.0",
            "labmcp": core,
            "attribution": ATTRIBUTION,
        },
        "packages": {p: results[p] for p in sorted(results)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    )
    failed = [p for p, r in results.items() if "error" in r]
    tools = sum(len(r.get("tools", {})) for r in results.values())
    print(f"{args.output}: {len(results) - len(failed)} servers, {tools} tools")
    for p in failed:
        print(f"  {p}=={results[p]['version']}: {results[p]['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
