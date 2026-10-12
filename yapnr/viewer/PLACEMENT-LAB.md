# Placement laboratory

Open `placement-lab.html` from the live viewer. The lab runs the real single-start
`pnr.place.model.global_place` optimizer locally, then the regular legalizer. It is a
placement experiment, not a route, electrical acceptance, or replacement for the
production power-first multi-start campaign. Recorded power-first A/B cases can be
reviewed in the same workspace, with their mode and seeds retained.

For agent control, use the [versioned HTTP/Python/JSON CLI API](PLACEMENT-API.md):
`yapnr placement schema` discovers its operations. Agents can drive runs, inspect
individual decisions, render actual geometry and discuss canonical notes without a browser.

## Live objective

Choose an input, seed, and iteration budget, then **Start new run**. Edit the expression
and press **Apply live** to change weights at the next optimizer iteration boundary.
**Pause** preserves the optimizer state; **Resume** continues it. **Stop** retains saved
frames. A single worker uses one Torch thread, at most 5,000 iterations and 600 seconds
per run, including pauses. Interrupted and timed-out runs remain in the experiment list.

The DSL accepts nonnegative weighted sums of existing terms, parentheses, `+` and `*`:

```text
wirelength + 2*spread_overlap + 20*outline + 40*keepout
+ 0.05*plane_area + 0.35*plane_separation + 4*pad_locality
+ source_constraints
```

Coefficients are finite and at most 1,000. Calls, attribute access, exponentiation,
nonlinear products, and Python evaluation are unavailable. `source_constraints` must
appear exactly once: authored group/edge/relationship intent and matching remain active.
Lowering a smooth penalty never disables final hard placement checks. The expression
controls **global placement**; legalizer displacement and channel weights remain separate.
Soft rotation mixtures drive the optimizer; the board displays each mixture's most likely
cardinal rotation. Apparent collisions during optimization are not legal placements.

Every run freezes its source graph, compiled constraints, routing rules, engine Python
sources, seed, initial expression, numeric runtime versions, and settings.
`iterations.jsonl` records poses,
rotation probabilities, actual loss, and weight changes at the exact iteration where they
were consumed. A replay must apply that iteration schedule to the frozen engine; a seed
alone is insufficient after live edits. Timing and UI pacing are not optimizer inputs.
The browser shows the acknowledged objective revision rather than inferring application
from the button click.

## Sequential legalization review

Use **Next**, **Previous**, or the component list to walk the accepted legalization path.
The board shows the actual decision-time context: prior placements solid, future
components faint, original pose amber, chosen pose green, and inspected alternative purple.
A/B comparison follows the same reference in the second run and shares the viewport.

Each decision displays translation, rotation, separate objective contributions and the
closest **evaluated** candidate, including its cost. The alternative menu retains the best
12 and closest four evaluated sites from each explored orientation. These are historical
candidate scores, not newly solved counterfactuals or copper-clearance predictions.
The nearest evaluated candidate is not proof of the smallest possible displacement.
Backtracking counts are retained; discarded backtracking branches are not reconstructed.

**Save comment** creates a canonical design note bound to the immutable decision hash,
run, component, poses, costs, and selected alternative. Comments survive reloads, appear
in the existing notes store, and can be read by the agent. They never approve a placement
or mutate the engine objective. Run/step/comparison links preserve the review selection.

## Sources and evidence

Global cost-capture events under the viewer's configured capture root populate source
choices automatically. Existing projects can register immutable JSON inputs under
`<live>/placement-lab/sources/<id>.json` with `title`, neutral `graph`, dataclass-serialized
compiled `constraints`, and optional compiled `rules`. The lab does not accept arbitrary
filesystem paths from the browser. Capture documents and candidate arrays are checked
against their content hashes before review.

New live runs also register in the workspace experiment/artifact index. A placement
pass there means the hard placement checks passed, never electrical acceptance.

Runs live under `<live>/placement-lab/runs/<id>/`. `request.json`, `input.json`, `engine/`,
`control.json`, `iterations.jsonl`, `status.json`, `frame.json`, `continuous.json`,
`placed.json`, and `captures/` preserve the experiment. `review.json` indexes the chosen
legalizer's decisions and whole-board translation/rotation metrics. Failed trials stay
visible. Mutations require an allowlisted browser Origin, as design notes do.

`PNR_PAD_LOCALITY=1` remains experimental and default-off in production. It associates
both terminals of an unambiguous small part with the matching physical pads of its IC,
rather than its centroid or a high-fanout net bounding box. Shared rails and a broad
hierarchy group alone do not establish ownership. Explicit pad groups and private
connections can establish it. Duplicate pad shapes remain alternatives. Independent
proximity reports without engineering thresholds remain **unqualified**.
