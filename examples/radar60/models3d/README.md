# radar60 3D models

STEP models for the radar60 parts KiCad's stock library has no 3D model for: `U1` (TI
IWR6843AQGABLR, package ABL0161B FCBGA-161), `U2` (TI LP87524JRNFRQ1, RNF0026C VQFN-HR-26), `U5`
(TI TPS259474ARPWR, RPW0010A VQFN-HR-10) and `J2` (Samtec QTH-030-01-L-D-A). Built with
[CadQuery](https://cadquery.readthedocs.io/) by `gen_models3d.py`, which imports
`../schematic/tools/footprints.py` and reads each part's own pad list back out of it, so the
model's leads, balls and contacts always sit exactly on that footprint's pads.

**These are approximations, not vendor models.** Each is a body block (chamfered at the pin-1
corner, this codebase's convention throughout `footprints.py`) with a lead, ball or contact at
every pad: body size, height and ball/lead count come from the datasheet drawing cited in the
matching function's docstring in `footprints.py`, but there is no internal lead frame, no real
ball dome profile, and the `J2` housing is a plain box with one contact-strip per row standing in
for 60 individual springs. No TI, Samtec, SnapEDA, Ultra Librarian or EasyEDA file was read to
make them, and none is committed here -- the geometry is ours (datasheet dimensions in, CadQuery
code, public domain by construction), which is why it can sit in this AGPL repository.

If you have an exact vendor STEP (from TI's or Samtec's own CAD download, or a SnapEDA/Ultra
Librarian account), don't commit it: put it in your own [part cache](../../../docs/part-cache.md)
instead, as that part's 3D model file (`yapnr part-cache import-parts`, or materialize the part
first and drop the `.step` in beside its `.kicad_mod`/`.kicad_sym` before importing). Mark it
`--local-only` if the vendor's terms don't allow redistributing it, and it stays on your machine
-- the part cache's clients never upload a local-only part and a `serve --public` cache refuses to
start on one.

## Rebuilding

```sh
python3 -m venv /path/to/venv && /path/to/venv/bin/pip install cadquery
/path/to/venv/bin/python gen_models3d.py              # writes *.step next to this file
```

CadQuery is not a yapnr dependency (nothing else here needs it); keep its venv out of the repo.

## How the footprints find them

`footprints.py`'s four generators that build one of these models (`abl0161b`, `rnf0026c`,
`rpw0010a`, `qth030_01_a`) end with `fp.model("${RADAR60_3D}/<name>.step")`. `RADAR60_3D` is a
plain environment variable (the same convention as KiCad's own `KICAD10_3DMODEL_DIR`, and as this
project's `YAPNR_KICAD_FOOTPRINTS`, `gen_parts.py:stock_dir`) naming this directory -- not a
`${KIPRJMOD}`-relative path, because the boards these footprints end up on get copied through
out-of-tree work directories (`examples/radar60/board/integrate.py`) at depths the project file
can't predict. `integrate.py`'s own `kicad-cli`/`pcbnew` calls set it automatically
(`_env`); for a one-off `kicad-cli pcb render`/`export` of a board copied elsewhere, either
`export RADAR60_3D=/path/to/examples/radar60/models3d` first or pass
`-D RADAR60_3D=/path/to/examples/radar60/models3d`.

Two more parts got a working model without any CadQuery here, just a corrected reference to a
model KiCad's stock library already ships:

- `R_SH1`'s generated footprint (`wfcp0612`, Vishay WFCP0612 current-shunt resistor) points at
  the stock `Resistor_SMD.3dshapes/R_0612_1632Metric.step` -- same 1.6x3.2 mm body, same two pads
  on the long sides as this footprint (its 1x3.4 mm pads bracket our 1.30x3.80 mm lands).
  `R_Shunt_Vishay_WSKW0612.step` was checked and rejected: a 4-pad Kelvin part with no geometric
  match.
- `RT1`/`RT2` (the RF macro's 0201 dummy loads, `examples/radar60/rf/rfmacro/kicad.py:
load_footprint`) point at the stock `Resistor_SMD.3dshapes/R_0201_0603Metric.step`, rotated 90
  degrees to match the footprint's own 90-degree pad rotation.

And two stock footprints had a `(model ...)` path that 404s even in KiCad's own library (checked
on the local headless KiCad 10.0.6: the `.kicad_mod` names a `.step` its own `3dmodels` directory
does not ship), fixed in `gen_parts.py:STOCK_MODEL_FIXUPS` to the closest model the library does
have, rather than by hand-editing a board:

- `U3` (Macronix MX25V1635FZNQ, `Package_SON:WSON-8-1EP_6x5mm_P1.27mm_EP3.4x4mm`) now points at
  `WSON-8-1EP_6x5mm_P1.27mm_EP3.4x4.3mm.step` (0.3 mm longer exposed-pad model than Macronix's own
  drawing; the only other 6x5mm/1.27mm WSON-8 model in the library).
- `U4` (TI TCAN1044AVDRBRQ1, `Package_DFN_QFN:Texas_DRB0008A`) now points at
  `DFN-8-1EP_3x3mm_P0.65mm_EP1.5x2.25mm.step` (matching pitch and EP width; TI's own DRB0008A EP
  height was not independently re-verified this pass -- this session's web-search budget ran out
  first. Worth a follow-up check against TI SLLSFJ3D's own package drawing).

Parts needing no model at all (test pads, Kelvin pads, fiducials, mounting holes, radome lands,
and the `RFM1` RF-macro placeholder) are unchanged.
