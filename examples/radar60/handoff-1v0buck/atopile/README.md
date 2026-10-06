# atopile board, ERC-equivalent report, and pin mapping

Added at the request of the agent receiving this handoff: "please include the original
atopile-generated board/netlist, its schematic/ERC check report, and the symbolic-to-numeric
IC pin mapping. The current schematic checker misreads the integrated KiCad 10 board, so
those will let me verify connectivity independently." The rest of the handoff (routed boards,
documents, simulations, manifest) is described in
`../README.md`.

## `board/` -- the original atopile-generated board (parts unplaced, unrouted)

atopile writes no KiCad schematic; `rev-a.kicad_pcb` **is** the netlist (every footprint pad
carries its net name) and is the pre-place/pre-route ground truth the router's seed and every
`candidate.kicad_pcb` descend from.

- `rev-a.kicad_pcb` -- the build output (atopile 0.15.8, KiCad CLI 10.0.6; see `result.json`).
  No zone fills, 520 KB uncompressed, committed as-is.
- `ato.log` -- the full atopile build log for this output (build-design, BOM, variable-report,
  power-tree, pinout targets).
- `result.json`, `catalog.json` -- build manifest and parts catalog atopile wrote alongside the
  board, including the sha256 of every output file atopile produced (`result.json.sha256`).
- `rev-a.bom.csv` / `rev-a.bom.json` -- bill of materials (manufacturer, MPN, LCSC part #).
- `power_tree.md`, `rev-a.power_tree.ato.json`, `rev-a.variables.ato.json` -- atopile's own
  power-tree and parameter reports for this build.
- `hook.jsonl` -- atopile's module-patch trace for this run (diagnostic only).

## `pinout/` -- the symbolic-to-numeric IC pin mapping

One JSON file per fitted part (atopile's `pinout` build target), each giving, per lead:
`leadDesignator` (the symbolic pin/ball name atopile's source uses), `padNumbers` (the numeric
KiCad pad number(s) that lead maps to), `netName` (the net on `rev-a.kicad_pcb`), and
`isConnected`. This is the authoritative symbolic<->numeric bridge for every IC on the board,
including:

- U1 (`155_radio_u1.json`, IWR6843) -- 161 leads against the ABL0161 ball map.
- U2 (`086_pmic_u2.json`, LP87524J) -- 27 leads.
- U3 (`023_flash_u3.json`) -- 9 leads.
- U4 (`013_can_u4.json`) -- 9 leads.
- U5 (`093_power_in_efuse.json`, TPS259474) -- 10 leads.
- J2 (`014_debug_j2.json`) -- 36 leads.
- Every other fitted part (passives, test points, connectors, mounting holes): one file each,
  180 files total.

The companion ball map the radar flow's own checker uses for U1 is
`examples/radar60/schematic/tools/data/abl0161_ballmap.json` (already on this branch; not
duplicated here).

## `erc/` -- the ERC-equivalent check and the reproduced "misreads KiCad 10" symptom

atopile emits no schematic, so there is no KiCad ERC to run. The radar flow's own
ERC-equivalent is `examples/radar60/schematic/tools/check_schematic.py` (already on this
branch), which re-derives pin-table, power, conflict, annotation and quantified-warning checks
directly from a built/placed/routed `.kicad_pcb`'s netlist. Its own docstring states the checks
it runs; see that file for the full list.

Both reports here were produced by running that exact script, unmodified, against two boards:

- `check-atopile-board.json` -- run against `board/rev-a.kicad_pcb` (parts unplaced).
  **64 errors**, all `"<ref> (<atopile address>) pad <N> (<N>): no pin type in the check
table"`, confined to U2 (pmic.u2), U3 (flash.u3), U4 (can.u4) and U5 (power_in.efuse).
- `check-routed-s1-board.json` -- run against
  `~/yapnr-runs/fetched/20261006-mceval-5818f8/tasks/mc~s1/result/out/s1/files/candidate.kicad_pcb`
  (the routed seed s1 board; see `../results/s1`). **573 errors**: the same 64, repeated, plus
  one additional `"net : 14 push-pull outputs [...]"` error naming 14 distinct U1 pins
  (SPIB_CS_N, LVDS_TXP/M[0:1], LVDS_CLKP/M, LVDS_FRCLKP/M, RS232_TX, MCU_CLKOUT, QSPI_CS_N,
  SPI_HOST_INTR, QSPI_CLK) as if they all sat on one net literally named `""` (empty).

### The symptom, reproduced and root-caused

This is the misread the receiving agent reported. Root cause, found by reading
`check_schematic.py`'s `Fp.__init__` (checker's S-expression footprint parser):

```python
for pad in children(node, "pad"):
    net = child(pad, "net")
    self.pads.append((pad[1], net[2] if net and len(net) > 2 else ""))
```

`pad[1]` is the **numeric KiCad pad designator** (`"1"`, `"18"`, ...) -- the first field of a
KiCad `(pad "18" smd rect ...)` node. `PIN_TYPES` (the table the error names), however, is
keyed by each part's **symbolic lead name** (`"FB_B3"`, `"CS_N"`, `"TXD"`, ...), taken from the
atopile source, not from the `.kicad_pcb` file. For U1 the two happen to coincide (the IWR6843's
BGA ball designators, e.g. `"B4"`, are used as both the KiCad pad number and the ball map key),
so U1's rows pass. For U2/U3/U4/U5 (QFN/SOIC parts with sequential numeric pads) they never
coincide, so every lookup misses and the checker reports "no pin type in the check table" for
every pad on those four parts -- a false positive, not a real connectivity fault. We confirmed
KiCad 10 writes no `pinfunction` node on these pads either (`grep pinfunction rev-a.kicad_pcb`
returns nothing), so the bridge the checker would need is not even present inline in the
`.kicad_pcb`; it exists only in the side-channel `pinout/*.json` files in this directory
(`leadDesignator` -> `padNumbers`). The checker does not currently read those files. A fix would
join `PIN_TYPES` through `pinout/*.json`'s `padNumbers` instead of assuming the `.kicad_pcb` pad
number already is the symbolic name.

The second symptom (routed board only: 14 outputs collapsed onto one `""`-named net) is a
separate, additional failure specific to the _routed/placed_ `.kicad_pcb` and was not
root-caused here; it is consistent with the net-name field going missing or empty for some pads
after place-and-route on this checker's parser, which independent connectivity verification
(e.g. against `erc/` and `pinout/` here, or a from-scratch netlist extraction) should treat as
a second, distinct bug in `check_schematic.py`, not a real short.

Neither report should be read as "64 wiring errors" or "573 wiring errors" on the actual board:
both counts are entirely checker false positives from this one parsing gap, confirmed identical
in kind (same four parts, same error text) on both the unplaced atopile board and the routed
seed, and the one extra routed-board error is a second, separate parser symptom. The real
connectivity is exactly what `pinout/*.json` and `board/rev-a.kicad_pcb` state net-by-net.

## `netlist/` -- independent netlists and the atopile-vs-routed-s1 diff

These netlists come from KiCad 10.0.6, not from `check_schematic.py`, so they are an
independent connectivity reference.

| File                                           | What                                                              | How                                                                                       |
| ---------------------------------------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `rev-a.d356`, `routed-s1.d356`                 | IPC-D-356 netlist (net names truncated to 14 chars by the format) | `kicad-cli pcb export ipcd356 -o OUT BOARD`                                               |
| `rev-a.netlist.json`, `routed-s1.netlist.json` | pad-level JSON: ref, pad, pinfunction, pintype, net               | `tools/netlist_json.py BOARD OUT` (KiCad's bundled Python)                                |
| `diff-rev-a-vs-routed-s1.txt`                  | net-by-net membership comparison                                  | `python3 tools/netlist_diff.py netlist/rev-a.netlist.json netlist/routed-s1.netlist.json` |

`routed-s1` is `candidate.kicad_pcb` from campaign 20261006-mceval-5818f8, seed s1. The
original file has zone fills and is 5.4 MB, so it is not committed here. Seeds s0, s2 and s3
were not exported.

Result: all 158 atopile nets have identical pad membership on routed s1, with none renamed
and none differing. The only pad-level differences come from the RF macro v2 frame:

- atopile has a single `RFM1.gnd` pad.
- The routed board adds `RFM1.rxd0`/`txd4`, which are on `RF_RXD0`/`RF_TXD4`.
- It also adds the terminations `RT1`/`RT2`, which run from `RF_RXD0`/`RF_TXD4` to `GND`.
- It also adds the dummy loads `RL1`/`RL2`, whose pad 1 has no net.

None of the pads on either board carries a `pinfunction`, so the symbolic names exist only in
`pinout/*.json`. KiCad reports pads with no net only on FID1-3, J2, U2, U3 and U4 (plus RL1/RL2
on s1), and none on U1. That confirms the routed-board checker symptom described above is a
parser artifact, not a short.

## `netlist/`: independent netlists for every board

- **IPC-D-356** (`kicad-cli pcb export ipcd356`): `rev-a.d356` and `routed-s0..s3.d356`. The format
  cuts net names to 14 characters.
- **JSON pad netlists** (`tools/netlist_json.py`, KiCad's headless Python): `rev-a.netlist.json` and
  `routed-s0..s3.netlist.json`, giving ref, pad, pin function, pin type and net per pad.
- **`diff-rev-a-vs-routed-sN.txt`** (`tools/netlist_diff.py`): on every seed, all 158 atopile nets
  have exactly the same pads on the routed board. The only differences are the RF macro v2 frame
  additions:
  - the atopile board has a single `RFM1.gnd` pad;
  - the routed boards add `RFM1.rxd0` and `RFM1.txd4`, the terminations `RT1`/`RT2`
    (`RF_RXD0`/`RF_TXD4` to `GND`), and the dummy loads `RL1`/`RL2`, whose pad 1 has no net.

## `pin-tables/`: symbolic lead to pad to net, per IC

`tools/pin_tables.py` writes one table per part (`U1`–`U6`, `J1`–`J3`, `Y1`, `Q1`, `D1`–`D3`, as
`.md` and `.csv`). Each row gives:

- the atopile lead name;
- its pad or ball;
- the net in atopile's pinout;
- the net on the atopile board;
- the net on each routed seed.

`INDEX.md` counts the rows where any board net differs from the pinout. **That count is 0 for every
part**, so connectivity agrees symbol by symbol.
