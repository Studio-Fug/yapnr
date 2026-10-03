# radar60 schematic (atopile)

The electronics of the radar60 Rev A board: a 60 GHz phased-array FMCW radar on TI's
IWR6843AQGABLR, written in [atopile](https://github.com/atopile/atopile) and built with yapnr's
atopile flow ([docs/frontends/atopile.md](../../../docs/frontends/atopile.md)). The antenna and
feed copper is the yapnr RF macro RFM1 (`../rf`); here it is a placeholder footprint with one pad
per RF port.

| File                              | Contents                                                                                            |
| --------------------------------- | --------------------------------------------------------------------------------------------------- |
| `elec/src/radar60.ato`            | top level, J1, test pads, net names, the `@pnr-*` PnR intent                                        |
| `elec/src/power_in.ato`           | TVS, TPS259474A eFuse (OVLO 5.45 V, UVLO 4.08 V, ILIM 2.0 A, PG divider)                            |
| `elec/src/pmic_lp87524j.ato`      | LP87524J-Q1, snubbers, LC filters, 1.0 V split and shunt, PGOOD to NRESET                           |
| `elec/src/radio_iwr6843.ato`      | IWR6843 supplies and decoupling, 40 MHz crystal, SOP straps, pull-ups, LED                          |
| `elec/src/flash_qspi.ato`         | MX25V1635F boot flash, WP#/HOLD# pull-ups                                                           |
| `elec/src/can_fd.ato`             | TCAN1044AV-Q1, ESD2CAN24-Q1, pin-mux options, DNP split termination                                 |
| `elec/src/uart.ato`               | UART ESD and 1 kOhm back-power limiting resistors                                                   |
| `elec/src/debug.ato`              | DCA1000 header J2 (development fit), JTAG J3 (DNP)                                                  |
| `elec/src/rf_macro.ato`           | RFM1 placeholder                                                                                    |
| `elec/src/mech.ato`               | mounting holes, fiducials                                                                           |
| `elec/src/radar60_prod.ato`       | product fit: J2 not fitted, 1 mOhm in the shunt footprint                                           |
| `tools/gen_parts.py`              | writes every part into `elec/src/parts/` (not committed)                                            |
| `tools/footprints.py`             | land patterns from the package drawings (ABL0161B, RNF0026C, RPW0010A, Samtec QTH, Vishay WFCP0612) |
| `tools/data/abl0161_ballmap.json` | the IWR6843 ABL0161 ball map, from TI SWRS219F                                                      |
| `tools/check_schematic.py`        | ERC-equivalent checks of the built board                                                            |
| `yapnr-parts.lock.json`           | the parts lock: every part directory by content id (below)                                          |
| `BUILD.bazel`                     | the generators and the lock for `//tests/unit/radar60`                                              |

## Build

```sh
examples/radar60/schematic/tools/gen_parts.py           # parts -> elec/src/parts/
yapnr part-cache import-parts --imported-from radar60 examples/radar60/schematic/elec/src/parts
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

Every fitted part has an LCSC number, taken from its public LCSC product page
(`https://www.lcsc.com/product-detail/<number>.html`, read 2026-10-03) where the manufacturer part
number and the maker matched. `gen_parts.py` refuses a fitted part without one, and the DNP parts
carry theirs too. The lines that had none in the first revision of this example:

| Part                                | LCSC     | Notes                                                                                                                            |
| ----------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Littelfuse SMF5.0A (D3)             | C151296  | Littelfuse SOD-123FL mounting pads equal KiCad's `D_SMF`                                                                         |
| TI TPD2E2U06DCKR                    | C1855726 | LCSC stock 0 on 2026-10-03; TPD2E2U06QDCKRQ1 (C915089, same package and pins) was stocked                                        |
| JST SM08B-GHS-TB(LF)(SN) (J1)       | C265111  | LCSC stock 0                                                                                                                     |
| UNI-ROYAL 0402WGF1651TCE, 1.65 kOhm | C25869   | eFuse ILM                                                                                                                        |
| Vishay CRCW04023R90JNED, 3.9 Ohm    | C3988797 | snubber, TI SNVSAW2B Table 56; LCSC stock 0; 46 mW per phase at 5.45 V and 4 MHz against TI's 62 mW rating                       |
| Murata GCM1555C1H391JA16D, 390 pF   | C723890  | snubber, TI SNVSAW2B Table 56                                                                                                    |
| Vishay WFCP06122L000FE66, 2 mOhm    | C3917256 | the 1.0 V shunt R_SH1 (development fit): 1 %, 100 ppm/K, 2 W                                                                     |
| Vishay WFCP06121L000FE66, 1 mOhm    | C4231187 | R_SH1 on product builds, on the same lands (a 0 Ohm 0612 jumper is only specified as at most 10 mOhm, 25 mV at 2.5 A)            |
| Vishay IHLP1616BZERR47M11           | C844982  | Buck2/3 inductors; LCSC stock 0; IHLP1616BZERR47M5A (C3013817, automotive, lower DCR) was stocked, its lands are not checked yet |
| Murata GRM155R71E473KA88D, 47 nF    | C77017   | VBGAP                                                                                                                            |

DNP: TPS22917DBVR C2681320, FTSH-105-01-L-DV-K C5155080, 0402WGF620JTCE C4962, and the damping
resistor's part number, UNI-ROYAL 0402WGF220LTCE (0.22 Ohm, C270628). Rev A is assembled from
manufacturer part numbers; the JLCPCB BOM (`rev-a.bom.csv`) now keeps every line.

## Land patterns

The stock KiCad footprints are KiCad 10.0.6's. The generated ones (`tools/footprints.py`) carry
a `Source` property naming the drawing:

| Part                       | Land pattern                               | Source                                                                                                                                                                                                                                                |
| -------------------------- | ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| J2 Samtec QTH-030-01-L-D-A | `qth030_01_a`                              | Samtec "Recommended PCB layout for QTH-XXX-XX-X-D-XXX" rev. M (2024-01-26), Fig. 1 and Tables 1-3, and the part drawing QTH-XXX-XX-X-D-XXX rev. BL ([prints](https://suddendocs.samtec.com/prints/qth-xxx-xx-x-d-xxx-footprint.pdf), read 2026-10-03) |
| R_SH1 Vishay WFCP0612      | `wfcp0612`                                 | Vishay document 30417 rev. 20-Nov-2023, recommended solder pad layout for 1-5 mOhm ([wfcp.pdf](https://www.vishay.com/docs/30417/wfcp.pdf), read 2026-10-03)                                                                                          |
| U3 Macronix MX25V1635FZNQ  | stock `WSON-8-1EP_6x5mm_P1.27mm_EP3.4x4mm` | Macronix MX25V1635F data sheet PM2257 rev. 1.4, section 18-3, drawing 6110-3401 rev. 8: exposed pad 3.40 x 4.00 mm nominal (read 2026-10-03)                                                                                                          |
| U1, PMIC, eFuse            | `abl0161b`, `rnf0026c`, `rpw0010a`         | TI drawings 4223365/A, 4223207/B, 4225183/A                                                                                                                                                                                                           |

J2: 60 lands of 0.305 x 1.45 mm at 0.5 mm in rows 6.172 mm apart, four ground-plane lands
MP1-MP4 on the centreline (on GND), and two 1.02 mm NPTH holes for the alignment pins, 18.48 mm
apart and 2.03 mm towards the pin-1 row. Samtec's stencil drawing opens the lands 1:1 for a
0.152 mm stencil. The flash's exposed pad was 3.4 x 4.3 mm (KiCad's Winbond variant), 0.2 mm
longer than the Macronix maximum. No vendor PDF is committed.

## Parts lock

`yapnr-parts.lock.json` pins every part directory by content id, and a build materializes the
locked parts from the [part cache](../../../docs/part-cache.md), checking every file. The parts
are generated, so the cache is filled from `gen_parts.py`'s output (Build, above). The ids hold
for KiCad 10.0.6's stock library, whose footprints are copied byte for byte; with another KiCad
the stock parts get other ids, and `yapnr atopile lock-parts examples/radar60/schematic` shows
which (`git diff`). `//tests/unit/radar60` checks that the parts built only from
`tools/footprints.py` (U1, the PMIC, the eFuse, J2 in both fits, R_SH1 in both fits, the Kelvin
pads, RFM1) still have their locked ids, so a change to a generator needs a new lock:

```sh
examples/radar60/schematic/tools/gen_parts.py --catalog /tmp/radar60-catalog.json
yapnr atopile lock-parts examples/radar60/schematic --upload --imported-from "yapnr@<commit>"
yapnr part-cache import-catalog /tmp/radar60-catalog.json
```

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
Land patterns: Samtec's QTH-XXX-XX-X-D-XXX footprint (rev. M) and part (rev. BL) drawings,
Vishay 30417 (WFCP), Macronix PM2257 rev. 1.4 (MX25V1635F), Littelfuse SMF series (rev. 06/07/17),
Vishay 34196 (IHLP-1616BZ-11); LCSC product pages for the LCSC numbers; all read 2026-10-03.
