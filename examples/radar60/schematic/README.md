# radar60 schematic (atopile)

The electronics of the radar60 Rev A board: a 60 GHz phased-array FMCW radar on TI's
IWR6843AQGABLR, written in [atopile](https://github.com/atopile/atopile) and built with yapnr's
atopile flow ([docs/frontends/atopile.md](../../../docs/frontends/atopile.md)). The antenna and
feed copper is the yapnr RF macro RFM1 (`../rf`); here it is a placeholder footprint with one pad
per RF port.

| File                              | Contents                                                                   |
| --------------------------------- | -------------------------------------------------------------------------- |
| `elec/src/radar60.ato`            | top level, J1, test pads, net names, the `@pnr-*` PnR intent               |
| `elec/src/power_in.ato`           | TVS, TPS259474A eFuse (OVLO 5.45 V, UVLO 4.08 V, ILIM 2.0 A, PG divider)   |
| `elec/src/pmic_lp87524j.ato`      | LP87524J-Q1, snubbers, LC filters, 1.0 V split and shunt, PGOOD to NRESET  |
| `elec/src/radio_iwr6843.ato`      | IWR6843 supplies and decoupling, 40 MHz crystal, SOP straps, pull-ups, LED |
| `elec/src/flash_qspi.ato`         | MX25V1635F boot flash, WP#/HOLD# pull-ups                                  |
| `elec/src/can_fd.ato`             | TCAN1044AV-Q1, ESD2CAN24-Q1, pin-mux options, DNP split termination        |
| `elec/src/uart.ato`               | UART ESD and 1 kOhm back-power limiting resistors                          |
| `elec/src/debug.ato`              | DCA1000 header J2 (development fit), JTAG J3 (DNP)                         |
| `elec/src/rf_macro.ato`           | RFM1 placeholder                                                           |
| `elec/src/mech.ato`               | mounting holes, fiducials                                                  |
| `elec/src/radar60_prod.ato`       | product fit: J2 not fitted, 0 Ohm in the shunt footprint                   |
| `tools/gen_parts.py`              | writes every part into `elec/src/parts/` (not committed)                   |
| `tools/footprints.py`             | land patterns from the package drawings (ABL0161B, RNF0026C, RPW0010A)     |
| `tools/data/abl0161_ballmap.json` | the IWR6843 ABL0161 ball map, from TI SWRS219F                             |
| `tools/check_schematic.py`        | ERC-equivalent checks of the built board                                   |

## Build

```sh
examples/radar60/schematic/tools/gen_parts.py           # parts -> elec/src/parts/
yapnr atopile build examples/radar60/schematic -b rev-a        # development fit
yapnr atopile build examples/radar60/schematic -b rev-a-prod   # product fit
examples/radar60/schematic/tools/check_schematic.py yapnr-out/schematic/rev-a/rev-a.kicad_pcb
```

`gen_parts.py` copies stock footprints from the local KiCad library (the headless KiCad's, or
`$YAPNR_KICAD_FOOTPRINTS`) and rewrites their format version for atopile 0.15.8; it also draws
squares instead of silk circles where a footprint's silk is only circles (atopile 0.15.8 cannot
size such a silk outline). The parts carry no EasyEDA data and no TI design file.

The build places nothing: the board comes out unplaced, for the PnR flow.

## Parts and LCSC numbers

A part has an LCSC number only where the public LCSC product page was read and its manufacturer
part number matched (2026-10-03). The others carry `LCSC-TBD-<mpn>`: atopile's JSON BOM keeps
them by manufacturer part number, its JLCPCB CSV drops them with a warning, and the part cache
refuses them, so the project has no parts lock yet. Rev A is assembled from manufacturer part
numbers.

The Samtec QTH-030-01-L-D-A land pattern (J2) is **provisional**: the Samtec footprint drawing
was not available. It carries the footprint property `Unverified` and must be regenerated before
any order.

## Checks

`check_schematic.py` reads the built board and fails on:

- U1 pads differing from the 161-ball map, the plan's pin table disagreeing with the ball map,
  a ball on another net than the design table gives it, or an "unused" ball sharing a net;
- a power pin with no rail or a rail outside the pin's data-sheet range (rails are traced from
  J1, the eFuse and the bucks through fitted inductors, beads and 0 Ohm/shunt resistors), a
  ground pin off GND;
- two push-pull outputs on one net, an input with nothing fitted to drive or pull it, an IC
  pin alone on its net;
- an `@pnr-*` annotation whose target or pads do not exist, or a `@pnr-current` pad set that
  spans several nets;
- the two PMIC CLKIN options both fitted (that shorts SOP2 to GND).

It also prints two quantified warnings for review: the 1.0 V rail's fitted capacitance (about
183 uF, TI's IWR6843ISK Rev D network) against the LP87524's 100 uF for a 1-phase output, and
the 1.0 V DC window at the balls. The latter is a firmware requirement: write BUCK2_VSET = 0x52
(1.025 V) over I2C before the RF starts (0.946 V worst case, 1.0455 V at no load, against
0.95-1.05 V).

## Sources

TI SWRS219F (IWR6843), SWRZ087D (errata), SNVSAW2B (LP87524-Q1), SLVSFC9C (TPS25947),
SLLSFJ3D (TCAN1044A-Q1), SLVSFW5D (ESD2CAN24-Q1), SPRUIJ4A (DCA1000EVM), and the
IWR6843ISK Rev D schematic (SWRR164) for decoupling values; each file names the sections it uses.
