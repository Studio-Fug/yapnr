# D1 re-optimization under the fixed corner-gap DRC (part 4/5)

Status: done 2026-10-05. Campaigns `20261005-mceval-f1e549` (submission 1) and
`20261005-mceval-451407` (the resubmission of f1e549's four timed-out D1 lines, with a longer
end window; see below), under this workflow's separate $8 GCP cap (`../../../../order0/gcp-spend.md`).

Evidence labels: **[S]** solver output, logs or records; **[D]** derived here (arithmetic,
judgement); nothing here is measured (no board has been fabricated).

## Why this exists

The committed `d1-star` (`docs/rf/order0/predictions/D1/`, part 2) reproduces two 0.100 mm
corner-gap violations against OSH Park's 0.127 mm rule under the exact corner-gap DRC checker
PR #53 added. O0-D cannot ship with a board that fails its own fab DRC, so D1 was re-optimized
from the same four formulations (base, robust, star, sched), same specs/criteria
(`docs/rf/order0/inputs/specs/d1-*.json`, `criteria/d1.json`), same pre-declared selection rule
(`part2/select.py`), under commit `ad2bda7` (PR #53, the fixed DRC; D2/O0-W's later changes
in PR #54 do not touch the RF solver or this spec/criteria set).

## Campaign history

- `20261005-mceval-f1e549` (submitted 2026-10-05 05:00 UTC, c4-highcpu-16 Spot
  northamerica-northeast1, `--attempt-s 10350 --end-s 3450`, `max_retries 1`): the 2 R1
  references passed; base/robust/star/sched all exited 75 (timeout) after both attempts — this
  workflow's own plan directory for this campaign was lost to a host disk-full event partway
  through (recovered byte-for-byte from `gs://yapnr-experiments-runs/campaigns/20261005-mceval-f1e549/`,
  which `yapnr exp submit` had already copied there; this is why the resubmission below is a new
  campaign id rather than a second submission of f1e549 — that campaign's `tasks.jsonl` is
  immutable once submitted, and a longer `--end-s` needs a different one).
- `20261005-mceval-451407` (order0-d1-resume, submitted 2026-10-05 11:38 UTC, same image digest,
  same commit/specs/criteria, `--end-s 6000` instead of 3450 — part 2 found the end phase
  (export, coarse, fine, finer) needs 5093-5100 s for a D1-size grid): base, sched and star all
  finished (SUCCEEDED); **robust timed out again** (reached iteration 69 of its schedule and the
  fine re-validation before the hard 10800 s wall, per its log tail) even with the longer window,
  and is reported **inconclusive**, not re-run further (the task asked for one resubmission).
  Checkpoints (`g45`/`g49`/`g47`/`g49` for base/robust/sched/star) were copied GCS-to-GCS from
  f1e549's checkpoint store before submitting, so every run resumed mid-optimization rather than
  starting over.

## Outcome: `d1-star` ships again

Applying the pre-declared rule exactly (`part2/select.py`, `selection.json` in this directory):
among the runs whose exported design passes every check on coarse, fine and finer, the largest
worst-case fine/finer margin; ties within 0.05 dB broken by fewest repaired pixels, then order.

| run         | ok                     | coarse \|S11\|/\|S21\| (dB) | fine           | finer          | fine/finer margin (dB) | repaired px | DRC             |
| ----------- | ---------------------- | --------------------------- | -------------- | -------------- | ---------------------- | ----------- | --------------- |
| `d1-base`   | fail                   | -7.46 / -4.16               | -7.57 / -4.16  | -7.59 / -4.15  | -7.428                 | 239         | **fails** (new) |
| `d1-robust` | inconclusive (timeout) | —                           | —              | —              | —                      | —           | —               |
| `d1-star`   | **pass**               | -20.38 / -3.26              | -20.77 / -3.27 | -20.77 / -3.26 | **0.282**              | 134         | ok              |
| `d1-sched`  | fail                   | -8.60 / -3.97               | -8.74 / -3.97  | -8.77 / -3.96  | -6.259                 | 328         | **fails** (new) |

`d1-star` is the only run that passes on all three grids, so it ships without a tie-break — the
same formulation part 2 shipped, now with a clean footprint export under the fixed DRC (the
width/space repair changed 134 px here, vs 30 in part 2's run; islands 25, 24 floating, vs 15/14
before — same topology, a different repaired boundary). `result.json` sha256
`7e070ca8e32dfa1c8d6755302681f3538e51ee65e90c361086badf1cc329c4b8`, so the label is
`O0 D1 divider-osh-m 7e070ca8` (supersedes part 2's `O0 D1 divider-osh-m ad20e643`). Under the
new DRC, `d1-base` and `d1-sched` additionally **fail export DRC** (`drc_ok: false`), not only
the S-parameter checks — they were DRC-clean under the old checker.

`d1-star`'s full numbers [S] (coarse / fine / finer, raw):

- \|S11\| max: -20.38 / -20.77 / -20.77 dB
- \|S21\| = \|S31\| min: -3.261 / -3.268 / -3.263 dB
- fine/finer margin over the re-validation criteria (-15/-3.55 dB): 0.282 dB

## Validation on the other two substrates (campaign `20261005-mceval-c70352`)

The shipped footprint re-validated coarse/fine/finer on M-eq (wide 3-7 GHz pulse), M-nom and
M-eq-em528 (`variants/`), mirroring part 2's `20261004-mceval-81c8f7` [S]:

| substrate         | coarse \|S11\|/\|S21\| (dB) | fine            | finer           | re-validation criteria (-17/-15 dB) |
| ----------------- | --------------------------- | --------------- | --------------- | ----------------------------------- |
| M-eq (wide pulse) | -20.36 / -3.259             | -20.72 / -3.265 | -20.74 / -3.263 | pass                                |
| M-nom             | -19.70 / -3.273             | -20.02 / -3.276 | -20.05 / -3.277 | pass                                |
| M-eq-em528        | -19.45 / -3.223             | -19.80 / -3.230 | -19.81 / -3.231 | pass                                |

All three substrates and all three grids pass the re-validation's own (looser, -17/-15 dB)
sanity criteria. Against the Order 0 design's actual -20 dB \|S11\| spec: M-eq and M-nom clear
it with a thin margin (M-nom's worst point, -19.70 dB coarse / -20.05 dB finer, is close to the
line); EM528 misses by 0.19-0.55 dB, the same pattern part 2 found (R1 misses there too, by
about the same amount — see below). This repeats part 2's finding almost exactly: the new
export's numbers are all within about 0.2 dB of the old ones.

## R1 (the textbook reference, for comparison): unchanged

R1 is a fixed hand-drawn geometry, not affected by the DRC fix, so it was only re-run as a
forward check (not re-optimized): M-nom \|S11\| -21.74 dB / \|S21\| -3.282 dB, M-eq-em528 \|S11\|
-19.78 dB / \|S21\| -3.249 dB [S] (coarse, 20261005-mceval-f1e549) — both within about 0.1 dB of
part 2's numbers (-21.64 / -19.69 dB), consistent with run-to-run solver noise rather than any
real change.

## openEMS (independent 3D FEM-free FDTD, 43 µm PEC copper, nominal FR408HR)

Campaign `20261005-mceval-5bb031` (order0-openems-d1-new-r05): the new D1 only, at 0.05 mm, P1
and P2 excited (P3 by mirror symmetry, `post.py`), exact end criteria — same method as part 2's
`20261004-mceval-11b741`/`e04335`. **R1 was not re-run**: it is a fixed geometry unaffected by
the DRC fix, so part 2's openEMS R1 predictions
(`../openems/r1-openems-r0{5,25}-loss-corrected.s3p`, `-19.69`/`-20.47` dB \|S11\|,
`-3.295`/`-3.294` dB \|S21\|) are reused as-is rather than re-run. **Only the 0.05 mm mesh was
run here** (not the 0.025 mm mesh-convergence pair part 2 also had for D1): this workflow's $8
cap left no safe room for both it and a Palace run after the resubmission and the 3-substrate
validation (see "Budget" below), and Palace — the independent _second_ solver, separate from
openEMS — was prioritized for the remaining headroom instead of a convergence check on the
solver already run.

|                                 | raw \|S11\| max (dB) | raw \|S21\|/\|S31\| min (dB) | loss-corrected \|S21\|/\|S31\| (dB) [D] |
| ------------------------------- | -------------------- | ---------------------------- | --------------------------------------- |
| New D1 (this campaign)          | -17.10               | -3.217 / -3.213              | -3.329 / -3.325                         |
| Old D1 (part 2, for comparison) | -17.42               | -3.210 / -3.206              | -3.322 / -3.317                         |
| yapnr.rf (new D1, M-eq, finer)  | -20.77               | -3.263 / -3.263              | n/a (see part 2's method)               |

The loss correction [D] reuses part 2's Δα/ratio from the 0.40 mm M line (unaffected by D1's
export change, since it is a property of the substrate and the solver's own line, not of D1's
footprint): +0.112 dB of additional loss on \|S21\|/\|S31\|, the same offset part 2 derived and
applied. **openEMS again predicts that D1 does not meet the -20 dB \|S11\| spec** (a 2.6-3.1 dB
miss here, vs 2.7-3.3 dB on the old export — essentially the same gap) while yapnr.rf predicts a
clean pass; the two agree on transmission (\|ΔS21\| 0.11 dB raw). This is registered as a design
that **passes yapnr.rf but misses on openEMS**, exactly part 2's finding, now confirmed to carry
over to the DRC-fixed export (the repair's 134 changed pixels did not change the match/mismatch
picture). D1's 14-islands-became-24-islands floating-copper suspect from part 2's note is
unchanged (more floating islands after this repair, not fewer).

## Palace (independent 3D FEM): not run

This workflow's $8 cap (separate from part 2's own $15 cap) was reached by the f1e549 submission
($3.74), its resubmission ($2.50), and the 3-substrate validation ($1.29) before the openEMS
run; after openEMS's reduced-scope run ($0.34) only about $0.13 of ceiling remained — not enough
for even a minimal Palace campaign's ceiling once Batch's own retry/attempt accounting is
included. Running Palace on D1 would also need a `divider`-shaped planar adapter (none of the
existing Palace cases — feed, launch, column — fit a 3-port MSL divider in a 12×15 mm window),
which was not attempted given the budget was already exhausted. **D1 and R1 have no Palace
comparison in this round**; this is an open item for a follow-up with its own budget.

## Files

- `runs/d1-star/`, `runs/d1-base/`, `runs/d1-sched/`: spec, `result.json`, `validation.json`,
  footprint and (for `d1-star`) the re-validation Touchstone files and optimizer history, from
  `20261005-mceval-451407`. `d1-robust` is not included (no result: timeout).
- `variants/d1-star-{m-eq,m-nom,m-eq-em528}/`: the 3-substrate re-validation, from
  `20261005-mceval-c70352`.
- `openems/d1-new-openems-r05.s3p`: the raw (not loss-corrected) S-matrix of the new D1 at
  0.05 mm, from `20261005-mceval-5bb031`, assembled by `docs/rf/order0/openems/post.py`.
- `selection.json`: `part2/select.py`'s output over this round's four formulations.
- Fetched results: `~/yapnr-runs/fetched/{20261005-mceval-f1e549,20261005-mceval-451407,20261005-mceval-c70352,20261005-mceval-5bb031}/`.
- Run summary and three-solver table: `../../../order0/part5/run.md` (outside this repo, in the
  Splanc workspace) and this file.
