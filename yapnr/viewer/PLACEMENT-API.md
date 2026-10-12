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
600-second worker budget still applies, including paused time.

`history` returns actual control/frame events from `iterations.jsonl`, a byte `cursor`,
and `more`. Pass the returned cursor to the next call; partial concurrently written
records are retained for the next poll. Nothing is synthesized. `run` includes the
latest frame, request, control and review index. `evidence RUN NAME` retrieves a named
JSON document and exact content hash; available names are returned by `status`.

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
