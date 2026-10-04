# radar60 board track: floorplan, rules, constraints and the BGA escape probe

The board-level inputs for the radar60 Rev A board (a 60 GHz IWR6843 FMCW radar, 60 × 47.35 mm,
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
| `dru_selftest.py`           | Plants 23 items on the floorplan board and checks that each board rule fires, and only where it should (headless kicad-cli)  |
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

| Item                                        | Pose or extent                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Outline                                     | 60.0 × 47.35 mm, 1 mm corner radii (47.35: the RF macro's region ends at y 47.024, and copper keeps 0.3 mm from the edge: the record's `board_frame.board_height_min`)                                                                                                                                                                                                                                                |
| U1 (IWR6843, FCBGA-161)                     | fixed at (26.0, 28.0), 270°: RX balls (column 2) face north, TX balls (row B) face east                                                                                                                                                                                                                                                                                                                               |
| Mounting holes                              | the schematic's 4 plated M2.5 hole parts on GND (mech.mh[0..3]) fixed at (3.5, 3.5), (56.5, 3.5), (3.5, 43.85), (56.5, 43.85); Ø6.5 mm copper keepout                                                                                                                                                                                                                                                                 |
| Radome standoff lands                       | Ø3.0 mm, Ø5.0 mm keepout, at (3.5, 27.0) and (56.5, 27.0): the test radome covers y ≥ 27                                                                                                                                                                                                                                                                                                                              |
| J1 (JST GH-8, right angle)                  | fixed on the south edge, centred at x = 30, cable entry south                                                                                                                                                                                                                                                                                                                                                         |
| J2 (QTH-030, development)                   | region x 8.0-34.0, y 7.25-15.6 (stage 3b: its fixed stage-2 pose predated the QTH land of the parts track and the 47.35 mm board)                                                                                                                                                                                                                                                                                     |
| J3 (JTAG, DNP)                              | region x 0.6-8.3, y 8.0-24.3 at the west edge, below the radome land RL1 and above MH1 (only its upright turn fits)                                                                                                                                                                                                                                                                                                   |
| RF region (F.Cu-In2.Cu)                     | RX fan, dummy columns and west ground margin x 12.599-29.557, y 33.2-47.35; TX launches, feeds, bank and east margin x 31.25-51.469, y 26.7-47.35; above the pocket (the RF macro's `board_frame`)                                                                                                                                                                                                                    |
| VOUT_PA pocket                              | x 29.557-31.25, y 33.2-34.2: the RF macro's pocket (from y 33.45), cut out of the RF region down to the package edge; no part fits                                                                                                                                                                                                                                                                                    |
| Dummy loads (RF macro)                      | RT1 (19.495, 35.018), RT2 (31.205, 35.018), RT3 (35.205, 33.130), RT4 (44.573, 33.130): 0201 50 Ω terminations of the dummy columns RXD0, RXD5, TXD0, TXD4, each in its mask island                                                                                                                                                                                                                                   |
| R4 guard (no digital copper on F.Cu-In2.Cu) | a 5 mm band around the RF region without the package body: x 7.599-12.599 for y ≥ 28.2, x 12.599-20.8 for y 28.2-33.2, x 31.2-56.469 for y 21.7-26.7, x 51.469-56.469 above. RF, PWR, GND and ANALOG nets are exempt                                                                                                                                                                                                  |
| PMIC block / switch nodes                   | x 38-58, y 2-26.65 / x 41-58, y 2-21.7 (≥ 8 mm from the crystal's pads, ≥ 5 mm from the RF region); the bead LC filters and the DNP damping options are outside the block                                                                                                                                                                                                                                             |
| Y1 (40 MHz) / load caps                     | x 28.4-33.0, y 15.75-19.7 (below its caps, east of the SPIA exit band) / CLKM cap upright x 28.2-29.6, CLKP cap flat x 29.5-31.7, y 19.9-22.05 (pads 2.0 and 2.5 mm from C15/B15)                                                                                                                                                                                                                                     |
| Fanout exit bands                           | every ball that escapes on F.Cu keeps a 0.5 mm strip from U1's courtyard 2.5 mm outwards free of parts (`integrate.py prepare` derives them from U1's fanout plan): south at x 22.5-26.25 (LVDS) and 27.7-28.2 (SPIA), west at y 23.85-27.925 (QSPI, JTAG, NRESET)                                                                                                                                                    |
| Ball-anchored radio parts                   | APLL, SYNTH and VBGAP caps, the RF1 220 nF, the SRAM 100 nF and the VCO 220 nF on the fanout's bottom sites under the array; VOUT_PA / VIN_13RF2 220 nF and the PA 0 Ω under the corner (bottom, an L clear of the macro's launch vias); their bulk on the bottom east of the package (x 31.4-37.5, y 19.5-26.6); the VIN_18BB caps west of the package; the QSPI clock resistor west of R12's neighbours' exit bands |
| Blocks                                      | flash x 8.3-19.3, y 15.75-24.6 (QSPI about 10 mm); eFuse and TVS west of J1 below J2; CAN transceiver north-east of J1 behind its ESD diodes (x 31.6-38.5, y 6.65-11); hard groups round U2 (inductors, input caps, VANA), U5, U4 and U3                                                                                                                                                                              |
| Pad-anchored groups                         | each snubber resistor within 3.5 mm of its inductor's switch-node pad; the 1V0_RF1 ferrite within 12 mm of ball H5 and the two 1V0_RF2 ferrites within 10 mm of D2; each DNP damping resistor within 3 mm of its ferrite's rail pad and its capacitor within 2 mm of the resistor's damping pad                                                                                                                       |

`gen_board.py --radome` checks the 30° radome visibility rule (a part of height h stays 1.73 h
from the patch copper) against the regions: J2 has 20.5 mm for 7.45 mm, J3 16.5 mm for 8.66 mm,
the PMIC inductors (IHLP-1616, 2.0 mm) 14.4 mm for 3.46 mm; J1 is more than 25 mm away.

## Custom rules (`radar60.kicad_dru`)

Part 1 is the `pcbway-adv-6l-rf` profile's rules, copied from `pnr.fab_profile.dru_text`. yapnr
writes those alone beside a board that has no rules file, and leaves a file without its
generated header untouched, so this file carries both parts. Part 2, the board's rules (rule
areas `RF_REGION`, `RF_POCKET` and `RF_GUARD` are drawn in `radar60.kicad_pcb`; net classes
`RF`, `XTAL`, `LVDS`/`dp_lvds_*`, `QSPI`, `PWR`, `SW`, `GND`, `ANALOG` come from `constraints.yaml`):

| Rule                                     | What it enforces                                                                                                |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `rf_region_tracks`                       | no non-RF tracks in the RF region on F.Cu-In2.Cu (In3-B.Cu stay free)                                           |
| `rf_region_vias`, `rf_fence_vias`        | no vias there but GND fence vias of 0.15/0.32 mm (pitch ≥ 0.45 mm from the 11 mil hole to hole)                 |
| `rf_region_parts`                        | no top-side parts there but RFM1, U1 and parts enclosed by the pocket                                           |
| `rf_no_vias`                             | the 60 GHz nets never change layer                                                                              |
| `rf_guard_digital`                       | R4: no tracks (F.Cu-In2.Cu) or vias in the guard band but RF, PWR, GND and ANALOG nets                          |
| `bga_signal_vias`, `bga_gnd_vias`        | under U1: signal and power vias 0.20/0.40 (dog-bones); 0.15/0.35 only for GND (interstitial)                    |
| `general_vias`                           | elsewhere 0.20/0.40 or larger                                                                                   |
| `xtal_no_vias`, `sw_to_xtal`, `sw_to_rf` | crystal nets on F.Cu only; switch nodes ≥ 8 mm from the crystal, ≥ 5 mm from RF copper                          |
| `lvds_pairs`, `lvds_outer_layers`        | 0.17/0.18 mm pairs (99-103 Ω for Dk 3.33-3.66 [D]), 0.1 mm intra-pair skew, ≤ 3 mm uncoupled; F.Cu or B.Cu only |
| `qspi_length`                            | ≤ 25 mm at 80 MHz                                                                                               |
| `plane_in1`, `plane_in4`                 | no tracks on the GND planes                                                                                     |

`dru_selftest.py` (KiCad 10.0.6, headless) passes all 23 planted cases; the floorplan board
itself is clean (0 violations, 0 unconnected) under these rules.

## Constraints (`constraints.yaml`)

Parts are named by atopile instance path and nets by the radio's ball (`net@<path>:<ball>`), so
no generated designator appears; `floorplan.yaml` (`parts:`) lists the paths the schematic must
provide. The file compiles with yapnr's compiler on `main` (`gen_board.py --compile
[CHECKOUT]`). Its sections:

- `fixed`: U1 at its pose in the RF macro's frame, J1 on the south edge, the four mounting-hole
  parts, the floorplan board's radome lands;
- `keepout`: the RF region less U1's courtyard (placement); `integrate.py prepare` adds the
  fanout's exit bands;
- `copper_keepout` (v1, routing): the RF region on F.Cu-In2.Cu with the macro's group exempt, the
  R4 guard band (RF, PWR, GND and ANALOG allowed), the mounting-hole and radome-land circles, and
  no via under the flash's exposed pad;
- `fixed_block`: the RF macro (group `RFM1_MACRO`, anchored at U1, its In2 GND pour solid);
- `fanout`: U1's escape plan (skipped balls, via classes, necks, drop nets, bottom sites);
- `legalize`: `outline: exact`, `order: scarcity`, `lookahead: regions`;
- `region` (hard): J2, J3, the PMIC block and switch nodes, Y1, the ball-anchored slots, the
  bottom-side VOUT_PA parts; `side` (those parts on the bottom); `orientation` (the slots' parts);
- `group` (hard): HF and bulk decoupling, Y1, the buck inductors, the blocks, and the
  pad-anchored groups (`anchor_pad`: snubbers, rail ferrites, damping options);
- `net_class`, `diff_pair` and `length_match` (LVDS 2 mm group skew).

`rf_macro`, `noise_keepout` and `height_limit` are proposed sections: the engine warns and
ignores them, and the custom rules, `integrate.py`'s derived R4 region and the audits stand in
for them. `radar60.kicad_pro` assigns the net classes by net-name pattern, so the custom rules'
`hasNetclass()` sees the real nets on any board that uses it.

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

The column and pocket positions follow the RF track's macro (`../rf`, rfm1-n, a placeholder until
the C1/C2 runs), which moved them against plan 5.2. `gen_board.py --macro` compares the floorplan
with the records of all three D12 variants (rfm1-m/-n/-p): each pocket is covered, all 364 vias
and 365 F.Cu points of each lie inside the RF region, the pocket or the package body, and the
rfm1-n patch extents are the floorplan's (m and p differ by 0.01 mm). The macro's fence vias are
0.15/0.32 mm at least 0.43 mm apart (the profile's 3 mil ring and 11 mil hole to hole).

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
RF macro (`../rf/generated/rfm1-n`, or `--macro-variant m|p` for the D12 brackets) and this
floorplan into one board, places it with yapnr and stops before routing. Nothing is placed by
hand: the fixed poses, regions and rotations are derived values in `floorplan.yaml`, and every
other pose comes from yapnr's Monte Carlo placement search and a mechanical selection.

```sh
# A numeric Python (torch, numpy, PyYAML); ENGINE: a yapnr checkout (default: this one).
python3 integrate.py source  --work W --ato-board BUILD/rev-a.kicad_pcb --kicad-python "$PNR_KICAD_PYTHON"
python3 integrate.py prepare --work W --engine ENGINE --kicad-python "$PNR_KICAD_PYTHON"
python3 integrate.py place   --work W --engine ENGINE --n0 32 --procs 4 --seed 0
python3 integrate.py select  --work W
python3 integrate.py finish  --work W --engine ENGINE --kicad-python "$PNR_KICAD_PYTHON" --kicad-cli "$PNR_KICAD_CLI"
python3 integrate.py render  --work W --kicad-cli "$PNR_KICAD_CLI" --renders OUT [--label-python PY]
```

- **Source.** The floorplan board (outline, stackup, planes, rule areas, radome lands) with every
  footprint of the build but the RF macro placeholder, U1 at its fixed pose, and the RF macro
  merged in U1's frame (`kicad_ops.merge_macro`): tracks, arcs, vias and zones as the RF track
  wrote them with nets renamed to the schematic's; the columns, dummy columns included, and the
  mask opening (its gaps are the loads' mask islands) as one locked footprint RFM1, its pads named
  by port (`rx1` ... `tx3`, `rxd0` ... `txd4`); the dummy loads RT1-RT4 as locked, assembled
  parts at the record's centres. Every macro item is in the KiCad group `RFM1_MACRO`, the
  engine's `fixed_block`. The board's In1 GND plane is cut out of the RF region, where the
  macro's own In1 ground is the reference.
- **Prepare.** The fixed block's copper goes into the rules (`pnr.fixed_copper`) and its
  footprints leave the placement graph. U1's fanout is planned as the router will plan it (U1
  fixed, the macro's copper known): every ball that escapes on F.Cu keeps a 0.5 mm strip from
  U1's courtyard 2.5 mm outwards free of parts (`bands.py`; a ball-anchored slot, `slot: true`,
  may stand only in its own ball's band, and one that meets another net's band is refused by
  name), and the parts given a bottom site lose their top-side regions (the engine fixes them at
  their sites). Every movable part with a pad on a guard-restricted net (a routed net outside
  the RF, PWR, GND and ANALOG classes, as the custom rule tests it) stays out of the RF region
  and its 5 mm guard band (R4; the engine's `noise_keepout` is only proposed). `prepare` also
  checks that the HF, bulk and crystal sets cover each radio capacitor exactly once. The plan is
  kept by its inputs' digest (`WORK/inputs/fanout-*.json`).
- **Place.** `pnr.mc.halving --stop-after place` (through `halving_seeded.py`, which hands each
  worker the prepared plan instead of planning it again): seeded stratified and Latin-hypercube
  global starts, each legalized and scored by the engine's routability proxy
  (`capacity-proxy-v1`).
- **Select.** Legal candidates that pass `audit.py` (R2 parts in the RF region, R4 digital pads
  in the guard band, R5 radome visibility with the [E] heights in `audit.py`, switch nodes to
  the crystal and the RF region, the pin distances), then the engine's stage-0 key (proxy score,
  cheap score), then HPWL, then id.
- **Finish.** `pnr.writeback` of the winner without routes (the copper keepouts become rule
  areas and, for the ones with allow lists or the exempt macro group, custom rules appended to
  the board's `.kicad_dru`); then `kicad_ops.py finish`: the rounded outline restored, macro
  items, U1 and the macro's footprints locked, zones filled, and the macro digest R1 v2 (copper,
  the loads' pads and the mask polygons in U1's frame) compared with the RF track's board. KiCad's
  DRC, the placement audit (regions, groups measured from their anchor pads, exit bands empty,
  bottom sites) and the RF audit A1-A6 (`rf_audit.py`) of the filled board go into
  `reva/placement-report.json`. `finish --placement reva/placement.json` rebuilds the board bit
  for bit.

Stage 3b (2026-10-04; the RF-uniformity macro rfm1-n, 32 starts, seed 0, 4 processes, 6 min):
26 legal (6 lost a hard group's slot or the CAN block's window), all 26 pass the audit, winner
`p030` (proxy: 1 unreachable branch, 4.6 overflow units). The one unreachable branch is in every
good candidate: J2's centreline ground lands (MP1-MP4 between the two signal rows) have no via
cell at the proxy's 2 mm pitch; routing needs vias in or beside them. On the written board KiCad's
DRC reports only the footprint libraries not being configured (187: 183 parts and the 4 macro
loads) and the 432 connections nothing has routed yet. The macro digest R1 v2 equals the RF
track's board (1664 copper items, 8 load pads, 12 mask polygons), every RF ball lands on its
macro port to 0 µm, and the RF audit passes A1-A6. No part stands in an exit band; the six
bottom sites (APLL, SYNTH, VBGAP, RF1, SRAM, VCO caps) are 0.1-1.6 mm from their balls; the
crystal's load caps 2.1 / 2.3 mm, the QSPI clock resistor 4.6 mm (west of R12's neighbours'
bands); each snubber resistor 1.6-3.5 mm from its inductor's switch-node pad (was 5-5.5 mm);
switch nodes 11.7 mm from the crystal and 5.65 mm from the RF region.

What stays open for routing: the PA and RF2 bulk caps sit 7-12 mm from their balls on the bottom
side (nearer needs the BGA shadow); A2/B2 (VOUT_PA) are skipped by the fanout until the RF
macro's v2 feed (D14); the fanout plan escapes 32 of the 37 Rev A signal balls (N4, E13, G14,
N13 and N7 lose their resources to higher-priority balls) and drops 84 of 92 plane and supply
balls (GND A3, A5, A7, G1, J1, L1 at the RF edges and L10, and 1V2 P14, have no legal site; a
failed P14 blocks the whole 1V2 net until partial fanout lands in the engine).

Routing inputs (stage 3b engine, later the same day): `floorplan.yaml` now turns on the router's
class clearances, the board's custom rules and its exact outline, partial fanout, an In3 plane
partition for 1V0_RF1 and 1V0_RF2 and an IR report per rail. `reva/` was placed before them and
is kept: with them the fanout plan moves 1V0_RF1/1V0_RF2 from drop nets to In3 plane nets and
C62 loses its bottom site, so `finish --placement` now rebuilds `p030` with a bottom-site and a
region finding. A re-place under the new inputs (19 of 32 legal, 18 pass the audit, `p005`)
routed worse in the stage-3b trial (170 unconnected items against 142 for `p030`), so the
placement waits for the stage-3c re-place with macro v2.
