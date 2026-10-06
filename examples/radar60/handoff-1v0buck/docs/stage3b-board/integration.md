<!-- markdownlint-disable -->

# radar60 stage 3b: integration of the router and power tracks, and the radar routing trial

2026-10-04. Engine branch `claude/radar-routing-engine-2` (worktree `yapnr-wt/s3b-int2`), off
`origin/main` 21042fb, pushed, **PR #49** (not merged). Radar branch `claude/radar60-3b`
(worktree `yapnr-wt/radar60-3b`, local only). [D] = derived here, [E] = estimate.

**2026-10-04, review-fix pass.** Two reviews of this document and its evidence found 5 engine
gaps (class-clearance necking not hard-limited, far-side lands half-handled, pair clearances
above 1 mm not enforced between routed nets, the IR report's `sinks: all`/raster/convergence
issues, and the KiCad-only tests having no CI guard) and 9 radar-fitness gaps, several of them
in this document's own reporting rather than the board. All 5 engine findings are fixed, tested
and pushed as `858062b`/`0c2ef1c` on top of `36b9d0f` (§2.2 below, superseding its "six defects"
list — those were from the radar trial, these are from the reviews). The reporting corrections
and verified-but-unfixed board gaps are inline in §4/§5 below, marked **correction**/**bold**.
Two reviewer claims were checked and are **not** carried in: the courtyard/off-site-via finding
(§3) was already noted as a false positive in the original regression table, and "necked"
detection itself (new in the fix) has not yet been re-run against this trial's board — the
numbers in §4 below are still from the pre-fix engine and are likely optimistic for 1V0_RF1 and
the SW-node class-clearance DRC passes; re-running needs a re-route, deferred to stage 3c with
the floorplan/macro-v2 changes rather than spent twice.

The fix also surfaced a real, narrower gap of its own: re-running the three affected hard rungs
(`-classes`, `-partial`, `-rails`, both seeds, on the Mac — `integration/review-fix-rungs/`)
gives 5/6 pass; `-rails` seed 1 now fails on `fanout-escape` (pad B12, net PD0, a plain GPIO not
on any plane rail). Making `min_width_mm` a hard keepout (the fix for finding 1) makes the VDD
plane trunk's exclusion zone real where it was previously only a cost term, and in this seed's
placement that zone is now wide enough to cost B12 its escape via — the fanout planner does not
yet know to route around a plane's hard keepout. Seed 0 and every other rung/seed still pass;
this rung is informational in the ladder (not a merge gate), so it does not block PR #49, but
fanout/plane-partition coordination is a real follow-up, not yet filed as its own issue.

**PR #49 at `0c2ef1c`: CI green, all 17 checks pass** (`lint`, `test` 26m23s, `test-macos` 27m0s,
`ladder`/`run the ladder` incl. the new KiCad-Python step, `docs`, `image`, `plan`, `deploy-preview`;
`build`/`wheel`/`publish` skip on a non-release push). Not merged; the radar branch stays local.

## 1. Verdict

- **Engine.** Both tracks merged (conflicts were appends only). The radar trial found six engine
  defects in the merged features; all are fixed with tests (§2.2). New and changed Bazel targets
  pass locally; the whole `//hardware/pnr/...` suite ran (136/138, the two failures were lint
  tests on track code, fixed and re-run). prek is clean over the branch.
- **Identity regression (GCP, C4D us-west4, one image, `--compact --gloss`).** 78 shared cells:
  placed, routes and rules byte-identical in **78/78**; routed boards identical modulo segment
  direction in **77/78**; the same verdict in **78/78**. The one board difference is the gloss
  stage's own (§3). New rungs: `-classes` 2/2 and `-partial` 2/2 pass; `-rails` failed 2/2 on
  GCP because the image's KiCad Python has no numpy (fixed, §2.2); at the final head all three
  pass on both seeds (Mac). The full re-run at the final head found no Spot capacity and was
  cancelled; the later commits act only where `plane_partition`/`ir_drop` are declared or a
  far-side land exists (none in the ladder). **CI is green at 36b9d0f** (lint, test 25 min,
  test-macos, docs, ladder).
- **Radar trial.** Rev A (`p030`) routes with **0 clearance, short, hole-to-edge, mask-bridge
  and not-allowed findings**, the RF macro digest R1 v2 unchanged and no foreign copper in the RF
  region, but **142 unconnected items** (trial 2: 149), 46 unfinished nets, LVDS not coupled and
  most supplies open or over budget. Pass marks of design §2.8 are not met; blockers ranked in §5.

## 2. Engine

### 2.1 Branch

| Commit  | What                                                                                                                                                                                                                                        |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1e32cee | merge `claude/s3b-clear` 0d525ff (A1-A4: class clearance in the maze, pad-local clearance, `dru_routing`, `edge: exact`; rung `-classes`)                                                                                                   |
| 269e4a9 | merge `claude/s3b-power` 01d2392 (A5-A7: partial fanout, `plane_partition`, `ir_drop`; rungs `-partial`, `-rails`). Conflicts in constraints.py, check_constraints.py, hard_rungs.py, test_hard_rungs.py: both sides kept                   |
| 01e5819 | run.py: a spec's `designed_open` pads are not counted as KiCad unconnected items; the contract requires an `unconnected` check holding exactly those pads (`-partial` failed run.py on its designed open)                                   |
| f1dcf57 | ir_drop: no self-loop edges after the via-land merge (they inflated the Jacobi diagonal by up to 6e4 S; radar 3V3 CG diverged to 9e129 mOhm, now 512 iterations); an unconverged solve is `unsolved`                                        |
| 75a439e | plane_partition: foreign copper at the pair's class clearance; a tree passes only where a zone of the fab track width fills (radar 1V0_RF1 reached U1 through a 0.1 mm core KiCad filled as an island, while the report said 10/10 reached) |
| 4372650 | `Pad.far_side`: an SMD land on the opposite outer layer (TCAN1044's B.Cu thermal land) bars that layer; the maze had run 3V3 through it (a KiCad short). No ladder footprint has one [V: 25 footprints]                                     |
| d069ad3 | ir_extract: without numpy (the container's KiCad Python) the solve runs in `PNR_PYTHON` (set by run.py and staged_signal)                                                                                                                   |
| 6a39944 | writeback: KiCad 10's LSET has no `count()`; the staged append failed on a board already holding partition zones                                                                                                                            |
| fbb5fe8 | bounded subprocess (pnr.proc) and `board.Delete` for replaced zones (proc_test, board_delete_test)                                                                                                                                          |
| 36b9d0f | WORKLOG                                                                                                                                                                                                                                     |

### 2.2 Tests

Bazel `--local_cpu_resources=2 --config=lowmem` (output base on a case-sensitive sparse image on
the external disk: the internal disk ran out at 0.45 GB free once): the 7 new targets and every
target of a changed module pass; `//hardware/pnr/...` 136/138 then the 2 fixed. KiCad lane by
hand under KiCad 10.0.6: `pad_clearance_kicad` (incl. far-side land), `ir_extract_kicad`,
`plane_partition_kicad` (now draws the regions twice). Radar `//tests/unit/radar60/...` 2/2.

## 3. Regression (`integration/gcp`, `integration/regression.md`)

| Campaign     | Head                | Cells | Result                                                                                                                                                                                                                                |
| ------------ | ------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ctl 9cf317   | origin/main 21042fb | 78    | 72 pass; 6 fail (09-mcu-usb-31 4L-SGPS s0/s1, 6L s0, abs s0, rel s0, header s0)                                                                                                                                                       |
| cand 2f1983  | 01e5819             | 84    | 78 shared: identity above, same 6 failures; new rungs as §1                                                                                                                                                                           |
| cand2 295755 | 6a39944             | 84    | cancelled while queued ($0)                                                                                                                                                                                                           |
| cand3 840284 | fbb5fe8             | 84    | cancelled after 75 min without Spot capacity in us-west4 ($0)                                                                                                                                                                         |
| new 33ab2e   | 36b9d0f             | 6     | cancelled while queued in Montreal ($0); the same 6 cells ran on the Mac (`integration/mac-new`): **6/6 pass** (`-classes` 15/15, DRC 0; `-partial` 14/14, only the designed open; `-rails` 18/18, IR 0.40-0.52 mV VDD against 33 mV) |

- **The one board difference** (`11-ufbga201-...-block` s1): placed, routes, rules and the
  pre-gloss board are identical; gloss trial 003 is the same copper in both arms, but the gloss
  writes some segments in the other direction and KiCad's DRC flags a 0.035 mm PH15 segment as
  dangling in one direction only, so the candidate rejected a dekink transaction the control
  accepted. Nothing the gloss imports changes behaviour on this rung; the gloss's direction
  choice varies between processes (stage 3a saw the same direction differences). Follow-up:
  deterministic segment direction in the gloss.
- `-rails` s0 also had one `via_class` finding on GCP: C7's VDD plane drop (0.4/0.2) lands inside
  U1's courtyard, which the check (courtyard, not the ball array) counts as an off-site fanout via.

## 4. Radar routing trial (`integration/radar/`, `route3b.py`, `run.sh`, `place3b.sh`)

Engine: scratch `yapnr-wt/s3b-int2-radar` (radar60-3b + the engine). Switches `class_clearance:
maze`, `dru_routing`, `edge: exact`; fanout `partial: {bridge: true}`; In3 typed power,
partitioned for 1V0_RF1/1V0_RF2 (fill GND); `ir_drop` per rail (design §2.7). The radar branch
now carries these inputs (88988e9: `floorplan.yaml` board.routing and power, `gen_board.py`).

|                                        | A0: p030, engine at 01e5819        | **A: p030, final engine** | P: re-placed with the inputs (p005) |
| -------------------------------------- | ---------------------------------- | ------------------------- | ----------------------------------- |
| clearance / short                      | 2 / 1 (3V3 through U4's B.Cu land) | **0 / 0**                 | 0 / 0                               |
| LVDS skew / uncoupled                  | 2 / 2                              | 2 / 2                     | 1 / 1                               |
| QSPI length > 25 mm                    | 0                                  | 0                         | 2                                   |
| hole-to-edge, mask bridge, not allowed | 0                                  | 0                         | 0                                   |
| unconnected items                      | 144                                | **142**                   | 170                                 |
| macro R1 v2 / RF-region foreign copper | equal / none                       | equal / none              | equal / none                        |

(B, a 6-rail partition on the pre-fix engine: 160 unconnected, 3 not-allowed; dropped.) P: 19/32
legal, 18 audit-pass; it routes worse, so `reva/` stays `p030` (README note, 50e1972).

**Arm A details.** Fanout: 37/40 signal balls escaped (5 retried by the board's escapes), P14
bridged to P15, A3 to B3; drops 84/92; open: GND A1/B1/C1/E1 and 1V0_RF2 C2/D2 (conflict with
the PA-corner bottom parts), GND A5/A7/G1/J1/L1, P4, P9, B10. Unconnected ends by net: 5V_SYS 42,
1V8 36, GND 30, 3V3_RADIO_IO 16, VIN_5V 14, 1V0_PA 12 (macro v2), 1V0_RF2 8, EFUSE/PMIC 32.
LVDS: TX0 and TX1 both legs, CLK_P and FRCLK_P unrouted; CLK_N 17.8 mm, FRCLK_N 9.9 mm, both on
F.Cu **and B.Cu** (design §2.6 asks F.Cu only; the DRU rule and the `lvds` via class both allow
B.Cu, so this is a gap, not a routing choice — flagged below).
QSPI: **5/7 routed, not 7/7** (correction: a net with any routed segment was being counted as
routed). QSPI_CS_N and QSPI_D3 are open at U1.P11/P12 (`route/candidate.drc.json`); the "2.1 mm"
for QSPI_CS_N in the first cut of this report was a stub, not the routed net. The 5 routed nets
are 2.1-15.9 mm (limit 25).
**Crystal: open on both legs** (XTAL_P at U1.B15, XTAL_N between C71 and Y1) — missed by the
first cut of this report; §4's net-level view above was wrong for the same reason as QSPI.
**U1 decoupling/LDO caps mostly unconnected:** of 6 bottom-site caps, only C61 is complete; C38
(APLL) and C63 (VBGAP) float entirely; C62 (SYNTH, GND open), C57 (1V0_RF1 open), C64 (1V8 open).
GND is also open on C46/C58/C59/C60/C67, 1V0_RF2 on C24. B.Cu carries no GND pour (design §2.6
plans one on L6) — only rule areas — so none of these have a plane drop to fall back on.
**1.0 V copper is undersized, not just over its IR budget:** 1V0_BUCK/SH route at the PWR class's
0.25 mm; `ir.json` implies about 10 A/mm on F.Cu/B.Cu against IPC's 2.35 A/mm (about 11x on
1V0_RF1), so the mOhm numbers below understate the problem — these tracks need widening or a
copper pour, not just a wider budget.

IR (60 C, copper only):

| Rail              | Budget mOhm   | Result                                                         |
| ----------------- | ------------- | -------------------------------------------------------------- |
| 1V0_RF1           | 4             | **21.5 (fail)**: In3 reaches G5/H5/J5 through one narrow trunk |
| 1V0_RF2           | 4             | open (C2, D2, R46.1)                                           |
| 1V0_BUCK / 1V0_SH | 0.5 / 0.5 [D] | 18.0 / 33.9 (fail)                                             |
| 1V2               | 12            | open at N11                                                    |
| 1V8               | 21            | open at all 6 balls                                            |
| 3V3_RADIO_IO      | 165           | open                                                           |
| 3V3               | 165           | **144.6 (pass)**                                               |
| 5V_SYS            | 33            | open                                                           |

## 5. Blockers, ranked

1. **U2 (LP87524, VQFN-HR 0.5 mm, 0.25 x 1.82 mm pads) and U5 (eFuse, VQFN-HR 0.45 mm) both get
   no escape option** (U2 also in stage-3a trial 2, so not the class clearances; the 0.6 mm neck
   authorization did not help U2 or cover U5). Together they strand 5V_SYS, VIN_5V, the four
   switch nodes, 1V0_BUCK/SH, EFUSE and the whole J1 power input (correction: the recommended
   fallback block below must include U5, D3, C33, C34 — U2 alone is not enough). Recommendation:
   the design §2.5 fallback, a fixed power-stage copper block covering both ICs.
2. **Supply delivery:** 1V8, 1V2 and 3V3_RADIO_IO as 0.25 mm drop-net traces do not connect;
   1V0_RF1 is 21.5 mOhm against 4, and that number itself understates the risk — see §4's IR note.
   Design §2.6 (c) (a 1.0 V rail misses 4 mOhm) holds on `p030`.
3. **LVDS:** legs routed singly (A8, coupled pairs from fanout exits, was not built by either
   track); skews of 10-18 mm, the TX pairs unrouted, and the routed CLK_N/FRCLK_N legs are partly
   on B.Cu against design §2.6 (F.Cu only) — restrict the DRU rule and the `lvds` via class to
   F.Cu once A8 lands.
4. **PA corner** (C2/D2, A1/B1/C1/E1 against C54/R46/C60): macro v2 (stage 3c). The swap is not
   yet mechanical: `merge_macro` refuses rf3b's `RFM1_PA_FEED` footprint, the macro's In3
   `l4_pa_tie` has no 1V0_PA partition rail to land on, `integrate.py` has no route step (arm A's
   route came from a scratch script, not the branch), and rf3b's variant names (s3b-A/B/C/S1)
   don't match what `macro_check` expects. These need fixing before the stage-3c swap, not after.
5. **GND:** 30 open ends. **Correction:** not RF-edge balls and J2's lands — J2 has no open GND
   pad in this trial. The open-GND refs are J1.3/4 (the power-input return), U2, U5, the flash
   pad U3.9, J3, and the bottom-site caps in §4 above (C38/46/58/59/60/62/63/67). B.Cu has no GND
   pour to fall back on (§4). Also unresolved: In2/B.Cu signals (I2C_SDA, FRAME_START, CAN_RX/TX)
   route directly under the XTAL copper, with no keepout there beyond the XTAL nets' own vias.

## 6. Spend and files

- GCP stage 3b: ~$0.6 [E] so far (compute ~$0.2 from task times, egress ~$0.37 for the two
  `--full` fetches) plus $0 for three cancelled campaigns; cap $6; nothing left running or queued. Logged in `radar60/gcp-spend.md`.
- Renders: `stage3b-board/renders/radar60-reva-3b-routed-{A,P}-{top,bottom,angled}.png`,
  IR heat maps `radar60-reva-3b-A-ir-*.png`.
- Regression: `integration/regression.md|json`, `integration/gcp/` (make_stage, collect,
  unpack, canon, configs, plans), `integration/runs/{ctl,cand}`.
- Radar: `integration/radar/{A,P}` (constraints, rules, route/, measure.json, diag.json),
  `first/` (pre-fix arms), `work/` and `board/` (the P re-place), `reva-rebuilt/`.
