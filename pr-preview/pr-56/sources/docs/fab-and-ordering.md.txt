# Fab bundles and staged orders

`yapnr fab` turns a routed board into the files a board house needs, checked under that vendor's
rules. `yapnr order stage` then prints an order card and takes you to the vendor's upload page.
Three vendors are covered: **OSH Park** (US, bare boards), **JLCPCB** and **PCBWay** (China, bare
or assembled boards).

**Staging only.** yapnr prepares files and opens the vendor's page in your browser. It never
uploads a file, never places, confirms or pays for an order, never logs in, never creates an
account and never accepts terms for you ([decisions](decisions.md)). You check the vendor's
preview, choose the options on the card and pay on the vendor's page. The design, with the
vendor research behind it, is [Fab outputs and staged ordering](design/fab-and-ordering.md).

```sh
# From a checkout: the command (or `bazel run //:yapnr -- <command>`) and the headless kicad-cli
# (DEVELOPERS.md; never the KiCad application bundle).
export PYTHONPATH="$PWD:$PWD/hardware/pnr"
export YAPNR_KICAD_CLI="$PNR_KICAD_CLI"

# 1. Route the board under the vendor's rules (here the ladder's case 08 for OSH Park 4 layer).
"$NUMERIC_PYTHON" hardware/pnr/regression/run.py --repo . --out .yapnr/fab/osh4 \
  --case 08-chaser-20-plane --seed 0 --fab-profile oshpark-4l \
  --python "$NUMERIC_PYTHON" --kicad-python "$PNR_KICAD_PYTHON" \
  --kicad-cli "$PNR_KICAD_CLI" --library "$PNR_KICAD_FOOTPRINTS"

# 2. Check it, build the bundle, print the order card, open the upload page.
python3 -m yapnr order stage .yapnr/fab/osh4/08-chaser-20-plane-seed-0/routed.kicad_pcb \
  --vendor oshpark --name chaser --out .yapnr/fab/bundles
```

`yapnr fab` uses the engine's rules (`pnr.fab_profile`), so it runs from a checkout (or through
Bazel); the wheel alone does not carry the engine yet. Python 3.9 or later is enough: the fab and
order commands need nothing outside the standard library.

## Where each vendor stands

| Vendor        | What `order stage` does                                                                                                                                                                                                                   | What you do                                                                                    |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| OSH Park (US) | Opens `oshpark.com` and names the zip to drop. For a zip that is already public (a release asset), `--via import-url --url <zip>` opens `oshpark.com/import?url=<zip>`: OSH Park fetches the file itself, so the order is one click away. | Drop the zip (or click the link), check the preview, choose the service, pay.                  |
| JLCPCB        | Opens `cart.jlcpcb.com/quote` and prints the options to set, including the stackup name under "Specify Stackup".                                                                                                                          | Upload the zip, set the options, upload the BOM and CPL for assembly, check the previews, pay. |
| PCBWay        | Opens the PCB quote page (or the assembly quote page) and prints the options and any remark for "Other special request".                                                                                                                  | Enter size and layers, upload the zip, set the options, add the remark, save to cart and pay.  |

Not built, by design, until the owner decides (design §12): an opt-in upload to OSH Park's
documented `import.json` endpoint (O2, decision D1), and vendor APIs for quotes and add-to-cart
(O3, decision D3 and API access). `yapnr order vendors` lists the mechanisms and their status.

## Route under the vendor's rules first

A board is routed and judged under one **fab profile**. A board routed for one vendor rarely
passes another's rules: JLCPCB's 0.45/0.30 mm vias have a 0.075 mm annular ring, under OSH Park's
4 mil (0.1016 mm) minimum, and `yapnr fab check` reports exactly that. Route for the vendor you
order from:

- the regression ladder: `run.py --fab-profile oshpark-4l` (any profile below);
- the engine: `PNR_FAB_PROFILE=oshpark-4l`. The default stays `jlc-pofv`.

Without `--profile`, `fab` and `order stage` take the profile the board was routed under (the one
its generated `.kicad_dru` names) when it is the chosen vendor's; otherwise the vendor's profile
for the board's copper layer count, and at JLCPCB `jlc-pofv` when a via sits on an SMD pad.

| Profile        | Status | Vendor, service                               | Default stackup      | Other stackups                                                     |
| -------------- | ------ | --------------------------------------------- | -------------------- | ------------------------------------------------------------------ |
| `oshpark-2l`   | active | OSH Park 2 Layer (and Super Swift)            | `oshpark-2l-fr4`     |                                                                    |
| `oshpark-4l`   | active | OSH Park 4 Layer (and Super Swift)            | `oshpark-4l-fr408hr` | `oshpark-4l-em528` (the alternate at checkout), `oshpark-4l-mixed` |
| `oshpark-6l`   | active | OSH Park 6 Layer                              | `oshpark-6l-fr408hr` |                                                                    |
| `jlc-4l`       | active | JLCPCB 4 layers, tented vias, no via in pad   | `jlc04161h-7628`     | `jlc04161h-2116`, `-3313`, `-1080`                                 |
| `jlc-pofv`     | active | JLCPCB 4 layers, epoxy filled and capped vias | `jlc04161h-7628`     | as `jlc-4l`                                                        |
| `jlc-6l`       | draft  | JLCPCB 6 layers                               | `jlc06161h-3313`     | `jlc06161h-2116c`, `jlc06161h-7628`                                |
| `pcbway-std`   | draft  | PCBWay standard 4 layers                      | `pcbway-4l-7628`     |                                                                    |
| `pcbway-hf-2l` | draft  | PCBWay Rogers 2 layers (RO4003C, RO4350B)     | none: choose one     | `pcbway-ro4003c-0.813`, `-0.508`, `-1.524`, `pcbway-ro4350b-0.508` |

A **draft** profile still has published values that are not transcribed; `fab check`, `fab
build` and `order stage` refuse it unless `--allow-draft` is given, and the card says so.
`yapnr fab profiles` lists them, `yapnr fab show <profile|stackup|vendor>` shows one, and
`yapnr fab show --sources` lists every source with its access date.

The profiles and stackups are data, in `yapnr/fab/data/`. Every value names its source (a vendor
page and the date it was read) or says `derived:` with the reason; unpublished values stay
`null`, and a stand-in is a `*_prior` with its own source. The engine's built-in `legacy` and
`jlc-pofv` profiles are unchanged, byte for byte.

## `yapnr fab check`

```sh
yapnr fab check BOARD.kicad_pcb --vendor oshpark [--profile oshpark-4l] [--stackup ID] [--qty 3]
```

The board is copied to a scratch directory (it is never modified). The copy gets the profile's
rules: the stricter of each board constraint in the project's `.kicad_pro` and the profile's,
and the profile's custom rules ahead of any hand-written `.kicad_dru`. Then KiCad's own DRC runs
with a zone refill (`kicad-cli pcb drc --refill-zones --save-board --severity-all`). KiCad's DRC
is the judge: no finding is relaxed. On top of it, the checks KiCad cannot express:

| Code                | Checks                                                                                                           |
| ------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `FAB-DRC`           | KiCad DRC under the vendor's rules (errors, warnings, unconnected items)                                         |
| `FAB-PLACEHOLDER`   | a footprint marked as a placeholder (an empty optimizer window on a coupon board): an error until it is filled   |
| `FAB-LAYERS`        | the copper layer count against the profile and stackup                                                           |
| `FAB-OUTLINE`       | a closed outline; several outlines (a panel) with the vendor's panel rules                                       |
| `FAB-SIZE`          | the vendor's minimum and maximum board size                                                                      |
| `FAB-VIA-TYPE`      | blind, buried or micro vias on a through-via-only service                                                        |
| `FAB-DRILL`         | holes the vendor mills instead of drilling; slots and the minimum slot; NPTH in OSH Park's merged drill file     |
| `FAB-CASTELLATED`   | castellated pads (OSH Park: "allowed, but not guaranteed"; JLC: the option and hole size)                        |
| `FAB-STACKUP`       | the board's KiCad stackup against the ordered stackup                                                            |
| `FAB-RF`            | an RF footprint designed for another substrate than the ordered stackup                                          |
| `FAB-IMPEDANCE`     | impedance-relevant items on a service without impedance control                                                  |
| `FAB-ALTERNATE`     | the substrate to pick at checkout when the vendor offers an alternate (OSH Park EM528)                           |
| `FAB-QTY`           | OSH Park: multiples of 3; JLCPCB and PCBWay: 5 or more                                                           |
| `FAB-MARKING`       | the order-number marking option (PCBWay prints its number unless told otherwise; JLCPCB adds no mark by default) |
| `FAB-ASSEMBLY`      | missing LCSC ids or MPNs, parts not in the parts lock, unchecked rotations, bottom parts                         |
| `FAB-PROFILE-DRAFT` | a draft profile                                                                                                  |
| `FAB-SOURCES`       | vendor data read more than 180 days ago                                                                          |
| `FAB-PRIVACY`       | with `--public`: absolute paths, home directories or e-mail addresses in the bundle                              |

`fab check` exits 1 on any error; `--json` prints every finding.

## `yapnr fab build`

```sh
yapnr fab build BOARD.kicad_pcb --vendor {oshpark,jlcpcb,pcbway} [--profile P] [--stackup S] \
    [--qty N] [--service super-swift] [--assembly] [--parts-lock yapnr-parts.lock.json] \
    [--consign J1,J2] [--name NAME] [--out DIR] [--source-date-epoch T] [--public]
```

`build` runs the check first and stops on any error. It then exports from the refilled scratch
copy, so the files are exactly what DRC judged, and writes `<out>/<name>-<profile>/`:

| File                                  | What it is                                                                    |
| ------------------------------------- | ----------------------------------------------------------------------------- |
| `<name>-<vendor>-gerbers.zip`         | **the file to upload**: fab files only, named as the vendor asks              |
| `<name>-<vendor>-bom.csv`, `-cpl.csv` | with `--assembly` (JLCPCB, PCBWay): kept out of the gerber zip                |
| `README.md`                           | board, stackup with sources, impedance notes, fab check, file list            |
| `order-card.md`, `.json`              | the order card                                                                |
| `drc.json`, `fab-check.json`          | KiCad's DRC report under the vendor's rules, and every finding                |
| `manifest.json`                       | sha256 of every file and zip member, the inputs and tools (no absolute paths) |
| `<name>-<vendor>-bundle.zip`          | everything above, for records or a release                                    |

| Vendor   | Gerbers                                                                     | Drill                                                      | Extra               |
| -------- | --------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------- |
| OSH Park | `.GTL .GBL .G2L .G3L .GTS .GBS .GTO .GBO .GKO` (6 layers: also `.G4L .G5L`) | `.XLN`: inches, decimal, absolute, PTH and NPTH merged     |                     |
| JLCPCB   | KiCad's Protel names, paste layers included                                 | `-PTH.drl`, `-NPTH.drl`: mm, decimal, absolute, drill maps | BOM and CPL         |
| PCBWay   | KiCad's own names (as PCBWay's KiCad plugin sends them)                     | `-PTH.drl`, `-NPTH.drl`: inches, decimal, absolute         | IPC-D-356, BOM, CPL |

Every gerber's X2 `FileFunction` is read back and must match its layer before it is renamed.
The bundle is reproducible: kicad-cli runs with `TZ=UTC`, its time stamps are rewritten to
`--source-date-epoch` (or `SOURCE_DATE_EPOCH`, else 1980-01-01), and the zips are stored with
sorted entries and fixed times, so the same board, rules and tools give the same bytes. Re-running
`build` on an unchanged board runs no KiCad at all.

**Assembly** (`--assembly`): parts come from the footprints' fields (`LCSC`, `lcsc_id`,
`Partnumber`, `Manufacturer`); DNP parts and RF footprints are left out; `--consign` lists parts
the vendor does not place; with `--parts-lock`, every LCSC id must be locked. The JLCPCB CPL
follows Fabrication Toolkit's conversion: each part's centre (its pads' bounding box, JLCPCB's
"Mid X/Mid Y") in the gerbers' coordinates, and bottom-side rotation `180 - r`. Footprints
excluded from position files stay in the BOM but not in the CPL. yapnr carries no vendor
rotation-correction table, so the card lists every part to check in JLC's placement preview.

**Preview**: `yapnr fab preview <gerber zip> [--out DIR]` reads the zip's gerbers and drill files
back and renders them to SVG without KiCad: each layer, and a top and a bottom composite (both
seen from the top). It shows what you are about to upload; the vendor's own preview remains the
check before paying.

## `yapnr order stage`

```sh
yapnr order stage BOARD --vendor V [build options]      # check, build, card, page
yapnr order stage --bundle DIR --vendor V               # a built bundle (hashes re-checked)
    [--qty N] [--service S] [--via manual|import-url --url URL [--verify-url]]
    [--dry-run] [--print-only] [--yes] [--reveal] [--json]
yapnr order vendors [--json]
```

1. **Validate and build** (or re-check a bundle's manifest hashes).
2. **The order card**, with this run's quantity, service and mechanism, printed and written as
   `order-card.staged.md` and `.json`.
3. **Stage**: `Open <url> in your browser? [y/N]`. `--yes` answers yes for opening the page and
   nothing else; `--print-only` prints the page and the file; `--reveal` also shows the zip in the
   file manager. Without a terminal, nothing waits: the page is printed.
4. **Log** one line to `staged.jsonl`: time, vendor, mechanism, page, the zip's sha256 and whether
   a page was opened.

An OSH Park card for the ladder's case 08 routed under `oshpark-4l` (`--dry-run`):

```text
ORDER CARD  staging only: yapnr uploads nothing, orders nothing, pays nothing
vendor      OSH Park (US), 4 Layer service
profile     oshpark-4l: OSH Park 4 Layer (1.6 mm FR408-HR, 1 oz / 0.5 oz, ENIG, purple)
board       08-chaser-20-plane   42.00 x 32.00 mm   2.083 sq in   input id bd196a7ea06c
layers      4: F.Cu, In1.Cu, In2.Cu, B.Cu   thickness 1.6 mm
stackup     oshpark-4l-fr408hr: Cu 43.2 um / FR408HR 2113 0.1999 mm Dk 3.61 @ 1 GHz / ...
CHECKOUT    Keep the standard FR408-HR substrate. Do not pick the EM528 alternate offered at
            checkout since 2026-09-21 when the board has RF structures designed for FR408-HR.
finish/mask ENIG / purple (fixed)   via covering: tented
copper      1 oz outer, 0.5 oz inner
impedance   not controlled (OSH Park has no impedance service); nominal stackup only
qty         3 (multiples of 3)
estimate    $20.83 (2.083 sq in x $10 per set of 3; https://docs.oshpark.com/services/ 2026-10-02)
checks      oshpark-4l: DRC 0 errors, 0 warnings; fab check 0 errors, 0 warnings, 1 notes
file        08-chaser-20-plane-oshpark-4l/08-chaser-20-plane-oshpark-gerbers.zip
            sha256 a282dbbf0d4754f6...  (10 files, 96 kB)
page        https://oshpark.com/
next        1. Drop 08-chaser-20-plane-oshpark-gerbers.zip on the page (or 'Browse for files').
            2. Check every layer in the preview and approve it.
            3. Choose the service and options shown on this card, add to cart and pay there.
DRY RUN: would open https://oshpark.com/
Nothing was opened, nothing was uploaded and no network connection was made.
```

JLCPCB and PCBWay cards also name the stackup to choose (JLCPCB's "Specify Stackup") and the
marking option (JLCPCB's "Mark on PCB", PCBWay's "Remove product No."), and PCBWay's card gives
any remark for "Other special request".

The estimate is printed only where the price is a public formula (OSH Park); elsewhere the card
says "see the vendor's quote".

**Dry run**: `--dry-run`, or forced whenever `CI` or `YAPNR_ORDER_DRY_RUN=1` is set, does steps 1
and 2 and prints what step 3 would do. It opens no browser, makes no network connection and
writes no log. **Agents run `order stage` only with `--dry-run`** ([AGENTS.md](../AGENTS.md)).

**One click at OSH Park**: OSH Park imports a public zip from a link,
`https://oshpark.com/import?url=<zip>`. Publish the gerber zip first (for example as a release
asset), then `--via import-url --url <zip URL>`; `--verify-url` downloads it once (read-only) and
checks that it is this bundle's zip. yapnr itself uploads nothing. Publishing is your decision
(design decision D2): anyone with the link can order the same board.

**Network**: `fab` and `order stage` open no network connection. Opening a page hands its URL to
your browser. The only exception is `--verify-url`, a GET of your own published zip (https only,
redirects only to https, a size limit and a timeout).

## RF boards: pin the substrate

An RF structure from `yapnr.rf` is designed for one substrate (Dk and thickness under the line).
Its footprint's description names them, and `FAB-RF` stops a build whose stackup differs by more
than 1 %; such boards also need `--stackup` named explicitly. The vendors differ a lot:

| Stackup (`yapnr fab show ID`) | Under F.Cu                          | Nominal 50 ohm width |
| ----------------------------- | ----------------------------------- | -------------------: |
| `oshpark-4l-fr408hr`          | 0.1999 mm FR408HR 2113, Dk 3.61     |              0.44 mm |
| `oshpark-4l-em528`            | 0.1948 mm EM528, Dk 3.76            |              0.42 mm |
| `oshpark-2l-fr4`              | about 1.53 mm FR-4, Dk 4.5 (10 MHz) |              2.88 mm |
| `jlc04161h-7628`              | 0.2104 mm 7628, Dk 4.4              |              0.40 mm |
| `pcbway-ro4003c-0.813`        | 0.813 mm RO4003C, design Dk 3.55    |              1.82 mm |

(Hammerstad-Jensen, zero-thickness strip, no mask; the 2D solver and test coupons set the real
numbers.) A line designed for OSH Park's FR408HR, built on the EM528 alternate, comes out at
48.3 ohm with a 1.9 % shorter wavelength, so the card says which substrate to keep at checkout.
OSH Park offers no impedance control: its stackup is nominal. `yapnr.fab.stackups` gives the
views the RF and coupon work use: `load(id).microstrip("F.Cu").rf_stackup_spec(f_ref_ghz)`
(refusing an unpublished Df unless `use_prior=True`), `cross_section()` and `rf_rules(profile)`.

## Sources

The vendor facts were read on 2026-10-02 (the JLCPCB and PCBWay capability rows on 2026-09-26)
from the pages below; `yapnr fab show --sources` prints the full list with dates. Vendors change
their pages and prices: re-verify before relying on a value, and `FAB-SOURCES` warns once a
source is older than 180 days.

- OSH Park: [upload page](https://oshpark.com/), [services](https://docs.oshpark.com/services/),
  [2 layer](https://docs.oshpark.com/services/two-layer/),
  [4 layer](https://docs.oshpark.com/services/four-layer/),
  [6 layer](https://docs.oshpark.com/services/six-layer/),
  [drill specs](https://docs.oshpark.com/submitting-orders/drill-specs/),
  [naming](https://docs.oshpark.com/troubleshooting/naming-pattern/),
  [KiCad gerbers](https://docs.oshpark.com/design-tools/kicad/generating-kicad-gerbers/),
  [import by URL](https://docs.oshpark.com/tips+tricks/api/),
  [EM528 alternate](https://docs.oshpark.com/troubleshooting/alternate-4-layer-stackup/).
- JLCPCB: [quote](https://cart.jlcpcb.com/quote),
  [capabilities](https://jlcpcb.com/capabilities/pcb-capabilities),
  [impedance and stackups](https://jlcpcb.com/impedance),
  [KiCad gerbers](https://jlcpcb.com/help/article/how-to-generate-gerber-and-drill-files-in-kicad-8),
  [BOM and CPL](https://jlcpcb.com/help/article/how-to-generate-the-bom-and-centroid-file-from-kicad).
- PCBWay: [quote](https://www.pcbway.com/orderonline.aspx),
  [assembly quote](https://www.pcbway.com/quotesmt.aspx),
  [capabilities](https://www.pcbway.com/capabilities.html),
  [laminated structure](https://www.pcbway.com/multi-layer-laminated-structure.html),
  [high frequency](https://www.pcbway.com/pcb_prototype/What_is_High_Frequency__HF__PCB_.html),
  [KiCad plugin](https://github.com/pcbway/PCBWay-Plug-in-for-Kicad) (file set and names).
- Conversions: [Fabrication Toolkit](https://github.com/bennymeg/Fabrication-Toolkit) (CPL
  rotation), [KiKit](https://github.com/yaqwsx/KiKit) (PCBWay BOM columns). Facts only; no code
  copied.
