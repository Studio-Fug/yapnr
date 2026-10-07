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

For a campaign running on `gcp-batch` or `slurm` ([cloud-experiments.md](cloud-experiments.md)),
there is no local directory to point at until `[live]` is on in the campaign file and
`yapnr exp live <plan>` mirrors the tasks' telemetry back from the runs store; point `--root` at
its `--out` directory instead (the "Live viewer mirror" section has the details).

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

## Touch and panels

**Touch** (Android Chrome, iOS Safari, or a trackpad/mouse — behaviour is unchanged for those):
pinch with two fingers to zoom the board, the schematic and the 3D view, anchored at the pinch
centre; a two-finger drag pans; double-tap zooms in (and back out, toggling) centred on the tap.
One-finger drag keeps its existing meaning on each surface: it pans the board or schematic, except
while the rectangle-annotation or "Ask region" tool is armed, where it draws that rectangle, same
as with a mouse. The 3D view's orbit/pan/zoom is three.js `OrbitControls`, wired for touch
(`controls.touches`) the same way. Every drawing surface sets CSS `touch-action: none` so the
browser's own page-level pinch-zoom and scroll never fight the gesture; `static/touch.js` holds
the shared pinch/double-tap math, used by `static/app.js` (the board canvas) and
`static/schematic.js` (the schematic SVG).

**Dockable panels**: the Experiment lanes column, the Exploration column (tree, placement costs,
events) and the Inspect/Source/Ask/Notes dock can each be closed and reopened from the small
toolbar in the header (`static/panels.js`, `static/dock.css`); with all three closed the board (or
schematic, or 3D view) fills the window. The Inspect/Source/Ask/Notes panel can also collapse to a
thin rail or be resized (drag its left edge); it is docked to the right edge (not yet to the left
or bottom — a follow-up). Layout (open/closed, the dock's width and last tab) persists per browser
in `localStorage` (wrapped in `try`/`catch`, so a private window or blocked storage degrades to
"nothing remembered", never an error), with a **Reset layout** button next to the toggles. The
Experiment lanes and Exploration panels can each also collapse to a thin rail (mirroring the
Inspect/Source/Ask/Notes dock) and dock to either the left or right edge, independent of one
another, on a wide screen. At ≤900px width (phone and small-tablet widths — a laptop keeps both
columns) every panel becomes a fixed overlay drawer instead of a grid column, closed by default,
so a phone opens straight onto the maximized board; opening one slides it in over the board rather
than squeezing the layout. The Inspect/Source/Ask/Notes dock has its own, wider breakpoint
(≤1360px), since it is a fourth column rather than two of three. A drawer paints a solid, themed
background — it must never let the board underneath show through — and a scrim dims the board
behind it and closes it on a tap outside. Every toggle is a real `<button>`
(keyboard-activatable, `aria-pressed` reflects state), and the page keeps no horizontal scroll down
to 360px wide.

## The experiment tree

The Experiment lanes panel (`#lanes`) is a collapsible, directory-like tree of every lane the
live directory has — a campaign's arms, a board case, a seed, and its placement/routing
candidates — built from the lane ids themselves, not hard-coded to one campaign's shape. A lane
id is slash-separated and already hierarchical in practice (`ladder/keep-on/09-mcu-usb-31-6L-
SGSGPS/s0/initial-start-04`, a bare `controller`, the real `pnr.mc.halving` shape
`<parent>/<candidate>/<stage>` such as `mc/cand03/screen`, or radar's flat `mc/s0`..`mc/s3` with
no per-stage nesting at all); `yapnr/viewer/lane_tree.py`'s `build_tree()` splits each id on `/`
and walks a trie
into a nested tree, shipped once per poll as `state["tree"]` (`/api/state`) so the front end never
re-derives the hierarchy. A lane id that is itself a prefix of other lane ids (`.../s0` is both a
lane and the parent of `.../s0/initial-start-00`) becomes a node that is simultaneously a leaf and
a group; there is no fixed depth limit. The candidate-search audit's own bookkeeping lanes
(`r03/search`) are excluded, same as the old flat list excluded them.

Every node carries `counts` (leaf-descendants by state — `running`/`stalled`/`failed`/`rejected`/
`queued`/`done`, see `lane_tree.STATES` — and a `total`) and a `fraction` (the mean of every
descendant leaf's own progress fraction — a group with 3 lanes done and 1 lane half-routed reads
as 0.875, not just "3 of 4 done") and `best_leaf` (the most relevant descendant: whatever is still
actively happening ranks first — running, then stalled — ahead of a terminal outcome; among
terminal outcomes, failed, then rejected, then queued, then done last — "select this group, show
one board"; deliberately _not_ "failed first": a single failed leaf in an otherwise-running group
must not steal the selection away from the work actually in progress). `static/lane-tree.js`
renders it: a toolbar (name filter, state filter chips, Expand
all/Collapse all) built once, then the row list, rebuilt each poll. Expand/collapse persists per
node per browser in `localStorage` (`yapnr-tree-expand-v1:<run>`, wrapped in `try`/`catch`); a
fresh page opens the top two levels and leaves deeper candidates collapsed. Arrow keys move
between visible rows, Right expands/descends, Left collapses/ascends, Enter selects a leaf or
toggles+selects a group's best leaf (roving `tabindex`, `role="tree"`/`"treeitem"`,
`aria-expanded`). Filtering by name or by state keeps a non-matching group visible whenever a
descendant of it matches, so a hit stays reachable without permanently changing what was left
collapsed. At ≤900px width it lives in the Experiments drawer like the rest of `#lanes-panel`
(see "Touch and panels" above).

### The phase/progress model

One place, `yapnr/viewer/progress.py`, turns a lane's raw telemetry into a progress bar and a
status string; nothing else in the viewer derives a phase from an event. Every lane is placed on
one canonical, ordered pipeline (`PHASES`), independent of which campaign produced it — a
campaign that never visits a stage (most stop at `place` or `check`) just has nothing to report
past the stage it stopped at:

| phase      | label              | kept-over fraction within the phase                                                 |
| ---------- | ------------------ | ----------------------------------------------------------------------------------- |
| `generate` | Generating         | 0.5 while running (no finer signal)                                                 |
| `place`    | Placing            | 0.5 while running                                                                   |
| `legalize` | Legalizing         | 0.5 while running                                                                   |
| `detail`   | Detailed placement | 0.5 while running                                                                   |
| `route`    | Routing            | `data.progress.done / data.progress.total` from the latest `route_result`, else 0.5 |
| `gloss`    | Glossing           | 0.5 while running                                                                   |
| `check`    | Checking           | 0.5 while running, 1.0 once done                                                    |

A lane's overall `fraction` is `(phase_index + phase_fraction) / len(PHASES)`. The raw phase/kind
string is matched against a keyword table gathered from the engine's own `phase=` call sites
(`hardware/pnr/pnr/{trace,provenance,native_loop,gloss,route/feedback,...}.py`) and real
live-directory samples — `"legalization"` → `legalize`, `"global-placement"`/`"placement"` →
`place`, `"source p/r"` (a feedback round restarting the pipeline) → `place`, `"routed"`/
`"commit"`/`"native-phase"`/`"usb-pairs"` → `route`, `"gloss"`/`"power-bank"` → `gloss`,
`"congestion"`/`"selection"`/`"drc"`/`"result"` → `check`, and so on (`progress.py`'s `_KEYWORDS`
is authoritative; the route family is checked _before_ the check family on purpose, since a bare
`route_result` event's `kind` string contains "result" as a substring — checking "result" first
would misfile it as `check`). **A phase string this has never seen before is never an error**: it
falls back to the lane's last known phase bucket (held over from its previous poll — this is a
_per-poll_, not per-event, fallback: several raw events folded into one poll only leave their
_last_ phase visible to it) or, for a lane with no classifiable event at all yet, to "running, 0%"
— never a crash, never a reset to zero for an unrelated event (a `worker_config_applied`
mid-route keeps the route phase). `status="queued"`/`kind="candidate_queued"` is the
zero-fraction `queued` state; `candidate_complete`/an accepted `iteration_complete` is `done`
(fraction 1.0); `candidate_failed` is `failed`, keeping whatever fraction the lane had reached
rather than resetting it — "it got this far, then failed" is more informative than snapping back
to zero. A **rejected** Monte Carlo candidate (`status="rejected"`, halving/beam-search discarding
a losing branch) gets its own `rejected` state the same way — it is a normal search outcome, not
a failure, and must not read as one. A **running** lane whose last event is more than
`STALE_SECONDS` (10 minutes) old reads `stalled` instead — a dead worker, a finished campaign the
viewer never got a final event for, and a lane actually mid-phase are otherwise indistinguishable.

**Repeated rounds never make the bar go backwards.** A feedback round (`pnr.route.feedback`,
`data.source_round`, mirrored onto `lane["round"]` by `apply_event`) restarts the whole
generate→check pipeline for another pass; naively re-mapping that onto the same 0..1 pipeline
fraction would make the bar visibly drop back toward "generate" every round. Instead, once a
round completes (`round` increments), the fraction it reached is folded into a `round_floor` —
raised toward 1.0 asymptotically (`floor + (1-floor) * this_round's_fraction`), never past it —
that the next round's own within-pipeline progress is added on top of. A lane that never repeats
a round (no `round` field at all, the common single-pass case) has its floor pinned at 0.0
forever, so this is a no-op for it: `fraction` is exactly the plain in-pipeline read, unchanged.

A group node's bar is the mean of its descendant leaves' fractions, coloured by the most urgent
state present — the same running-first priority as `best_leaf` above — the width says how far
along, the colour says whether something needs attention.

### Status text

`progress.humanize()` turns that classification into one short, plain-language line (fits the
narrow lanes panel; the raw phase/kind and full counts are in each row's `title` tooltip instead):

- `Queued`
- `Waiting for placement` — no classifiable event yet
- `Generating` / `Placing` / `Legalizing: pass 2` / `Glossing` — mid-phase, no finer detail
- `Routing: 412 of 530 connections (78%)` — from `data.progress` on the latest `route_result`
- `Checking: 2 opens / 0 violations`
- `Stalled: idle 23 min` — a running lane with no event in over `STALE_SECONDS`
- `Rejected candidate (reached Routing)` — a normal Monte Carlo search outcome, not a failure
- `Failed: 3 unconnected, 1 DRC error` — from the lane's `opens`/`violations` (a routed/checked
  candidate) or, for a screening-only candidate that never got a board extracted, from its own
  `candidate_failed`/`candidate_complete` payload (`data.missing_connections`/`data.opens`,
  `data.violations`; stored as `lane["last_candidate"]` by `apply_event`)
- `Done: all connected, DRC clean, 12.4 s` — **only when the telemetry actually says so**: `opens`
  must be exactly `0` for "all connected" (otherwise it reports the real count, or omits the claim
  entirely if unknown — a screening-only run that never extracted a board never ran DRC, so it
  must not claim "DRC clean" either, and reads plain `Done: finished` instead). The duration is
  `lane["time"] - lane["started_at"]` (`started_at`: the lane's first event time, tracked per-lane
  server-side, mirrored onto the lane dict itself since `humanize()` is a pure function of the
  lane alone); omitted when either timestamp is unavailable.

Tests: `tests/unit/viewer/test_lane_tree.py` and `test_progress.py` (hermetic, against lane-id and
lane-dict samples drawn from real live directories plus odd shapes — a lane that is both leaf and
group, empty input, very deep nesting, an unrecognised phase string, the real `pnr.mc.halving`
`<parent>/<candidate>/<stage>` shape and radar's flat `mc/s0..s3`, stalled/rejected states, and
the round-repeat lap-compounding model); `tests/e2e/viewer/test_experiment_tree.py` (headless
Chrome: the tree renders/collapses/expands and persists, filtering, keyboard navigation (including
that focus survives a live rebuild, not falling back to `<body>`), progress bar widths against
this file's model, status text, board selection via a leaf or a group's best leaf, and phone
width). The `tests/e2e/viewer` package is tagged `manual` (see its `BUILD.bazel`) and needs a real
Chrome/Chromium on the machine; it is **not** run by CI's `bazel test //...` and must be run
explicitly (`bazel test //tests/e2e/viewer:test_experiment_tree`) before relying on it.

## Rendering and performance

The board canvas (`static/app.js`) used to redraw everything, every frame, in immediate mode: each
via was two `beginPath`/`arc`/`fill` calls with a `fillStyle` change, forced to at least a 2px
radius even when zoomed out past legibility, and both drawing and hover/click hit-testing scanned
every via and track on the board linearly. On a real board with thousands of ground-stitching
vias (the reported case) this made panning, pinching and zooming visibly laggy. Four changes fix
it, in order of how much they matter:

1. **A spatial grid** (`gridFor()`, one per geometry object, built lazily and cached by object
   identity — nothing invalidates it by hand, a new geometry is simply a new object): uniform cells
   in board mm, sized so each holds a handful of vias and tracks. `paintBoard()` and `boardHit()`
   both use it — drawing culls to the cells the current viewport actually overlaps; hit-testing
   checks only the cells near the query point. Both return exactly what a full linear scan would
   have (same candidates, same priority order), just over far fewer items; this is the one checked
   directly, not just measured, in `tests/e2e/viewer/test_viewer_perf.py`.
2. **Via batching**: the via rings and holes in the culled, visible set are drawn as one `Path2D`
   fill each (board-space coordinates, one `setTransform` per tier) instead of two `arc`/`fill`
   calls and a `fillStyle` change per via. Tracks are culled by the same grid but still drawn one
   at a time, since their stroke width and colour both vary per track (a diff view, a draft net) —
   batching those would need bucketing by width too, judged not worth the added complexity once
   culling and the via work below met the target.
3. **Via level of detail**, by true on-screen diameter (`diameter * view.scale`, no artificial
   minimum): ≥1.5px draws the normal ring and hole; 0.5–1.5px draws a plain dot at its true size
   (a ring is not legible at that size anyway); below 0.5px, too small to resolve individually, one
   representative dot stands in for every via in that grid cell — a cheap stand-in for a
   pre-rendered texture tile that reuses the grid already built for culling. Hit-testing always
   uses a via's true diameter regardless of which tier drew it, so a via that is just a cell fleck
   on screen is exactly as clickable as it always was.
4. **A gesture raster cache**: while a pan, pinch, wheel-zoom or rectangle drag is live, the board
   canvas just blits an offscreen raster of the static board layers with a scale+translate
   transform, at most once per animation frame however many input events arrive; the dynamic
   overlay (selection, routing target, highlights, drag/annotation rectangles, cost dots, note
   badges) is still drawn live on top every frame. The cache is built at a margin around the
   viewport (snapped so its backing store is an exact pixel multiple — no sub-pixel resampling blur
   when nothing has actually moved yet) and is reused for as long as the board content (its
   content hash, layers, toggles, diff and draft) and the view stay the same.
5. **Nothing heavy while a finger is down.** A full `render()` requested mid-gesture (a live poll,
   a note, a highlight) is deferred to the release, which renders anyway; the live poll does not
   even advance its revision until the gesture is over. The full-quality redraw after a gesture is
   the release itself (a pause mid-drag no longer triggers one); only the wheel, which has no
   release, still settles 150ms after its last step. After every full render whose content or view
   the cache does not match, the cache is rebuilt — and forced to rasterise, since canvas drawing
   is otherwise only executed on first use — in idle time, so the next drag starts from a ready
   cache and its first frame is a blit like any other. The one exception mid-gesture: a finger held
   still for 0.7s over a region the cache does not cover (a long pan past the margin, or a pinch
   past 2x) rebuilds the cache at the current view, so the blank edge fills in while the user looks.

   Before this, every full render marked the cache stale and the first gesture frame rebuilt it, so
   every drag started with a 60–140ms hitch on a phone; worse, the settle render fired whenever a
   finger paused for 150ms mid-drag (or a frame took that long), which made the next move rebuild
   the cache again — a loop that turned one slow frame into continuous stutter.

A further option, instanced WebGL vias, was in the original plan but turned out unnecessary: items
1–4 alone already meet the <16ms p95 target during gestures at 1x CPU throttling (see the
before/after table in the PR). Pads and other tracks were left without additional level-of-detail
tiers for the same reason — their counts were never the bottleneck.

Tests: `tests/e2e/viewer/test_viewer_perf.py` (manual, like its siblings — a real headless Chrome
against a real viewer server and a synthetic dense board; skips without Chrome) checks that
`boardHit()` matches a verbatim copy of the pre-grid linear scan at many points and across a dense
sweep at a zoomed-out LoD tier, that the gesture fast path reproduces a full render pixel-for-pixel
at each via LoD tier, and that panning does not rebuild the cache mid-gesture.
`tests/e2e/viewer/test_viewer_gesture.py` (same setup, phone profile with touch emulation) drives
real touch drags with 16ms move steps, mid-drag pauses and a live poll that re-renders mid-drag,
and checks that no full render and no cache rebuild happen while a finger is down, that the drag
starts from a cache built in idle time, and frame-time bounds under a modest CPU throttle.

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

| Feature                       | Needs                                                              | Without it                                                            |
| ----------------------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------- |
| Board geometry of checkpoints | KiCad's Python (`--kicad-python`, `YAPNR_KICAD_PYTHON`)            | events without a layout show no board                                 |
| Placement cost inspection     | the engine's cost model (`--engine-runtime`, default: imported)    | the cost panel says unavailable                                       |
| Schematic view                | `--graph` (plus `--constraints`, `--parts` symbols)                | the schematic tab says unavailable                                    |
| Source browser (Inspect)      | atopile sources (`--atopile-root` + `--atopile-build`) and a graph | the Source tab says why; notes use the graph                          |
| 3D view                       | a headless `kicad-cli` (`--kicad-cli`, `YAPNR_KICAD_CLI`)          | the 3D pane says why                                                  |
| Design notes                  | nothing (on by default)                                            |                                                                       |
| Ask agent, AI net labels      | `--agent on`, `--net-summaries on` and the `claude` CLI            | off (the default): no Ask buttons; the Ask tab says how to turn it on |

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
  dossier (per ~60k characters of it; about $0.20 for a hundred nets with sonnet), labelled as AI
  output in Inspect.

What that means:

- **Cost.** Every turn and every labelling call is paid on the operator's Claude account (the
  `claude` CLI's login). Each call has a budget that the CLI enforces (`--max-budget-usd`): an Ask
  turn `--agent-budget-usd` (default $2), a net-label call `--net-summary-budget-usd` (default $1).
  Both kinds count against one cap per server process, `--agent-total-usd` (default $20; `0` turns
  the cap off). A call starts only while the cap still covers its whole budget on top of what was
  spent and what the running calls hold, so concurrent turns cannot pass it together; a call that
  ends without a cost report from the CLI (timeout, cancel, a stopped unsafe turn) is counted at
  its whole budget. When the cap cannot cover another call, Ask says so and the remaining net
  labels stay missing until a restart, which resets the count (it is kept in memory). The CLI
  checks its budget as the turn runs, so one call can end slightly above its budget. The default
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

### Scope: which lane(s) a note shows on

One experiment (one `--root`) can have many lanes (candidates/trials), each with its own board,
and the same refdes (`C1`, `U5`, …) names a _different_ instance on each one. A note is therefore
scoped to the lane it was recorded in, not to the refdes alone: a note about `C1` written while
looking at lane `r05/c02` is never drawn on, or counted for, lane `r05/c07`'s board, even though
both boards have their own `C1`.

Every note carries a computed `scope` (`notes/store.py`'s `derive_scope()`, schema
`yapnr-notes-v2`), from two inputs already on the note:

- `provenance.lane`, recorded automatically from the lane you were viewing when you created the
  note (via the Inspect panel, a board click, or the Ask agent) — scope `{"kind":"lane","lane":…}`.
- an explicit, optional **global** field, off by default, you set yourself (the Notes editor's
  "Show in every experiment" checkbox; only a user can set it, like status) — scope
  `{"kind":"global"}`.
- neither: scope `{"kind":"unscoped"}` — a note with no recorded lane (written before scoping
  existed, or with the board paused on no particular lane). An unscoped note is never drawn on any
  board; the Notes tab lists it under "Unscoped notes" instead of guessing where it belongs, with
  a button to tag it with the lane you are currently viewing.

Scope is computed fresh every time a note is read, not stored in `notes.jsonl` — so this needed no
migration: a note from an older viewer that never had a "scope" or "global" field is classified
the same way new ones are (by its `provenance.lane`, or unscoped), and nothing in the log is
rewritten or lost. Board badges (`note-badges`), the Inspect panel's "Notes" section, and the Ask
agent's dossier (`notes/store.py`'s `relevant()`) all filter by scope against the lane you are
currently looking at; the Notes tab's own search list still shows every note (each with a scope
label) so you can find and re-tag an unscoped one.

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

The page header links to the viewer's source (AGPL-3.0-or-later, section 13): the source tree of
the revision the server runs (`YAPNR_SOURCE_REVISION`, or the git checkout's `HEAD`), marked
"modified" when the checkout has uncommitted changes to tracked files. If you serve a modified
viewer to other people, publish your changes and point `--source-url` at them. The revision is the
viewer's own; runs do not record the engine commit that produced them yet. The front end loads two
third-party libraries, served unmodified next to their licenses: elkjs (EPL-2.0, with an
Apache-2.0 web-worker shim; both texts are served) and three.js (MIT); see
[THIRD_PARTY.md](../THIRD_PARTY.md).
