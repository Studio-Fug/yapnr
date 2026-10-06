<!-- markdownlint-disable -->

# Stage 3b EM: column retune, TX feed loss, PA feed (D14)

Branch `claude/radar60-rf3b` (worktree `yapnr-wt/radar60-rf3b`), commit f4e8c5a + three
follow-on commits (de19c97, 1143900, 5f16ddc: ring-down probes, column/feed options). All
runs: GCP Batch openEMS, exact end-criteria, one shared mesh template per resolution.
Campaigns `s3b-c1-r15` (8cdf38), `s3b-c1-r20` (f9787d), `s3b-pa-r20`/`s3b-pa2` (3afe96,
5abb0f), `s3b-tx-r20` (e04fa3), `s3b-cav` (08e2c7, all 5 of 5 now fetched) — all SUCCEEDED,
fetched and parsed from `result.json` (S-parameters only, no field dumps kept on disk).
`s3b-c1-k` (D5 series-fed, K1 cavity) and `s3b-c1-wall` are still RUNNING at this writing;
their D5/cavity conclusions are deferred to the bank-isolation report, not blocking this
file. **Review fix pass (this file, 2026-10-04 PM):** F1 (false C1 band-clean claim), F2
(misreported D14 numbers) and F7 (wrong evidence labels) corrected below with re-parsed
data; see `stage3b-rf/freeze.md` for the bank/cavity (F4, F5) and cost-ledger (F6) fixes.

## C1 column retune — NOT band-clean; the 3-sample table below was wrong

**Correction (review F1, confirmed by re-parsing the full 641-point sweeps in
`em/col/out/column.json` and the raw `s.csv` fetched from `20261004-mceval-f9787d`):** the
table that stood here previously judged each candidate at only three sampled frequencies
(60.30/62.05/63.80 GHz) and called several of them a band-clean "yes". The full sweep shows
none of them are. `cell-c1w-030`'s quoted -10.7/-23.3/-15.6 dB are exactly the three sample
points of a 641-point sweep whose worst in-band value is **-5.17 dB at 60.75 GHz**, with 45%
of 60.3-63.8 GHz above -10 dB RL — the opposite of "best margin". The same sampling error
made `cell-c1a-10` look like a clean "yes" (worst in-band actually -5.23 dB, 40% above
-10 dB). `cell-c1w-030` is also the one candidate the pipeline's own post-processing never
finished for (it is absent from `column.json`'s `column` map; its sampled-point numbers were
read by hand off the raw CSV, bypassing the `worst_inband_s11_db`/`covers_band` fields the
other five candidates already have). The `l_solve` length-sensitivity fit for this mesh also
disagreed in sign between the openEMS and Palace-reference branches (+24 vs -136 GHz per
unit length), so it gave no usable tuning direction either.

Judged correctly — worst S11 anywhere in 60.3-63.8 GHz, from the full sweep, 20 µm mesh
unless noted:

| Candidate                      | Worst in-band S11 | % of band > -10 dB | Covers band?                |
| ------------------------------ | ----------------- | ------------------ | --------------------------- |
| cell-c1a-07 (today's L, 15 µm) | -3.76 dB          | 78%                | no                          |
| cell-c1a-13                    | -3.83 dB          | 60%                | no                          |
| cell-c1a-11                    | -4.80 dB          | 55%                | no                          |
| cell-c1a-09                    | -5.15 dB          | 74%                | no                          |
| cell-c1w-030                   | -5.17 dB          | 45%                | no                          |
| cell-c1a-10                    | -5.23 dB          | 40%                | no                          |
| **cell-c1w-042**               | **-5.38 dB**      | **33%**            | **no (best of this sweep)** |

**No candidate in this sweep meets the ANT-02/RF-03 10 dB RL target anywhere close to
convergence.** `cell-c1w-042` is the marginally best of the seven by this metric, but all
seven share a ripple with the bondply-cavity signature (see below): a resonance near
60.7-60.8 GHz that length/divider/inset tweaks on this mesh do not remove. **Withdrawing the
`cell-c1w-030` recommendation.**

**Update (2026-10-04 evening, all E1 campaigns fetched full — bank-a/b/c/s1, cav, c1-k,
c1-wall all SUCCEEDED): the cavity-upper-bound and K1/series arms are in now and do not
converge either.** Full 16-point table, worst S11 anywhere in 60.3-63.8 GHz:

| Candidate              | Group                                                    | Mesh        | Worst in-band S11                                               |
| ---------------------- | -------------------------------------------------------- | ----------- | --------------------------------------------------------------- |
| cell-c1a-07            | L (today's)                                              | 15/20/27 µm | -3.76 / — / -3.42 dB                                            |
| cell-c1a-10            | L                                                        | 15/20/27 µm | -3.62 / -5.23 / -3.47 dB                                        |
| cell-c1a-13            | L                                                        | 15/20/27 µm | -3.73 / -3.83 / -3.40 dB                                        |
| cell-c1a-11            | inset                                                    | 20 µm       | -4.80 dB                                                        |
| **cell-c1w-042**       | w35                                                      | 20 µm       | **-5.38 dB (best of all 16)**                                   |
| cell-c1a-07/10/13-wall | model-only L2-L3 PEC walls (upper bound of any via cage) | 20 µm       | -3.4 to -5.2 dB range (`em/col/runs/*-wall`), no better than K0 |
| cell-c1k-106/109/112   | K1 (solid L2), L 1.06/1.09/1.12                          | 20 µm       | -2.14 / -1.77 / -1.70 dB (**worse** than K0's best)             |
| cell-ser-1/2/8         | D5 series-fed, L sweep                                   | 20 µm       | -2.36 / -2.00 / -1.80 dB                                        |

**The cavity-wall upper bound confirms the cavity is not the only limiter**: even with the
L2-L3 resonance walled off (removing the bondply coupling path outright, a stronger fix than
K1's solid pour), the column's worst-case in-band S11 does not improve on the K0 baseline's
best point (-5.38 dB). This means C1's bandwidth problem is the matching network itself (the
patch/feed impedance over frequency), not primarily the cavity — the owner's "lengthen the
column" framing undersells it; width (`w35`) and the K1/series arms move the result by only
a few tenths of a dB either way, none toward -10 dB. **C1 is not converged and needs a
structurally different match (wider bandwidth taper, multi-section feed, or an accepted
reduction in RF-03/ANT-02 margin), not a longer run of the same parameterization.** The `w35
x t_y` round-the-best sweep (E2, campaign `s3b-e2`) is in flight on GCP to check whether
`cell-c1w-042`'s neighbourhood holds a usable point; see the Recommendation summary for its
status.

**D5 (corporate vs. series-fed): recommend corporate.** The series-fed arm (`cell-ser-*`,
L 1.00/1.02/1.04 at 20 µm) is worse than every corporate (L-group) candidate at the same
mesh (-1.80 to -2.36 dB vs. corporate's -3.4 to -5.38 dB) and shows no trend toward
convergence over its 3-point L sweep. No evidence favours switching away from the corporate
column; D5 is closed in favour of **corporate**, pending the owner's confirmation.

## TX feed loss — radiated fraction isolated, T2 excluded, S1/T4 best

Lossy vs. lossless `feed_sim.py` runs (20 µm, 8 z-cells) isolate radiation: with no
conductor/dielectric loss, remaining insertion loss at TX1.P1 (62 GHz) is pure radiation.

| TX option                      | Lossy IL     | Lossless IL (= radiated) | Conductor+dielectric |
| ------------------------------ | ------------ | ------------------------ | -------------------- |
| T0 (plan baseline)             | -1.78 dB     | -0.43 dB                 | -1.34 dB             |
| B (pour B bank)                | -1.78 dB     | -0.44 dB                 | -1.34 dB             |
| C (pour C bank)                | -1.79 dB     | -0.44 dB                 | -1.35 dB             |
| **S1 (open-end dummies only)** | **-1.50 dB** | **-0.39 dB**             | **-1.12 dB**         |
| T2 (fails G4 fence)            | -1.83 dB     | -0.50 dB                 | -1.34 dB             |
| T4 (needs skew waiver)         | -1.65 dB     | -0.42 dB                 | -1.23 dB             |

[S] (openEMS; corrected label, review F7) Radiated loss alone (0.39-0.50 dB) can't explain the RF-03 overage; the dominant term is
conductor/dielectric loss on the meander run (1.1-1.35 dB), consistent with the "shorter/
fewer meanders" lead already flagged. T2 is dropped regardless of its 0.05 dB loss penalty —
it fails G4 with no via fix available. Of the rest, **S1** (open-end dummies only) is
lowest-loss by ~0.28 dB total, because removing the input-side dummy shortens the shared
run-in; **T4** is second-best, viable if the 2 ps skew waiver is granted. Recommend S1 over
undecided S2 (all-four dummies) on loss grounds; S2's only edge is dummy symmetry, which the
pending isolation/pattern bank runs should confirm isn't worth 0.28 dB of TX1 budget.

## D14 PA feed — negligible RF perturbation (numbers corrected, review F2)

`pa_sim.py`, TX1/RX4 corners, PA termination short/port/open, with and without the D14
via-feed geometry (`-u15`/`-u20` = uniform mesh variants), from `column.json`'s `pa` block.
**Correction:** the "-1.07 to -1.10 dB" and "-1.10 to -1.11 dB" TX1.Pf/RX4.Pf figures quoted
here previously appear in no run. The actual through-port loss (`TX1.Pf`, port-terminated
case, across 60.3-63.8 GHz) is **-1.24 to -1.39 dB**; RX4.Pf is -1.15 to -1.20 dB. `short`
and `open` terminations are within 0.01 dB of `port`. [S] (openEMS; corrected label — this
was a solver output, not a derived figure.)

`pa_coupling_db` is `null` only for the `short`/`open` cases, by construction (there is no PA
port to couple into without the feed modeled as a port). With the PA modeled as a port, TX1's
PA-to-antenna coupling is **-34.6 to -34.9 dB at 62 GHz (15/20 µm, consistent)**, -28.3 dB
worst case over 54-70 GHz; RX4's corner is similar. Adding the D14 feed moves TX1's ball
return (`TX1.Pb`) from -14.65 dB (no feed baseline, `d_db` deltas near 0) to as much as
0.74 dB / 5.6° worse in-band, and 2.01 dB worse over the full 54-70 GHz sweep — small next to
the TX1 feed-loss problem below, but non-zero. [S] The A2/B2 feed bar and via pocket perturb
the antenna feed match at the few-tenths-of-a-dB level, not the 0.03 dB this file previously
claimed; the RF data still places no objection on the modeled geometry. **Open physics gaps
not resolved here, flagged for owner review, not closed by this fix:** the 8.4 mV IR-drop
number assumes 20°C copper (≈10.5 mV at 85°C, over the self-imposed 10 mV budget — a budget
change needs owner sign-off, not an agent call); the via-inductance figure used ignores
mutual inductance (effective L at 0.5 mm pitch is closer to 0.35-0.4 nH than the quoted
0.24 nH); and no cap-to-ball loop inductance or bottom-cap placement has been computed, so
the decoupling loop itself is unverified. The RF "no objection" conclusion stands; the power-
integrity sign-off behind it does not yet.

## Recommendation summary (updated: all E1 fetched full; E2 submitted)

- **C1**: **not converged, no pick, on any of the 16 points now on disk** (K0 baseline,
  model-only cavity-wall upper bound, K1, or series — worst in-band -1.70 to -5.38 dB).
  The cavity-wall result shows this is chiefly a matching-bandwidth problem, not the
  cavity. `cell-c1w-042` (w35=0.42, L=1.025) stays the best point; `s3b-e2`
  (20261005-mceval-0ec3ac, GCP, 9 `w35 x t_y` points round it + the D14 fence re-run) is
  submitted (ceiling $4.85) and its result will be appended here.
- **TX feed**: drop T2 (G4 fail); **S1** confirmed over S2 at both feed-loss (-1.50 vs
  -1.78/-1.79 dB) and now bank level (isolation 38.2 vs 36.7 dB, no ≥1 dB active-match or
  pattern benefit from S2) — recommend **S1**. T4 is the fallback if S1 is later vetoed.
- **D14 PA feed**: no RF objection to the modeled geometry on the antenna side (numbers
  above); the PA-to-antenna coupling itself is **-34.6/-34.9 dB (port case, 62 GHz)**,
  short of the plan's ≤-40 dB rule — a model-only ground-via fence (5-6 vias fit the
  pocket's tight clearance) is in the same `s3b-e2` submission; result pending. The 10 mV
  IR-drop budget still needs owner review (85°C derate and mutual inductance both push it
  over 10 mV) — not resolved here.
- **D5 (corporate vs. series)**: **recommend corporate.** `cell-ser-1/2/8` (series, 20 µm)
  is worse than every corporate L-group point at the same mesh (-1.80 to -2.36 dB vs.
  -3.4 to -5.38 dB) with no convergence trend; no evidence favours switching.
- **D15/cavity**: see `freeze.md` — A/B/C are a near-tie on isolation/RL/pattern (36.7/
  37.2/37.4 dB, -2.75/-2.76 dB active) but none clears the cavity-mode gate; pick **A**
  with a quantified warning (its trapped mode is the most weakly excited of the three).
  K0 stays (K1's cost is now confirmed severe: -1.70 to -2.14 dB vs. K0's -5.38 dB best,
  on this L sweep) — **K0 with a quantified warning**.
