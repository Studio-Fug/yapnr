# Energy track optimizer: isolated prototype v0.2

Historical standalone candidate generator and transactional controller.
The engine integration now lives in `hardware/pnr/pnr/energy_track*.py` and is
documented in `docs/design/energy-track.md`; these prototype files preserve the
earlier experiments and their more limited validation scope.
The optional spatial follow-on is documented in [SPATIAL.md](SPATIAL.md); it is
default-off and preserves the separately delivered v0.1 baseline. The production
historical experiment did not alter the production engine or active board.
`prototype/hook.py` is a lazy, default-off
hook; no existing engine code imports it.

## Measured results

- Real `SPIA_MOSI` / `In2.Cu`: 7 bends → 1; length stays
  9.125093586810731 mm. Energy 10.175093586810732 → 9.275093586810732
  mm-equivalent. Generated from the original witness; the earlier proof's new
  path is not an input.
- Fresh KiCad 10.0.6 baseline/candidate DRC: no new violation keys, no new opens.
  Existing 31 opens and 37 dangling-item violations remain. Same connectivity
  partition and objective `[37,87,0,114,4,31]`. The resulting board is NOT clean.
- Joint refill: RADIO_IO effective resistance 45.218274 → 45.667370 mΩ
  (+0.993174%), inside the existing 165 mΩ budget. Other rail outcomes and
  reported resistances unchanged, including existing open/failing rails.
- Synthetic neighbors: B cannot improve first. A shortens 10 → 4 mm, requeues
  B, then B shortens 11.159797974644665 → 10 mm. Two accepted transactions,
  four attempts, convergence. This is geometry-only, not a native board test.
- Synthetic two obstacles: length 11.65685424949238 → 10.912473541833065 mm;
  both obstacles remain on the witness side with hard distance clearance.

## What the construction actually means

This interprets the informal “reverse Steiner” idea as a bounded, witness-based
roadmap, not a literal Steiner solver, continuous optimal-control method, PDE
or global geometric shortest-path solver.

1. Keep the existing legal polyline as a feasibility witness. Reject illegal,
   self-intersecting or contract-violating witnesses. Grow a local corridor by
   buffering that polyline; radius and growth levels are explicit.
2. For every relevant fixed obstacle, construct candidate contacts on a
   clearance-offset contour, including all polygon components and holes. Add
   octilinear connectors and original vertices. Candidate generation uses
   conservative polygonal offsets; validity does not rely on those offsets.
3. Check each proposed segment against the actual point/line/polygon geometry
   using Euclidean distance, with track width, obstacle radius and clearance.
   For the real adapter, also run exact native KiCad shape/contact predicates.
4. Connect candidates in witness arclength order. Growth spokes retract each
   candidate to the original path. A swept-polygon check prevents crossing
   fixed copper / switching an obstacle to the other side. This is a
   conservative local homotopy restriction, not complete homotopy enumeration.
5. Count shortest remaining length backward from the endpoint on this finite
   graph. That reverse-cost tree is an admissible heuristic for an
   incoming-edge-state energy search. The result is one start-to-end span
   among candidate contacts, never the complete contour around an obstacle.
6. Keep only strict objective improvements that obey hard geometry, length,
   anchor and protection rules. Commit only after the external transaction
   gate succeeds. Refuse/timeout leaves controller state unchanged.
7. Requeue tracks in the changed region, including a validator's additional
   refill-affected region. Deterministic ID ordering, hysteresis, fingerprints
   and attempt/state/edge/transaction/time budgets bound repeated work.

Energy units: `1.0 × length_mm + 0.15 mm × bends +
0.0 mm/rad² × sum(turn_radians²)` by default. Curvature weight is explicit
and tested; electrical constraints are never soft energy penalties. Hard
no-length-increase is the default. Optional min/max length are acceptance
constraints, not a promise that a matched-length route will be found.

## Important limits and refusal cases

- With the spatial option off, neighbor discovery and per-track obstacle collection are LINEAR SCANS.
  Sparse requeue is not sublinear discovery. Each accepted move sorts/scans N
  tracks, then checks dirty-region intersection: O(N log N) sorting plus O(N)
  geometry checks. No adjacency matrix is used. Opt-in v0.2 spatial lookup and its remaining
  linear/global costs are documented in SPATIAL.md.
- The finite, capped roadmap is incomplete. Node pruning, the witness-progress
  DAG, conservative growth spokes and homotopy checks can miss legal
  improvements. Minimum means minimum of the explored unconstrained energy
  roadmap; arbitrary path guards and length lower bounds can require a more
  expensive alternate label pruned by the present state dominance rule.
  Such cases fail closed / retain the witness, without claiming optimality.
- Existing feasibility does not guarantee space for additional length or skew
  correction. There is no serpentine synthesizer or joint pair/shove planner.
  A bounded group transaction API checks atomic proposals, but group proposal
  search is absent. Pair/protected tracks are locked even in group mode.
- Metadata, anchors, width, net, layer and vias cannot be changed by a path
  replacement. Locked/grouped plane copper is refused by the real adapter.
- Dependent-plane generation is explicitly provisional. Only the exact plane
  IDs are removed from the planning oracle and fixed-obstacle homotopy set.
  Their native clearance is checked again after refill, with the full oracle.
  The transaction checks DRC, connectivity, reference contracts and all
  configured rail IR results before recording isolated no-regression success.
- The existing LVDS skew values are null and four pairs are already
  unqualified. They are not silently promoted to pass. Reference-failure lists
  are empty, which is not full RF certification. No new full-wave/EM check,
  SI simulation or additional RF authorization was performed.
- Shapely uses floating-point polygonal geometry; analytic distance is exact
  for the represented segments/polygons, not exact rational arithmetic.
  Disc/capsule radii are carried analytically. Coordinates are quantized to
  1 nm. Native KiCad DRC remains the board-level authority.
- IR uses the engine's existing 0.1 mm raster, temperature/current/load-split
  assumptions. It is a regression check, not a new physical accuracy claim.
- One selected real-chain fixture proves this case, not boardwide generality.
  Production promotion remains blocked on broader coverage and missing hard
  electrical checks. The algorithm is not automatically enabled in radar.

## Files

- `prototype/energy_track.py`: geometry/energy planner, bounded transactions,
  dirty-neighbor loop.
- `prototype/real_case.py`: isolated KiCad/refill/native/IR adapter.
- `prototype/hook.py`: explicitly enabled, lazy optional hook.
- `tests/test_energy_track.py`: adversarial geometry and transaction tests.
- `results/real/validation.json`: measured real-board gate results.
- `results/dependency.json`, `results/multiple_obstacles.json`: synthetic data.
- `figures/`: the three rendered, checked diagrams.
- `source.patch`: standalone added-source patch.
- `fixtures/`: original board/project/rules plus original chain witness.

## Reproduce

Use Python 3.12 with Shapely 2.1.2. The real adapter also needs NumPy and the
headless KiCad 10.0.6 Python/CLI used by the engine. Matplotlib 3.11.2 is optional
for re-rendering figures; it is not a routing dependency. No new large toolchain
is needed.

Run focused tests from the extracted package root:

```sh
PYTHON=python3 bash scripts/test.sh
```

Use an existing checkout of yapnr pinned to
`dd1ba7d47c2e18607f1772a9a48acabe8a62045d`. Both its repository root and
`hardware/pnr` must be on PYTHONPATH so data-driven fab profiles resolve.
Run the isolated real fixture; choose a disposable output location only:

```sh
ENGINE=/path/to/pinned/yapnr \
KICAD_PYTHON=/path/to/headless/python \
KICAD_CLI=/path/to/headless/kicad-cli \
bash scripts/real_case.sh
```

The script explicitly selects `pcbway-adv-6l-rf`, retains the fixture's custom
DRU and executes every external worker under a timeout. The board hash must
remain `81ba7ce92b644df7b07218b10d402293632716c924a32147aa28efc30a642dde`.
Do not substitute an active board or suppress any failing gate.

## Verification status

43 prototype unit tests passed (28 baseline plus 15 spatial follow-on tests).
The pinned upstream glosser's 65 pure-geometry
tests passed. Source privacy scan passed. Full Bazel and presubmit results are
recorded separately in `results/check-status.json`; focused passes do not imply
those aggregate gates passed. No engine source was edited.
