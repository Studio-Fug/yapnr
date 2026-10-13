# Connection review

Open `connection-review.html` from the viewer toolbar. Select a run prefix to
collect unconnected minimum island spans from the latest final checkpoint of each
lane. Each card pins the lane, board hash, phase, net and native result counts.
Unassessed cards stay visibly unassessed until an agent supplies a hypothesis.

Review an assessed card with **Agree**, **Different finding**, or **Skip**.
Left/right swipes on the gesture strip and arrow keys perform the same actions.
A different finding requires a written conclusion or a rectangle drawn on the
board. Pan or zoom the native copper, select layers, and compare the full board
with the highlighted focus region. Undo preserves the history. Browser drafts
survive navigation and refresh.

Verdicts append to `connection-review/events.jsonl` under the viewer root. Writes
are locked, revision-checked and fsynced. Revising a hypothesis makes its previous
verdict outdated; a different board hash creates a separate card. Agreement is a
review of a hypothesis, never PCB approval or passing electrical evidence.

## Agent API

Read `GET /api/connection-review/v1` for the schema. Send JSON to
`POST /api/connection-review/v1` with an allowlisted Origin. Actions:

- `scan`, with optional `scope`: index final failures across matching lanes.
- `list`, with optional `scope`: retrieve cards without rescanning.
- `card`, with `id`: retrieve a pinned card and its current revision.
- `annotate`, with `id`, `expect_revision`, and `annotation`: supply a hypothesis,
  confidence (`low`, `medium`, `high`), question, suggestion, evidence strings,
  bounds `[left, bottom, right, top]`, highlighted `refs` and `nets`. Coordinates
  are millimetres, y-up. Inspect the actual geometry/image before diagnosing it.
- `history`, with optional `id`: retrieve annotations and designer verdicts.
- `respond`, with `id`, `expect_revision`, `body`, and `status` (`planned`,
  `working`, `tested`, `blocked`): answer designer feedback without changing
  the reviewed hypothesis or invalidating its verdict.

Geometry is available at `/api/geometry/<board_sha256>`. The review page renders
that exact checkpoint, including filled zones, copper layers, pads and vias.
The agent API has no human-verdict action. The UI submits to the separate
`/api/connection-review/decision` endpoint with its revision guard.

## Experimental access repairs

Opt-in engine flags support the initial reviewed failure classes:

- `PNR_REPAIR_TARGET_GUARD=1`: require the specifically requested pad connection
  in the final make-room gate, in addition to preserving every original connected
  pad group. Unrelated improvements cannot substitute for the requested repair.
- `PNR_PAD_LOCALITY_PASSIVE_NETWORKS=1`: infer small, uniquely owned passive
  networks at a hub pad, including both arms of a shared divider node.
- `PNR_LEGALIZE_ACCESS_SEARCH=1`: rank local offsets and allowed cardinal turns
  before attempting a bounded collective placement shove. Hard relations and
  the complete placement objective still govern acceptance.
- `PNR_POWER_DETOUR_VIA_ACCESS=1`: consider opposite-layer full-width power
  detours and score native via-in-pad access without narrowing existing copper.
- `PNR_POWER_POUR_ACCESS=1`: try bounded local power pours. Certify the actual
  filled polygon with a common full-current-width core and source-authorized
  terminal necks; reject disconnected, unfilled or undersized copper.

Generated pours remain audited even when candidate generation is disabled. The
certificate is a geometric/resistive check, not thermal qualification. Native DRC,
SMPS current/path/loop requirements and every other electrical check remain
independent gates. A failed or timed-out placement proposal retains its baseline.
