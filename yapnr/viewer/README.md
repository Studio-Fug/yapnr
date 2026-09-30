# yapnr.viewer

The live viewer: a web front end for place-and-route experiments. It replays the telemetry an
experiment writes (a _live directory_) into lanes and serves them with the static front end in
`static/`, plus optional services: placement cost inspection, a schematic view, an atopile source
browser, design notes, a 3D view, and an Ask agent and AI net labels (both **off by default**;
paid). The operator's guide is [docs/viewer.md](../../docs/viewer.md): configuration, the optional
services, network access and the cost and security of the agent.

```sh
bazel run //:viewer -- --root runs/example/live       # http://127.0.0.1:8766
bazel run //:viewer -- --help
```

## Layout

| Path              | What                                                                |
| ----------------- | ------------------------------------------------------------------- |
| `server.py`       | the HTTP server (`Viewer`, the request handler, `main`)             |
| `config.py`       | flags and the `yapnr-viewer-v1` TOML file; no machine defaults      |
| `toolchain.py`    | the headless `kicad-cli` and KiCad's Python; the GUI app is refused |
| `runtime.py`      | the engine runtime and the environments of child processes          |
| `event_schema.py` | normalization of completed-phase events                             |
| `settings.py`     | runtime control requests and saved preferences                      |
| `services/`       | cost replay, schematic builder and symbols, 3D export queue         |
| `sources/`        | the atopile source index (entry from the ato.yaml build target)     |
| `notes/`          | the design notes store and the Ask agent's notes MCP server         |
| `agent/`          | the Ask agent, AI net labels and the WebFetch guard hook            |
| `kicad_scripts/`  | geometry and graph extraction, run by path under KiCad's Python     |
| `static/`         | the front end (plain JavaScript, no build step)                     |
| `testing.py`      | helpers for the tests (not used by the viewer)                      |
| `COSTS.md`        | what the placement cost panel shows (also offered to the Ask agent) |

Processes and interpreters:

- The server runs on the hermetic Python 3.11 and imports the engine (`pnr`) in-process.
- The cost replay (`services/cost_compute*.py`), the schematic builder
  (`services/schematic_build.py`), the notes MCP server and the 3D export job run as
  `python -m yapnr.viewer...` children with `PYTHONPATH` = the engine runtime + the server's
  `sys.path` (`runtime.hermetic_env`). `--engine-runtime` puts a frozen engine first.
- `kicad_scripts/*.py` run under KiCad's Python (3.9 on macOS) with only the engine runtime on
  the path (`runtime.kicad_env`): stdlib, `pcbnew` and `pnr.ingest`, nothing GUI-bound.
- The WebFetch guard hook (`agent/web_guard.py`) is stdlib-only and imports nothing from the
  package, so a half-edited package cannot break it; the Claude CLI runs it by path.

The served directory is `//yapnr/viewer:dist`: `static/` plus elkjs and three.js, fetched pinned
by Bazel and copied unmodified ([THIRD_PARTY.md](../../THIRD_PARTY.md)). Without it (a plain
checkout that has not built it) the schematic layout and the 3D view report their library as
missing; `--dist bazel-bin/yapnr/viewer/dist` serves a built copy.

## Tests

```sh
bazel test //tests/unit/viewer/...
```

The unit tests are offline and free: fake `claude` and `kicad-cli` programs stand in for the
real ones, and `tests/fixtures/viewer/design` is a small synthetic atopile project (regulator,
capacitors, a pull-up) with its netlist graph, rules, constraints and two symbol libraries. They
cover the server's routes and origin checks, ingestion, pins and snapshots, the source index, the
schematic builder, notes and the MCP server, the agent's command line and stream handling, the web
guard, the 3D export queue, the cost replay (with torch), the configuration, the toolchain rules
and the served files.

`tests/e2e/viewer` holds the live checks, tagged `manual`: real Ask turns and net labels (paid;
`YAPNR_AGENT_LIVE=1`, `YAPNR_NET_LLM_LIVE=1`) and a real headless KiCad export
(`YAPNR_V3D_LIVE=1`, `YAPNR_V3D_BOARD`).

There is no JavaScript test runner: the earlier node tests read a captured state from the old
repository's paths and are not ported, and yapnr has no node toolchain. Browser checks through the Chrome
DevTools protocol are planned for `tests/e2e`; until then check changes to `static/` in a
browser, for example against a dev server on a spare port (`--port 8795`).
