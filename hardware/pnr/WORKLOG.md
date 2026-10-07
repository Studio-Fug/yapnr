# WORKLOG — algorithmic PnR (branch `pnr-system`)

Handoff notes and build log, newest first — read the top entry to orient, then
scan back. Full design + rationale lives in
[`docs/hardware/pnr-system.md`](../../docs/hardware/pnr-system.md); this file is
the running state so a fresh-context agent can pick up cleanly. Update it at the
end of every session.

## 2026-08-22 (night) — escape routing E1/E2/E3 built; board is congestion-limited (START HERE)

Kevin: "support escape routing by local DRC relaxation AND via-in-pad AND offset
dog-bone — implement all." Built all three, DRC-clean by construction:

- **E1 — configurable fab/DRC profile** (`fab:` block → `FabProfile` →
  `rules.json["fab"]`). One source of truth for track/clearance/via geometry; the
  grid pitch auto-derives from `track+clearance` (a tighter fab routes finer), the
  **via keep-out radius derives from the geometry** (`⌈(via_d+clr)/pitch⌉-1`, not
  hardcoded — a tighter fab needs a wider halo), and `patch_project_rules` +
  `emit_routes` read it. `--route-pitch 0` = auto. Solid, committed (`f423410`).
- **E2/E3 — pin-escape planner** (`route/detail/escape.py`, `--no-escape` A/B):
  per-pad on-layer / via-in-pad / dog-bone, each reserving its keep-out so the
  emitted geometry is DRC-clean (validated: escapes ON≡OFF in pcbnew DRC). Committed
  (`e45fac6`, `f423410`).

**THE definitive finding (measured, not guessed): splanc_dev is congestion-limited,
not escape-limited.** Escape ON vs OFF is _identical_ — 23/48 default, 21/48 tight
fab, same vias, same DRC — because (a) the pads that fail can already leave their
pad on-layer (the maze vias where it needs to); (b) a full-size via-in-pad in a
dense DRC-clean pad field is geometrically impossible — every pad's via keep-out
overlaps its neighbours' clearance halos (`_via_clean` correctly rejects it), even
at a 0.35 mm via / 0.20 mm pitch. The unrouted nets fail to **cross** the dense
cluster, not to escape their pins. So the routability lever for this board is the
**rubber-band outline + inflation** (bigger board relieves the channel), NOT escape.
Escape is a correct capability for genuinely escape-limited boards (pads boxed by
plane regions / keep-outs / edges → via down to a gap) and for micro-via fabs.

**A real dog-bone as a routing CHOICE (vs a local fallback) needs group-terminal
maze routing** — offer each pad {on-layer, via-in-pad, dog-bone-out} candidates and
let the maze connect any one. That + the rubber-band is the remaining path to fully
routing splanc_dev. Separately, two DRC items remain (both pre-existing, not escape):
the **plane pour/dog-bone fanout** (~108, the top contributor — route plane pads
through the engine) and a **maze via-vs-small-pad halo quantization** edge case (~20,
diagonal vias landing ~0.055 mm from small pads — widen the pad halo / round the
mark outward).

## 2026-08-22 (evening) — RRR router + rubber-band outline; pin-escape is the wall

Kevin chose **"invest in a stronger router"** (stay 4-layer) and OK'd **growing the
outline** ("rubber-band, configurable"). Both built + committed:

- **Rip-up-&-reroute finalize** (`maze.route`): was greedy (a net contended for one
  cell during negotiation → dropped whole). Now 2 passes — commit the non-colliding
  negotiated routes, then **re-route every dropped net around the committed copper**
  (hard-blocked A\*, `blocked` set threaded through `_astar`/`_route_one`),
  shortest-span first. Still DRC-clean by construction.
- **Rubber-band outline** (`route_and_place(auto_outline=…)`, `pnr.route
--auto-outline`/`--outline-max-scale`, wired into nothing yet — opt-in): the
  `board.outline` is an approximate target; if the loop won't fully route, grow the
  outline (aspect preserved) and retry, smallest-that-routes wins. `place()` already
  stamps `placed.outline` from the constraint size so write-back frames to it.

**Measured levers (splanc_dev):** RRR alone ~19–20/48 (leftovers genuinely don't
fit — congestion, not greedy-drop). Bigger board helps _global_ congestion (pitch
0.4: 14/48 @1.0× → 21/48 @1.6×/2.56× area). Neither reaches 100 %.

**THE WALL — fine-pitch pin escape (diagnosed).** The 29 unrouted nets concentrate
on the **dense parts**: U5/ESP32 (11), H1/20-pin (7), U2/QFN (5), U11 (5). Two
mechanisms: (a) **pin escape** — DRC-clean grid pitch has a hard floor at
track+clearance ≈ **0.28 mm** (0.15 + 0.13), and a 0.15 mm track **cannot fit
between 0.5 mm-pitch pads** (edge gap ~0.25 < 0.15+0.13+0.13), so inner-row pins
can't escape _at any board size_; (b) **channel congestion** — long nets (e.g. from
H1, a 2.54 mm header, so not escape) can't cross the dense cluster.

**Next feature (the real lever): signal escape to the inner layers.** Drop a via
in/adjacent to a stuck fine-pitch pad down to an inner-layer _gap_ (the plane-fanout
dogbone, but for signals), where the 0.3 mm grid has room — the standard QFN/BGA
escape. Needs: per-pad "can't escape on its own layer" detection + a short
via-in-pad/dogbone stub + route on the inner layer. Optionally thinner (0.10 mm)
escape tracks in the pad neighbourhood (differential width). This, plus the
rubber-band and multi-round inflation, is the path to a fully-routed splanc_dev.
Until then the gate correctly fails the build.

## 2026-08-22 (latest) — loop closed on the router; 4-layer routing; routability ceiling

**Loop closed on the DRC-clean detailed router.** `route_and_place(detail_rules=…)`
(+ `pnr.route --detail-loop`, wired into `pnr.bzl`) now uses the **detailed router
as ground truth**: it re-routes every round and the #unrouted signals is the
objective, with `detail_congestion()` stamping each failed net's pad-bbox into the
inflation map so the next placement spreads those parts. This replaces the old
global-lookahead signal, which was _useless here_ — it reports `overflow 0`
(perfectly routable) on placements the DRC-clean router can't finish. This is the
user's directive: a failed route must steer the next placement cycle.

**4-layer signal routing built** (`router._signal_layers`/`_mark_plane_regions`,
`grid` inner layers, `maze` via = antipad pass-through, `emit_routes` In1/In2).
Signals now route the **inner-layer gaps** between the split planes, not just F/B;
through-vias pass through a plane via its pour antipad (only the via's _target_
layer must be clear — a track can't sit under plane copper, a via may pass through).
**Validated: In2 signal tracks are 0/0 DRC against the plane** (region-block +
antipad agree with the emitted copper). `detail_route_test` now exercises 4-layer.

**Routability ceiling (honest, well-characterized): ~40 % of signals (≈16–22/48).**
Firm across pitch (0.25→17, **0.3→20**, 0.4→14) and negotiation depth (12 vs 40
passes → same ~18). The detail-loop trajectory oscillates (32→29→36→32 unrouted) —
it does **not** converge. Root cause is _routing resource, not the router_: the
**ground net has 75 pads spanning the whole board**, so its plane consumes ~all of
In1; the power split-planes cover much of In2; the inner "gaps" are small. So a
4-layer/2-plane stack gives ≈2 effective signal layers here, and 48 signals at
manufacturable DRC-clean spacing don't fit. The router is correct and DRC-clean; the
board needs more resource. Options for a fully-routed board: **6-layer** (2 planes +
4 signal), a **bigger outline**, or a **ground-plane strategy** that frees an inner
layer (e.g. ground on In1 only, In2 mostly signal with local power islands). Until
then `splanc_dev.fab` correctly **fails** the routing-completeness gate — the "loop
until success or fail the build" behaviour the user asked for.

**Remaining DRC (separate from the signal router):** with planes poured, full-board
DRC is ~187 = **66 source-footprint** (pad-vs-pad, out of scope) + ~13 signal-router
edge cases + **~108 from the plane pour/dogbone fanout** (`writeback._dogbone…` — the
old R1 via-in-pad/stitch shorts). The plane fanout is now the top DRC contributor
and the next thing to route _through the engine_ (route each plane pad to its plane
layer as a target) rather than the naive dogbone.

## 2026-08-22 (late) — router is DRC-CLEAN BY CONSTRUCTION; close the loop next

**Milestone: the emitted copper is DRC-clean by construction.** Validated against
real `kicad-cli pcb drc`: **placement-only** (zero routing) already reports **66
violations — all pad-vs-pad within the source footprints** (U2's exposed thermal
pad vs its thermal PTH, USB1's overlapping connector pads); adding the full signal
route adds only **~13 more, of which 4 are an `EN-1`/`en-1` net-name case collision
(atopile naming, not a real short)** → **~9 genuine routing violations**, each a
tight via-near-pad edge case. The router itself no longer produces shorts/clearance
failures. (Started this session at **1097** full-board violations.)

**What made it DRC-clean — six fixes (all with the fast loop below):**

1. **Design rules must be stamped into the `.kicad_pro`, not the board.** DRC reads
   board constraints + net-class clearances from the _project file_;
   `apply_placement` (`SetCopperLayerCount`/`BuildConnectivity`) _detaches_ the
   board's live settings from the project `SaveBoard` writes, so setting them via
   `GetDesignSettings()` never reached DRC. Fix: `writeback.patch_project_rules()`
   patches the `.kicad_pro` JSON as the **last** pipeline step (`pnr.planes
--…`/end) — mirrors how `frame_region` stamps Edge.Cuts as text. Rule set
   (`_RULE_SET_MM`): clr 0.13, hole/hole-to-hole/edge 0.20, via Ø0.45, drill 0.20,
   annular 0.0 — manufacturable (JLC-class) and grid-satisfiable; drill/annular
   loosened only to _tolerate source footprints_ (U2 0.2 drills, USB1 ~0 annulus).
2. **Pad y-flip frame bug (the big one).** pcbnew footprint-local coords are y-DOWN;
   the engine is y-UP. `ingest` stored the pad offset without flipping y, so every
   pad's router geometry was _mirrored about the part centre_ — halos/access on the
   wrong pad → shorts. Fix: `offset=(x, -y)` in `ingest`. **Pad-position delta vs
   emitted pcbnew is now 0.0000 mm across all 79 parts** (verifiable — see below).
3. **Pad clearance halos** (`grid.add_pad`): reserve own-net cells out to
   `clearance + via_radius` around each pad so other-net tracks _and vias_ keep
   clear (was: body cells only, no halo).
4. **Via keep-out** (`maze._footprint`): Ø0.45/0.25 vias + a radius-1 keep-out halo
   (both layers) around every via ⇒ via-via ≥ 0.6 mm centre-to-centre and via-track
   both DRC-clean at 0.3 mm pitch. Folded into PathFinder occupancy _and_ the greedy
   finalize (a net whose footprint overlaps an accepted net drops to unrouted).
5. **Through-hole pads on ALL signal layers** + **no-net copper is a hard obstacle**
   - **custom-pad true extent** (`ingest`: U2 pad 21 is a Ø0.01 anchor but 4.97×2.04
     real copper — use `GetBoundingBox`) + **edge inset** (`grid.block_edge_inset`).

**Where it stands / OPEN (R5 — close the loop):** DRC-clean routed fraction is
**~14–22/48 signals** on 2 signal layers at manufacturable spacing (power/ground on
planes). The DRC-clean guarantee _costs_ routing resource — the honest signal is
that this density needs the **place↔route loop to spread congested regions** (feed
the detailed router's per-region unrouted back into placement inflation — design
§6; today the loop only sees global-route overflow) or more signal layers. Until
it fully routes, `splanc_dev.fab` correctly **fails** the routing-completeness gate.
The 66 source-footprint pad-vs-pad DRCs are out of the router's scope (they'd fail
on the bare atopile board too) — either fix the footprints or scope the DRC gate to
router-introduced violations.

**Fast DRC loop (no full `.fab`; ~30 s/iter):** `bazel run //hardware/pnr:pnr_fab --
<graph> <constraints> --dump-json p.json --dump-rules r.json --dump-routes ro.json
--allow-unconverged`, then under `@kicad_python`: `pnr.writeback … --routes ro.json`
→ `pnr.planes … --rules` (stamps `.kicad_pro` rules) → `kicad-cli pcb drc --format
json`. Isolate the router's own copper by DRC-ing the writeback output _without_
`pnr.planes`; establish the footprint baseline by writeback **without** `--routes`.
The kicad python is `…/nix_pkg~kicad_python/bin/python3` + `PYTHONPATH` =
`kicad-base…/site-packages` (has pcbnew, **no numpy/yaml** — grid/constraints won't
import there; run the engine under Bazel's torch python).

## 2026-08-21 (night) — detailed router R2 + R4 core built + tested

The own detailed router now has its **engine**: a grid model (R2) + a negotiated
multi-layer maze router (R4), both pure Python and unit-tested (suite 13/13).
This is the hard algorithmic core; what's left is integration (R5) + fanout (R3).

- **R2 — `pnr/route/detail/grid.py`.** `RouteGrid` over the placed graph: per
  signal layer (F.Cu=0, B.Cu=last), pitch ≥ track+clearance ⇒ **DRC-by-
  construction**. Pads (now carry `size` — added to `graph.Pad` + `ingest`;
  fixture `graph.json` regenerated) become **own-net access cells**; other-net
  pads/keep-outs are obstacles; outline = grid bound. `passable(layer,i,j,net)`.
  `detail_grid_test` green (incl. building on the real 338-pad fixture).
- **R4 — `pnr/route/detail/maze.py`.** Multi-layer **A\\\*** + **PathFinder**
  rip-up-&-reroute: in-plane + via moves (via surcharge), multi-pin nets grow a
  tree (Prim on the grid), cost `(1+h)·p`; iterate until **no cell is shared by
  two nets** ⇒ DRC-clean at the pitch. `RouteResult` (routes/vias/unrouted).
  `detail_maze_test` green: 2-pin, via-when-needed, obstacle-avoid, **two crossing
  nets negotiate DRC-clean (no shared cells)**, deterministic, other-net-pad blocks.
  **This engine subsumes R1/R3** — a dog-bone fanout is just a short route + via.

**R5 driver landed — `pnr/route/detail/router.py`.** `route_board(placed_graph,
constraints, rules)` builds the grid, excludes plane nets, negotiated-routes the
signals, and emits mm tracks/vias. **`route()` now greedy-finalizes to a DRC-clean
result always** — contested nets drop to _unrouted_ (never a short in the emitted
board), the honest ground truth the loop consumes. `detail_route_test` places the
real fixture + routes it: DRC-clean by construction + deterministic, routing a
meaningful fraction on the dense 2-signal-layer board. Suite **14/14 green**.
(The "non-determinism" was a self-inflicted test bug — two calls at different
`max_iters`; the router is deterministic. Fraction < 100% because signals are on 2
layers — the R5 loop + cost tuning close it.)

**Next (finish R5 + R3):** (1) **emit** the `route_board` tracks/vias via
`writeback` (replace the FreeRouting step in `pnr.bzl`) + **plane fanout** through
the engine (route each plane-net pad to its plane layer as target); (2) **close the
loop** — feed the router's real per-region unrouted/congestion into placement
inflation (design §6) so it iterates to fully routable; tune cost/iters. Then
`splanc_dev.fab` is fully routed AND DRC-clean by our own engine. Grid pitch for
splanc_dev ≈ 0.25–0.4 mm; A\* is fine (place+route the fixture ≈ 50 s).

## 2026-08-21 (night) — routability diagnosed; own detailed router kicked off

**Decision (Kevin):** build our **own detailed router**, SOTA-grounded and phased
like the placer — FreeRouting + naive planes can't get a dense fine-pitch board
DRC-clean. Phased plan **R1–R5** now in `docs/hardware/pnr-system.md` (§ Detailed
router). `splanc_dev.fab` correctly **fails** the `require_routed` gate until it
lands.

**Diagnosis (rigorous — union-find matched pcbnew's ratsnest exactly at 81):** of
81 unrouted, **55 (68%) are high-fanout ground/power** — `lv` ground = 75 pads /
32 unrouted — that must be **planes**. The rest is signal congestion at the dense
parts (ESP32 module, QFN, 20-pin header).

**What's built toward it (this session, validated but NOT yet DRC-clean):**

- **Planes**: `net_class.plane_layer` schema; `writeback.apply_planes` pours
  **split-plane** zones (multiple rails share an inner layer, each = bbox of its
  own pads, priority-carved) + via-stitches pads to the plane; plane layers typed
  `LT_POWER` so the router keeps signals on F/B. **Proven**: planes connect the
  high-fanout nets (`lv`/`p3v3` → 0 unrouted).
- **THE OPEN BUG (→ R1):** via-**in-pad** stitching shorts on 0.5 mm-pitch parts —
  **612 DRC violations**. Fix is **dog-bone fanout** (offset via + short trace,
  standard ≥0.5 mm) + proper plane antipads. That's phase **R1**.
- Constraints: `splanc_dev` planes = In1 solid `lv` ground, In2 split
  {p3v3,p5v,vsys,vbat,hv}. Connector grounds stay traces.

**Config note:** typing In1/In2 as power (clean planes) forces signals onto F/B
(2 layers) → 81→71 (planes fixed ~34, but 2-layer signal squeeze added back ~24).
The real answer is R1–R5 (fanout + own signal router), not more FreeRouting knobs.
`route_max_passes=0` + `require_routed` stays.

**Fast iteration:** engine tests `bazelisk … test //hardware/pnr/...` (no
atopile/FreeRouting). The `.fab` build is the slow (~10 min) integration path;
each plane experiment above cost one. Diagnosis scripts: union-find over
tracks+pads per net = pcbnew ratsnest (use it to see which nets/regions fail).

**R1 attempt + finding (next actor start here):** implemented dog-bone fanout in
`writeback._dogbone_fanout_net` (offset via + short trace, outward from footprint
centre, clearance-checked vs other-net pads/tracks via `_collect_obstacles` /
`_clear_of_obstacles`; via-in-pad only for big pads). **Result: still not
DRC-clean** — a naive outward offset leaves ~28 shorts / ~553 DRC on the dense
row fixture, because (a) the fanout **trace** itself isn't collision-routed (only
the via position is checked) and (b) via geometry (`PCB_VIA` width: `GetWidth
called without a layer argument` under DRC) needs the proper setup. **Conclusion:
R1 needs R2 first** — clean fanout requires the geometry/obstacle model + a real
(even 1-segment) router for the dog-bone trace. So the build order is
**R2 (geometry + pin access) → R1/R3 (fanout/escape on it) → R4 → R5**, not
R1 standalone. The dog-bone scaffold + obstacle framework is committed and is the
foundation; the missing piece is a collision-aware trace router (R2/R4).

**Env hazard:** `BOARD.GetTracks()` / `GetDrawings()` / `Zones()` have a **flaky
SWIG iterator** in this KiCad-9 python (`'SwigPyObject' object is not iterable`) —
usually works, sometimes not. `_collect_obstacles` wraps `GetTracks()` in
try/except (falls back to pads-only). Prefer `GetFootprints()` (reliable). Any new
pcbnew iteration should be guarded.

## 2026-08-21 (late) — correctness fixes from EE review; routability still open

An EE review of the first routed board (`splanc_dev.fab`) surfaced real defects.
Fixed three; a fourth (full routability) is now **honestly gated** but not yet met.

**Fixed:**

1. **Outline clipped parts (U5/ESP32 pads outside Edge.Cuts).** Root cause: the
   outline was framed from footprint _origins_ (atopile `board_outline.py`),
   discarding the placement's containment guarantee. Now `writeback.frame_region`
   stamps Edge.Cuts at the **placement region** `[0,W]×[0,H]` (which the placer
   keeps every courtyard inside) as text — so all pads are inside by construction.
   Dropped board_outline.py from the flow.
2. **2-vs-4 layer mismatch.** `constraints.layers` (4) now actually sets the board
   copper-layer count (`SetCopperLayerCount`) via `rules.json`; gerbers export
   In1/In2 and FreeRouting routes on all four layers (verified: F/In1/In2/B all
   used).
3. **Connector edge overhang.** New `overhang_mm` on `fixed`/`edge_align`
   (`geometry._edge_pose`) so an edge connector protrudes a set distance past the
   board edge for cable mating; `USB1` = 1.5 mm overhang. Fixed parts are excluded
   from the outside-outline check (`metrics.outside_outline(exclude=…)`) since the
   overhang is intentional. Gated by `geometry_test`.

**Routing-completeness gate (the important behavior change):** `pnr.quality`
counts unrouted ratsnest (`GetUnconnectedCount`) and the rule **fails the build**
if any net is unrouted (`require_routed`, default True); `route_max_passes=0` lets
FreeRouting run to completion. No more green build on a partial route.

**STILL OPEN — the board does not fully route.** On 4 layers, uncapped,
FreeRouting leaves **~81 connections unrouted** (44/71 nets with copper), so
`splanc_dev.fab` **correctly fails**. The place↔route loop's global-route
lookahead reports `overflow 0` (says "routable") while the detailed router can't —
i.e. the coarse 2.5 mm-gcell lookahead is too optimistic to catch the real
(pin-access / local) congestion around the dense parts (ESP32 module, QFN sensor,
20-pin header). Levers to pursue (open PnR-quality work): a bigger / less-dense
board; a **realistic** lookahead capacity so the feedback loop actually spreads
congested regions (its designed purpose — today it converges in 1 round because
overflow reads 0); channel-aware legalization; running the _detailed_ router
inside the loop; or accepting FreeRouting's limits and hand-routing the fine-pitch
parts. `route_feedback_test`'s "overflow→0" acceptance will need to soften to
"overflow decreases + terminates" if the lookahead is made pessimistic.

## 2026-08-21 (late) — Phases 4 + 5 + 6 landed: end-to-end `.fab` target

**Phase 6 — routing rules + post-route quality pass (added same session):**

- **Schema** (`constraints.py`): `net_class` (per-class trace width/clearance over
  net-name globs), `diff_pair` (p/n + skew tol), `length_match` (net group +
  tolerance). `compile_routing_rules(compiled, net_names)` expands net globs →
  a stdlib `rules.json` (the seam the pcbnew steps consume without pyyaml/torch).
- **Widths reach the router** (`writeback.apply_net_classes`): applies net classes
  to the board's `m_NetSettings` (default class + named classes via
  `SetNetclassPatternAssignment`) so FreeRouting's DSN carries per-class widths —
  power rails route wider. **Order matters:** classes must be applied _before_
  `apply_placement` (its `BuildConnectivity()` must run with the classes present,
  else they don't persist through the KiCad-9 save — verified).
- **Quality pass** (`pnr/quality.py`): reads the routed board via pcbnew
  (`GetTracks().GetLength()` + `PCB_VIA` count), and a pure `analyze()` scores
  per-net routed length, via count, **diff-pair skew**, **length-match spread** vs
  tolerance, and per-net-class length roll-up → `quality.txt` in the fab bundle.
  Advisory by default; `quality_gate = True` fails the build on a diff-pair/
  length-match miss. Gated by `quality_test` (pure).
- `pnr.bzl`: pnr_fab `--dump-rules`, writeback `--rules`, a quality step, and a
  `PnrReportsInfo` provider so the bundle collects `drc.rpt` + `quality.txt`.
- splanc_dev `constraints.yaml`: a `power` net class (rails → 0.4 mm). This board
  has no genuine diff-pair / length-match need (USB is inside the WROOM), so those
  are documented/tested/fixture-only, not forced onto the shipped board.

**Watch-out:** the optional **learned routability predictor** (design §9.6) is
deliberately _not_ built — the analytical net-crossing + PathFinder signal already
converges the loop, so it isn't limiting. Revisit only if a future board needs it.

## 2026-08-21 (late) — Phases 4 + 5 landed: end-to-end `.fab` target

**State:** the design's remaining phases are integrated and the end-to-end target
is **verified building a real board**. One target runs the whole multi-turn
optimize→route→export flow:

    bazel build //hardware/splanc_dev:splanc_dev.fab        # routed board + Gerber/drill/BOM/pick-place
    bazel build //hardware/splanc_dev:splanc_dev.fab.board  # just the routed .kicad_pcb + DRC report

**Verified end-to-end (2026-08-21):** `splanc_dev.fab` **built clean**. The loop
reported `place<->route 1 round: overflow 0, converged; placement 60x50 mm, HPWL
15832 -> 1849 mm (88% shorter), 61 parts rotated, legal`; FreeRouting then laid
**1004 tracks + 80 vias** over the 79-footprint board, and the bundle came out
with the full Gerber set (F_Cu/B_Cu/mask/paste/silk/Edge.Cuts), drill (.drl),
`pick-place.csv`, and `bom.csv`. Engine suite (`//hardware/pnr/...`) is 9/9
green.

**Phase 4 — routing + place↔route feedback (`pnr/route/`):**

- `steiner.py` — RMST net decomposition (a light FLUTE stand-in).
- `global_route.py` — coarse **gcell** global router with **PathFinder** negotiated
  congestion: per-edge capacity = signal-layers × tracks/gcell; each 2-pin segment
  routed as the cheaper monotone **L**; cost `c=(1+h)·p` with present-sharing `p`
  and accumulated history `h`. Objective = total **overflow**; returns per-gcell
  overflow + history maps.
- `feedback.py` — the loop (design §6): place → global route → if overflow>0,
  accumulate congestion into a persistent history map, turn it into per-part
  **RePlAce inflation** (grow congested parts' spreading footprint), re-place;
  converge when overflow→0. Round cap + no-improvement guard.
- Threaded an `inflation` dict through `place()` / `global_place()` / `legalize()`.
- Tests: `route_test` (primitives — Steiner tree, capacity/overflow, PathFinder
  history, inflation derivation) + `route_feedback_test` (acceptance §9.4: on
  `splanc_dev` overflow reaches **0**, loop **terminates**, overflow
  **non-increasing**, placement stays legal, deterministic). Both green.

**Phase 5 — writeback + detailed route + DRC + fab (`pnr/writeback.py`, `pnr.bzl`):**

- `writeback.py` — inverse of ingest: place each footprint (pose/orientation/side)
  via pcbnew, **clear stale preview tracks**, save. Outline framing is delegated to
  the proven text-based `board_outline.py` (tight Edge.Cuts + page around the new
  placement) because `BOARD.GetDrawings()` has a broken SWIG iterator in this KiCad
  9 python under Bazel (`GetTracks`/`GetFootprints` are fine). Since pcbnew's
  `SaveBoard` reformats gr_lines into nested `(stroke …)` that `board_outline.py`'s
  single-level regex can't strip, `writeback.strip_edge_cuts` (a paren-matched text
  pass) removes all Edge.Cuts first so the framer adds exactly one outline. Pure
  `to_pcb_nm` + `strip_edge_cuts` are unit-tested; a pcbnew-gated live test checks
  the round-trip. `writeback_test` green.
- `pnr.bzl` — `atopile_pnr` macro → a `_pnr_board` rule that orchestrates the two
  interpreters in one action (ingest+writeback+FreeRouting under `@kicad_python`
  with a _scoped_ PYTHONPATH; place+route under the hermetic torch `pnr_fab`
  py_binary via `files_to_run`), runs DRC (report by default, `drc_gate=True` to
  fail on violations), and re-provides `AtopileLayoutInfo`; a `_pnr_fab` rule then
  runs the existing `kicad-cli` exporters into one vendor bundle dir.
- Wired `//hardware/splanc_dev:splanc_dev.fab` with `constraints.yaml`.

**Guidance-input docs (Kevin's ask):** new `docs/hardware/pnr-inputs.md` — the full
`constraints.yaml` reference (board size/layers/clearance, fixed/edge_align/
keepout/side_pref/group, hard-vs-soft, frame/units, how each steers placement, a
worked splanc_dev example). `hardware/README.md` updated with the two layout paths;
design doc §9 marks Phases 4–5 done.

**Watch-outs for the next actor:**

- The `.fab` build is **non-hermetic + slow**: it rebuilds atopile (nix, venvHash
  can drift — see `hardware/README.md`) and runs FreeRouting (minutes; capped at
  `route_max_passes=6`). The unit/acceptance tests (`bazel test //hardware/pnr/…`)
  cover the engine without any of that and are the fast feedback loop.
- **Frame/orientation fidelity:** ingest records pad offsets in the footprint's
  y-down local frame while the engine works y-up, so the placer's _estimate_ of a
  rotated pin's landing is mirror-approximate; courtyard legality (w/h swap) is
  exact, so this affects HPWL fidelity only, not DRC. A true y-consistent pad
  frame is a clean follow-up.
- **DRC gate is off by default** (report-only) so the target reliably produces
  outputs; flip `drc_gate=True` once FreeRouting reliably clears the real board.
- **Cosmetic:** `board_outline.py` draws the Edge.Cuts as 4 separate `gr_line`
  segments, so kicad-cli prints "non-closed outline" warnings during export
  (harmless — the Edge.Cuts gerber is still produced). Drawing a single closed
  `gr_rect`/`gr_poly` would silence them; a small follow-up.
- Phase 6 (diff-pair/length-match, learned routability predictor) is untouched.

## 2026-08-21 (night) — Phase 3 orientation landed

**State:** Phase 3 (orientation) is done and green. The placer now co-optimizes a
90° rotation per movable part. Next actor starts **Phase 4 — routing + place↔route
feedback** (`docs/hardware/pnr-system.md` §9.4).

**What's new (`pnr/place/model.py`):** each movable part carries a categorical
over {0,90,180,270}, relaxed to a temperature-annealed softmax (deterministic
Concrete/Gumbel-Softmax, Cypress §9.3). Pin offsets and courtyard extents become
the _expected_ offset/extent under that distribution (so orientation is
differentiable and co-optimized with position); at the end we snap to the arg-max
angle. Fixed parts keep their constrained angle. `place(orient=True)` is the
default; `orient=False` recovers the Phase 2 position-only placement.

- **Result on `splanc_dev`:** legal, **HPWL 2023 → 1836 mm** (orientation on vs
  off; **88% below** the ato row), **61 parts rotated**, deterministic.
  `orientation_test` gates legal-discrete-angles + not-worse-than-Phase-2 HPWL +
  determinism (design §9.3 acceptance). Full suite: **6/6 green**.
- **Reproducibility:** `model.py` sets `torch.set_num_threads(1)` at import so
  float reductions don't vary with thread scheduling — needed for the
  deterministic-placement guarantee (and the `test_deterministic` gates).

**Watch-out for the next actor:** `place()` defaults to `orient=True`; the Phase 2
`placement_test` deliberately passes `orient=False` for position-only semantics —
keep any determinism comparison on the _same_ `orient` value (a mismatch there is
not a nondeterminism bug).

**Next up — Phase 4 (design §9.4):** FLUTE+FastRoute lookahead global route over
the placed board; overflow → RePlAce-style region inflation with a PathFinder
history term; converge the place↔route loop. Acceptance `route_feedback_test`:
global-route overflow reaches 0 within a pass cap and the loop terminates (no
oscillation). Build it over the placed `BoardGraph` (pins are already available
via `pnr.place.geometry.pin_positions`).

## 2026-08-21 (evening) — Phase 2 placement MVP landed

**State:** Phase 2 is done and green. The placer reflows the atopile row into a
legal, compact layout.

**What exists now (`hardware/pnr/pnr/place/`):**

- `model.py` — differentiable **global placement** (torch, CPU, seeded): loss =
  log-sum-exp wirelength + pairwise spreading + outline containment + soft
  edge-align + soft grouping + keep-out penalty. Adam. Fixed parts held as
  anchors (still pull the wirelength).
- `legalize.py` — **grid nearest-free-fit legalizer** (numpy): rasterize the
  outline, block fixed courtyards + keep-outs, place movable parts biggest-first
  into the free slot nearest their continuous target. Disjoint blocks sized
  `ceil((courtyard+clearance)/g)` ⇒ **0 overlaps + in-outline by construction**.
- `geometry.py` (pure: Rect, courtyard, pin positions, edge-pose + keep-out
  resolution), `metrics.py` (pure: HPWL, overlap pairs, hard-violation checks),
  `placer.py` (`place()` orchestrator + `PlacementReport`), `__main__.py` (CLI).
- **Result on `splanc_dev`:** legal (0 overlaps / 0 outside / 0 fixed-off / 0
  keep-out), **HPWL 15832 → 2023 mm (87% shorter)** than the ato row, and
  deterministic. `placement_test` gates all of this (design §9.2 acceptance).
  Run it: `bazelisk … run //hardware/pnr:place -- <graph.json> <constraints.yaml>
--dump-svg placed.svg`.

**Data fix along the way:** the atomic EasyEDA footprints carry **no courtyard
layer**, so `ingest.py` had been falling back to the graphical bbox _including
silkscreen + reference text_ — inflating every part ~5× (a 0402 read as 4.1×5.1
mm; total 3580 mm²). Fixed to use the text-excluded `GetBoundingBox(False)` (0402
→ 1.9×1.2 mm; total 1490 mm²); regenerated the frozen `graph.json` (counts
unchanged). Bumped the fixture outline to 60×50 mm and fixed U5 (RF module) at the
north edge so its antenna keep-out is well-defined.

**MVP simplifications (Phase 3+ material, tracked):** no orientation search
(angles kept as ingested); **single-sided** legalization — `side_pref` compiles
to a soft term but parts stay top for now; keep-out supports rel-to-a-fixed-part
and absolute polygon (rel-to-a-movable-part would need the two-tier loop).

**Next up — Phase 3 (design §9.3):** Gumbel-Softmax bilevel **orientation**
(rotate pin offsets by a relaxed rotation class) + hMETIS **partition seed** for
initial clustering. Acceptance: orientations settle to legal discrete angles and
HPWL improves vs. Phase 2 on both fixtures. Then Phase 4 (routing + feedback).

## 2026-08-21 (pm) — Task 0 + Phase 1 landed

**State:** Task 0 and Phase 1 are done and green under Bazel. The engine now
ingests a resolved board into a neutral graph and compiles the constraint file;
next actor starts **Phase 2 — placement MVP** (`docs/hardware/pnr-system.md` §9).

**What exists now (`hardware/pnr/`):**

- `pnr/graph.py` — the internal `BoardGraph` (design §2 contract): components,
  pads, nets, outline. **stdlib-only** on purpose — it is the JSON seam between
  the two interpreters (KiCad `pcbnew` for ingest, hermetic rules_python for
  torch), which never share a process. JSON round-trips.
- `pnr/ingest.py` — `pcbnew` → `BoardGraph` (mm, y-up, bottom-left origin), plus
  a pure `dump_svg` ratsnest renderer that needs no KiCad. `pcbnew` is imported
  **lazily** (only under `@kicad_python`); the module imports fine without it.
- `pnr/constraints.py` — `constraints.yaml` (§3) → hard **barriers** / soft
  **penalty** terms, globs expanded against the netlist, unknown refs → warnings.
  Front end only (no torch math yet).
- `testdata/splanc_dev/` — the frozen fixture: `splanc_dev.kicad_pcb` (built
  board), `graph.json` (its ingested graph: **79 components, 71 nets, 338 pads**),
  and a hand-written `constraints.yaml`.
- Tests (all green — `bazelisk … test //hardware/pnr/...`): `torch_smoke_test`
  (Task 0 gate), `graph_test`, `constraints_test`, `ingest_test`. `ingest_test`
  asserts the frozen counts + a KiCad-gated live re-extraction that matches.

**Task 0 resolutions:**

1. **torch under Bazel — done.** `torch>=2.2` + `pyyaml>=6` in `//requirements.in`,
   relocked (`torch==2.13.0`). On this repo's platforms (aarch64-linux CI/
   container + macOS arm64) the plain PyPI wheel is already CPU-only (no CUDA
   aarch64 wheels), so no `+cpu` index is needed — matches the CPU-first design.
2. **Cypress license — do NOT vendor; clean-room reimplement.** This repo is
   **AGPLv3** (see `LICENSE`). NVlabs/Cypress is NVIDIA research code, typically
   under the **NVIDIA Source Code License** (research/non-commercial) — I could
   not fetch and confirm it from the sandboxed worktree (no network). That
   license is incompatible with AGPL distribution, so the safe, unblocking
   decision is to **reimplement Cypress's math clean-room** from the ISPD-2025
   paper (LSE wirelength, per-side density, Gumbel-Softmax orientation,
   net-crossing term) rather than fork the repo. Revisit only if a human confirms
   the actual license permits reuse. Phases 2–4 don't depend on the fork either
   way — the design already treats clean-room as the fallback.

**Gotcha cleared:** building the `splanc_dev` fixture hit the known atopile
**venvHash drift** (`hardware/README.md`) again — re-pinned the aarch64-linux
hash in `patches/rules_atopile-venvhash.patch` to build the fixture once. The
fixture is frozen in `testdata/`, so the tests never rebuild atopile.

**Next up — Phase 2 (placement MVP), design §9.2:** differentiable LSE-WL +
per-side density + hard-constraint penalties (fixed/edge/keep-out), gradient
descent, Tetris legalization (no orientation search yet). Acceptance
`placement_test`: 0 courtyard overlaps, 0 hard-constraint violations, all parts
inside the outline, HPWL ≤ the ato-row baseline. The graph/constraints front end
it consumes is in place; wire the loss over `BoardGraph` + `CompiledConstraints`.

### To regenerate the fixture graph (needs the KiCad `pcbnew` python)

    bazelisk --output_base=$HOME/.cache/bazel-atopile build //hardware/splanc_dev:splanc_dev
    KP=$HOME/.cache/bazel-atopile/external/rules_nixpkgs_core~~nix_pkg~kicad_python
    PCBNEW=$(dirname $(find /nix/store -name pcbnew.py -path '*kicad-base*' | head -1))
    PYTHONPATH="$PCBNEW:hardware/pnr" "$KP/bin/python3.12" -m pnr.ingest \
      hardware/pnr/testdata/splanc_dev/splanc_dev.kicad_pcb --name splanc_dev \
      --dump-json hardware/pnr/testdata/splanc_dev/graph.json

## 2026-08-21 (am) — design doc drafted; no code yet

**State:** design/strategy is done; **no PnR code exists**. Next actor starts at
Task 0 → Phase 1 in `docs/hardware/pnr-system.md` §9/§11.

- **What exists:** the design doc (problem space, cited SOTA survey, architecture,
  phased plan with acceptance tests, handoff/bootstrapping). The two boards
  (`//hardware/splanc_dev`, `//hardware/splanc_eol_tester`) build a resolved
  `.kicad_pcb` (row placement + ratsnest) — the ingestion input for this system.
- **What does NOT exist:** `hardware/pnr/` code (only this WORKLOG), the
  `constraints.yaml` fixtures, the `atopile_pnr` / `<board>.fab` rule.
- **Chosen architecture (don't re-litigate — see doc):** differentiable
  graph-relaxation placement (DREAMPlace/**Cypress**-style: LSE wirelength +
  per-side density + net-crossing routability, Gumbel-Softmax orientation) →
  FastRoute/FLUTE lookahead global route → place↔route feedback made convergent
  with a **PathFinder-style history term** (region inflation à la RePlAce) →
  FreeRouting detailed route → DRC → fab export via the existing kicad-cli
  exporters. Python + PyTorch (CPU-first). No RL / no learned router.

**Blockers to clear first (Task 0, doc §9/§11):**

1. **Cypress license** — confirm NVlabs/Cypress permits vendoring/forking as our
   placement base; if not, plan a clean-room reimpl of its math. Record the
   verdict here.
2. **PyTorch under Bazel** — add `torch` (CPU wheel) to `//requirements.in`,
   `bazel run //:requirements.update`, prove `import torch` in
   `//hardware/pnr:torch_smoke_test`. Record any wheel/platform gotchas here.

**First vertical slice (Phase 1):** `hardware/pnr/pnr/ingest.py` loads a fixture
`.kicad_pcb` via pcbnew (the `@kicad_python` interpreter, see `//hardware/atopile`)
and prints component/net/pad counts + `--dump-svg`; `constraints.py` parses the
example `constraints.yaml`. Green `ingest_test`.

**Environment reminders (full list in doc §11):** build with
`bazelisk --output_base=$HOME/.cache/bazel-atopile build //hardware/...` (the
`/workspace` mount is macOS-synced); pip = root `requirements.in` + `@pypi`;
prek hooks can abort commits (re-add + re-commit); don't push from the container.

## 2026-09-09 — Splanc routing integrity corrections

- FreeRouting merged distinct atopile nets `EN` and `en`; KiCad reported four
  shorts after the raw SES import. `pnr.specctra` now exports a private board
  with unique ASCII net aliases, preserves a checked mapping, and restores
  original names on import. Live KiCad tests verify both track memberships.
- Plane fanout previously checked track endpoints only, ignored same-net drill
  spacing, forced unchecked vias into congested pads, and hardcoded undersized
  vias. It now checks full segments, conservative pad/arc envelopes, drilled
  holes and rule areas; uses the fab geometry; and leaves blocked pads for the
  connectivity gate. It does not claim that all pads can be escaped.
- `copper_keepout` specifies an all-layer rectangle relative to a stable source
  reference. Writeback reconstructs it after placement; atopile drops the MPU
  footprint's embedded zone. Live tests verify transformed bounds and layers.
- The original Splanc USB receptacle is rated only 5V/3A. The hardware now uses
  GCT USB4105-GF-A-060 and a routable QFN TPD6S300A CC protector. Neither a legal
  placement nor a completed router run establishes manufacturing readiness.

Optional diagnostic FreeRouting bridge (run Python steps with KiCad Python):

```
python -m pnr.specctra export placed.kicad_pcb private.dsn --map net-map.json --rules rules.json
freerouting -de private.dsn -do private.ses -mt 1 -mp 10
python -m pnr.specctra import private.ses routed.kicad_pcb --map net-map.json
python -m pnr.planes routed.kicad_pcb --rules rules.json
```

Run independent DRC and quality gates afterward. The primary Bazel flow continues
using the custom detailed router; the bridge is a diagnostic/alternate backend.

### Reliable DRC identities and offset bodies

Atopile cloned 2,237 internal item IDs across repeated parts on this board.
Writeback now regenerates duplicated child UUIDs deterministically, retaining
footprint identities and rejecting duplicate parent IDs. A live KiCad regression
checks uniqueness and idempotence. KiCad's mutable KIID.Clone API is required;
the m_Uuid property itself is read-only.

Ingestion now measures unrotated body/courtyard extents symmetrically around the
footprint origin. Bounding-box width alone underrepresented offset connector
bodies, allowing an XT30 body to collide with a FET despite legal placement.
A rotated, offset-pad regression covers this geometry. The earlier 224-part
Splanc placement passes KiCad DRC with zero non-connectivity violations before
routing; it still has 499 unconnected items and is not a fabrication release.

### Thermal-via and CAD review integration

The 229-component revision includes source-controlled ground thermal vias for
TPS25730, TPD6S300A, TPS552882 PGND and both TPS25200 switches, with paste windows
kept off drill openings. Both factory assemblies build; 44 source-board checks
pass per assembly, and the placed-board audit passes 45 including the MPU keepout.
Plane-first writeback leaves 455 unconnected items and zero other KiCad DRC
violations. This is an unrouted diagnostic board, not a manufacturing artifact.

Plane fanout rejects distant obstacles by bounding-box distance before exact
segment intersection tests. Its conservative collision criteria remain intact.
The complete Python router suite passes 114 tests with 7 skips; the KiCad
writeback and ingest suites pass 14 and 6 tests respectively.

`hardware/tools/package_pcb_models.py` restores model declarations from source
footprints, copies assets and provenance into a portable review directory, and
checks that packaging preserves pad identity, geometry and nets. All 229 model
instances resolve. Simplified PD/microphone and representative barometer models
are explicitly recorded in the manifest. The native KiCad render is a placement
study until routing, mechanical geometry and power validation are complete.

### Hard electrical proximity through legalization

The largest-first legalizer could scatter small power support parts 20–34mm
from their IC even when global placement used soft groups. `group.hard: true`
now requires a fixed anchor, known members and finite positive radius. Legalizer
candidates intersect all hard group discs; tight groups reserve space before
unrestricted parts. No feasible slot raises with the component reference.
Independent metrics and PlacementReport.legal also check group distances.

Regression coverage includes distant targets, occupied group regions, invalid
radii/anchors, and independent detection of scattered parts. 23 group/constraint
tests and 15 placement/orientation tests pass. Both 229-part factory placements
remain clearance-clean with 499 unconnected items. The previous 360-unconnected
partial route is obsolete after these component moves. Power classification now
includes 21 nets; this is not a substitute for qualified power copper geometry.

### Power-first placement (`PNR_POWER_FIRST=1`, opt-in)

Converter trials put Q1, Q2, L2 and the input caps in a star around U5. The
global objective weighted every net equally, and authored 12 mm / 5 mm hard
groups anchor everything to the IC, so nothing tied the switching loop
together. `pnr/power_topology.py` (pure Python) derives from @pnr-current
envelopes, terminal budgets, plane classes/intents and the netlist: carrying
pads (series bound, pad-share guard), tiers (power stage, controllers,
passives), width weights and hot loops (Horton minimum cycle basis of the
class/net graph, shunt class = hot). It never names refs. `pnr/place/power_first.py`
minimises J1 (power trunks and hot loops), then J2 (controller/sense), then J3
(passives) as lexicographic stages with epsilon guards. Stage 1 runs 8 batched
starts. The continuous overlap reserves one legalizer grid cell, and the
legalizer (tier order, relative targets, trial-pack look-ahead, pour channels
on trunk nets both facing parts carry) gets one bounded retry from the stage-1
runner-up.
synth_native records placement and routed power quality and ranks by open hot
loops, q_band and crossings after opens and violations. `model.py`,
`channels.py` and `batched_cost.py` are unchanged. With the flag unset,
placements, reports, trial/library records and capture payloads are
byte-identical to the base (fixed PYTHONHASHSEED; captured channel fields already
vary with the hash seed). Converter, 12 outlines x 3 seeds: legal 36/36 (default
30/36), buck-loop pad gaps median 28.1 -> 12.2 mm, Q1-Q2 15.7 -> 7.6 mm, power-net
MST 113 -> 79 mm, signal HPWL +18%. Routed effects (vias, single-layer power,
opens) still need a native A/B. The SW2 pin-25 open is a router pin-access
issue outside placement. Tests: tests/test_power_first.py; fixtures via
tests/power_topology_golden.py.

Repair after review. (1) Pour channels first dropped escape demand for any
trunk net of either tier-1 part, so output and input caps packed against the
16 A switch-node rows of U5 (SW2) and Q1/Q2 (SW1) at no channel cost. Copper
pours across a gap only on a net both parts carry, so only that intersection
is exempt now. Converter, 36 runs: layouts with a tier-1 switch-node row blocked
<1 mm by a power part off that net 13 -> 0 (default 0/30); channel shortage
median 36.7 -> 19.6 (default 12.1); unshared switch-node shortage 11.5 -> 2.3
(default 3.2). (2) The retry gate compared total J1, which the VOUT trunk
dominates, so legalization doubled the buck loop in 10/36 runs and never
retried. Every hot loop's Lambda is now also checked (retry above RETRY_RATIO x
its continuous value, floored at links x (clearance + grid)). The two attempts
are ranked by legality, then sum w_L Lambda_L with an EPS[0] tie band, then J1.
Buck gaps median/p90 12.2/19.4 -> 13.2/17.6 mm (default 28.1/38.8), and runs
with legal gaps > 1.5x continuous 10 -> 5. Retries rise 3 -> 18 of 36; the
median placement takes 1.8 s. The correct pour rule costs about 2% J1 and
power MST (799 -> 817, 78.6 -> 80.5 mm); signal HPWL is unchanged (+18% vs
default).

## PNR_SHOVE=1: make-room transactions and power scheduling (converter block case)

Case: blocks/nb3-pf/d5be6b6d0e99/native/board_converter-s1-35.75x27.75 (4 opens:
U5.13->R13.2 p5v-hv, and SW2 U5.21/U5.25/C9.2/L2.2). Everything below is behind
PNR_SHOVE=1; with it unset nothing new is imported and worker results/inventories
are identical to src10.base (only KiCad's random UUIDs of new items differ run to
run). New package pnr/shove (targets, geom, qp, world, relax, ladder, gates,
control, __main__).

Scheduling and access (stage A):
* In-pad escape for trapped leaves: power_plan(_force_in_pad) retries once with the
  terminal's in-pad array when its surface access points exist but are all walled in
  (in_pad_skipped); via_in_pad.general_attach_sites samples non-analytic custom lands
  (the L-shaped U5.13 ISN pin: 0.35/0.20 on y=45.025, x 51.50-51.71).
* Trunk-first targets: inspect pre-unions the trunk hops (highest terminal contract ->
  uncontracted bulk-cap land within 4 mm -> net-scope carrier) and marks them
  trunk=True; scheduled_route_jobs routes trunks first. Trunks search the whole
  board; branches search local box U root copper box + 1 mm.
* Array-attached lands are branch roots in the 'existing' strategy (U5.21 -> U5.25
  1.7 mm bridge instead of a 10 mm run to L2.2).
* Sense-escape reservation: every power worker reserves, in its Oracle only, the
  in-pad via + 1 mm stub of each still-isolated sense leaf (terminal <= 50 mA) of
  another net. Routing sense leaves first was tried and dropped: it closed the
  phase-03 board but cost two SW2 opens on the final board.
* Same-net self hits are dropped from static_blockers; result.json keeps
  forward_policy after the reversed retry.

Make-room worker (python -m pnr.shove, hooked after electrical_blocker_repair for
failed power/plane targets, <=3 per nested early loop, <=6 per refinement loop):
L1 copper QP (delta-relaxed wish plan from power_plan, joints/welds/rigid banks,
Hildreth dual ascent, caps signal 0.6 / power line 0.35 / power via 0.25 mm),
L2 adds small nearby parts (<=4 pads, not locked/fixed/intent; 0.5 mm cap, soft
lands, claim attaches ride with the part), L4 rips whole local signal nets (<=3,
bounded negotiation) and/or one sub-trunk-width power branch, routes the target with
native_electrical, restores signals with keyhole_region and power branches with
native_electrical. Commits replay the plan through native_electrical --replay-plan,
then pnr.shove.gates judges against the ORIGINAL board (partition, lost connections,
pad entries, reference, new forbidden SMD vias, native DRC keys/dangling/per-moved-uuid,
strictly fewer opens, no leftover PNR shove:/leaf: areas, unjustified sub-width not up).
Failures fold the certificate's solid owners into static_blockers.

Case results (src10 contracts from the prepare worker, PNR_SUBBOARD=1; scratch):
* final board 4 -> 0 opens, 0 DRC violations before/after, all gates clean (jobs in
  scheduler order, worker then shove as the hook runs them): SW2 trunk by L4 (rip
  COMP+MODE, 3x0.35/0.20 in-strip array, 1.19 mm B.Cu, both restored), U5.21 by L1
  (0.049 mm), C9.2 by L2 (C19/C24/C9/R10 nudged <= 0.083 mm), U5.13 by the forced
  in-pad via whose escape the earlier jobs had to leave free. The user's picture (standard via beside U5.13):
  L2 moves the VOUT bank/landing and C24 0.15 mm north (not +x: C24.1's via-to-SMD
  keepout and FB via e5536d17 block +x), 4 -> 3, DRC clean.
* phase-03 board 22 -> 18 (all four case opens) with stage A alone.
* One emulated early-power sweep (worker per scheduled power job, shove on failure)
  from the case's real phase-03 input (02b board): every power job routes with the
  plain worker, 46 -> 18 opens, only signal targets left, 0 DRC violations.
* One full_iteration of the layout (1 worker, 600 s, PNR_SHOVE/POWER_FIRST/
  FANOUT_RESERVE, intermediate build without reservations/bulk-cap hop): objective
  [0,0,0,54,0,2] vs [0,0,0,15,0,4]; SW2 and U5.13 closed (U5.13 by L2 with C24 and C9
  nudged 0.12/0.21 mm); open: C24.1's 1.5 mm VOUT landing and ILIM (signal).
Feedback: native placement trials are still off in hier (full_iteration passes
--route-only unless PNR_SHOVE=1 and PNR_NATIVE_PLACEMENT=1); feedback.json gains a
'shove' section; synth_native writes accepted nudges back into the layout so
hier.assemble stays rigid. Tests: tests/test_shove.py, tests/test_shove_native.py.

### PNR_SHOVE=1 review fixes (placement legality, necks, branch roots, template write-back)

* Part nudges are checked against the hard placement constraints. New
  pnr/shove/placement.py: the KiCad side lists moved footprints and rejects locked,
  rotated/flipped and source-owned parts (plane-access power array / copper keepout
  owners, as incremental_place refuses); the PnR runtime (`python -m
  pnr.shove.placement`, needs yaml) runs place.metrics.hard_violations on both
  build_graph graphs under the loop constraints.yaml and rejects any new violation
  (HARD group radii, rows, keepouts, outline, fixed poses/rotations, sides, overlaps).
  Runs as G0 inside L2 before commit and again in the whole-transaction gate;
  without --constraints/--placement-python a nudge is unverifiable and rejected.
  native_loop passes the loop constraints, its own interpreter and the origin board;
  nudge_candidates also excludes source-owned parts.
* Cumulative nudge cap: 0.5 mm from the ORIGIN placement (the outer loop's
  baseline board, handed to nested early phases as --shove-origin-board), not from
  each transaction's start. The QP carries 16 inscribed-polygon rows per part; the
  ladder drops parts with < 0.02 mm budget left; invariants and the gate re-check.
* Courtyards of both sides are QP rows (B.CrtYd too), same side only.
* Necks are rigid: power/plane copper narrower than its land's required entry width
  binds both vertices to the land's joint (world.rigid_necks), so it cannot stretch
  or turn. Per moved power line |dlength| <= min(0.5 mm, 5 %) as designed (the
  0.15 mm floor is gone) and is now also a linearised QP row family, so the solver
  searches inside the guard instead of being rejected after the fact.
* gates.unjustified_subwidth judges each thin power track by the terminal it
  serves: a track entering a terminal land must be at least its outer width, a
  source-bounded neck of it (neck_budget: length <= neck_max_length_mm, loss/drop),
  or a branch landing on an in-pad-array-attached land; and it must be a valid neck
  or as wide as a terminal its sub-width branch serves (joined through shared ends
  and vias inside its copper; an in-pad via under a collector serves its land).
* A4 branch roots: only full-current array-attached terminals (terminal rms/peak >=
  the net envelope and the branch, as qualified_tree_pads) - a sense leaf's single
  in-pad via (U5.13) never roots another branch; U5.25 (5 A = net) still does.
* synth_native write-back: any disagreement between template instances, including
  one instance not nudging a part another nudged or unreadable nudges, fails the
  record (nudged_conflict); an agreed nudged layout must add no hard violation in
  the block frame (instance_board: block rectangle, groups, rows, overlaps) or the
  record fails, so no illegal pose reaches hier.assemble / top placement.
* Case re-verification (src10 contracts, scratch): final board 4 -> 0 opens, 0 DRC
  before/after, every gate incl. placement clean (L2 nudges C19/C24/C9/R10 <= 0.083
  mm total from origin, no new hard violation); phase-03 board 22 -> 18 (all four
  case opens). The user's-picture variant (standard via beside U5.13, in-pad
  disabled) is no longer accepted: it needs a 0.148 mm (11 %) stretch of a 1.35 mm
  VOUT branch segment, beyond the 5 % per-line guard; with the guard in the QP the
  solver finds no feasible placement (certificate: U5.15 / FB track / VOUT track).

## PNR_FEEDBACK=1: routing failures drive later placement rounds (synth_native, halving)

Before: feedback.json was written for every block trial and halving candidate and read by
nothing; in the hier flow the only routing -> placement effect was PNR_SHOVE nudges
(<= 0.5 mm, inside one evaluation). Opt-in now (PNR_FEEDBACK=1 plus --rounds/--generations
> 0; defaults unchanged, place() bit-identical with pair_weights=None, golden-tested):

* pnr/feedback/signals.py: one evaluated round -> the failed cross-part connections keyed
  by block-local path (template) or ref (top level), mode, shove no_make_room; same-part
  failures counted, never acted on; per-part scores kept as diagnostics only (the signal
  study found them IC-dominated and moving their parts goes with more opens). Router key
  (router, stage, budget, power-first, fanout, fab profile, inputs sha); imports must match.
* pnr/feedback/table.py: per-scope failure rates, rebuilt each round from records;
  floor = >= 90 % of >= 6 evaluations (reported, never a target); lineage failure counts;
  router-separated.
* pnr/feedback/moves.py: PULL moves one non-anchor end of a failed connection (weight
  1 + 2 x lineage failures + power + no_room, cap 5) on a 0.25 mm lattice <= 3 mm (tier-1
  power parts only for tier1-tier1 connections, <= 1 mm), any allowed rotation; every other
  part keeps its exact parent pose; target must shorten >= 0.25 mm, lineage cap 4 mm from
  the root pose; legality = mover clears everything by the board clearance with plane-array
  reservations + no new hard violation + driver check (template rigidity over all
  instances / halving source checks) + power guard (no new power crossings, q_band <= +1).
  RAND = matched random control. pair_weights = population attraction for 'prior' samples.
* place(..., pair_weights=): sum w * |pad_a - pad_b| in global_place and in every
  power-first stage (x W); hierarchical_place maps flat pads to macro pads.
* synth_native --rounds R: round 0 = stage A/B or --import-trials (never re-evaluated);
  parents = top ceil(P / 2^(g-1)) with a PULL child; children, RAND, prior/fresh evaluated
  like stage B (tag suffix -g<g><arm><j>-<parent sha>); one pool, unchanged rank_key; stops
  on --enough / --gen-plateau; resume replays the plan and skips existing tags.
* halving --generations G: between the native rung and deep; --seed-from imports earlier
  runs' native records (--n0 0 skips own stages); children from the parent's evaluated pose
  (shove nudges / USB rescue included); entry rung1 (best half promoted) or native; repeat
  child in generation 1 measures noise; deep takes the best of the whole native pool.
  Library mode moves library blocks rigidly (translation) and glue parts alone.
* pnr/feedback/report.py: mechanical progress report (complete per evaluation, per-arm
  child - parent, PULL vs RAND sign test, repeat delta, load, disk).
* Tier-1 movers: no pad moves more than 1 mm (rotations included); RAND never rotates them.
* Offline replay (84 real block layouts nb3-pf/nb4-pf/nb5-*): 58/65 layouts with cross-part
  failures yield a PULL child; 90/90 PULL and 62/62 RAND children pass an independent
  legality re-check and move only the mover; median move 0.79 mm, target shortening 0.90 mm.
* End-to-end (scratch, one native evaluation, 1 worker, 240 s budget, 382 s wall): nb5-noshove
  converter import, round 1 parent s1-34x34 (2 opens at 600 s), PULL child moved L2 0.56 mm
  (U5.21-L2.2 0.34 mm shorter) -> 3 opens; record carries gen/arm/parent/op_detail/fb, the
  library ranks it among the 12 imports (none re-run). Plumbing check only (budget differs).
* Library-mode hierarchical placement with the nb5 libraries fails legalization (MB00: no
  free slot) for 6/6 seeds with or without weights: pre-existing, not from this change.

### PNR_FEEDBACK review repairs (same day)

* Code identity in the router key: `signals.eval_code_files` hashes the static import closure
  of the evaluation entry points (full_iteration, hier.native_block/synth/blocks; function-local
  imports, `-m pnr.x` targets and package `__main__` followed; drivers pnr.feedback, pnr.mc,
  hier.synth_native, hier.top excluded; pnr.shove only for the shove router, every outside
  import of it being PNR_SHOVE-gated). Records made with PNR_FEEDBACK=1 stamp `code`; imports
  without it are traced to their tree through `electrical/native-loop/source-inputs/origins.json`
  (the --annotation-source the driver passed), refused as unknown if any closure module is newer
  than the round. nb5-shove = src10.frozen (d1b507fd94: shove/world.py without the 0.15 mm floor
  + pair_weights plumbing) != src11 shove f5f699666e; h4 = src10b = src11.base (7364369252).
* `--import-code-mismatch {error,warn,rebase}` (both drivers, default error), `--import-rebase N`:
  rebase re-evaluates the stale imports' own router inputs (blocks: the stage-A layout or the
  stamped `input_layout`; halving: the seed placed.json) under this code as round 0 (arm rebase,
  tag -g0b-<sha> / id rb-<id>); stale records are never ranked, counted for --enough, pooled in the
  failure table or taken by deep.
* synth_native --import-trials selects only templates with imported native records (--block
  naming another is an error); imports are round 0 (gen 0, source_gen kept).
* Fresh/prior: a stage-A (seed, outline) already handed to any router is not spare (dedup by
  base tag + stamped/stage-A input keys, not the nudged layout); 'prior' is skipped when the
  table has no pair weights (both drivers), instead of reproducing the unweighted sample.
* RAND records `matched` = the parent's k=0 PULL child; the report pairs exactly those (best-of-
  several PULL vs one RAND favoured PULL under the null) and prints n and the smallest reachable p.
* halving --seed-from: the seed run must have finished its native stage (status.json) unless
  --seed-allow-running; torn dataset lines skipped with a warning; the import set and code policy
  are frozen at the first start (a 'seed-from' dataset record); a resume whose regenerated child
  differs in parent/arm/poses_sha from the stored gen-place record stops ("resume plan changed").
* E2E (one native evaluation, 1 worker, 240 s, 385 s wall): nb5-noshove converter s1-34x34
  rebased under src11 (plain): 2 -> 2 opens; input = nb5 stage-A layout; stamped code 97e9032431
  = the code derived from the new round's origins.json; library ranks only the rebase (12 stale).

## src12b (2026-09-29): USB pair chain after a via bridge (opt-in flags, default off)

src12b = src12 + opt-in flags in pnr/native_electrical.py and pnr/paired_bootstrap.py (unset =
src12 behaviour; the 336 native_electrical/paired_bootstrap tests are unchanged, +12 new in
tests/test_pair_post_bridge.py under KiCad Python). Validation and commands: hier/pairs/fix-README.md.

* PNR_PAIR_POST_BRIDGE_SURFACE=1 (_pair_plan_order): every routed surface leg is checked with the
  exact endpoint graph (surface_leg_graph_failure -> path_metrics). Oracle.clear ignores same-net
  copper, so the D2->U6 leg could cross the stage-0 bridge's via->D2-pad fanout; that loop was only
  rejected at the end (pair_endpoint_graph_invalid). A looping leg is now pair_surface_leg_cycle;
  after a bridge the next fallback is a surface leg from that bridge's via pair (offsets = measured
  prefix - fanout; D2 fanout becomes a stub as in the old bridge->bridge reuse; 0 new vias), then
  the old reuse bridge. Records: post_bridge_legs, segment post_bridge_start.
* PNR_PAIR_HAND_SWAP_TRIAL=1 (paired_bootstrap, needs PNR_PAIR_JOINT_TOPOLOGIES=1): one extra
  trial after trial 0 on the unmoved pose with PNR_PAIR_JOINT_HAND_FIRST=-1 (pair_plan runs hand
  -1 first within each joint seed). Hand +1 alone can use the whole 60 s joint window.
* PNR_PAIR_JOINT_FAIR=1 (pair_plan): first config per hand capped at window/hands-not-started.
  Works at PNR_PAIR_SEARCH_SECONDS=300, but at 90 s it loses p007 (hand +1 needs 22-46 s) -> use
  HAND_SWAP_TRIAL instead. PNR_PAIR_FALLBACK_RESERVE_SECONDS (default 30) did not change that.
* PNR_PAIR_PREFER_INLINE=1 (pair_plan score): (vias, stub legs, length) - keeps D2 in line when
  an equal-via in-line route exists.

Recommended: POST_BRIDGE_SURFACE=1 + HAND_SWAP_TRIAL=1 at the default 90 s trial: h4p032, g1c04,
g1c05 0/5 -> accepted (4 vias, skew 0.203-0.285 <= 0.3, 0 DRC violations, opens 266 -> 260);
controls p007 (t0) and h3p010 (production route, one trial later) identical to production.
Still failing (stage 1, TP1 landing blocker + shared uncoupled budget = design proposals 2/3):
ab-h4p030, ab-deepS, ab-h5r0, h3p035, g1c00.

## Hard-rung gap fixes (2026-10-03): stackups, via kinds, sides, length tuning, router speed

Branch `claude/gap-fixes` (five tracks merged on `claude/ladder-hard-rungs`). New behaviour comes
from the board's own inputs; a board without them routes as before (the eight ladder cases and the
four showcases give byte-identical `placed.json`, `routes.json` and boards on two seeds).

| Input (what turns it on) | Behaviour | Code |
| --- | --- | --- |
| a KiCad stackup with a `power`/`mixed` layer or a no-track `.kicad_dru` rule (all conditions: `docs/hardware/pnr-inputs.md`) | every signal layer routed, any layer count; plane drops planned with the escapes; planes formed by writeback | `pnr/stack.py`, `router.layer_plan` |
| the rules' `via_policy` (ladder drivers: `via_policy.board_policy`) | blind, buried and micro spans the stack can build, one build per board, return ties | `pnr/via_policy.py` |
| `board.sides: double` | placement chooses the side of free parts (global relaxation, legalizer, flips and swaps) | `pnr/place/sides.py`, `place/detail_moves.py` |
| `diff_pairs` / `length_match` (`tuning.meanders`, `tuning.placement`, both on) | meander tuning to KiCad's length measure; matched-leg placement | `route/detail/tune.py`, `length_model.py`, `place/matched.py` |
| none: default kernel (`route/detail/kernels.py`) | native A*: the packed search over dense per-net fields, in C (identical routes; packed Python where no library loads) | `route/detail/dense_maze.py`, `packed_maze.py`, `native_maze.py`, `native/maze.c` |
| none: `PNR_EXACT_SEPARATION=recover` (default; `full`, `off`) | a route the halo model leaves open is routed again with the exact pairwise separation, kept if fewer open | `route/detail/exact_route.py` |

Opt-ins: `PNR_MAZE_KERNEL=packed` (the search in Python; the default is `native`, the C loop of
`route/detail/native/maze.c` loaded with ctypes from `PNR_MAZE_LIB`, beside the package, Bazel's
runfiles or the yapnr wheel's `yapnr/native/`, only when it records this `maze.c`'s sha256; packed
otherwise), `PNR_PACKED_MAZE=0` or
`PNR_MAZE_KERNEL=reference` (the reference A*), `PNR_TUNE_STRICT=1` (a tuning error raises instead
of keeping the untuned route). `run.py`: `--maze-kernel packed|native`, `--reference-maze`,
`--exact-separation`, `--design-json` (the length-matching scratch designs); `--packed-maze` is a
no-op kept for recorded configurations.

Limits: a grid with a via model routes on the reference kernel (the dense fields do not model
spans), so the exact recovery skips it, and the tuner only adds meanders there; the native KiCad
repair loop and the hierarchical driver add through vias only; matched-net parts keep their side;
the detail side pass has no channel-aware cost; a placement courtyard is centred on the footprint
origin (the THT-header rung never legalizes); region and align constraints are on
`claude/gap-constraints`.
