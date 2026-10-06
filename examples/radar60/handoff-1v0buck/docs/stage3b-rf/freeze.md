<!-- markdownlint-disable -->

# Stage 3b RF macro v2 — freeze attempt: BLOCKED, not frozen

Branch `claude/radar60-rf3b` / worktree `yapnr-wt/radar60-rf3b` (HEAD `5f16ddc`, unchanged
by this pass — review-fix only, no generator/coupon edits). **Review-fix pass (2026-10-04
PM)**: verified the prior review's F1–F7 findings against the data on disk; all CONFIRMED.
Fixed in `em-column.md` (F1 false C1 pick, F2 misreported D14 numbers, F7 wrong [D]/[S]
labels) and here (F4 cavity fetch, F5 D15 auto-pick, F6 cost ledger). No code or coupon
change; this stays a status record, not a frozen macro, and correctly so.

## Why still not frozen

| Campaign                        | Variant               | State (this check)                   | Note              |
| ------------------------------- | --------------------- | ------------------------------------ | ----------------- |
| s3b-bank-a `3b60dc`             | pour A                | **SUCCEEDED**, 7/7 fetched           | re-analyzed below |
| s3b-bank-b `05776b`             | pour B                | **SCHEDULED**, 0 done                | not started       |
| s3b-bank-c `914040`             | pour C                | RUNNING, ~3.8 h elapsed              |                   |
| s3b-bank-s1 `443dad`            | dummies S1            | RUNNING, ~1.7 h elapsed              |                   |
| s3b-cav `08e2c7`                | K0/K1/K2 cavity       | **SUCCEEDED**, 5/5 fetched (was 2/5) | re-analyzed below |
| s3b-c1-k `0c07c4` (Montreal)    | C1 27 µm + D5 series  | RUNNING, ~2.1 h elapsed              |                   |
| s3b-c1-wall `9a3e4d` (Montreal) | cavity via-cage bound | RUNNING, ~0.5 h elapsed              |                   |

D15 still needs B and C; D5 still needs `s3b-c1-k`. Fetched and re-ran `s3b-cav` (5/5, was
2/5 — F4) and `s3b-bank-a` (7/7, was 2/7 — F5) through `s3b_bank_analysis.py`; no new GCP
submission, no process touched (the PID-93427 poller from a prior step is still running
and untouched).

## F1/F2/F7 — fixed in `em-column.md`

The C1 "band-clean" pick (`cell-c1w-030`) was false: it read only 3 sample points off a
641-point sweep whose true worst in-band S11 is -5.17 dB (45% of the band above -10 dB),
and the model is missing from the pipeline's own post-processed `column.json`. Re-parsed
all 7 candidates on the full sweep: **none meets -10 dB RL in-band**; `cell-c1w-042` is
marginally best (-5.38 dB worst) but is not a pick. C1 stays unconverged, deferred to
`s3b-c1-wall`/`s3b-c1-k`. D14's quoted TX1.Pf range (-1.07/-1.10 dB) appeared in no run;
corrected to -1.24/-1.39 dB, and `pa_coupling_db` (port case) corrected to -34.6/-34.9 dB
at 62 GHz (not null — null only applies to the short/open cases by construction). RF
labels changed from [D] to [S] throughout (solver output, not derived).

## F4 — cavity now fully measured (5/5)

K0 (today's L2 windows) bondply transfer: **-3.32 dB at 20 µm, -4.66 dB at 27 µm**, both
near 60.9-60.95 GHz — consistent in magnitude across mesh, so this is a real resonance,
not a 20-µm meshing artifact (stage-2's concern). It is almost certainly the cause of the
60.7-60.8 GHz ripple that kills every C1 candidate above. **K1 (solid L2 under patches)
removes it**: -45.5 dB transfer, vs. +57.8 dB drop relative to K0 at 27 µm — but on the
un-retuned column this costs 1.4 dB worse worst-case S11 and up to 2.5 dB of realized gain
at band edges (`K1_vs_K0_r27`/`K2_vs_K0_r27` in `summary.json`). K2 (vias around the
windows, no solid pour) only partially helps (-1.1 to +2.3 dB vs K0, mesh-dependent) and
is not a clean fix. **Recommendation: K1 (solid L2) is the right direction, but its cost
must be re-measured on a retuned C1 column, not the baseline one** — that comparison
doesn't exist yet. No pick made.

## F5 — bank A now fully measured (7/7); D15 still open, auto-pick disabled

Bank A (pour A, 40 µm mesh, un-retuned column — not sign-off grade per the 15-27 µm mesh
rule, but now complete): isolation **36.7 dB** (TX1→RX4, worst pair) — clears both RF-07
thresholds (≥27 dB required, 35 dB design). Active reflection worst **-2.76 dB at
TX1, +45°** scan; TX1 broadside realized gain 4.5 dBi at 60.3 GHz; E-plane pattern spread
TX1-vs-TX2 5.76 dB. `summary.json`'s `decisions.D15.pick: "A"` is **not a valid decision**
— it only has a row for A; B and C have no data yet, so the gate is vacuous as the review
flagged. Treating this pick as advisory-only pending B/C; D15 stays open.

## Resolved without new data

- **D14 (PA feed)**: still no RF objection (corrected numbers above); the 10 mV IR-drop
  budget needs owner review (85 °C derate pushes it to ~10.5 mV; mutual inductance raises
  effective via L to ~0.35–0.4 nH) — power-integrity call, not EM, not closed here.
- **Dummy columns**: S1 (open-end only) still beats S2/none on TX1 loss (-1.50 vs -1.78 dB
  lossy); bank-level isolation/pattern confirmation for S1 is still pending (`s3b-bank-s1`
  RUNNING).
- **TX feed / RF-03**: T2 dropped (G4 fail); keep T0; real fix (shorter/fewer meanders) is
  unstarted.
- **D5**: still no data; `s3b-c1-k`'s series-fed arm is running.

## F6 — cost ledger corrected

The review's "$16.80, over cap" was accurate at the time but is now stale: `s3b-bank-a`
and `s3b-cav` both finished, well under their ceilings.

| Line                                | Ceiling | Actual           |
| ----------------------------------- | ------- | ---------------- |
| wave 1 (c1-r15/r20, pa-r20, tx-r20) | $5.20   | $1.74 (done)     |
| pa2                                 | $0.49   | $0.03 (done)     |
| pp                                  | $0.77   | $0.19 (done)     |
| **cav** (5/5 now)                   | $1.16   | **$0.48** (done) |
| **bank-a** (7/7 now)                | $1.76   | **$1.16** (done) |
| bank-b                              | $1.76   | $0 (not started) |
| bank-c                              | $1.76   | running          |
| bank-s1                             | $1.25   | running          |
| c1-k                                | $1.99   | running          |
| c1-wall                             | $0.66   | running          |

Actual spend logged so far: **~$3.60**. Open ceiling still committed (jobs not yet done):
**~$7.42**. Total committed against the stage-3b cap: **~$11.02 of $15 — about $4 of
headroom**, not the $0.14–negative the stale note claimed. Radar program total (incl. this
stage): ~$8.4 + $11.02 ≈ **$19.4 of the $50 program cap**. `gcp-spend.md` updated with the
bank-a/cav actual rows.

## Board-frame numbers (unchanged, not re-derived — no macro geometry changed)

Board 60 × 47.35 mm; RX phase centres y 39.988, x 20.666/23.008/25.350/27.692; TX phase
centres y 38.100, x 38.718/41.060/43.402; RF region x 12.599–51.469, y 26.7–47.024;
VOUT_PA pocket 29.557–31.25 × 33.45–34.2.

## Recommendation to the orchestrator (superseded by the pass below)

Still do not freeze. Re-dispatch once: (1) `s3b-bank-b/c/s1` and `s3b-c1-k`/`s3b-c1-wall`
land (same poller, still watching), (2) C1 is retuned against the now-confirmed cavity fix
(K1) rather than the baseline column, and (3) D15 is picked from all three of A/B/C, not
just A. Budget has ~$4 of headroom for that; no new submission needed yet. No renders were
regenerated this pass (no `matplotlib` in this environment's Python; `em/figs/*.png` from
the prior pass still stand for C1/TX/PA — they are now stale for C1 and should be dropped
on the next pass, not trusted). No code, coupon, or generator files were touched.

## Full-sweep pass (2026-10-04 evening): all E1 campaigns SUCCEEDED and fetched `--full`

`s3b-bank-b` (05776b, 7/7), `s3b-bank-c` (914040, 7/7), `s3b-bank-s1` (443dad, 5/5),
`s3b-c1-k` (0c07c4, 9/9) and `s3b-c1-wall` (9a3e4d, 3/3) all finished and were fetched with
`yapnr exp fetch --full` (no time-domain dumps in this track, so the egress is small — each
campaign's `result.json`/`s.csv`/`probes.json`/one bondply `e_bond.h5` per drive). Re-ran
`openems/s3b_bank_analysis.py` over all seven bank/triplet/pp campaigns merged, and
`openems/stage3b_col.py` over all column/PA campaigns merged (16 C1-family points, 2 PA
corners). Every candidate below is judged on its **full sweep**, not sample points (the
prior pass's C1 review already made that fix for the wave-1 data; this extends it to the
rest).

### D15 — still no variant passes every gate; pick A with a quantified warning

|       | Isolation (worst pair, dB) | Active RL worst (dB) | Edge-pattern spread H (dB) | Drills | Trapped mode (Q≥20, 57-70 GHz)                                             |
| ----- | -------------------------- | -------------------- | -------------------------- | ------ | -------------------------------------------------------------------------- |
| **A** | 36.7                       | -2.76                | 2.42                       | 1437   | 1 mode, 63.0 GHz, Q=21.9, **amp_rel 0.0012** (negligibly excited)          |
| B     | 37.2                       | -2.75                | 3.50                       | 970    | 3 modes 58.4-67.5 GHz, Q 86-123, **amp_rel up to 0.21** (strongly excited) |
| C     | 37.4                       | -2.75                | 3.53                       | 986    | 2 modes 62.1-63.1 GHz, Q 22-23, amp_rel ~0.11                              |

All three clear RF-07 on isolation (≥27 dB required, 35 dB design) with ~1 dB to spare over
each other; active RL and pattern spread are within the mesh noise floor of each other too.
None passes the plane-pair mode gate (`passes_gates: false` for all three), so the rule's
clean tie-break (isolation/RL/pattern, then via count) does not resolve on its own — the
isolation/RL/pattern numbers are themselves tied. Breaking the tie on the gate data itself:
**A's only trapped mode is two orders of magnitude more weakly excited than B's, and lower-Q
and less excited than C's** — A is the safest choice of the three despite having the most
vias. **Pick: A, with a quantified warning** (one weak 63 GHz cavity mode, amp_rel 0.0012,
Q just over the 20 threshold). B is not recommended (the 58.4 GHz mode is strongly excited).

### S1 vs. S2 (dummy columns) — S1 confirmed at bank level

Isolation: S1 **38.2 dB** vs. S2 (pour A) 36.7 dB. Active-match and pattern deltas are all
<0.5 dB and none reaches the ≥1 dB bar the rule sets for S2 to be worth its cost. Combined
with the feed-loss result (S1 0.28 dB better on TX1), **S1 is recommended outright** — not
just "preferred pending bank data" as the prior pass had it.

### K (cavity option) — confirmed: K0 with a quantified warning

The triplet comparisons (`K2_vs_K0`, `K1_vs_K0` in `summary.json`) stand from the prior
pass; this pass adds the column-side confirmation that **K1's RL cost is severe, not just
"1.4 dB"**: `cell-c1k-106/109/112` (K1, L retuned to 1.06/1.09/1.12) land at -1.70 to
-2.14 dB worst-in-band, worse than K0's best point (-5.38 dB) by 3-4 dB. K1 is not a free
fix on any L tried. **K0 stays, with a quantified warning** (the -3.3 to -4.7 dB bondply
transfer at 20/27 µm is real and mesh-consistent, but the alternative costs more than it
saves on this data). See `em-column.md` for the full 16-point C1 table and the D5
recommendation (corporate, over series-fed).

### D14 — PA island coupling fails its own -40 dB rule; fence submitted

Re-checking the port-case coupling figure in `em-column.md`: TX1's PA-to-antenna coupling is
**-34.6/-34.9 dB at 62 GHz**, short of the plan's ≤-40 dB rule by 5-6 dB (RX4's corner is
similar). A model-only ground-via fence was added around the PA pocket (`pa.py`'s own
clearance geometry reused to filter candidate sites; only 5-6 vias fit per corner — the
pocket is 1.69 × 0.75 mm and already shares its north edge with the A1/B1 keepout) and
submitted as part of `s3b-e2` (20261005-mceval-0ec3ac, GCP Batch c4-highcpu-8 Montreal,
ceiling $4.85, logged in `../gcp-spend.md`). This is a model-only geometry test (same tier
as `s3b-c1-wall`'s PEC walls) to measure whether even a partial fence helps before any
generator/DRC change is proposed; result pending.

### Cost ledger (final for this pass)

Stage-3b actual: ~$3.60 (bank-a + cav, prior pass) + ~$6.8 [E] (bank-b/c/s1, c1-k, c1-wall,
this pass, VM-hours × $0.0795-0.08/VM-h, no preemptions) ≈ **$10.4 of the $15 original cap**
(raised to $25 this session). `s3b-e2` adds a $4.85 ceiling (submitted, pending). Program
total ≈ $8.4 (pre-stage-3b) + $10.4 + up to $4.85 ≈ **$23.6 of $50**, all within budget.

## Recommendation to the orchestrator (this pass)

Still do not freeze — `s3b-e2` is the only thing left running (one C1 retune attempt and the
D14 fence check). Once it lands: (1) if any `cell-c1b-*` point covers 60.3-63.8 GHz at
-10 dB, promote it and re-run a confirming triplet (not centre-only) before freezing; if
none does, escalate C1 to the owner as a structural (not parametric) problem — the
cavity-wall upper bound already rules out "just the cavity" as the cause; (2) if the D14
fence pushes coupling past -40 dB, write it up as a generator change (board-level via
placement + DRC, not just the model) before claiming D14 closed; if it doesn't help with
only 5-6 vias fitting, say so and flag the pocket's size as the limiter for owner review.
D15/S1/K are now final calls from this pass (A / S1 / K0-with-warning) and do not need
more data to freeze on their own.

## FREEZE v2 pass (2026-10-05, blocked mid-pass by a Mac boot-disk ENOSPC)

`s3b-e2a` (6 tasks, `cell-c1b-*` round-the-best C1 points) SUCCEEDED 6/6 (4 pass, 2 hit the
wall-clock timeout — their partial S-parameters may still be usable, not yet checked).
`s3b-e2b` (3 `cell-c1b-*` + 2 D14 fence re-runs) was at 4/5 SUCCEEDED, 1 still RUNNING, when
the Mac's boot disk (not `<local-path>`) hit ENOSPC and every shell tool call on this
machine started failing — unrelated to this workflow's own GCP spend or field-dump habits;
cause not yet identified (another process, possibly the concurrent Codex agent, filling
the system temp directory or the boot volume). **Fetch and full-sweep analysis of `s3b-e2a`/`s3b-e2b` did not
happen** — this section records what was done before the blocker, not final numbers.

Meanwhile, with the tool still available, the D14 ground-via fence (owner D15 instruction)
was **implemented in the real generator**, not just the EM test model: `rfmacro/params.py`
(`pa_fence`, `pa_fence_offset=0.31`, `pa_fence_pitch=0.475`, default on) and
`rfmacro/vias.py` (`place()`, step 3b) place one row of GND through vias along the PA
pocket's north edge (facing the antenna bank), matching the EM-tested geometry in
`em/models/pa-pa-tx1-fence.json`/`pa-pa-rx4-fence.json`. On the current default geometry
only **2 of the EM test's 5 sites are free** (a dummy-load pad sits closer to the pocket on
this corner than in the tested cell) — flagged for owner review: the fence may need the
load cell nudged, or a tighter `pa_fence_pitch`, to recover the full 5-via row before the
coupling number is taken as closed. Build + all G1-G9 generator checks pass with the fence
on (`python3 -m rfmacro build`, checks_failed: []); `kicad-cli pcb drc` on the standalone
macro cell gives the same 199 violations / 499 unconnected as the no-fence baseline (both
are artifacts of DRC-ing an isolated macro, not full-board DRC; the fence adds zero new
violations). D15=A and dummies=S1 (`"dummies": "outer"`) are also now the frozen defaults
in `params.py`; D5 stays "corporate" (already the default, no evidence favours switching).

**Not done by this pass, left for whoever continues:** fetch/analyze `s3b-e2a`/`s3b-e2b`
(campaign ids in `../gcp-spend.md`) for the actual post-fence D14 coupling number and any
C1 `cell-c1b-*` point meeting -10 dB in-band; confirm whether the 2-via partial fence (vs.
the tested 5-via row) still closes coupling toward -40 dB or needs the load-pad fix first;
set `div_l_scale` from whatever C1 pick the data supports (still 1.0, unconverged, in this
commit); the one-wave confirmation EM run of the frozen macro; before/after renders; commit
the `rfmacro` changes above (made in the worktree, not yet committed when the blocker hit).

## FREEZE v3 pass (2026-10-05, blocked again — same ENOSPC, second consecutive pass)

**Still not frozen.** This pass was asked to pick up `s3b-e2a`/`s3b-e2b` (reported finished
by the main loop) plus the review's findings above, fence D14 properly and re-run, run the
E2 column wave, and commit. None of that could happen: **the Bash tool failed with
`ENOSPC: no space left on device` on the boot disk (`<tmp-path>` and
`~/.config/gcloud/logs`) on every single invocation this pass** — `df`, `echo`, `:`, `pwd`,
all of them, retried seven times over several minutes, not transient. This is the identical
failure mode the v2 pass recorded, now recurring a second time in a row; its cause (another
process filling the boot volume — possibly the concurrent Codex/Astra agent noted in
memory) is still not identified, and this task has no tool to diagnose or clear it (Bash
_is_ the tool that would do that, and Bash is what's broken). Per the no-kill /
don't-clean-up-others'-work rules and the scope guard, I did not attempt to free space.
`Read`/`Edit` on this external volume (`<local-path>`) kept working throughout,
which is how this section exists.

**Consequently, none of the following happened this pass:** `gcloud batch jobs describe` on
any of the named campaigns (bank-a/b/c, bank-s1, cav, c1-k, c1-wall, e2a, e2b) — their state
is whatever v2 last recorded above, not re-verified; `yapnr exp fetch` of `s3b-e2a`/`s3b-e2b`
or any full-sweep analysis of them; the D14 fence fix; the E2 C1 column retune wave; any new
GCP submission (so **no new spend this pass** — the program total stands at the last-logged
≈$23.6 of $50, well under the raised $25 stage cap minus what's already committed); `git
commit` of the uncommitted `rfmacro` changes; `python3 -m rfmacro build`/DRC re-verification;
renders.

**What _was_ done, read-only, against the adversarial review quoted in this pass's task
(no execution, so "confirmed" below means confirmed by reading the code on disk, not by
running it):**

- **Review finding 2 (D14 fence not a ring) — CONFIRMED.** `vias.py` step 3b (`place()`,
  the `# 3b. D15 PA fence` block) places exactly one row at `y1 + pa_fence_offset` across the
  pocket's x-span, nothing on the east/west/south sides — not "round the pocket box" as
  `params.py`'s own comment (the `pa_fence` block, "A ring of GND through vias round the
  pocket box") claims. `n = floor((x1-x0)/pa_fence_pitch) + 1 = floor(1.693/0.475)+1 = 4`
  sites max, never the 5 the EM test used — matches the review's math, not freeze v2's "5
  EM-tested" framing. The comment-vs-code mismatch (ring vs. row) is real and should be
  fixed by whoever next edits this file: either implement the ring the comment describes,
  or correct the comment to say "one row, the bank-facing edge only" and add a G-check for
  minimum site count. The row _is_ on the edge nearest the antenna bank (pocket top is
  y≤34.2; the RX/TX phase centres sit at y 38.1-39.99, i.e. north/higher-y; the fence sits
  at `y1+t`, further north, toward the bank) — so the "wrong side for TX1" half of the
  review's finding does not hold on this reading; the "not a ring / too few sites / comment
  mismatch / no G-check" parts do.
- **Review finding 7 (stale Palace status) — CONFIRMED.** `palace/READY.md` line 1 says the
  HOLD was lifted 2026-10-04 and column-sized sign-off runs are READY; this file (freeze.md,
  throughout) still describes Palace as on hold / not run. No Palace run happened this pass
  either (no Bash).
- Findings about GCP state, e2a/e2b fetch, generator re-run, cost-ledger reconciliation,
  D15/S1/K/D5 full-sweep re-judgment, and ANT-02 open-item flagging from the earlier review
  all require either execution or data this pass could not reach; they are **not
  adjudicated here** and should not be read as rejected — only as unverified for a second
  pass running.

**Orchestrator: this is now a two-for-two blocker.** The stage-3b RF freeze cannot progress
on this Mac until its boot disk has free space again; that is an infrastructure action
outside this task's tools and scope. The last analysis anyone should treat as live is the
"Full-sweep pass (2026-10-04 evening)" section above (D15=A with a quantified warning, S1
dummies, K0-with-warning, D14 open at -34.6/-34.9 dB, C1 unconverged) plus the v2 pass's
real-generator D14 fence code (uncommitted, 2-of-4-sites free on current geometry, now also
flagged above as not actually a ring). Nothing in this v3 pass changes any of those calls;
it only reads code and reports the blocker.

## FREEZE v4 pass (2026-10-05, later the same session): disk freed, E2 fetched and analyzed, still not frozen

The boot disk freed up partway through this pass (100% -> 87% -> 69% full over about ten
minutes, with no action by this agent) and Bash started working again. What follows used
the recovered Bash: `yapnr exp status`/`fetch` on the real campaigns, reading and editing
`rfmacro` code, rebuilding the macro, and one new GCP plan attempt. **Still not frozen** --
see the blocker at the end.

### Campaign status (verified live, not carried over from notes)

All of `s3b-bank-a` (3b60dc), `s3b-bank-b` (05776b), `s3b-bank-c` (914040), `s3b-bank-s1`
(443dad), `s3b-cav` (08e2c7), `s3b-c1-k` (0c07c4), `s3b-c1-wall` (9a3e4d) read **done,
all tasks SUCCEEDED** via `yapnr exp status` -- confirms the main loop's "ALL E1 campaigns
finished" and matches the full-sweep pass already recorded above; no re-analysis needed
there. `s3b-e2` (0ec3ac) is CANCELLED (0/11, superseded by the split below, matches
gcp-spend row 96). `s3b-e2a` (22f21a): SUCCEEDED 6/6 (4 with post-processed `result.json`,
2 -- `cell-c1b-0`/`cell-c1b-4` -- hit the wall-clock timeout with raw field dumps only, no
`result.json`; per the exact-end-criteria rule these two are **excluded**, not "partial
S-parameters used"). `s3b-e2b` (540c3c) was 4/5 SUCCEEDED, 1 (`cell-c1b-7-r20`) RUNNING when
checked. Fetched both with `yapnr exp fetch --full`.

### Lost mid-pass: `~/yapnr-runs/plans` is being deleted by something else on this Mac

While trying to plan a new campaign, `~/yapnr-runs/plans/<id>/` was observed being deleted
within seconds of being written -- twice, on two different new plan directories -- and the
entire `~/yapnr-runs/plans` tree (all prior campaigns' local tracking, including
`20261005-mceval-540c3c`) is now gone; `gcloud batch jobs describe` on that campaign's id
also now returns "not found" in us-west4, so the GCP side is gone too, not just the local
cache. This cost the still-running `cell-c1b-7-r20` task (its result, if it ever finished,
is now unrecoverable) and blocked a new submission (below). Not caused by this agent -- no
`rm`, no `yapnr exp cancel` was run on it -- and matches the "Astra coexistence" memory note
(a concurrent Codex agent may be running on this Mac); most likely that agent or a disk
watchdog is garbage-collecting `~/yapnr-runs/plans` under disk pressure. **Orchestrator:
worth a direct check with whatever else is running on this Mac before the next pass
submits anything** -- a plan directory cannot be trusted to survive long enough to submit
and track a job right now. Already-fetched results (`~/yapnr-runs/fetched/...`, and this
pass's own copies into `stage3b-rf/em/col/runs/` on `<local-path>`) were not
affected and are safe.

### C1 -- the E2 "round-the-best" wave ran; still does not converge (review finding #1 narrowed, not closed)

Six usable `cell-c1b-*` points (1,2,3,5,6,8; L=1.025, i.e. squarely in the 1.00-1.04 bracket
the adversarial review asked for, varying inset/w35), all K0 (today's L2 windows), full
54-70 GHz sweep, `exact-endcriteria`:

| id         | inset | w35   | worst in-band S11 (dB) | covers -10 dB? |
| ---------- | ----- | ----- | ---------------------- | -------------- |
| cell-c1b-1 | 0.325 | 0.300 | -5.14                  | no             |
| cell-c1b-2 | 0.325 | 0.300 | -6.34                  | no             |
| cell-c1b-3 | 0.325 | 0.353 | -5.95                  | no             |
| cell-c1b-5 | 0.325 | 0.353 | -5.95                  | no             |
| cell-c1b-6 | 0.325 | 0.420 | -6.51                  | no             |
| cell-c1b-8 | 0.325 | 0.420 | -5.92                  | no             |

None covers the band at -10 dB; best is -6.51 dB. **This directly answers the review's
critical finding #1's immediate ask** (L in the 1.00-1.04 bracket, not just 1.06-1.12) **for
K0**: correcting L alone, in the right bracket, does not fix the band -- the problem is the
cavity (K0's -3.3 to -4.7 dB bondply transfer), not mistuning, consistent with freeze v2's
"escalate as structural" call. What the review actually asked for -- **K1 (solid L2) at
L 1.00/1.015/1.03**, the one comparison that would show whether closing the cavity _and_
retuning together clears -10 dB -- is still not run: `s3b-c1-e2`'s own attempt at exactly
this (gcp-spend row 97) was cancelled for a quota conflict, and this pass's attempt to
resubmit it was blocked by the `~/yapnr-runs/plans` deletion above before a job was ever
created (no GCP spend occurred from the attempt). **K1 at the right L remains the one
open experiment that could change the K/C1 picture**; K0 stays the pick in the meantime,
C1 stays unconverged, both now on better evidence than v2 had.

D5: `cell-ser-1/2/8` (series-fed, same K0 cavity) land at -1.8 to -2.36 dB worst-in-band,
3-4 dB worse than corporate's -5.1 to -6.5 dB under the _same_ confounding cavity (review
finding 11 is right that this isn't a clean A/B -- both sit in the unfixed K0 cavity -- but
corporate's consistent several-dB edge even so gives D5=corporate a real, if not final,
basis instead of "no evidence either way." Recommendation stands: corporate.

### D14 -- real numbers now, and two of freeze v2's claims were wrong

`em/col/runs/pa-pa-tx1-port`, `pa-pa-rx4-port` (baseline, no fence) and
`pa-pa-tx1-fence-port-u20`, `pa-pa-rx4-fence-port-u20` (this pass's fetch, the full 5-6-via
EM test ring) give, port case, 62.05 GHz:

|     | baseline                                    | with EM-test fence                        |
| --- | ------------------------------------------- | ----------------------------------------- |
| TX1 | **-34.9 dB** (-33.7 at 60.3, -35.8 at 63.8) | **-46.1 dB** (-45.2 to -47.0 across band) |
| RX4 | **-42.2 dB** (-41.5 to -42.5 across band)   | **-45.6 dB** (-45.5 to -45.7 across band) |

Two corrections to freeze v2 and the main-loop task text, both of which quoted "-34.6/-34.9
dB... RX4's corner similar": **RX4's corner already clears the <=-40 dB rule unfenced**
(-42.2 dB); only TX1 fails (-34.9 dB, by ~5 dB). With the EM-tested fence, **both clear the
rule by 5-6 dB of extra margin** (TX1 -46.1, RX4 -45.6) -- a real, not marginal, fix at the
model level.

What this pass found by reading the generator code (not just the model) **also corrects
freeze v2's "2 of 5 sites free"**: that count was taken with `dummies="both"` (S2). Rebuilt
the macro with the frozen default `dummies="outer"` (S1) and checked
`mc.via_log["dropped"]["pa_fence"]` directly: **all 4 of the generator's own maximum 4
sites place, 0 dropped** (with `"both"`, 2 drop -- 1 spacing conflict, 1 load-pad conflict --
matching the old count, just under the wrong dummy setting). So the real gap is 4 built
vias vs. the 5-6 the EM test modelled, not 2 vs. 5 -- smaller than freeze v2 said, and no
drops at all under the frozen defaults. Fixed the comment mismatch the adversarial review
flagged (`params.py`'s "ring... round the pocket box" vs. `vias.py`'s actual single row):
both files now say plainly that this places one row, not a ring, and name the real gap to
the EM-tested geometry. **D14 is not fully closed**: the exact 4-via-row geometry has not
itself been run through openEMS (only the 5-6-via ring was), so the -46/-46 dB number is
strong supporting evidence, not a direct measurement of what the generator builds. Verified
`python3 -m rfmacro build` still passes all checks (`checks_failed: []`) with these comment
fixes.

### D15 / S1 / K -- unchanged from the full-sweep pass; still stand

No new data this pass bears on D15 (pick A, quantified warning), S1 (dummies="outer"), or
whether K0 vs. K1 changes once retuned (see C1 above, still open). These are not
re-adjudicated here.

### Not done this pass

The K1 L-retune submission (blocked, see above); any new EM point on the generator's actual
4-via D14 fence geometry; `div_l_scale` is still 1.0 (C1 unconverged, nothing to set it to);
the one-wave confirmation EM run of the frozen macro; before/after renders (still no
`matplotlib` reported available; not re-checked this pass); a Palace column run (READY per
`palace/READY.md`, still not actually run for radar60).

### Committed this pass

`rfmacro/params.py`, `rfmacro/vias.py`: the D14 fence comment fixes above (accuracy only,
no geometry change -- the macro still places the same vias it did before this pass) plus
the v2 pass's frozen `pa_fence`/`dummies="outer"` defaults, now committed together. No new
GCP spend: the only submission attempt this pass never reached `yapnr exp submit` (blocked
during planning), so nothing billed. Program total is unchanged from the last-logged
~$23.6-25.8 of $50 (gcp-spend.md rows 94-97); this pass's own GCP activity was read-only
(`status`/`fetch` of already-running/finished campaigns).

**Orchestrator: still do not freeze.** Two things stand between this and a real freeze: (1)
the K1-at-L-1.00-1.04 point, blocked by the `~/yapnr-runs/plans` instability above, not by
budget or by anything in this task's control; (2) an EM point on the as-built 4-via D14
fence (cheap, ~1 task, once planning is stable again). Everything else (D15/S1/D5/K0
defaults, the corrected D14 baseline and EM-test-fence numbers, the corrected "2 of 5" ->
"4 of 4" generator count) is solid enough to carry into stage 3c as current best numbers,
clearly labelled provisional where noted above.

## Freeze (macro v2): 2026-10-05, s3b-e3 + s3b-palace-c1 applied

**Frozen: yes, on claude/radar60-rf3b 727df41 (pushed).** ANT-02's RL miss is preregistered as the
D5 rule requires, and the D14 TX1 miss is recorded as an open warning (see below). rfm1-n
`geometry_sha256` c017a7bc…71ce0 (`outside_cutouts_sha256` e49790dd…f22ba6).

Inputs: `20261005-mceval-1f3598` (8/8 pass) and `20261005-mceval-f13ef3` (1/1 pass). Both plans
had gone from `~/yapnr-runs/plans/` again. They were restored from `stage3b-rf/plans/` copies
before status and fetch. Both were fetched `--full`; the runs were copied to `em/col/runs/` and
re-analysed with `stage3b_col.py` (`em/col/out3/column.json`). No resubmission was needed.

### K1 at the right L (review finding #1's open experiment): closed, K1 rejected

| point (20 um, inset 0.30) | min \|S11\|      | worst in-band (oems / Palace-ref) | gain 60.3/62.05/63.8 dBi |
| ------------------------- | ---------------- | --------------------------------- | ------------------------ |
| K1 L 1.000                | -8.83 dB @ 62.23 | -5.16 / -3.83                     | 5.81 / 7.15 / 6.13       |
| K1 L 1.015                | -8.27 dB @ 61.45 | -4.06 / -4.83                     | 6.42 / 6.82 / 4.94       |
| K1 L 1.030                | -7.80 dB @ 60.62 | -3.16 / -4.41                     | 6.74 / 5.95 / 3.70       |
| K0 L 1.000                | -25.4 @ 64.85    | -3.56 / -3.56                     | 6.23 / 2.72 / 6.95       |
| K0 L 1.015                | -41.4 @ 64.53    | -3.58 / -3.58                     | 6.94 / 2.89 / 6.32       |
| K0 L 1.030                | -25.6 @ 65.83    | -3.81 / -3.81                     | 7.56 / 2.01 / 6.05       |

Solid L2 (K1) never reaches -10 dB anywhere: closing the cavity removes K0's gain notch at 62 GHz,
but at this inset it leaves the patch under-coupled. By the K rule ("K1 only if C1 can still reach
RL >= 10 dB on solid L2"), K1 is rejected. **K0 stays**, and the bondply cavity remains a quantified
warning (-3.3/-4.7 dB transfer, 20/27 um).

### Palace cross-check (s3b-palace-c1, patch-w p2, uniform -> 1 AMR pass, 1.04 M dof)

L x1.023: dip 61.65 GHz (-17.5 dB), RL-10 band 60.40-63.15 GHz. The uniform stage gave 61.55,
so the AMR pass moved it +0.1 GHz. Against Palace patch-w at L x1.0 (63.54 GHz), that is -1.89
GHz for +2.3 % L. The palace_ref frame (k = 1.0165 at 20 um) still holds at L 1.0: 62.54 x 1.0165
= 63.57 against 63.54. Even the isolated patch's RL-10 band is 2.75 GHz wide against the
3.5 GHz band, which is independent evidence that no L alone can cover 60.3-63.8 GHz at -10 dB.
The L that centres the Palace patch is about 1.020. The K1 column, Palace-referred, needs about
1.022, so the two agree within 0.2 %.

### C1: the rule's fallback pick

No point on disk meets |S11| <= -10 dB (22 points at 20 um). The rule's fallback is to
"maximize the minimum in-band RL with the band centred". In the Palace-referred frame, that is
**cell-c1b-6**: K0, L x1.025, inset 0.325, w35 0.42, t_y 0.25. Worst in-band is -6.51 dB in
openEMS and -7.2 dB Palace-referred. Its RL-10 band centre is 62.63 GHz Palace-referred, the best
centred of all points. Gain is 4.80 / 5.90 / 6.87 dBi and efficiency 0.54 / 0.54 / 0.61.
**Preregistered ANT-02 misses**: RL (-7.2 dB against -10 dB), gain at 60.3 GHz (4.8 against
5 dBi), and efficiency (0.54 against 0.6). Gain-clean alternate: cell-c1b-2 (w35 0.30, t_y 0.49).
It is -6.34 dB in both frames and meets the gain floor, but its band is centred 1.8 GHz high
(63.81 Palace-ref). Not checked: the 15 um trend of c1b-6 (only 20 um exists); D12 brackets
+-1.8 % around L 1.025 (rfm1-m/p). D5 is corporate (series is 3-4 dB worse under the same
cavity, both miss).

### D14 on the as-built fence: TX1 FAILS, RX4 passes

| port case, 62.05 GHz | baseline (old board) | EM-test fence (model-only) | **as built (fence4)**                                     |
| -------------------- | -------------------- | -------------------------- | --------------------------------------------------------- |
| TX1                  | -34.9                | -46.1                      | **-33.5** (-31.6 @ 60.3, -35.3 @ 63.8; -26.9 worst 54-70) |
| RX4                  | -42.2                | -45.6                      | **-44.9** (-44.6 to -45.1)                                |

Why: the generator places its 4 fence vias at y = pocket top + 0.31 (the bank side, U1 frame
y 7.31). The EM-tested fence was a row at y 4.82, i.e. _inside the U1 package outline_ (|x|,|y| <
5.2) between the TX1 launch and the PA neck, where the generator's `package` keepout forbids any via. A probe
of a south (package-side) row placed 0 of 4 (all "package"). An east column placed 1 of 4, and
TX1's own inner fence row (x 5.45) already lines that side. TX1's crop (y <= 6.8) does not even
contain the north row. So **no buildable macro fence reaches the coupling path**. The only lever
left is under the BGA: U1's GND ball vias and fanout, which belong to integration (`merge_macro`
/ 3c). The port case is pessimistic, since the real island is cap-shorted. Rule D14 is recorded
as **not met for TX1 (-33.5 dB against -40 dB)**, handed off as a warning, and not re-run.

### What changed in the generator and boards (727df41)

- params: the C1 pick above. All other v2 defaults were already in a847d62: D15 A, S1
  `dummies="outer"`, K0, corporate, D14 `pa_feed` + `pa_fence`. `div_l_scale` stays 1.0.
- rfm1-m/-n/-p and coupons regenerated. They had not been regenerated since the S1/fence
  defaults were set. KiCad DRC (headless): 0 violations, 0 unconnected, all three. Unit tests:
  46/46. Six stale S2 expectations were fixed; they were already failing at a4934b8.
- **board_frame moved**: the TX bank is 3.5 mm further west (S1 dropped TXD0/RXD5), the region is
  x 12.599-47.972, y 26.7-47.059, `board_height_min` 47.384, and the VOUT_PA pocket is
  [29.563, 33.45, 31.25, 35.0]. `board/floorplan.yaml` now follows it: height 47.40, patches,
  region, pocket, guard, and holes at y 43.9. `gen_board.py --macro` reports covered/same and
  0 items outside, and `--check` is clean. **Stage 3c must take this macro and outline**. The
  RF macro digest changes from the one radar60-3c holds; that is intended, since it is the
  freeze.

Cost of this step: s3b-e3 4.65 VM-h c4-highcpu-8 Spot ~ $0.25, s3b-palace-c1 0.35 VM-h
c4d-highcpu-16 Spot ~ $0.05, plus ~$0.02 egress: **~ $0.32**. No new submission.
