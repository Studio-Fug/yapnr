<!-- markdownlint-disable -->

# radar60 stage 3b: radar integration prep (track RADAR)

2026-10-04. Branch `claude/radar60-3b` in `yapnr-wt/radar60-3b` (local only, not pushed), head
**4a48e6a**. It is `claude/radar60` ab57757, then `claude/radar60-parts` 6c5743a
(fast-forward), then `claude/radar60-rfuni` 49be425, then `origin/main` 21042fb (PR #44 and the
legalizer PR #48). The engine is main as is. prek is clean from origin/main to HEAD. No GCP was
used. The Mac runs went through `gapfix/heavy.sh`.

## 1. Verdict

- **Re-placed board:** Rev A is re-placed on the 60 × 47.35 mm board, with the RF-uniformity
  macro rfm1-n as a `fixed_block`.
  - 32 starts: 26 legal, all 26 pass the audit. The winner is `p030`.
  - KiCad DRC: only the 187 unconfigured footprint libraries (183 parts plus the 4 macro loads)
    and the 432 unrouted connections. There are no courtyard, keepout, rule-area, via or
    clearance findings.
- **Macro checks:**
  - The macro digest R1 v2 equals the RF track's board: 1664 copper items, 8 load pads and 12
    mask polygons.
  - The RF balls land on their ports to 0 µm.
  - **rf_audit A1–A6 all pass** on the filled board.
- **Placement checks:**
  - No part stands in an exit band.
  - All six bottom sites hold their parts.
  - Every pad-anchored group is within its radius.
  - `finish --placement reva/placement.json` rebuilds the board bit for bit (sha256 46d45ce5…).
- **Tests:**
  - `//tests/unit/radar60:test_board_integration` (new, 20 tests) and `:test_parts` pass under
    Bazel locally.
  - `dru_selftest` passes 23/23 (headless KiCad).
  - `gen_board.py --check --compile --macro` passes.
  - `check_schematic` is OK, with 43/43 annotations resolved.

## 2. What was built

| Commit  | What                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| e74039c | `kicad_ops.merge_macro` v2. The columns come from the record, with the dummy columns as RFM1 pads `rxd0/rxd5/txd0/txd4` on nets `RF_RXD0…`. RT1–RT4 are locked, assembled parts at the record's centres. The mask polygons are copied as they are. Any other footprint or a disagreeing record is refused. The R1 v2 digest is added. `source` now poses U1, merges the macro, groups it as `RFM1_MACRO` and cuts PLANE_In1 out of the RF region (668 mm²).     |
| f04af22 | `integrate.py prepare` now: extracts the fixed copper, holds out RFM1/RT1–4, plans U1's fanout (cached by digest), turns surface exits into placement keepouts (`bands.py`: 0.5 mm strips running 2.5 mm out, merged), refuses a slot that meets another net's band, and removes the bottom-site parts from their regions. `finish` adds R1 v2, the RF audit, band and site checks.                                                                             |
| fba7f18 | Floorplan: J2/J3 as regions, Y1 and the crystal caps east of the SPIA band, the QSPI resistor west of the R12 bands, pad-anchored groups (snubbers 3.5 mm from L pad 1, ferrites 12/10 mm from H5/D2, damping R 3 mm, C 2 mm), v1 copper keepouts (RF region with the macro exempt, guard, MH and radome circles, no via under the flash EP), the fixed block, the fanout entry (A2/B2 skipped, D14), `plane_fallback_drops: false`, and the legalizer options. |
| ff6337f | Terminal `@pnr-current` lines with `neck_max_length_mm 0.6` for U2 SW 10/12/23/25, U2 5V_SYS and U5:6. `neck_budget` reads only terminal records, so the existing net lines could not authorize a neck.                                                                                                                                                                                                                                                         |
| 7096a61 | Unit tests and the Bazel wiring (`examples/radar60/{board,rf}/BUILD.bazel`).                                                                                                                                                                                                                                                                                                                                                                                    |
| f1fb0d1 | `halving_seeded.py`: the placement workers reuse the prepared plan. The search took 6 min instead of about 13 [E].                                                                                                                                                                                                                                                                                                                                              |
| 7be29a4 | Fixes from the first run: the J2 window gives 0.5 mm play; a new DRU rule `pocket_fence_vias`, because rfuni's 4 GND vias of 0.15/0.32 mm in the VOUT_PA pocket broke the general and U1 via rules (6 findings); deterministic keepout UUIDs; audit exemptions.                                                                                                                                                                                                 |
| 4a48e6a | `reva/` re-placed, and the README.                                                                                                                                                                                                                                                                                                                                                                                                                              |

## 3. Numbers

| Check                              | Result                                                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Pin distances (pad to ball)        | APLL 0.93, SYNTH 0.74, VBGAP 1.57, RF1 0.09, SRAM 1.34, VCO 0.41 mm (bottom sites); CLKM/CLKP caps 2.13/2.34; QSPI R 4.57; PA corner 0.53–1.05 |
| Snubber R to its inductor's SW pad | 1.63, 3.21, 3.48, 2.24 mm (stage 2: 5–5.5)                                                                                                     |
| Ferrites                           | FB3 11.84 mm from H5; FB4/FB5 9.97 mm from D2 (at the radius limit)                                                                            |
| Distances                          | switch node to crystal 11.66 mm; to the RF region 5.65 mm                                                                                      |
| Soft misses                        | PA/RF2 bulk 8.8–11.5 mm (bottom, east of the package)                                                                                          |
| Exit bands                         | south: J14–M15 at x 22.5–26.25 and E15 at 27.7–28.2; west: P10–R14 and R3 at x 17.75–20.25. B15/C15 belong to their own load-cap slots.        |
| Fanout plan (pre-route)            | 32/37 Rev A signal balls; N4, E13, G14, N13 and N7 lost their resources. 84/92 drops; GND A3/A5/A7/G1/J1/L1/L10 and 1V2 P14 failed.            |

## 4. Deviations from the design (all derived, flagged for review)

1. **Y1 region:** x 28.4–33.0, y 15.75–19.7. The design's x 28.9–32.9 cannot hold Y1 (4.29 mm
   wide) beside its caps.
   - CLKM cap: upright over C15's band.
   - CLKP cap: flat, below the guard band.
2. **J2 window:** y 6.95–15.7 instead of 7.25–15.6. With 0.11 mm of play, J2 got no slot in 4 of
   4 starts.
3. **QSPI clock resistor:** 4.6 mm from R12, not 2.1 mm. R12's neighbours' bands leave no 0402
   room in front of it. The audit limit for it is 5.0 mm.
4. **RT1–RT4 are not in the schematic or BOM yet.** Under the parts rule a fitted part needs a
   verified LCSC page, and LCSC was not readable here (JS pages, API 403).
   - The merge already carries the fields of a schematic `rf_macro.rt[i]` onto RT(i+1) once one
     exists.
   - The loads are assembled footprints with value "50R 0201 thin film".
5. **The `pocket_fence_vias` DRU rule is new.** The RF track should confirm that the pocket's
   stitching vias are intended.

## 5. Open for routing (stage 3b engine / 3c)

- **J2 ground lands:** the centreline lands MP1–MP4 have no via cell at the proxy's pitch, which
  is the one unreachable branch in every good candidate. They need vias in or beside the lands.
- **Fanout failures:** 5 Rev A signal balls lose to higher-priority balls, and P14 (1V2) blocks
  its net. These need A5 (partial fanout).
- **PA corner:** waits for macro v2 (D14).
- **Not judged at this stage:** IR budgets and the 8-layer criteria need routing.

## 6. Files

All under `<notes>/radar60/stage3b-board/radar/`:

| What          | Where                                                                                                                       |
| ------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Renders       | `renders/radar60-reva-placement-{top,angled,bottom}.png`, `renders/radar60-reva-3b-placement-map.png` (bands, slots, sites) |
| Board         | `board/` (board, rules, `placement.json`, report)                                                                           |
| Run           | `work/` (source, inputs, fanout plan, `mc/`, selection, DRC, RF audit); `ato-rev-a/` (build d1ca441e)                       |
| Rebuild proof | `rebuild-check/`                                                                                                            |

The disk is low: external 5.3 GB free and internal 4.9 GB. `hier/PAUSED-DISK` has been set since
09:50 by others' runs; my runs use about 25 MB and 213 MB of Bazel output.
