# The live viewer

The viewer is a web front end for place-and-route experiments. A running experiment writes
telemetry into a _live directory_; the viewer replays it into lanes (candidates, trials, rounds)
and shows each lane's board as it changes: native copper, provisional routes, checkpoints,
placement costs and the search's alternatives. Optional services add a schematic view, an
atopile source browser, design notes, a 3D view and an assistant. The viewer never changes the
experiment; the only thing it writes into the live directory is what you ask for (runtime
control requests, pins, annotation drafts and snapshots).

Code: `yapnr/viewer/`; its [README][viewer-readme] describes the layout and the tests.

[viewer-readme]: https://github.com/Studio-Fug/yapnr/blob/main/yapnr/viewer/README.md

## Running it

```sh
bazel run //:viewer -- --root runs/example/live            # http://127.0.0.1:8766
bazel run //:viewer -- --config viewer.toml --port 8795
```

Relative paths are relative to the directory `bazel run` was started from. The server listens on
`127.0.0.1` only, unless you add listeners (see [Network access](#network-access)).

From a plain checkout, build the static files once and run the package with Python 3.11 or
newer, with the engine on the import path:

```sh
bazel build //yapnr/viewer:dist
PYTHONPATH=.:hardware/pnr python3 -m yapnr.viewer --root runs/example/live \
    --dist bazel-bin/yapnr/viewer/dist
```

Without the assembled dist the viewer serves its bare static files: everything works except the
schematic layout (elkjs) and the 3D view (three.js), which say so in the page.

## The live directory

| Path                             | Written by     | What                                                  |
| -------------------------------- | -------------- | ----------------------------------------------------- |
| `events/*.json`                  | the experiment | immutable telemetry events (`pnr-live-event-v1`)      |
| `boards/<sha256>.kicad_pcb`      | the experiment | native board checkpoints, named by content hash       |
| `geometry/<sha256>.json`         | the viewer     | board geometry extracted with KiCad's Python (cached) |
| `control.json`                   | the viewer     | runtime control requests the experiment reads         |
| `pins/`, `drafts/`, `snapshots/` | the viewer     | pinned states and annotated snapshots                 |
| `schematic/`, `source/`, ...     | the viewer     | caches (`--cache-dir` moves them)                     |

The _experiment folder_ is the root's parent unless `--experiment-dir` says otherwise. Trial run
directories must lie under it; the viewers of one experiment share its 3D export lock, and an
optional `restart-status.json` there is shown in the page. Design notes default to
`<experiment>/notes`, control preferences to `<experiment>/viewer-preferences.json`.

## Configuration

Every setting has a flag; a TOML file (`--config`, schema `yapnr-viewer-v1`) holds the same
settings, with paths relative to the file. A flag wins over the file, the file over the default,
and every default is derived from the root: nothing points at a machine path.
`bazel run //:viewer -- --help` lists the flags; the file format is in the docstring of
`yapnr/viewer/config.py`. A typical file:

```toml
schema = "yapnr-viewer-v1"
root = "runs/example/live"

[server]
port = 8766
title = "Example board"

[design]
graph = "inputs/graph.json"          # netlist: schematic view, notes, source index
constraints = "inputs/constraints.yaml"

[design.atopile]
root = "hardware/example"            # the folder with ato.yaml
build = "default"                    # its build target: the entry module
```

Machine settings never go in that file. KiCad and the `claude` CLI come from flags, the
environment or the user's machine config, `~/.config/yapnr/config.toml` (or
`$XDG_CONFIG_HOME/yapnr/config.toml`; `YAPNR_USER_CONFIG` names another file, empty disables it):

```toml
[kicad]
cli = "~/Applications/KiCad-headless.app/Contents/MacOS/kicad-cli"
python = "~/Applications/KiCad-headless.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"

[agent]
claude = "/opt/claude/bin/claude"
```

## Design inputs and optional services

| Feature                       | Needs                                                              | Without it                                   |
| ----------------------------- | ------------------------------------------------------------------ | -------------------------------------------- |
| Board geometry of checkpoints | KiCad's Python (`--kicad-python`, `YAPNR_KICAD_PYTHON`)            | events without a layout show no board        |
| Placement cost inspection     | the engine's cost model (`--engine-runtime`, default: imported)    | the cost panel says unavailable              |
| Schematic view                | `--graph` (plus `--constraints`, `--parts` symbols)                | the schematic tab says unavailable           |
| Source browser (Inspect)      | atopile sources (`--atopile-root` + `--atopile-build`) and a graph | the Source tab says why; notes use the graph |
| 3D view                       | a headless `kicad-cli` (`--kicad-cli`, `YAPNR_KICAD_CLI`)          | the 3D pane says why                         |
| Design notes                  | nothing (on by default)                                            |                                              |
| Ask agent, AI net labels      | `--agent on`, `--net-summaries on` and the `claude` CLI            | off (the default)                            |

The atopile source browser reads the project's `ato.yaml`: `builds.<build>.entry` names the entry
module, and the source folder is that file's folder (`--atopile-src` overrides it; `--parts`
overrides `<src>/parts`). It joins the instance tree to the netlist by component address and pad
sets, so its net titles and summaries are mechanical. Without atopile sources the viewer still
runs: the schematic draws generic symbols, notes resolve targets against the netlist alone and
3D model paths are used as the board has them.

`--engine-runtime` points the subprocess services (cost replay, schematic builder) at a frozen
copy of the engine, the one a run was made with; the directory is the one that contains `pnr`.

### KiCad

The viewer runs KiCad only headlessly: KiCad's Python extracts board geometry (and the graph of
a routed checkpoint for the cost replay), and `kicad-cli` exports the 3D view's GLB. The order
is: the flag, the environment (`YAPNR_KICAD_CLI`, alias `PNR_KICAD_CLI`; `YAPNR_KICAD_PYTHON`),
the machine config, then discovery: on macOS only the headless copy in
`~/Applications/KiCad-headless.app` (make it as in [DEVELOPERS.md](../DEVELOPERS.md#kicad)), on
Linux `kicad-cli` on `PATH` and a `python3` that imports `pcbnew`. The GUI application is never
used: a path inside `KiCad.app` or `/Applications/KiCad`, or inside any application bundle that is
not background-only, is refused, because every call would put an icon in the Dock.

## Network access

By default the viewer listens on `127.0.0.1`. To reach it from another machine, add a listener and
the origin the browser will use, for example:

```sh
bazel run //:viewer -- --root runs/example/live --listen 127.0.0.1 --listen 192.0.2.10 \
    --allow-origin http://192.0.2.10:8766 --allow-origin http://viewer.example.com:8766
```

- **Host guard (DNS rebinding):** a request must carry a `Host` that is a loopback name, a
  `--listen` address, an `--allow-origin` host (or its first label) or an `--allow-host` name;
  anything else gets 421.
- **Origin check:** writes need an allowlisted `Origin` (`http://127.0.0.1:<port>`,
  `http://localhost:<port>`, `--allow-origin`). The Ask agent and every notes write reject a
  missing `Origin` too.
- Everyone who reaches the port can see the experiment, the design sources you configured and
  the notes and stored conversations, and can write notes as "the user" (recorded with their
  address). The Origin check stops other web pages, not a client that forges the header. Expose the
  viewer only on networks you trust, and never to the internet.

## The Ask agent and AI net labels (off by default)

Both are **off unless you turn them on**, also on a loopback listener:

- `--agent on` enables **Ask**: a read-only assistant that answers questions about the selection
  (parts, nets, pads, regions, source lines, lanes, events). Each question is one headless
  `claude -p` run. `--agent-web on` also lets it use WebSearch and WebFetch.
- `--net-summaries on` adds **AI net labels**: one tool-less `claude` call per changed netlist
  dossier (about $0.20 for a hundred nets with sonnet), labelled as AI output in Inspect.

What that means:

- **Cost.** Every turn and every labelling call is paid on the operator's Claude account (the
  `claude` CLI's login). Spend is capped per turn (`--agent-budget-usd`, default $2) and per server
  process (`--agent-total-usd`, default $20; the assistant stops when it is used up). The default
  model is opus (`--agent-model sonnet` is cheaper).
- **Reads.** The agent can read the working directory (`--agent-cwd`, default the experiment
  folder) and the folders you allow (`--agent-read-dir`, default the atopile sources and the
  experiment folder). It has no shell and no write tools; its only write path is the design-notes
  tools of a per-turn MCP server, and it can only propose, never accept, a change. A turn whose
  CLI reports any other tool or server is killed.
- **Web.** With `--agent-web on` fetched pages are untrusted input: a page can try to steer the
  model (prompt injection). WebFetch goes through a guard hook that allows public hosts only
  (loopback, private, link-local, tailnet and `.local` names are refused, also after DNS
  resolution), and the prompt forbids putting design data into URLs or queries, but a model can
  still be misled. Leave web off unless you need datasheets.
- **Exposure.** On a non-loopback listener anyone who reaches the port can run paid turns and
  read what the agent can read; the server warns at startup. Do not enable the agent there unless
  you understand and accept that.

## Design notes

Notes (`<experiment>/notes`, shared by all viewers of an experiment) record observations,
questions, requirements, decisions and proposals, with targets on the board or in the source.
People create, edit, accept, reject, apply and delete them in the Notes tab; the Ask agent can
only add notes, comment and edit its own open notes. `design-notes.md` in the same folder is the
feed for the next design pass, and `python -m yapnr.viewer.notes.store report --dir <notes>`
prints it (`--status accepted`, `--json`).

## HTTP interface

| Route                                                                    | What                                          |
| ------------------------------------------------------------------------ | --------------------------------------------- |
| `GET /api/state?lane=&since=&run=`                                       | lanes, events, errors (`unchanged` when idle) |
| `GET /api/geometry/<sha256>`                                             | a checkpoint's geometry                       |
| `GET, POST /api/controls`                                                | runtime controls (requested, active, limits)  |
| `POST /api/pin`, `/api/draft`, `/api/snapshot`                           | immutable pins, drafts, annotated snapshots   |
| `GET /api/component-cost?event_id=&ref=`                                 | placement cost replay                         |
| `GET /api/schematic?lane=[&scope=board]`, `/api/schematic/payload/<key>` | the schematic model                           |
| `GET /api/source/index`, `/api/source/file?path=`                        | the atopile source index and files            |
| `GET, POST /api/notes…`, `GET /api/notes/export?format=md\|json`         | design notes                                  |
| `GET /api/agent/status`, `POST /api/agent/chat`, `/api/agent/cancel`     | the Ask agent (server-sent events)            |
| `GET /api/3d?lane=&phase=`, `/api/3d/glb/<key>`, `/api/3d/status`        | the 3D view                                   |
| `GET /api/about`                                                         | version, source repository and revision       |

## Source code and license

The page header links to the viewer's source (AGPL-3.0-or-later, section 13), with the revision
the server runs (`YAPNR_SOURCE_REVISION`, or the git checkout). The front end loads two
third-party libraries, served unmodified next to their licenses: elkjs (EPL-2.0) and three.js
(MIT); see [THIRD_PARTY.md](../THIRD_PARTY.md).
