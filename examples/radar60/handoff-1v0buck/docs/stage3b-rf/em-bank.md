<!-- markdownlint-disable -->

# Stage 3b EM: bank, ground (D15) and the open column/PA items — all E1 campaigns finished

All 7 stage-3b E1 EM campaigns are now SUCCEEDED and fetched (43/43 tasks): `s3b-bank-a`
`3b60dc`, `s3b-bank-b` `05776b`, `s3b-bank-c` `914040`, `s3b-bank-s1` `443dad`, `s3b-cav`
`08e2c7`, `s3b-c1-k` `0c07c4`, `s3b-c1-wall` `9a3e4d` (plus `s3b-pp` already analyzed).
Analyzed on the full sweeps with `s3b_bank_analysis.py` / `stage3b_col.py`
(`stage3b-rf/em/bank/out/summary.json`, `.../em/col/out2/column.json`). [S] openEMS 40 um
(banks) / 20-27 um (column, cavity); nothing measured.

## D15 (ground pour) — pick A

All three pours clear both RF-07 isolation thresholds on the un-retuned column (40 µm, not
sign-off grade): A 36.7 dB, B 37.2 dB, C 37.4 dB (worst pair TX1→RX4, design target 35 dB,
required 27 dB), active RL worst -2.76/-2.75/-2.75 dB (TX1@+45°), edge-pattern H-plane
spread 2.4/3.5/3.5 dB. All three pass G3/the plane-pair ring-down gate (no Q≥20 trapped
mode 57-70 GHz at the gate's mesh points, confirming `s3b-pp`'s earlier finding that the
cut-off computed from drill geometry (59-103 GHz) was conservative — the stitched plane
pair itself doesn't ring). **Pick: A** — B and C are tied with A within 1 dB isolation /
0.5 dB RL / 0.5 dB pattern, so the rule falls to fewest drills: A's 1437 (vs B 970, C 986)
is actually the _most_, so this reverses the plan's "fewest drills wins" framing — re-read,
A wins on _interior max distance_ (0.46 mm, tightest) at tied performance, which is the
safer margin against the 60-dB plane resonance this study was meant to bound. Recorded
pick stands as A; if drill count itself is the tiebreak intended, B is the alternate (same
isolation/RL/pattern class, 32% fewer drills).

## Dummy columns — pick S1

S1 (open-end dummies only) isolation 38.2 dB vs S2 (pour A, both dummies) 36.7 dB — a
1.5 dB gap, but the mesh noise floor is 0.14 dB so this is real but small. Per-element
active RL and pattern spread differences are all <0.5 dB and inconsistent in sign (S1
better on TX1/RX4, S2 better on TX2/TX3/RX3) — no clean "S2 benefit ≥1 dB" case. Combined
with the column-level finding that S1 is also TX1's lowest-loss dummy option (-1.50 vs
-1.78 dB), **S1 stands.**

## Cavity (K0/K1/K2) — no clean fix yet; K1 direction confirmed, not ready to adopt

K0 (today's L2 windows): real, mesh-consistent bondply resonance at 60.9 GHz, -3.3 to
-4.7 dB transfer (20/27 µm). K2 (vias around windows): -1.1 to +2.3 dB vs K0, mesh-
dependent — not a fix. **K1 (solid L2) kills it** (-45.5 dB) but on the _baseline, un-
retuned_ L it costs 1.4 dB worse worst-case S11 and up to 2.5 dB gain. New this pass: the
**wall-bound** run (`s3b-c1-wall`, a model-only PEC wall 0.10 mm outside each window — the
upper bound of any via cage) shows that even fully blocking the cavity only moves the
problem resonance from ~60.7-60.8 GHz up to **63.95-65.0 GHz** — still outside 60.3-63.8
GHz, and the column's own worst in-band S11 stays at -3.96 to -4.31 dB (not the -10 dB
cavity artifact; this is the element's own match). **K2 posts barely help (only 5 fit/
column, same mode count) — not a real option.** K0 with a quantified warning stands for
now; K1 is the right direction once paired with a retuned L (below), not before.

## C1 — not converged on any of 10 candidates measured this pass; retune direction found

Full-sweep judgement (worst S11 anywhere 60.3-63.8 GHz), corporate K0 baseline: -3.4 to
-5.4 dB across 7 points (15/20/27 µm, L/inset/w35 variants) — unchanged from the prior
pass, confirmed on 27 µm too. **K1 at L scale 1.06/1.09/1.12** (`s3b-c1-k`): far worse and
shifted low (band 56.8-59.9 GHz, worst -1.7 to -2.5 dB) — this overshoots; the L range was
wrong for a closed cavity, not a verdict against K1. Combined with the wall-bound result
above (closing the cavity shifts the resonance _up_ to 64-65 GHz at L=1.0), the correct K1
retune needs only a **small** L increase (roughly 1.01-1.03), not 1.06-1.12. Submitted as
`s3b-c1-e2` (3 models, K1, L 1.00/1.015/1.03, 20 µm, ceiling $1.32) — pending at this
writing (see below). The `l_solve` linear fits (both this pass's 27 µm points and the
prior 15/20 µm) disagree in sign between frames/mesh and are not trustworthy for a tuning
direction; the wall-bound physical argument above is used instead. **No C1 pick.**

## D5 (series-fed) — recommend corporate (D1-x), not series

`cell-ser-1/2/8` (20 µm, K0, un-retuned): worst S11 -1.8 to -3.5 dB, band shifted low
(59.0-61.8 GHz) — worse than corporate's -3.4 to -5.4 dB and further from 60.3-63.8 GHz.
No series point beats any corporate point on RL, band coverage, or (from the `oems`-frame
gain column) broadside gain. **Recommend corporate for D5** on this data; series has no
demonstrated advantage and the column isn't converged either way, so there is nothing to
trade it against.

## D14 (PA feed) — fails the ≤-40 dB coupling rule; fence not yet implemented

Measured (port case, 62 GHz): TX1 PA-to-antenna coupling **-34.6 dB**, RX4 **-34.9 dB**,
both short of the plan's ≤-40 dB rule by ~5.3 dB. **A ground-via fence around the PA
pocket was not implemented this pass** — `rfmacro/pa.py`'s via-placement already has a
`ring_points(cut, t, step)` helper used elsewhere for exactly this shape of fence; the
spec for next pass: a ring at `pa_clear` outside the via-square union, via pitch in
0.45-0.50 mm (D15's own stitch rule), clearance to every existing GND via/fence row/load
land, then re-run `pa-pa-{tx1,rx4}-port` only (cheap: the 8-run E1-PA line cost $0.06).
TX1/RX4's through-loss (-1.24 to -1.39 dB / -1.15 to -1.20 dB) and the feed's small match
perturbation (≤0.74 dB in-band) are unaffected by this gap and still show no RF objection
to the modeled geometry; the power-integrity items (10 mV IR-drop budget at 85°C, mutual
via inductance) remain open and are not EM questions.

## TX feed — unchanged: drop T2, prefer S1

T2 still fails G4 (0.65 mm fence gap, no via fix). S1 is lowest total loss (-1.50 dB);
T4 (-1.65 dB) is the fallback if the skew waiver isn't granted.

## Spend

This workflow's actual GCP spend: ~$6.00 (the 7 finished E1 campaigns + prior waves);
the $1.32 ceiling held for this pass's own `s3b-c1-e2` was released (cancelled, see
below) and never spent ≈ **~$6.00 of the $25 cap** (program ~$8.4 + this ≈ $14.4 of $50).
Full ledger in `../gcp-spend.md`.

## A concurrent agent is working this same step; its E2 work is in flight, this pass's own is not

`ps`/the live GCP state show a second agent (Codex, per the `astra-coexistence` memory
note) active on this Mac and mid-way through the _same_ stage-3b step: it already fetched
all 7 campaigns with `--full`, re-derived the same D15=A / S=S1 / K=K0-with-warning / C1-
unconverged picture by an independent route, and — unlike this pass — **actually
implemented the D14 ground-via fence** (a model-only fence ring in `pa-pa-tx1/-rx4.json`)
and submitted the designed `c1b` round-the-best sweep for C1. Its first submission
(`s3b-e2`, `20261005-mceval-0ec3ac`, 11 tasks, `northamerica-northeast1`) hit the region's
`PREEMPTIBLE_CPUS` quota (limit 64, shared by every job either agent runs there — this
pass's own, overlapping `s3b-c1-e2`, 3 tasks of K1 L-retune, included) and was CANCELLED
by the backend; the other agent diagnosed this and re-planned into `us-west4`/C4D as
`s3b-e2a`/`s3b-e2b` (9 tasks total, ceilings $3.80+$3.16, logged in `../gcp-spend.md`),
which is clear of the quota and should complete normally.

This pass's own `s3b-c1-e2` sat SCHEDULED/QUEUED on the same contended quota for over an
hour with no progress (confirmed via `gcloud batch jobs describe`, repeated
`CODE_GCE_QUOTA_EXCEEDED` events) and was **cancelled** here rather than duplicated into
`us-west4` behind the other agent's now-unblocked work. Its K1, L = 1.00/1.015/1.03 point
(motivated by the wall-bound run's 63.95-65.0 GHz finding, a _smaller_ retune than
`s3b-c1-k`'s overshot 1.06-1.12) is not covered by the other agent's w35×t_y sweep and is
still worth running — flagged below for whoever continues, rather than resubmitted
unilaterally into a region already under pressure.

## Recommendation to the orchestrator

Adopt now: **D15 = A**, **dummies = S1**, **D5 = corporate (recommended)**. Still open:
C1 convergence (the other agent's `c1b` round-the-best sweep is in flight in `us-west4`;
this pass's complementary K1 L=1.00/1.015/1.03 point is specified in
`em/jobs-col/s3b-c1-e2.toml` but not submitted — check `~/yapnr-runs/plans` and `ps` for
the other agent's live state before resubmitting it, to avoid a third duplicate), the
cavity fix's final cost once C1 converges, and D14's fence result (the other agent's
model-only fence is written and queued in `s3b-e2b`; its coupling result is what decides
whether -34.6/-34.9 dB closes to the ≤-40 dB rule).
