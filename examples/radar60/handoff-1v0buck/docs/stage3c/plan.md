<!-- markdownlint-disable -->

# radar60 stage 3c: plan to a fully routed Rev A

2026-10-05. Owner: "What about the PnR on the radar? We need to get to 100% on that board."
[D] = derived here, [E] = estimate, [V] = checked in code or files while writing this plan.

## 0. Starting point (verified)

- **yapnr main** `ad2bda7` contains PR #49 (squash `163cdaf`, merged 2026-10-04, head `0c2ef1c`
  including the review fixes). **A8 (coupled pairs from fanout exits in `route_board`) did not
  land** [V]: `solve_pair` is called only from `native_electrical`/`paired_bootstrap` (the
  native loop); `route_board` still routes each LVDS leg alone and then tunes length.
- **Radar branch** `claude/radar60-3b` `c6a737b` carries the engine only up to `fbb5fe8`. It lacks
  `858062b`/`0c2ef1c`: necking is now a hard limit, far-side lands, and pair clearances above
  1 mm. So the 3b numbers (142 open, RF1 21.5 mΩ) are **optimistic**.
- **Arm A (p030, v1 macro):** 142 unconnected items and 46 unfinished nets. Root causes from
  `diag.json`:
  - U2 has 13/27 pads with 0 escape candidates; U5 has 6/10; U3.9 (flash EP) has 0.
  - LVDS: TX0 and TX1 unrouted; CLK and FRCLK have one leg each, partly on B.Cu.
  - QSPI_CS_N and QSPI_D3 are open (U1 P11/P12).
  - XTAL is open on both legs.
  - 1V8, 1V2, 3V3_RADIO_IO and 5V_SYS are open.
  - IR fails: 1V0_RF1 21.5 mΩ, 1V0_BUCK 18 mΩ, 1V0_SH 33.9 mΩ.
  - 30 GND ends are open.
- **The scratch route step:** `stage3b-board/integration/radar/route3b.py`. It is not on any
  branch. `integrate.py` has the steps source/prepare/place/select/finish/render and no route
  step [V].
- **Macro v2:** `stage3b-rf/freeze.md` says "BLOCKED, not frozen". D15 = A, S1 and K0 are final.
  C1 and the D14 fence wait on `s3b-e2a`/`s3b-e2b`.
  - The v2 boards are `stage3b-rf/gen/s3b-*`. Each carries `radar60:RFM1_PA_FEED` (1V0_PA pads,
    and F.Cu–B.Cu vias in the pocket) [V].
  - `merge_macro` raises on that footprint [V].
  - `macro_board()` and `macro_check` hard-code `rfm1-{m,n,p}` [V].

## 1. Blockers: root cause → fix

Tag: **E** = engine (general, opt-in, byte-identical when undeclared, in the s3c PR); **R** =
radar input (`claude/radar60-3c`).

1. **U2 (LP87524 VQFN-HR) and U5 (eFuse) stranded.**
   - **Root cause:** the hot-rod lands are 0.25 × 1.82 mm at 0.5 mm pitch, and a PWR/SW track at
     its class clearance cannot enter between them.
   - Neck authorization was already tried in 3b and fails. The escape model is wrong for these
     parts: the parts are meant to be connected by copper that overlaps the land along its length.
   - **Fix:**
     - **E1 `plane_partition` on an outer layer inside a region.** Optional new keys: `region`
       (polygon or block outline), `terminals: pad` (the whole land on that layer, not a drop
       disc) and `connect: solid`. The existing raster/KMB/territory code is reused, so the
       territories become solid-connect pours that own the lands.
     - **R1 power-stage subcell template.** U2, L1–L4, CIN, snubbers, U5, D3, C33, C34, R_SH and
       the kelvin TP. It goes through `pnr.hier.synth`: MC trials on its own board, ranked by
       `hot_loops_open`, then IR, then area. The pours are 5V_SYS, SW_B0..B3, 1V0_BUCK, 1V0_SH
       and GND, all on F.Cu.
     - **E2 hier → fixed_block bridge.**
       - The chosen layout is collapsed to a rigid macro for the top placement (`hier.macro`).
       - After placement its copper, **including zones** (`hier.assemble` drops zones today:
         opt-in `--zones`), is expanded as a `fixed_block` anchored on U2. That fixed_block
         carries a digest.
   - **Pass:** U2/U5 opens 0; DRC 0; hot-loop audit pass; 1V0_BUCK and 1V0_SH within budget, or
     the warning of Q2.
2. **The buck/ferrite chain (1V0_SH → FB3/FB4/FB5).**

   - **Root cause:** p030 predates the anchors. In the 3b probe, 1V0_RF1 runs 85 mm from FB3
     (`power.md`), so the budget widths (17 mm) hit the 10 mm cap.
   - **Fix:**
     - **R2:** anchored groups at re-place. `fb_rf1` → U1:H5 ≤ 12 mm; `fb_rf2_a/b` → U1:D2
       ≤ 10 mm; R_SH ≤ 2 mm from L_b2:2 inside the block.
     - 1V0_SH becomes an In3 partition rail from the block to the ferrite cluster.
   - **Feasibility of 1V0_RF1 at 4 mΩ [D]:**

     | Part of the path                                       | mΩ        |
     | ------------------------------------------------------ | --------- |
     | In3, 12 mm at 3 mm wide (4 squares × 0.57 mΩ at 60 °C) | 2.3       |
     | Vias                                                   | 0.86      |
     | Stubs                                                  | 0.33      |
     | **Total**                                              | **≈ 3.5** |

     That leaves about 0.5 mΩ of margin. If the BGA-field segment is narrower than designed,
     this fails, and it then feeds 8-layer criterion (c).

3. **In3 partition and the 1.0 V trunks.**

   - **Fix (R3):** In3 typed `power` in `gen_board.py` (it writes `mixed` today; `route3b.py`
     patched this by hand). The partition order is by current:

     | Rail             | Notes                                |
     | ---------------- | ------------------------------------ |
     | 1V0_RF1          |                                      |
     | 1V0_RF2 + 1V0_PA | ≤ 4 mΩ together                      |
     | 1V2              |                                      |
     | 1V8              |                                      |
     | 5V_SYS           |                                      |
     | 1V0_SH           |                                      |
     | 3V3_RADIO_IO     | stays a trace (0.2 A against 165 mΩ) |

   - Widths come from `max(min_width, IPC internal, budget)`.
   - **Current density [D]:** IPC-2221 internal, 1 oz, 10 °C rise:

     | Current              | Width     |
     | -------------------- | --------- |
     | 2.5 A trunk          | ≥ 2.8 mm  |
     | ≤ 0.85 A ball branch | ≥ 0.62 mm |

     A7's neck flag must report 0 on every 1.0 V rail.

   - **E3:** the partition terminals include the net's **fixed-copper through vias**, so 1V0_PA
     lands on the macro's PA-feed vias (`l4_pa_tie`). The source is R46:2.
   - **E4 fanout/partition coordination:**
     - Partition trunk cores keep out of planned fanout escape cells. This is the `-rails`
       seed-1 B12 failure.
     - The drop planner counts a bottom-site pad's fanout stub as its connection (the open
       follow-up in `power.md`).

4. **LVDS (A8 not landed).**
   - **E5 = A8 as designed** (design §1 A8). It runs after `plan_fanouts`, from the escape exit
     points, using `coupled.solve_pair` on the exit layer. Obstacles: grid pads, fixed copper,
     escapes, keepouts, A1/A3 clearances.
     - Uncoupled budget = DRU 3 mm − the escape length outside the courtyard (1.0 mm where the
       annotation is stricter).
     - The copper is committed as escape copper.
     - Then an intra-pair tune on the shorter leg (≤ 0.1 mm), then a group tune (≤ 2 mm).
     - An unsolved pair falls back and is reported.
   - **R4:** the DRU rule and the `lvds` via class become F.Cu only.
   - **Tests:** a unit test (BGA exit → connector; gap and uncoupled length checked exactly); the
     `09-mcu-usb` rung A/B with `route_pairs: coupled`.
5. **`merge_macro` and the v2 names (R5).**
   - `RFM1_PA_FEED` is accepted as a macro footprint (locked, in group RFM1_MACRO, pads on
     1V0_PA). It is included in the R1 v2 digest.
   - `--macro DIR|RECORD` replaces `rfm1-<variant>`.
   - `floorplan.yaml rf.siblings` lists the bracketing variants. A missing sibling is reported,
     not fatal, for v2.
   - `MACRO_NETS` gains 1V0_PA.
   - **Swap trigger:** the first line of `freeze.md` reads FROZEN and names the record and
     `geometry_sha256`. On that, run the 3b §2.9 steps:
     1. `--macro`;
     2. `gen_board --macro`;
     3. finish;
     4. re-derive the bottom sites from the pocket vias;
     5. re-place only if the region or pocket changed;
     6. re-route.
   - **Test:** the s3b-S1 fixture.
6. **Route step (R6).**
   - `integrate.py route` and `integrate.py check` replace `route3b.py`:
     - `route`: `staged_signal` with all switches.
     - `check`: refill, DRC on all severities, R1/R2/R4/R6, `rf_audit`, IR per rail, and LVDS/QSPI
       lengths from KiCad, written to `measure.json`.
   - `select` ranks mechanically, in this order:
     1. DRC errors;
     2. opens;
     3. IR fails;
     4. 8-layer criteria;
     5. length.
   - Wave 0 must reproduce arm A within ±5 opens on the current engine.
7. **Re-place (R7).**
   - Exit bands from the U1 fanout plan (strip 0.5 mm wide, running 2.5 mm out from the
     courtyard).
   - South-edge order and the Y1 region at x 28.9–32.9.
   - J2 and J3 regions; the power block as a macro; the anchors of item 2; U3 within 10 mm of
     U1:P11 (QSPI).
   - Bottom sites (C38, C57, C61–C64, the LDO caps).
   - Placement uses the main legalizer options and 32 MC starts. The audit must show the bands
     empty.
8. **B.Cu GND pour (R8).**
   - A GND zone on B.Cu, under the rule areas, with islands removed. It is the fallback for the
     bottom-site caps and J1/J3.
   - **Check:** whether `staged_signal` treats pads joined by an outer pour as connected. If not,
     **E6** adds `pour: [{layer, net}]`: the pour counts as the connection, judged after the
     refill, and a stitch via is added where an island remains.
9. **Crystal and QSPI (R9).**
   - **XTAL:**
     - Cause: B15/C15 are on the edge column, and the old `xtal_cap_n` slot sat in their exit
       band.
     - The re-place fixes this, with the caps re-derived at x ≥ 28.9.
     - Add a keepout on In2/B.Cu under Y1 and the XTAL nets (I2C/CAN/FRAME_START run under it
       today).
   - **QSPI:** route it first in the maze, with U3 anchored. Each of the 7 nets ≤ 25 mm.
   - **Flash EP (U3.9):**
     - Narrow the F.Cu keepout to vias and foreign tracks.
     - GND reaches the EP by a solid F.Cu pour at its edge. No via under the EP.

## 2. Engine work (one PR, never merged)

The tracks branch from `origin/main` (`yapnr-wt/s3c-<track>`):

| Track       | Features       |
| ----------- | -------------- |
| `s3c-pour`  | E1, E3, E4, E6 |
| `s3c-pairs` | E5             |
| `s3c-block` | E2             |

- The tracks are integrated on `claude/s3c-engine`, which is the one PR.
- Each feature gets unit tests and docs (`pnr-inputs.md`) and a hard rung:
  - `-pour`: a VQFN-HR plus an inductor block;
  - `-pairs`: a UFBGA pair to a header.
- Bazel runs the changed targets locally (`--local_cpu_resources=2 --config=lowmem`, with the
  output base on the external sparse image). prek must be clean.
- **Identity regression:** 84 cells, control against candidate. It runs on GCP when Spot
  capacity exists, otherwise on the Mac through heavy.sh, as in 3b.
- The radar branch `claude/radar60-3c` (off `claude/radar60-3b`) merges `origin/main` first,
  then the engine branch.

## 3. Iteration loop

- **Wave 0, Mac:**
  - Merge main into `radar60-3c` and add the R6 route step.
  - Re-route p030 with v1. This gives the true baseline under the hard necking.
- **Waves 1…n:**
  1. `finish` → `compile` → `sites` → MC re-place: 32 starts on the Mac, 15–20 min.
  2. Filter: legal, audit pass.
  3. Route the top 4. GCP c4d-highcpu-8 Spot, 1 per VM, about 0.25 VM-h each [E]. The Mac via
     heavy.sh is the fallback, at 7–10 min each.
  4. Cold checks: `integrate.py check`.
  5. IR report and heat maps; renders go to the user and `progress-gallery`.
  6. Feedback: each failure site is classified as one of:
     - an input error, which goes back to R;
     - an engine gap, which goes back to E with a test;
     - capacity, which counts toward the 8-layer criteria.
  - The next wave changes inputs only through the files above. No copper is placed by hand.
- **Pass marks (final):**
  - KiCad DRC 0 on all severities after a refill, with the DRU. Unconnected 0 on v2; on v1 only
    A2/B2 are open.
  - R1 digest equal, R2/R4/R6 pass, `rf_audit` A1–A6 pass.
  - LVDS: 4 pairs on F.Cu, gap 0.16–0.22 mm, ≤ 3 mm uncoupled, skew ≤ 0.1 mm per pair and
    ≤ 2 mm across the group.
  - QSPI: 7/7 nets, each ≤ 25 mm.
  - IR: every rail in its 3b §2.7 budget, no neck flag, CG residual ≤ 1e-10.
  - Placement legal, audit pass. Renders sent.
- **Wave gates:**
  - Opens must fall every wave.
  - Three waves without improvement in one category put that category under §4.
- **Cost [E]:**

  | Item                      | Cost          |
  | ------------------------- | ------------- |
  | Routes, per wave          | ~$0.2         |
  | Routes, 10 waves          | ~$2           |
  | Engine regression, 2 arms | ~$1.7 ceiling |
  | 8-layer trial reserve     | ~$3           |
  | **Plan**                  | **≤ $7**      |
  | Cap                       | $15           |

  - No GCP submit is ≥ $5.
  - Every submit is logged in `gcp-spend.md` before it is sent.

- **Time [E]:**
  - Engine tracks in parallel: 1–1.5 days.
  - Wave 0: 1 h. Each later wave: about 1.5 h.
  - Converged on v1 in about 2–3 days. The v2 swap adds about half a day once it is frozen.

## 4. 8-layer fallback

- **Criteria (3b §2.6), applied to the top 4 routed candidates of the final two waves:**
  - (a) an LVDS pair cannot be coupled on F.Cu;
  - (b) more than 2 signal nets are open with failure sites in the escape or south corridor, by
    capacity;
  - (c) a 1.0 V rail misses 4 mΩ at maximum territory, or keeps a neck flag;
  - (d) a QSPI net needs more than 25 mm.
- **Decision:** `integrate.py select` evaluates the criteria mechanically. If any criterion holds
  on all 4 candidates, the stage prepares the 8-layer inputs in parallel:
  - total thickness ≤ 1.2 mm (the 0.15 mm drills stay within aspect ratio 8);
  - L1–L3 unchanged, so the macro does not move;
  - a second power layer.
    It routes one 8-layer wave as evidence, then puts the switch to the owner (Q3).

## 5. Owner questions (defaults are taken now and flagged)

1. **R46 (0402 0 Ω carrying 2.5 A).** Default: the WFCP0612 1 mΩ, as R_SH, with its LCSC
   number verified.
2. **1V0_BUCK and 1V0_SH at 0.5 mΩ each [D].** Default: keep the local FB_B2 sense and the
   budgets. If only those two legs fail while the chain from buck to ball is ≤ 5 mΩ, report a
   quantified warning and propose fitting `r_fb2_remote`.
3. **8 layers.** Default: stay on 6. Switch only on §4 evidence with owner confirmation (fab
   cost).
4. **Flash EP.** Default: GND by a surface pour, with no vias under the EP.
5. **Hot-rod power stage connected by engine-generated pours (E1).** Default: yes. TI's EVM
   copper is not copied.
6. **v2 timing.** Default: converge on v1 now and swap the moment `freeze.md` says frozen.
   "100%" is claimed only on v2.

The splanc mini board is outside this workflow's scope and is tracked separately.
