# radar60 board track: floorplan, rules, constraints and the BGA escape probe

The board-level inputs for the radar60 Rev A board (a 60 GHz IWR6843 FMCW radar, 60 × 46 mm,
6-layer RO4835/RO4450F + FR-4 hybrid at PCBWay): the outline and fixed items, the RF region,
the KiCad custom rules, the yapnr constraint file, and a probe of whether yapnr can escape the
radio's FCBGA-161 on this stackup. Nothing here is routed yet; the board is placed and routed
by yapnr from the atopile source in `../schematic` and the RF macro in `../rf`.

Every value comes from the radar60 plan (sections 4, 5 and 7), from TI's public data sheet
SWRS219F, from PCBWay's capability pages or from the Rogers data sheets; numbers marked [D] in
`floorplan.yaml` are derived, [E] are planning allowances. No TI design file is used or
committed.

## Files

| File                        | What it is                                                                                                                   |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `floorplan.yaml`            | The single source: outline, U1 pose, J1, holes, radome lands, RF region and pocket, R4 guard, placement regions, net classes |
| `gen_board.py`              | Generates the four files below from it; `--check` fails on a stale file, `--compile` runs yapnr's constraint compiler        |
| `constraints.yaml`          | yapnr constraints (generated)                                                                                                |
| `radar60.kicad_pcb`         | Floorplan board (generated): outline with 1 mm corner radii, 4 plated M2.5 holes, 2 radome lands, GND planes, rule areas     |
| `radar60.kicad_pro`         | Design rules of the `pcbway-adv-6l-rf` profile and the net classes (generated)                                               |
| `radar60.kicad_dru`         | KiCad custom rules: the profile's, then the board's own (generated)                                                          |
| `fcbga161.py`               | IWR6843 FCBGA-161 (ABL0161) ball map and KiCad footprint, from SWRS219F only                                                 |
| `dru_selftest.py`           | Plants 20 items on the floorplan board and checks that each board rule fires, and only where it should (headless kicad-cli)  |
| `escape_probe.py`           | The BGA escape probe: builds the synthetic board as a regression-runner design and analyses a run                            |
| `escape-probe-results.json` | The probe's runs (2026-10-03) and the ball-by-ball result                                                                    |
| `integrate.py`              | Rev A integration: schematic board + RF macro + floorplan, yapnr's Monte Carlo placement, selection, DRC, renders            |
| `kicad_ops.py`              | Its KiCad-side steps (KiCad's own Python): the source board, the macro merge, zone fill, poses                               |
| `audit.py`                  | Placement audits (R2, R4, R5, noise distances, regions, groups, the macro's ports at the RF balls)                           |
| `label_png.py`              | Captions the renders (needs Pillow)                                                                                          |
| `reva/`                     | Rev A placed, not routed: `placement.json` (the selected poses), the project and rules, `placement-report.json`              |

The fab data lives in the yapnr package: profile `yapnr/fab/data/profiles/pcbway-adv-6l-rf.json`
and stackup `yapnr/fab/data/stackups/pcbway-6l-ro4835-ro4450f.json` (status draft until the
PCBWay quote and CAM reply confirm the hybrid materials).

## Floorplan (origin at the lower-left corner, +y north towards the antennas)

| Item                                        | Pose or extent                                                                                                                              |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Outline                                     | 60.0 × 46.0 mm, 1 mm corner radii                                                                                                           |
| U1 (IWR6843, FCBGA-161)                     | fixed at (26.0, 28.0), 270°: RX balls (column 2) face north, TX balls (row B) face east                                                     |
| Mounting holes                              | the schematic's 4 M2.5 hole parts (mech.mh[0..3]) fixed at (3.5, 3.5), (56.5, 3.5), (3.5, 42.5), (56.5, 42.5); Ø6.5 mm copper keepout       |
| Radome standoff lands                       | Ø3.0 mm, Ø5.0 mm keepout, at (3.5, 27.0) and (56.5, 27.0): the test radome covers y ≥ 27                                                    |
| J1 (JST GH-8, right angle)                  | fixed on the south edge, centred at x = 30, cable entry south                                                                               |
| RF region (F.Cu-In2.Cu)                     | RX fan and west ground margin x 14.94-29.56, y 33.2-46; TX launches, feeds, bank and east margin x 31.25-45.58, y 26.7-46; above the pocket |
| VOUT_PA pocket                              | x 29.56-31.25, y 33.2-35.0: the RF macro's pocket (from y 33.45), cut out of the RF region down to the package edge                         |
| R4 guard (no digital copper on F.Cu-In2.Cu) | a 5 mm band around the RF region without the package body: x 9.94-20.8 for y ≥ 28.2, x 31.2-50.58 for y 21.7-26.7, x 45.58-50.58 above      |
| J2 (QTH-030, development)                   | region x 10-42, y 4-18 (south of the LVDS balls)                                                                                            |
| J3 (JTAG, DNP)                              | region x 1-14, y 8-28.2 (the plan's y 40 breaks the radome rule for a 5 mm header; y 28.2 is the R4 guard's edge)                           |
| PMIC block / switch nodes                   | x 34-58, y 2-24 / x 41-58, y 2-21.7 (≥ 9.8 mm from the crystal zone, ≥ 5 mm from the RF region)                                             |
| Y1 (40 MHz) and load caps                   | x 24-31.2, y 16-22.6, next to CLKP/CLKM (B15/C15 at (29.9, 23.45) and (29.25, 23.45)), below U1's courtyard                                 |
| VOUT_PA / VIN_13RF2 caps                    | bottom side under the corner, an L clear of the macro's launch vias (the pocket holds no 0402 courtyard)                                    |

`gen_board.py --radome` checks the 30° radome visibility rule (a part of height h stays 1.73 h
from the patch copper) against the regions: J2 has 16.8 mm for 7.45 mm, J3 10.6 mm for 8.66 mm,
the PMIC inductors 13.1 mm for 1.73 mm; J1 is more than 25 mm away.

## Custom rules (`radar60.kicad_dru`)

Part 1 is the `pcbway-adv-6l-rf` profile's rules, copied from `pnr.fab_profile.dru_text`. yapnr
writes those alone beside a board that has no rules file, and leaves a file without its
generated header untouched, so this file carries both parts. Part 2, the board's rules (rule
areas `RF_REGION`, `RF_POCKET` and `RF_GUARD` are drawn in `radar60.kicad_pcb`; net classes
`RF`, `XTAL`, `LVDS`/`dp_lvds_*`, `QSPI`, `PWR`, `SW`, `GND` come from `constraints.yaml`):

| Rule                                     | What it enforces                                                                                                |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `rf_region_tracks`                       | no non-RF tracks in the RF region on F.Cu-In2.Cu (In3-B.Cu stay free)                                           |
| `rf_region_vias`, `rf_fence_vias`        | no vias there but GND fence vias of 0.15/0.32 mm (pitch ≥ 0.45 mm from the 11 mil hole to hole)                 |
| `rf_region_parts`                        | no top-side parts there but RFM1, U1 and parts enclosed by the pocket                                           |
| `rf_no_vias`                             | the 60 GHz nets never change layer                                                                              |
| `rf_guard_digital`                       | R4: no digital tracks (F.Cu-In2.Cu) or vias in the guard band                                                   |
| `bga_signal_vias`, `bga_gnd_vias`        | under U1: signal and power vias 0.20/0.40 (dog-bones); 0.15/0.35 only for GND (interstitial)                    |
| `general_vias`                           | elsewhere 0.20/0.40 or larger                                                                                   |
| `xtal_no_vias`, `sw_to_xtal`, `sw_to_rf` | crystal nets on F.Cu only; switch nodes ≥ 8 mm from the crystal, ≥ 5 mm from RF copper                          |
| `lvds_pairs`, `lvds_outer_layers`        | 0.17/0.18 mm pairs (99-103 Ω for Dk 3.33-3.66 [D]), 0.1 mm intra-pair skew, ≤ 3 mm uncoupled; F.Cu or B.Cu only |
| `qspi_length`                            | ≤ 25 mm at 80 MHz                                                                                               |
| `plane_in1`, `plane_in4`                 | no tracks on the GND planes                                                                                     |

`dru_selftest.py` (KiCad 10.0.6, headless) passes all 20 planted cases; the floorplan board
itself is clean (0 violations, 0 unconnected) under these rules.

## Constraints (`constraints.yaml`)

Parts are named by atopile instance path and nets by the radio's ball (`net@<path>:<ball>`), so
no generated designator appears; `floorplan.yaml` (`parts:`) lists the paths the schematic must
provide. The file compiles with yapnr's compiler on `main` and on the region branch
(`gen_board.py --compile [CHECKOUT]`): `fixed` (U1 at its pose in the RF macro's frame, J1 on
the south edge, the four mounting-hole parts, the floorplan board's radome lands), `keepout`
(the RF region less U1's courtyard, for placement) and `copper_keepout` (the RF region in U1's
frame, for routing), `region` (J2, J3, PMIC block, switch nodes, Y1, the bottom-side VOUT_PA
caps; hard; needs the region work), `side` (those two caps on the bottom), `group` (HF and bulk
decoupling, Y1, buck inductors; hard, centre to centre), `net_class`, `diff_pair` and
`length_match` (LVDS 2 mm group skew). `rf_macro`, `noise_keepout` and `height_limit` are
proposed sections: the engine warns and ignores them, and the custom rules, `integrate.py`'s
derived R4 region and the audits stand in for them. `radar60.kicad_pro` assigns the net classes
by net-name pattern, so the custom rules' `hasNetclass()` sees the real nets on any board that
uses it.

## Regenerating and checking

```sh
# From the repository root, with PyYAML.
PYTHONPATH=.:hardware/pnr python3 examples/radar60/board/gen_board.py            # write
PYTHONPATH=.:hardware/pnr python3 examples/radar60/board/gen_board.py --check --compile --radome
PYTHONPATH=.:hardware/pnr python3 examples/radar60/board/dru_selftest.py --kicad-cli "$PNR_KICAD_CLI"
```

## U1 footprint

`fcbga161.py` builds the ball map from SWRS219F sections 6.2 and Table 6-1 (70 GND, 25 supply,
7 RF and 59 signal balls; 64 depopulated sites) and the footprint from its package drawing and
example board layout (0.32 mm NSMD lands, 0.42 mm mask openings, 0.125 mm stencil). The top view
puts A1 at the top left. Checked locally against the IPC-356 netlist of TI's IWR6843LEVM design
files (not committed): at 270° every ball lands within 2 µm of TI's U1 coordinates, and the GND
and RF balls agree.

## RF macro

The column and pocket positions follow the RF track's first macro draw (`../rf`, rfm1-n), which
moved them against plan 5.2. `gen_board.py --macro` compares the floorplan with the macro's
record: the pocket is covered, the patch extents are equal and all 158 macro vias lie inside the
RF region, the pocket or the package body. Judged with `radar60.kicad_dru`, the macro board has
two findings for the RF track: 0.15/0.30 mm fence vias (a 2.95 mil ring; PCBWay's minimum is
3 mil, so 0.32 mm pads) and two via pairs 0.26-0.27 mm hole to hole (11 mil, 0.28 mm, needed).

## BGA escape probe

`escape_probe.py` builds a board with only U1 at its pose and one 0.3 mm target pad per escaping
net, in line with its ball on the west or south side (the RF edges carry no signal escape), one
target row per ball row of depth; the RF exits and the RF region are reserved for the macro. yapnr
(the integrated gap branch, native maze kernel) places nothing and routes everything on the
declared stack (F signal, In1 GND, In2 signal, In3 supply, In4 GND, B signal; through vias;
0.10/0.10 mm, 0.40/0.20 mm vias); KiCad's DRC decides which nets are complete.
`escape-probe-results.json` holds every run and the ball-by-ball result.

| Run (Rev A pin plan: 37 signal balls, 3 reference capacitors)  | Signals escaped |  Ring 0 | Ring 1 |  Ring 2 |
| -------------------------------------------------------------- | --------------: | ------: | -----: | ------: |
| 0.325 mm grid (ball-aligned), with the 91 GND and supply drops |         17 / 37 |  9 / 12 | 5 / 12 |  3 / 13 |
| 0.25 mm grid (the runner's default), with drops                |         21 / 37 | 12 / 12 | 0 / 12 |  9 / 13 |
| 0.325 mm grid, every via 0.35/0.15                             |         19 / 37 |  8 / 12 | 7 / 12 |  4 / 13 |
| 0.325 mm grid, GND and supply balls left open                  |         31 / 37 | 12 / 12 | 8 / 12 | 11 / 13 |

Every GND and supply ball got its drop in every run (69 or 70 of 70 GND, 21 of 21 supply). All 59
signal balls (worst case): 25, 31, 26 and 41 of 59 in the same four runs. What it shows:

- **The plane drops compete with the escape.** With the drops, GND and supply vias take the
  depopulated ring-3 sites the ring-2 dog-bones need; without them 31 of 37 escape. The 0.40 mm
  via does not fit the interstitial sites (0.0996 mm to the lands), and the engine has one via
  class, so the drops cannot move there.
- **Ring 1 needs a ball-aligned grid.** On the default 0.25 mm grid no ring-1 ball escapes: the
  one channel between two ring-0 lands (0.33 mm of copper gap for a 0.10 mm track) is off grid.
- **The RF exits must be reserved.** In a first run without the reservation the GND drops took
  the depopulated site in front of all seven RF balls.
- **R4.** West-edge escapes on F.Cu north of y 28.2 (NRESET, the UART and NERROR balls in row N)
  cross the RF guard band; they need B.Cu (R4 covers F.Cu-In2.Cu only) or a dog-bone south first.

## Rev A integration: placed, not routed (`reva/`)

`integrate.py` combines the schematic's board (`yapnr atopile build ../schematic -b rev-a`), the
RF macro (`../rf/generated/rfm1-n`) and this floorplan into one board, places it with yapnr and
stops before routing. Nothing is placed by hand: the poses come from yapnr's Monte Carlo
placement search and a mechanical selection.

```sh
# A numeric Python (torch, numpy, PyYAML); ENGINE: a yapnr checkout with the region, side and
# stackup work (the integrated gap branch merged with main).
python3 integrate.py source  --work W --ato-board BUILD/rev-a.kicad_pcb --kicad-python "$PNR_KICAD_PYTHON"
python3 integrate.py prepare --work W --engine ENGINE --kicad-python "$PNR_KICAD_PYTHON"
python3 integrate.py place   --work W --engine ENGINE --n0 32 --procs 4 --seed 0
python3 integrate.py select  --work W
python3 integrate.py finish  --work W --engine ENGINE --kicad-python "$PNR_KICAD_PYTHON" --kicad-cli "$PNR_KICAD_CLI"
python3 integrate.py render  --work W --kicad-cli "$PNR_KICAD_CLI" --renders OUT [--label-python PY]
```

- **Source.** The floorplan board (outline, stackup, planes, rule areas, radome lands) with every
  footprint of the build, except the RF macro placeholder RFM1: yapnr models a part as a box
  about its origin, and the macro is neither a box nor movable, so its area enters the
  placement as the RF region keepout (less U1's courtyard, which the macro is drawn around).
- **Prepare.** The constraints as compiled for placement add one derived region: every movable
  part with a pad on a digital net stays out of the RF region and its 5 mm guard band (R4; the
  engine's `noise_keepout` is only proposed). `prepare` also checks that the HF, bulk and crystal
  sets cover each radio capacitor exactly once.
- **Place.** `pnr.mc.halving --stop-after place`: seeded stratified and Latin-hypercube global
  starts, each legalized and scored by the engine's routability proxy (`capacity-proxy-v1`).
- **Select.** Legal candidates that pass `audit.py` (R2 parts in the RF region, R4 digital pads
  in the guard band, R5 radome visibility with the [E] heights in `audit.py`, switch nodes to
  the crystal and the RF region), then the engine's stage-0 key (proxy score, cheap score), then
  HPWL, then id.
- **Finish.** `pnr.writeback` of the winner without routes; then `kicad_ops.py finish`: the
  rounded outline restored; the engine's all-layer copper-keepout rule areas checked against the
  `RF_REGION` pieces (corners equal) and dropped, since they forbid the macro's own copper on
  every layer; the plane drops writeback adds on its own (a via from each plane pad, which
  ignores the macro and lands on its launches) removed; the macro merged in U1's frame (tracks,
  arcs, vias and zones as the RF track wrote them, nets renamed to the schematic's; the column
  patches and the mask opening as one locked footprint RFM1, pads numbered as the placeholder's);
  zones filled. The copper digest of the macro's tracks, arcs and vias in U1's frame equals the
  RF track's board (R1), and every RF ball lands on its macro port to 0 µm.

The run of 2026-10-03 (32 starts, seed 0, 4 processes, 14 min): 10 legal, 9 pass the audit,
winner `p008` (no unreachable branch in the proxy; the next best has one). On the written
board KiCad's DRC finds no courtyard, keepout or rule-area violation; what it reports belongs to
the RF macro (144 fence vias 0.30 mm against the board's 0.32 mm minimum, two via pairs 0.26-0.27
mm hole to hole), U1's silkscreen corner marks clipped by the RF mask opening (6), the footprint
libraries not being configured (171), and the 413 connections nothing has routed yet.
`reva/placement-report.json` holds the audit, the constraint checks, the macro checks and the
DRC summary.

The placed board itself (1.2 MB) is over the repository's 600 kB file limit and is not committed:
`reva/placement.json` holds the winner's poses (by atopile address), its scores and the
engine it came from, and `integrate.py finish --placement reva/placement.json` (after `source`
and `prepare`) rebuilds `reva/radar60-reva.kicad_pcb` bit for bit; its sha256 is in the report.

`floorplan.yaml`'s `blocks_pending` keeps the small blocks' groups (ESD and TVS at J1, the eFuse,
CAN and flash support parts) that this placement lacks: without them the placer left those
passives up to 40 mm from their IC, and with them yapnr's legalizer, which places every grouped
part before any other, found no legal placement in 32 starts. They wait for the engine's
hierarchical blocks.
