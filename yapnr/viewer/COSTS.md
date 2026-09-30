# Component placement cost inspection

Select a recorded global or legalizer phase, then click a pad or use Jump to component.
`PNR_COST_CAPTURE_DIR` records the phases alongside normal `PNR_LIVE_DIR` events.

Select a component by clicking a pad or using Jump to component. The panel gives its composite score
and each raw term, weight and weighted contribution. Select a field term; use Fit board to see the
full field. Download cost data preserves the report. Snapshot annotations include the selected
component, phase, objective/context hashes and term totals.

## Three distinct contexts

1. **Recorded global objective**: immutable optimizer state immediately before the final gradient
   update, including soft rotation probabilities, effective pad offsets, inflated extents,
   constraints, and exact weights. Displayed footprints use the most likely cardinal rotation; cost
   calculations retain the actual mixture. This is a continuous, possibly overlapping intermediate
   state, not a legal or routed board.
2. **Recorded legalization**: actual evaluated legal centers/orientations when the component was
   placed. The stationary neighbors and continuous target are saved separately for every component.
   Later parts were not yet occupied. The final placement is displayed, but every component's
   score/field retains its sequential decision context. Source-fixed components have no legalization
   decision score.
3. **Retrospective routed checkpoint**: available only with an explicit `--cost-contexts` file. It
   recomputes a rigid-pose objective and labels it retrospective. Without a recorded phase or
   explicit context the viewer reports the score unavailable, rather than guessing historical
   weights or inflation.

These objectives must never be added together. Native routing acceptance and electrical
qualification remain separate gates.

## Global terms

The scalar board objective is the sum of these terms. Shared net/group terms are divided equally
among distinct participating components; pair-overlap cost is divided equally between its two
components. Thus every component's terms sum to its score, and all component scores sum to the board
objective. A component's allocated score is not its marginal effect on the whole board.

| Term                    | Raw value                                                                                   | Weight                 |
| ----------------------- | ------------------------------------------------------------------------------------------- | ---------------------- |
| Smooth wirelength       | Sum of log-sum-exp bounding spans per net, smoothing gamma 1 mm                             | 1                      |
| Courtyard spreading     | Overlap area of clearance/inflation-expanded component rectangles on occupied faces         | `w_spread`             |
| Outline penalty         | Squared overflow beyond placement bounds for movable components                             | `w_bound`              |
| Plane bounding-box area | Smoothed bounding-box area of each plane net                                                | `w_plane`              |
| Plane separation        | Pairwise overlap area of plane-net boxes                                                    | `w_plane_sep`          |
| Edge alignment          | Squared component distance from its required edge target                                    | Per source constraint  |
| Group radius            | Squared excess separation beyond the source group radius                                    | Per source constraint  |
| Keepout overlap         | Intersection area against reserved rectangles                                               | `w_keep`               |
| Local capacitor loop    | Nearest associated device power/return pad distances from each annotated capacitor terminal | Per capacitor relation |

Zero terms remain visible with their configured weight. Where different source constraints
contribute with different weights, the report retains their weights and weighted sum rather than
claiming one universal weight. Typical defaults in the diagnostic are 1, 20, .05, .35 and 40 for
spreading, outline, plane area, plane separation and keepout respectively. Consult the selected
report's parameters for its actual values.

The global field is the **whole-board objective change** when moving only the selected component.
Other components, face and orientation mixture are frozen. Each term's field is its whole-board
delta. The 1.5 mm sampled field is a placement proxy, not a copper clearance/DRC result.

## Legalization terms

```text
candidate cost = squared distance from continuous target
               + 25 × surface escape shortage
               + weighted local capacitor loop
```

The loop term records raw distances and source weights, including individual constraint
contributions. All explored cardinal orientations and exact evaluated sites are retained in
compressed arrays. The UI's 0.25 mm pixels show the minimum evaluated selected-term cost in each
pixel; hover reports the exact chosen site and total. Grey means no evaluated legal center in that
pixel, not a prediction that routing is impossible. The chosen orientation may differ from the field
orientation selected for comparison.

The production model currently has no capacitor-role constraints; its local capacitor-loop row is
zero. This exposes missing policy rather than pretending the experimental capacitor placement
objective is integrated. Fixed components have global objective shares but no sequential legalizer
decision score.

## Capture and provenance

Opt in for future source placements with `PNR_COST_CAPTURE_DIR` plus the normal `PNR_LIVE_DIR`.
Capture files and arrays are immutable and hash bound. The UI evaluates on a single background
worker, caches results by board/capture/context/model hashes, rejects stale asynchronous selections
and rasterizes the field once per selection. It does not put NumPy/DRC work in the browser render
loop. Captures are opt-in and do not influence placement decisions.

Source hooks live in `hardware/pnr/pnr/place/{model,legalize,cost_capture,cost_inspect}.py`. Set
`--engine-runtime` to the frozen engine a run used (the directory containing `pnr`); otherwise the
viewer uses the engine it imports. Replay requires the capture's model hash and selected layout to
match. Old native relocation/probe events lack full additive decomposition; the inspector does not
misrepresent them as recorded scores. The fields cover continuous placement and sequential
legalization, not the native routing acceptance tuple.

The global field is whole-board cost change, not the allocated component share: moving one component
may change other components' allocated shares. Data downloads and annotation snapshots preserve
phase and source provenance.

## Validation

- Numerical tests compare replay to the actual Torch loss, additive allocation and independently
  recomputed field deltas.
- Capture-on/off tests require identical placement; the real 137-component case also matches
  byte-for-byte.
- Cache tests reject a reused capture attached to a different layout and changed capture/model
  hashes.
- Browser checks cover phases, term rows, evaluated orientations, fixed parts, immutable layout
  snapshots and draft persistence.
- Fields are placement proxies, not routing-clearance, current-capacity or impedance certificates.

## Recorded re-placement probes

A `routing-probe` phase records the exact current-site score and all evaluated candidate sites for
the selected component. Its additive terms are layered path length (1/mm), through-layer transitions
(3 each), foreign-copper crossing estimate (100/mm), pad-to-grid approach distance (1/mm), and
unreachable endpoints (10,000 each). Single-part translation also adds 0.1 times endpoint Manhattan
distance. Capturing path terms does not choose a different path: it records the terms along the
existing Dijkstra winner, including deterministic ties.

For a K-part batch, all K pads and their incident-net copper are held out. Fields retain stationary
endpoints only; omitted plane/internal-only nets are explicitly listed. A component not evaluated in
this phase has no invented score. The batch ranking separately adds 0.1 whole-board HPWL. If the
hierarchical capacity screen runs, that score replaces the probe ranking and is decomposed into
unreachable branches, capacity overflow, near-saturation, wirelength and via demand. The earlier
screen terms are preserved separately and must not be added to the capacity score.

Initial-start captures carry their own lane, seed and iteration. Context restores even after failed
legalization. A locked component has a global objective share but no sequential legalization score;
the UI reports no decision rather than a synthetic zero.
