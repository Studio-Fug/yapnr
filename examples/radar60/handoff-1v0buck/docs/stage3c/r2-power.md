<!-- markdownlint-disable -->

# Stage 3c power track: R1 subcells, E1/E2 on the radar, R2 anchors, R3 In3 (wave 4)

2026-10-05.

- **Spend:** $0 GCP. Mac only, through `heavy.sh`.
- **Branch:** `claude/radar60-3c-power`, pushed. It is based on `claude/radar60-3c` after `origin/main` 44e3aab was merged in (a7344d3).
- **Engine PRs (open, not merged):**

  - #56 `claude/s3c-pour-pieces`
  - #57 `claude/s3c-assemble-zone-connect`

  Both are also merged into the radar branch.

- **Run directory:** `stage3c/wave4/`, with `run.sh` and the logs. Renders are archived in `progress-gallery/2026-10-05/radar60/wave4-*`.

## Why the work is not on `claude/radar60-3c`

Another agent edits `yapnr-wt/radar60-3c` at the same time. Its uncommitted changes are R7/R9: `audit.py`, `gen_board.py`, `integrate.py`, `floorplan.yaml` (an `r46` group, the `pa_r` region removed), and `constraints.yaml`. On `claude/radar60-3c` I committed only my own hunks, as dda3d13, 8f373ea and 6a1ef64. Everything after that is on `claude/radar60-3c-power`, built and run from `yapnr-wt/radar60-3c-power`. Merging it into `claude/radar60-3c` waits until that tree is clean, because our edits overlap in `floorplan.yaml`, `integrate.py` and `constraints.yaml`.

## What landed

### R1: the power-stage subcell, extracted by `pnr.hier.blocks`

The block comes from hard groups. `power_block.py` handles it:

- **`synth`:** placement trials, then the production router (`staged_signal`) on the block's own KiCad board. That board is refilled, DRC'd, IR-extracted and checked for hot loops.
- **`rank`:** open hot-loop links (each part pad, and **each IC land**, on a hot net), then unconnected, then IR, then area.
- **`merge`, `rescore`.**

**Measured decision: two blocks, not one.**

- One block of U2+U5 (37 parts, 450–715 mm²) left 24/24 hierarchical top starts with no slot.
- The 6 best U2 layouts alone (15–19 mm) also failed 24/24 as free macros. `pmic_switching` pinned them onto MH2's keepout (window analysis).

So the subcell is split along its own clusters, and each block is synthesized **in situ** at a floorplan `site` [D]:

| Block          | Members                                | Site (x, y)            |
| -------------- | -------------------------------------- | ---------------------- |
| U2 buck stage  | L1–L4, CIN, snubbers, VANA, R_SH1, TP1 | x 40–58, y 6.75–21.7   |
| U5 eFuse stage | D3, C33, C34, C36, C37                 | x 6.8–22.5, y 0.4–6.95 |

The eFuse thresholds became a soft group. A hard group cannot straddle a fixed block, and these parts do not fit in the site.

### Outer pours (E1)

- F.Cu, region, `terminals: pad`, `connect: solid`, `h_mm` 0.05.
- New engine key `pieces: [GND, 5V_SYS]` (#56). On each row (5V, SW, PGND, SW, 5V) a single 5V_SYS tree walled in the SW/PGND lands in 28–30 of 30 layouts. With pieces, the best block went from 19 to 14 open links out of 43.
- The eFuse block does not pour GND. U5 pin 8 had no room for a stitch via.

### Fixed blocks (E2)

`integrate.py fixblocks` poses the best layout on the source board and runs `pnr.hier.assemble --zones --group --anchor`. `fixed_block_from_macro` then gives `fixed` and `fixed_block` entries with the digest. `prepare` merges them in, holds the members out, prunes their selectors, and keeps the site as a placement keepout.

Two fixes were needed:

- `kicad_ops finish` kept only `RFM1_MACRO` copper; it now keeps the power groups too.
- `assemble` cloned the zones as thermal instead of solid (#57). That caused 15 `starved_thermal` errors and left the U2/U5 lands open.

### R2 anchors

`fb_rf1` (≤ 12 mm from H5) and `fb_rf2` (≤ 10 mm from D2) were already present. The `r_sh` radius was 2.0 mm, which is infeasible: R_SH1's centre can be no closer than 3.32 mm. It is now 4.0 [D].

### R3: In3

- Partition order (listed): 1V0_RF1, 1V0_RF2, 1V0_PA, 1V2, 1V8, 5V_SYS, 1V0_SH.
- `fixed_lands: true`.
- 1V0_SH's 0.5 mΩ budget does not size its trunk (Q2).

## Before (wave 3) and after (wave 4)

Wave 4 uses a new placement (16/24 legal, 16/16 pass the audit, winner p007) and the same route settings. It is not a same-placement control.

| Net           | Wave 3: items / pads | Wave 4: items / pads |
| ------------- | -------------------- | -------------------- |
| PMIC_SW_B0–B3 | 2/2/2/2              | **0/0/0/0**          |
| 5V_SYS        | 21 / 22              | **11 / 4**           |
| VIN_5V        | 7 / 8                | **4 / 4**            |
| 1V8           | 18 / 19              | **8 / 3**            |
| GND           | 19 / 20              | 25 / 19              |
| EFUSE\_\*     | 7                    | 10                   |
| 1V0_RF2       | 5                    | 11                   |
| 1V0_SH        | 0                    | 4                    |
| 1V2           | 0                    | 2                    |
| 1V2_BUCK      | 0                    | 2                    |
| 3V3           | 0                    | 2                    |
| 3V3_RADIO_IO  | 11                   | 13                   |
| 1V0_PA        | 6                    | 6                    |
| 1V0_RF1       | 0                    | 1                    |

**U2 pads open:** 18 → 11. Every hot-rod land and the EP are now joined. The remaining ones are signal pins 1–3, 5–8, 15, 16, 19 and 20.

**U5 pads open:** the VIN and OUT lands are joined. Pins 2–4 and 8–10 are still open.

**Board totals:**

- Unconnected: 142 → 152.
- DRC (as configured): 0 → 9. All 9 are off the power blocks: 1V8/1V0_SH vias dangling, In3 islands, QSPI_D1 length.
- R1 macro digest: equal.
- R4: 0 items.
- QSPI: 4/7 → 5/7.
- LVDS: 0/8.

| IR (mΩ)                            | Wave 3 | Wave 4         | Budget |
| ---------------------------------- | ------ | -------------- | ------ |
| 1V0_BUCK                           | 27.6   | **0.383 pass** | 0.5    |
| 1V0_RF1                            | 12.4   | 7.9            | 4      |
| 1V0_SH                             | 40.0   | open           | 0.5    |
| 1V2                                | 192    | open           | 12     |
| 3V3                                | 178    | open           | 165    |
| 1V0_RF2, 1V8, 3V3_RADIO_IO, 5V_SYS | open   | open           |        |

## Open items (next pass)

- **36 block-port opens.** These are U2/U5 signal pins and the thresholds, routed to fixed-block pads inside the F.Cu pours.
- **U5.8 GND.**
- **In3 partition:** 1V0_SH, 1V0_RF2 and 1V8 islands and opens, and the dangling vias. R3 as configured made these worse, so it needs its order, widths or terminals re-derived.
- **Via-in-pad.** The DRU's `via_to_smd_pad` rule (any net) bars stitching the EP.
- **Q1, R46 to WFCP0612.** Not applied here; the R7 track has it in progress in the shared tree.
- **Cleanup.** Intermediate `wave4/block-v1`, `block-joint` and `old/` (~200 MB) are left for the owner to delete.
