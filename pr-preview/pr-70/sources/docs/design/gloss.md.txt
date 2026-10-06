# Design: the gloss, dekink and corridor-coalescing pass (`PNR_GLOSS`)

Status: `PNR_GLOSS` is the runner's own default since ladder-v2 (2026-10-06,
`docs/decisions.md`; `--no-gloss` restores the plain runner path), once a native-loop A/B on GCP
(section 10) showed no regression. Implemented on branch `claude/gloss-port`. The pass was
designed and built in Splanc's engine (snapshot src18) and is ported here as a copy plus the
loop hunks; where
the port differs from the source, §9 says so. Code: `hardware/pnr/pnr/gloss_geometry.py` (pure
geometry, no KiCad) and `hardware/pnr/pnr/gloss.py` (KiCad adapter, workers, transactional
controller); the loop hooks are in `pnr/native_loop.py` and `pnr/full_iteration.py`, the ladder
stage in `regression/run.py`.

> Owner request (2026-09-30): add a glossing and corridor coalescing phase to the router, under
> the same cost constraints as drive the current routing; remove unnecessary direction changes
> ("dekinking"); when several tracks of the same signal class run next to each other, place them
> parallel and directly adjacent to minimize dead space; run it within the PnR loop, and let the
> legalizer override its decisions if they are illegal or not DRC/ERC clean.

## 1. Summary

- **One phase, two passes in `pnr.native_loop`.** `06g-gloss` runs after `06-signals` and `06b`,
  before the refinement clock starts; `07g-gloss` runs after the refinement cycles and the loop's
  own cleanup, before `08b`.
- **Four steps per pass, in this order:**

  1. **normalize:** merge collinear, zero-length and duplicate segments; the copper does not
     change.
  2. **dekink:** inside each monotone run, use the path with the fewest bends at the same length.
  3. **gloss** (pull-tight): the shortest homotopic octilinear path within a 1 mm tube, found with
     the router's own A\* and bend cost.
  4. **corridor:** pack parallel legs of the same signal class to minimum legal pitch.

  A second gloss and corridor sweep follows by default (`PNR_GLOSS_SWEEPS=2`).

- **What may change:** only the centreline vertices of ordinary signal tracks between fixed
  anchors. Anchors, widths, layers, vias, nets, pad and neck copper and locked copper never
  change; power, plane, pair, length-match and (by default) SI nets are never touched; no new
  contact, crossing or via is created.
- **Cost:** the router's. Octilinear legs, chain cost `C = L + 0.15 mm × bends`, the keyhole
  `relax` rule (shorter, or the same length with fewer bends) and the `align_parallel` slack of
  0.2 mm for corridor moves and adjacency tie-breaks. Legality is the native shape checker's
  (`Oracle`).
- **Legalizer override, four layers** (§5): every transaction is re-checked on the applied board
  and gated by native checks including a cold KiCad DRC; a phase-end gate bisects or reverts the
  pass; the loop's own `gate()` can keep the pre-pass board; later legalizers rip up glossed copper
  like any other.
- **Off by default.** With `PNR_GLOSS` unset, `pnr.gloss` is never imported and no label, event,
  progress key, router key or code key depends on it (§6).

## 2. Eligible copper and anchors

A net is eligible only if all of these hold (each is an existing engine predicate):

| #   | Condition                                                                                                                                                                                       |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| E1  | `electrical.net_policy` mode `signal` (no plane, no class wider than the fab track, no known current, no pair)                                                                                  |
| E2  | not in `via_coalesce.protected` (planes, wider classes, pairs, length-match nets, plane-access, thermal and local-return intents)                                                               |
| E3  | not an `@pnr-si` intent net (from the rules, or resolved from the annotation sources; fail closed). `PNR_GLOSS_SI=1` admits them to normalize, dekink and gloss, never corridor, under guard G3 |
| E4  | the effective KiCad netclass is `Default` (the regional router's gate)                                                                                                                          |
| E5  | no arcs of the net on the layer                                                                                                                                                                 |

Per eligible (net, layer), in nm integers: segments are split at T points; **anchors** (never
moved) are vertices in a same-net pad or via, vertices of degree other than 2, width changes,
ends of frozen segments and vertices in a same-net filled zone. A **chain** is a maximal path
between two anchors. **Frozen** segments are never edited: locked copper, neck and terminal zones
(a pad with a terminal contract or a required entry width, and its neck length), every track a
DRC violation of the pre-pass board names, and every track of a net with an unconnected item. The
frozen and guard sets come from the pre-pass board and its DRC report only, so they do not depend
on which items KiCad happens to name.

## 3. Legality of an edit (L1-L8)

An edit replaces the old sub-path(s) of a chain (several chains for corridor) by new ones with the
same anchors. It is legal only if:

| #   | Check                                                                                                                                                                                                                          |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| L1  | every new segment passes `Oracle.clear` (exact native shapes, +0.001 mm), on the board with the whole batch applied                                                                                                            |
| L2  | no new contact with same-net copper beyond the chain's own anchors (exact shapes, zero clearance): the contact graph and the pad partition cannot change                                                                       |
| L3  | homotopy: the old path closed with the reversed new path winds 0 times around every other item's test point (pads, vias, track ends, keepout and outline vertices, holes): no obstacle changes side, so no crossing is created |
| L4  | anchors, widths, layer, vias and net identical; every new leg octilinear; rounded to 1 nm and checked again                                                                                                                    |
| L5  | no interior turn sharper than 90 deg and no new acute angle at an anchor (no acid traps)                                                                                                                                       |
| L6  | a pad-entry witness end segment stays a witness                                                                                                                                                                                |
| L7  | proximity guards never lose distance: G1 diff-pair copper (cap 3 × the pair gap), G2 the pads of open nets (1 mm; at 06g also their tracks and vias), G3 any foreign copper for SI chains (3 × width)                          |
| L8  | locality: gloss vertices within 1 mm of the old chain; dekink inside the run's parallelogram; corridor shift at most 2 mm                                                                                                      |

With functional groups (§7), an edit is also legal only if no pair of different groups gains
parallel run at minimum pitch beyond `max(allowance, run before)`.

## 4. Acceptance

- **normalize:** the copper union is identical (XOR at most 2e-4 mm², arc approximation) and the
  segment count falls.
- **dekink and gloss, rule R:** shorter by more than 1 µm, or the same length with fewer bends;
  90° and 135° turns never increase; `C` strictly falls. Length is never traded for bends. Among
  equal-cost options, the one hugging same-class neighbours tightest wins, and an edit saving
  less than 0.2 mm is declined if it loosens adjacency.
- **corridor:** per member no added bend and at most 0.2 mm of extra length (drag-only moves; a
  blocked full shift is bisected to the largest legal one); per stack the adjacency metric X falls
  by at least 0.05 mm² and the window's dead space DS does not grow.
- **Metrics.** L (eligible length), bends (`bends_all` counts every degree-2 vertex), X (the
  sampled gap to same-class neighbours within 2 mm, minus the required clearance), T (length
  packed at minimum pitch), DS (free area no track of the class can use) and A2/A3 (area usable by
  a 2- or 3-track bus). X and the hug score do not depend on which way a segment is drawn: each
  segment is sampled from its canonical (sorted) first end, and a ray that meets a same-class
  parallel neighbour and other copper at the same distance (a neighbour's vertex) counts the
  neighbour. The source sampled from the drawn first end and broke such ties by an order that
  followed the drawing direction: one merged segment drawn the other way changed X by 0.155 mm²
  on a ladder board with the same copper. `python -m pnr.gloss --measure BOARD... --rules R --out J`
  reports them with the cold DRC, the objective and the audit.

## 5. Transactions and the legalizer override

1. **Per transaction.** One inventory worker plans a step; edits are packed into batches of up to
   16 with distinct nets and disjoint influence boxes (4 corridor stacks; normalize is one batch).
   A trial worker applies
   a batch, re-checks every edit on the applied board (L1-L8, the edit rule, the group cap), drops
   failures, refills and collects facts; then a cold KiCad DRC. The gate requires: the same pad
   partition, no lost connection, no lost or newly bad pad entry, reference failures a subset,
   no new forbidden via, sub-width, unjustified sub-width, unqualified pairs and pair skews not up,
   no new DRC violation key, opens and dangling items not up, the objective not worse in any
   component, and a positive gain. A rejected batch drops the edits the findings name, else is
   halved (4 levels); an edit rejected alone is blacklisted for the pass.
2. **Phase end.** The last checkpoint is checked against the pre-pass board with the strongest
   headless checks (cold DRC, facts, audit; the SI report when SI nets were edited). On failure
   the controller bisects the checkpoints, drops the failing transaction, replays the later ones
   (each gated again), and after 3 rounds reverts to the byte-identical input.
3. **The loop's gate.** `native_loop` runs its own inspect, check and DRC `gate()` on the pass
   output, as for cleanup and bank consolidation, and keeps the pre-pass board on failure.
4. **Later legalizers.** Glossed copper has no special status: refinement rip-up, shove,
   regional blocker plans, cleanup, coalescing and pad-entry repair override it like any other
   signal copper.

Budgets (global, never per board): 240 s per pass, checked before each inventory and transaction;
48 transactions; a deterministic per-chain planning budget (6000 shape and contact checks), with a
120 s inventory safety net that is reported when hit. Every worker runs under its `pnr.proc`
deadline; a signal exit is retried once, a deadline kill is not. The 240 s and 120 s are wall
clock: a slower machine can stop a pass on `time_budget` where a faster one finishes. The pass
reports why it stopped (`stop`: `time_budget`, `max_transactions` or null), its budget and its
planning totals (`deadline_hits`); the ladder keeps them in `result.json`. For an A/B across
machines, raise `PNR_GLOSS_SECONDS` well above the pass's need and bound the pass by
`PNR_GLOSS_MAX_TRANSACTIONS`, so both arms stop for the same reason.

## 6. Flags

None is read unless `PNR_GLOSS=1`.

| Flag                         | Default                           | Meaning                                                |
| ---------------------------- | --------------------------------- | ------------------------------------------------------ |
| `PNR_GLOSS`                  | unset                             | master switch; unset: no import, label, event or key   |
| `PNR_GLOSS_STEPS`            | `normalize,dekink,gloss,corridor` | steps (the order is fixed)                             |
| `PNR_GLOSS_PASSES`           | `06g-gloss,07g-gloss`             | loop passes                                            |
| `PNR_GLOSS_SI`               | `0`                               | SI nets may enter normalize, dekink and gloss (G3)     |
| `PNR_GLOSS_SECONDS`          | `240`                             | budget per pass                                        |
| `PNR_GLOSS_MAX_TRANSACTIONS` | `48`                              |                                                        |
| `PNR_GLOSS_BATCH`            | `16`                              | edits per transaction                                  |
| `PNR_GLOSS_HUG`              | `1`                               | same-class adjacency tie-breaks in dekink and gloss    |
| `PNR_GLOSS_SWEEPS`           | `2`                               | 2: a final gloss and corridor sweep                    |
| `PNR_GLOSS_CLASSES`          | unset                             | functional groups file (§7)                            |
| `PNR_GLOSS_CLASSES_FROM`     | unset                             | derive groups: `netclasses,pairs,si,length_match` (§7) |
| `PNR_GLOSS_CROSS_GROUP_MM`   | `10`                              | cross-group cap in mm; needs one of the two above      |
| `PNR_GLOSS_CYCLE`            | rejected                          | the in-cycle pass and un-glossing are not implemented  |

A malformed value, or a groups file or rules that give no functional groups, stops `native_loop`
before any phase runs. `PNR_STOP_AFTER_PHASE` accepts the two gloss labels only with the flag set
(and its error then lists them). With the flag, `progress.json` gains `gloss` (per pass: the
summary, `accepted`, `status` and `cost`, its wall and CPU seconds) and `gloss_key`, and
`evaluation.json` the side fields `gloss_summary` and `gloss_metrics` (the metrics worker measures
the passes' functional groups); the objective vector is unchanged.

**Keys.** The router key (`pnr.feedback.signals.current_key`) gains `gloss`: null with the flag
unset, else `pnr.gloss.settings_key`, a digest of every sub-flag's effective value and of the
groups file's content (never its path). `key_string` adds a `gloss=` field only then, so flag-off
key strings are what they were. A record's gloss key comes from its `progress.json`, and
`check_import` refuses an evaluation whose gloss key differs from the run's: the two arms of a
gloss A/B, or two gloss configurations, never mix in a feedback library or a halving import.
`pnr.gloss` and `pnr.gloss_geometry` are evaluation code (part of the code key) only with
`PNR_GLOSS=1`, as `pnr.shove` is only with `PNR_SHOVE=1`; with the flag the key also covers
`pnr.shove.gates`, whose unjustified sub-width check the pass runs under any router. Gloss edits
therefore never change a flag-off code key; the hooks in `native_loop` and `full_iteration`
changed the plain-router code key once, when they landed.

## 7. Functional groups and the cross-group cap

Packing tracks at minimum pitch over long runs couples them. The owner's rule: parallel run at
minimum pitch is **unlimited within a functional group** and **capped at 10 mm between groups**
(a net in no group is a group of its own). The measure: for two tracks of different nets on one
layer within 30° of parallel, the part of each centreline whose foot lies on the other track and
whose distance is at most the minimum pitch + 0.1 mm; the run `C(a, b)` is the mean of both
directions, summed over layers. An edit may never raise a cross-group pair above
`max(allowance, C before the pass)`, so a pair already over the cap may stay or fall but never
grow. The allowance comes from one hook, `gloss_geometry.allowed_parallel_mm`, which a
noise-budget model can replace with per-pair allowances. The planner, the trial worker and the
gates all enforce it.

**Groups file** (`PNR_GLOSS_CLASSES`):

```json
{
  "classes": {
    "GROUP": ["NET", "NET"],
    "OTHER": { "nets": ["NET"], "interface": "free text", "provenance": ["free text"] }
  }
}
```

A group lists net names, either directly or under `nets`; other keys are documentation. A net in
two groups is an error. `docs/examples/gloss-groups-chaser.json` is the file of ladder case
`07-chaser-20` (timer, clock and reset, LED drive).

**Derived groups** (`PNR_GLOSS_CLASSES_FROM`, a comma list of `netclasses`, `pairs`, `si`,
`length_match`): each rules net class, differential pair, `@pnr-si` intent or length-match group
becomes a group. Derived groups with the same members are one group. A net derived into groups
with different members joins the most specific one: the fewest members, then the first source in
the order above. A differential pair inside a wider net class is thus a group of its own, and the
class keeps its other nets (the more specific group caps more pairs, the conservative choice). A
groups-file entry wins for its net. The groups are checked against the rules at controller start
(`si` needs the board and is resolved by the pass).

E4 still limits edits to `Default`-class nets; admitting named signal classes is a later
decision.

## 8. The ladder stage

The regression ladder has no native loop. `run.py --gloss` runs one pass with 07g semantics on the
refilled board, before the audit, transactional and gated as in the loop, then gated again by the
kicad-cli DRC that judges the case, as strictly as the pass's own transaction gate
(`via_coalesce.violation_keys`): if opens rise, a violation is new by its type and items (one
that moved to other items counts, even when its type's count holds) or dangling tracks or vias
rise, `routed.pre-gloss.kicad_pcb` (always kept) is restored. A failing stage restores the board
and fails the case (`gloss_error`). `--gloss-flag PNR_GLOSS_NAME=VALUE` passes sub-flags (the
runner strips ambient `PNR_*` variables); a groups file is copied into `source-freeze` and read
from there, and `provenance.json` records the sub-flags and the file by name and sha256, never by
path. `--gloss-measure` measures each final board, for both arms of an A/B. Each case's
`result.json` gains a path-free `gloss` block (pass summary with `stop`, `budget` and the planning
totals, metrics before and after, the outer gate, the pre- and post-gloss board and copper hashes,
the sub-flags and the groups file by name and sha256); per-stage CPU is `cpu_stages` and
`cpu_seconds`, as on the hard-rungs branch. `--trace` snapshots the board after the stage. The
KiCad-lane test `gloss_e2e_test` runs the stage on the public case `04-inverter-leds-8`, and the
nightly ladder runs it on `04-inverter-leds-8` and `07-chaser-20` (seed 0): a stage error fails
the job, a stage that keeps no edit on either case is a warning.

```sh
hardware/pnr/regression/run.py --repo . --out .yapnr/ladder/AB-OFF --seed 0 --seed 1 \
  --showcases --gloss-measure ...
hardware/pnr/regression/run.py --repo . --out .yapnr/ladder/AB-ON --seed 0 --seed 1 \
  --showcases --gloss --gloss-measure ...
```

Writeback gives tracks random uuids, so each case also records `copper_sha256`, a hash of its
copper without uuids and without the direction a segment is drawn in (it raises on a board whose
track or via blocks it cannot parse, rather than hashing nothing). An on-arm case pairs with its
off-arm twin when its
`pre_gloss_copper_sha256`, `placed.json` and `routes.json` match the twin's. The ladder
measures what the pass does to a finished board; it cannot measure completion, because the pass
runs after a complete route. Results:
[Regression ladder](../regression-ladder.md#gloss).

## 9. The port

The engine here descends from Splanc's src15 snapshot plus the Electrical221 integration
([import manifest](../history/import-manifest.md)). Normalized, the source's `native_loop.py`
differs from src15 only by the gloss hunks, and every engine API the pass calls keeps its
signature; src16 and src17 changed the pass's dependencies only behind an opt-in flag this engine
does not have. Differences from the source:

- black and isort; flake8 clean without a baseline; comments scrubbed of private references.
- `apply_spec` deletes replaced copper with `board.Delete` (never `Remove`; `test_board_delete`
  has a gloss case). Delete invalidates the old tracks' wrappers, so the trial worker scores a
  corridor edit's planned state before applying it (the source scored it after, through the
  pre-apply board, which `Remove` left valid); the value is the same.
- Workers run through `pnr.proc.run_status`, retried once after a signal exit only.
- The tests' board-derived geometry is generated, with the same asserted properties, and net
  names are neutral.
- The ray metric (X and the hug score) samples from a segment's canonical end and counts a
  same-class neighbour at a tie (§4); the source depended on the drawing direction. The same
  patch is handed back to the source.
- New: derived functional groups (the most specific group wins), the ladder stage and the public
  example groups file; the gloss settings in the router key and the flag-gated code key (§6); the
  passes' cost in `progress.json`; the functional groups in `full_iteration`'s metrics.

Identity: the port and the source engine were run on three replay points of a private board
(a finished board as 07g, with and without groups, and a 06-signals capture as 06g), each with a
fresh DRC cache; the evidence stays private. Before the ray-metric fix, the port reproduced the
source exactly on all three: the same output copper (a uuid-free hash, also equal to the source's
archived run), the same transaction sequence and accepted count, the same edits per step and the
same metrics; the cold DRC, the objective and the audit are unchanged by the pass in both
engines. The first run diverged at the first corridor transaction (the `Delete` difference
above) and led to that fix. After the ray-metric fix, the port is again exact against the source
with the same patch on all three points (the run with groups needs a budget above 240 s in both
engines: under load the source's pass stopped on its wall-clock budget one transaction short,
the variance §5 describes). Against the unpatched source the fix moves a few decisions: one gloss
edit more on one point, one corridor edit fewer on another; length within 0.03 mm, bends equal,
DRC, objective and audit unchanged. With the flag unset, the stubbed loop controller of this
branch and of `main` produce identical outputs, and `pnr.gloss` is not imported.

## 10. Open points

- **Default-on (resolved, 2026-10-06):** the native-loop A/B this needed ran on GCP (PR #62,
  campaign `20261005-ladder-4b88dd`): ladder + showcases + `09-mcu-usb-31` + the two
  UFBGA-201 rungs, seeds 0 and 1, gloss off vs on. 52 of 60 tasks passed; the 8 failures are a
  pre-existing, unrelated `jlc-pofv` BGA fanout via-drill mismatch, identical in both arms. Over
  the 26 remaining paired cells: 0 regressions (opens and DRC violations stay 0 in every cell,
  both arms); bends -19.6 %, track segments -38.0 %, copper length -1.4 %, vias unchanged, CPU
  +393 %. `--gloss` is now the runner's own default (ladder-v2, `docs/decisions.md`).
- **06g routability** on public boards is unmeasured: the ladder runs the pass after a complete
  route. A real native loop with the flag on a public ladder board (no annotation sources, two
  refinement cycles) ran both passes with real workers: `06g-gloss` (open-net guard) kept two
  transactions, 4 fewer bends and 0.7 mm less length, the loop's gate accepted it, and the loop
  ended with the same opens as the same run without the flag. One board, one seed: a smoke test,
  not evidence about completion.
- **Cost.** On the ladder the stage costs a roughly fixed 30 to 125 s of CPU per case (KiCad
  worker start-up and three cold DRCs dominate on small boards), so the overhead is largest on
  the smallest cases (see the [ladder A/B](../regression-ladder.md#gloss)). In the native
  loop each pass adds its own inspect, check and DRC workers to the evaluation, and
  `full_iteration` one metrics worker; on the private board each pass took 2 to 4 minutes of
  wall time.
- Gloss may loosen adjacency where it saves at least 0.2 mm (router cost ranks first).
- Whole open nets are frozen (deterministic, conservative).
- Corridor moves are drag-only; no jogs, no co-mitred bus corners.
- E4 limits edits to the `Default` netclass.
- No crosstalk budget beyond the cross-group cap.
- No fixture has more than two copper layers; a four-layer gloss fixture belongs with the N-layer
  stackup work. The KiCad-lane tests (`gloss_native_test`, `gloss_e2e_test`) are `manual`, so
  only the nightly ladder's gloss stage runs the pass on KiCad in CI.
- When the hard rungs land, their independent `checks` stage runs after the gloss stage on the
  final board; the gloss outer gate does not consult it, so a pass that broke a declared
  constraint would fail the rung rather than be reverted.
