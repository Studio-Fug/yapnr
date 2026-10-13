# Placement lab agent API

Use the same running viewer as the designer. No browser automation or edits to the
server's working files are needed. The API does not approve artifacts or relax electrical
contracts. Start the viewer with an engine runtime as described in the lab guide.

Set `YAPNR_PLACEMENT_URL` to the viewer **origin** (scheme, host and port), or pass
`yapnr placement --url "$YAPNR_PLACEMENT_URL" ...`. The CLI prints JSON, including errors;
rejected requests and wait expiry exit nonzero. Requests time out after 30 seconds by
default; redirects are rejected. Use the existing private viewer access boundary. Origin
checks protect browser writes; they are not authentication for a public deployment.

## Discover and drive

```sh
yapnr placement schema
yapnr placement catalog
yapnr placement source converter
yapnr placement validate-objective 'wirelength + 2*spread_overlap + source_constraints'
yapnr placement start converter --seed 0 --iterations 400 --paused
```

A paused start publishes `initial.json` and the visible seeded initial frame before
any optimizer update. Its history record has `kind: initialization` and
`completed_iterations: 0`; it is not an iteration or an evaluated placement.

Use the returned run `id` as `RUN` below. Run IDs are opaque strings. Read `status`
before making changes; `control.revision` is the last **requested** revision, while
`status.control_revision` is the last revision **consumed** by the optimizer.

```sh
yapnr placement status "$RUN"
yapnr placement wait "$RUN" --state paused --seconds 20
yapnr placement apply "$RUN" 'wirelength + 2*spread_overlap + source_constraints' --expect-revision 0
yapnr placement resume "$RUN" --expect-revision 1
yapnr placement wait "$RUN" --revision 2 --seconds 20
yapnr placement history "$RUN" --limit 10
yapnr placement pause "$RUN"
yapnr placement resume "$RUN"
yapnr placement stop "$RUN"
```

Revision guards reject stale edits from another agent or the designer. No mutation is
retried automatically. `wait` polls for at most 60 seconds; expiry does **not** stop the
run. Poll again or continue other work. Without state/revision options it waits for any
terminal state, including failure. Inspect `status.state`, `legal` and `violations`;
a completed placement is not a routed or electrically qualified board. The separate
600-second worker budget applies to bounded runs, including paused time.
Opt into unlimited interactive optimization with `start --run-forever`; this removes
both the iteration cap and wall-time deadline. Stop retains frames without legalization.

`history` returns actual control/frame events from `iterations.jsonl`, a byte `cursor`,
and `more`. Pass the returned cursor to the next call; partial concurrently written
records are retained for the next poll. Nothing is synthesized. `run` includes the
latest frame, request, control and review index. `evidence RUN NAME` retrieves a named
JSON document and exact content hash; available names are returned by `status`.

## Unlimited runs and rotation control

```sh
yapnr placement start converter --run-forever --paused
yapnr placement annealing "$RUN" '{"mode":"hold","temperature":1.5}'
yapnr placement annealing "$RUN" '{"mode":"schedule","start_temperature":2,"end_temperature":0.2,"steps":800}' --expect-revision 3
```

Start also accepts `--annealing` with the same JSON object. Python/HTTP use
`run_forever=True` and an `annealing` object on `start`, or action `annealing`
with `id`, `annealing` and optional `expect_revision` for live changes. Temperatures
are finite and between 0.05 and 10; schedule steps range from 1 to 1,000,000.
The server records the current iteration as `start_step` when applying a schedule.
Its endpoint is held after that horizon. Pause and Adam state are preserved.
`status.rotation_temperature` is the next update's temperature; history includes
actual evaluation temperatures and consumed schedules. A schedule replay must retain
these control events. The `iterations` setting is the default annealing horizon
when starting through the API without an explicit schedule; it is not a stopping
limit in unlimited mode. Worker crashes/server interruptions still end a run.

## Inspect, compare, render and discuss

```sh
yapnr placement decisions "$RUN"
yapnr placement decision "$RUN" --ref C9
yapnr placement decision "$RUN" --step 19
yapnr placement compare "$RUN" "$BASELINE"
yapnr placement render "$RUN" --ref C9 --alternative 0
yapnr placement notes "$RUN" --ref C9
yapnr placement note "$RUN" --ref C9 --body 'Investigating excess translation.'
yapnr placement reply "$RUN" N-0002 --body 'The closer evaluated site has a higher channel cost.'
yapnr placement link "$RUN" --step 19 --compare "$BASELINE"
```

Decision and alternative indices are **zero-based**. Reference selection must identify
one decision. A decision returns the hash-verified capture, costs, predecessors and the
same evaluated alternatives as the browser. `compare` reports per-component and total
translation deltas (run minus baseline), input/seed equality and missing references.
Differing inputs are explicitly not a controlled A/B. It does not choose a winner.

`render` returns an SVG and its hash: actual recorded footprints/pads, original and
chosen positions and an optional evaluated alternative. It is a headless placement
render, not copper or a native DRC result. Without a decision selector it renders the
latest frame. Save the returned `svg` as an artifact for inline visual feedback.

Notes and replies use the canonical store, are always marked **agent** authored and
appear in the browser. The server derives decision hash/provenance and the selected
alternative; the caller cannot supply user identity, approval, resolution or deletion.
The designer's existing notes remain unchanged. New runs appear in the open browser's
experiment list without resetting its selected review or draft objective.

`register-source FILE` uploads a JSON document with `graph` and dataclass-serialized
compiled `constraints`, plus optional `title` and compiled `rules`. The server assigns
an immutable content-addressed source ID. Repeating the exact document returns that ID;
changing it creates a new source. Structural admission checks do not replace the
worker's engine validation. Use `source ID` to inspect the format of an existing input.

## Python and HTTP

The client uses only Python's standard library:

```python
from yapnr.placement import PlacementClient

lab = PlacementClient()  # YAPNR_PLACEMENT_URL
catalog = lab.catalog()
run = lab.start(catalog['sources'][0]['id'], seed=0, iterations=120, paused=True)
lab.wait(run['id'], state='paused', seconds=20)
status = lab.status(run['id'])
lab.control(run['id'], 'resume', expect_revision=status['control']['revision'])
# Every API operation is available through call(action, **arguments).
result = lab.call('decision', id=run['id'], ref='C9')  # after legalization completes
```

`GET /api/placement-lab/v1` describes the contract. Send a JSON object containing
`action` and its documented arguments to `POST /api/placement-lab/v1`. Every POST
requires an allowlisted `Origin`; the client sends its configured viewer origin.
Successful replies have `{schema: "yapnr-placement-api-v1", result: ...}`. Invalid
requests return an HTTP error and `{error: ...}`. Unknown action arguments are rejected.
The CLI's `schema` action exposes required/optional fields, terms, limits and evidence
names. Both browser and agent use one lab service, worker and notes store.

## Single-step and topology

From an acknowledged paused state, `yapnr placement step RUN --count 1` performs
one optimizer update and pauses again. Counts range from 1 to 100 and may not
exceed the remaining iteration budget. Use `--expect-revision` to guard edits.
Wait for `paused` **and the returned revision** before issuing another step; an old
paused response is not acknowledgement. `resume` clears the step limit; at the
iteration budget it continues into legalization. A pending or running step is rejected.

```python
result = client.control(run_id, "step", count=1, expect_revision=revision)
client.wait(run_id, state="paused", revision=result["control"]["revision"])
```

`run.airwires` contains per-net minimum spanning trees, pad endpoints, lengths in
mm and any missing pins. These are geometric spans at the displayed rotations,
not routing or loss values. `render` includes them by default; pass `airwires=false`
to omit them. History distinguishes `objective_evaluation` (before an update) from
`optimizer_update` (after it); initialization remains step zero.

## Cost surfaces

`surface` takes `id`, `ref`, `expression` (e.g. `objective` or `8*pad_locality`),
and optional `pitch` in mm. It returns a cache key and `pending`/`ready` status.
Poll `surface-result` with `id` and `key`; ready responses include all four
cardinal fields, their sampled minima and a shared color range. The report and
request are registered as a discovery experiment under the placement attempt.

```sh
yapnr placement surface RUN --ref C1 --expression 'wirelength + 8*pad_locality'
yapnr placement surface-result RUN KEY
```

The Python equivalent is `client.call("surface", id=run_id, ref="C1",
expression="objective", pitch=1.5)`. This is independent from optimizer control.
The report names the frozen frame and rigid-pose scope; it is not the historical
soft-mixture loss. See [surface semantics and limits](PLACEMENT-LAB.md#four-rotation-cost-surfaces).

`surface-render` takes the same run/key and optional `rotation` (0, 90, 180, 270)
and returns SVG from that frozen frame, with its surface hash. The CLI is
`yapnr placement surface-render RUN KEY --rotation 90`. Browser links retain
`surface=KEY&rotation=90` to restore the selected field without recomputation.

## Experimental legalization repair

`start` accepts optional booleans `shove`, `placement_banks` and `legalize_only`
(default false), also available as CLI switches `--shove`, `--placement-banks`,
`--legalize-only`. Legalization-only replay consumes the registered source's poses
without optimizer updates; it rejects `paused` and `run_forever`. Bank membership
and diameter are authored in the source constraints, never guessed from net names.
See [the design](../../docs/design/placement-shove-and-banks.md) for the bounded
search, cost definition, validation, A/B results and limitations.

A shove decision carries `transaction_kind: shove`, `moves` (all before/after
poses), `before_terms` and `after_terms`. Its cost covers the whole placement.
Use the decision **step** to identify later repairs of a reference that already
has an earlier packing decision. Rendering and airwires include every moved
component. Canonical notes remain attached to the immutable decision hash.
