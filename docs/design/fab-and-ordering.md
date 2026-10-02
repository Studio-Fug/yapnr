# Design: fab outputs and staged ordering (`yapnr fab`, `yapnr order stage`)

Status: **F2, F1 and O1 built** (2026-10-02; user guide [Fab bundles and staged
orders](../fab-and-ordering.md), differences in §14). O2 and O3 wait for the owner's decisions D1
and D3. The sections below are the design as proposed; §1 describes the state before it. It
covers the fab outputs and ordering items of the end-to-end plan (F1, F2, O1 to O3, named in
`WORKLOG.md`). The owner's request was:

> where are we at with one-click order on JLC and PCBWay? Please also include a US vendor like
> OSHPark--I want to start sending out some boards ASAP.

The first boards are RF demo boards (microstrip structures from `yapnr.rf` topology optimization)
and fab-model test coupons, probably at OSH Park. Assembled boards would go to JLCPCB or PCBWay
later.

Vendor facts come from public pages read on 2026-10-02 (§13). Nothing was uploaded to any vendor
and no vendor API was called while writing this design. The headless kicad-cli 10.0.6 was run on a
test board in the repository, and those runs are labelled "measured". Numbers marked "derived" are
choices made in this design and need the owner's review (§12).

## 1. Where ordering stands

- **No ordering code exists in yapnr.** No command writes a vendor bundle, BOM, CPL or zip.
- **`hardware/pnr/pnr/fab_profile.py` is the only fab code.** It has the default `jlc-pofv`
  profile (JLCPCB 4-layer JLC04161H-7628 with epoxy-filled, capped vias) and `legacy`. It has no
  OSH Park or PCBWay profile and no stackup data.
- **The plan items were named but never defined.** `WORKLOG.md` lists F1, F2 and O1 to O3 after
  the atopile, spec and agent items, without definitions. §2 defines them here.
- **The owner wants boards out quickly.** This design therefore puts OSH Park first: bare boards
  with no assembly, which is all the RF demos and coupons need.

The closest each vendor can get to one click, under the staging-only rule:

| Vendor            | Best mechanism                                                                                                                                                                                     | What the human does                                       | Plan item | Needs                                                                                        |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | --------- | -------------------------------------------------------------------------------------------- |
| **OSH Park (US)** | **Public boards:** an import link, `https://oshpark.com/import?url=<public zip>` [O-api]. OSH Park fetches the zip itself, so yapnr uploads nothing. This is one click for anyone, owner included. | Clicks the link, reviews the preview, picks options, pays | O1        | A public URL for the zip, for example a docs-site or release asset. Owner decision D2 (§12). |
| OSH Park (US)     | **Private boards:** prints the card, then opens oshpark.com.                                                                                                                                       | Drags the zip onto the page                               | O1        | Nothing                                                                                      |
| OSH Park (US)     | **Opt-in upload:** a POST to the documented anonymous endpoint `/import.json`, then opens the returned page [O-api].                                                                               | Confirms the upload at the prompt, reviews, pays          | O2        | Owner decision D1                                                                            |
| **PCBWay**        | Prints the card, then opens the quote page [P-order] [P-smt].                                                                                                                                      | Uploads the zip and fills in options from the card        | O1        | Nothing                                                                                      |
| PCBWay            | The partner API: quote calls and "Add to Cart" only [P-api].                                                                                                                                       | Pays in the PCBWay web account                            | O3        | Partner access (application) and decision D3                                                 |
| **JLCPCB**        | Prints the card, then opens `cart.jlcpcb.com/quote` [J-quote].                                                                                                                                     | Uploads the zip, picks the named stackup, adds BOM/CPL    | O1        | Nothing                                                                                      |
| JLCPCB            | The official API, quotes only [J-api].                                                                                                                                                             | As above                                                  | O3        | API access (application)                                                                     |

PCBWay's own KiCad plugin uploads anonymously to an endpoint it does not document as an API
[P-plug]. This design does not use that endpoint unless the owner decides otherwise (D1).

## 2. Plan items

| Item   | Scope                                                                                                                                                                                                                                                                                                         | Command                         |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| **F1** | Per-vendor fab bundles from a routed `.kicad_pcb`: gerbers with vendor layer names, drill files, IPC-D-356 where useful, BOM and CPL with LCSC numbers, stackup and impedance notes, a fab README, deterministic zips, a manifest with hashes, and provenance.                                                | `yapnr fab build`               |
| **F2** | Vendor capability profiles and stackups as data, `fab_profile.py` extended to use them, and `yapnr fab check` (native KiCad DRC under the vendor's rules, plus the checks KiCad cannot express). Also the stackup adapters for `yapnr.rf` and the coupon work.                                                | `yapnr fab profiles/show/check` |
| **O1** | `yapnr order stage` with no network access. It validates, builds or verifies the bundle, prints the order card and asks for confirmation, then opens or prints the vendor's upload or quote page and names the exact file to drop. Includes OSH Park import links for public zips, and a dry-run mode for CI. | `yapnr order stage`             |
| **O2** | Opt-in uploads through a documented vendor mechanism that never orders or pays. Today that is only OSH Park's `/import.json`. The human opts in on each run, and at an interactive prompt.                                                                                                                    | `yapnr order stage --upload`    |
| **O3** | Vendor APIs, where access has been granted. Calls go through an allowlist of quote and add-to-cart endpoints; payment, confirmation and balance endpoints cannot be reached. Credentials come from the environment or the OS keychain at run time.                                                            | `yapnr order stage --api`       |

### 2.1 Delivery order

Each step is one pull request. Each is useful on its own, and none changes the engine's defaults.

1. **F2a: OSH Park profiles and stackup data.** The `oshpark-2l`, `oshpark-4l` and `oshpark-6l`
   profiles, `jlc-4l`, the stackup files, the loader, `yapnr fab profiles/show/check`, the
   `fab_profile.py` adapter and the `yapnr.rf` stackup adapter. This step is enough to route an
   RF demo or coupon under OSH Park's rules.
2. **F1a: bare-board bundles for all three vendors.** No assembly yet. Determinism, the manifest
   and the README are included.
3. **O1a: `yapnr order stage` for bare boards.** Manual staging for all three vendors, OSH Park
   import links, the order card and `--dry-run`. **After this step the first OSH Park order can be
   placed.**
4. **F1b: assembly outputs.** BOM and CPL for JLCPCB and PCBWay, and per-vendor placement
   corrections in the part cache.
5. **O1b: assembly staging.** The card's assembly section, and the BOM and CPL steps on the
   vendor pages.
6. **O2: the OSH Park opt-in upload.** Lands only after decision D1.
7. **O3: vendor APIs.** Lands only after access is granted and decision D3 is made.

Later, the Bazel rule `yapnr_fab` (migration plan §4.3, PR8) wraps `yapnr fab build`. The project
manifest also gains a `[fab]` table (PR3d, §6.1).

**Not in scope:**

- **Panelization.** KiKit does it; `fab check` only checks OSH Park's panel rules.
- **Other vendors:** Aisler and Eurocircuits in the EU, and the US shops in §13. The vendor data
  format allows them later.
- **Stencil-only orders, price comparison across vendors, and a "stage order" button in the
  viewer.**

## 3. Rules that bind every part of this design

1. **Staging only.** yapnr prepares files and takes the human to the vendor's upload, quote or cart
   page. It never pays, never confirms or submits an order, never stores payment data, never logs
   in, never creates an account and never accepts terms for anyone. The owner decided this
   (`docs/decisions.md`: "the human pays on the vendor's page").
2. **No network by default.** `fab check`, `fab build` and `order stage` (O1) open no network
   connection. Opening a browser hands a URL to the human's browser; yapnr does not fetch it. The
   only exceptions are the opt-in paths of O2 and O3, and `--verify-url` (§8.3), a read-only GET of
   our own published zip.
3. **Opt-in on each run, by a human.** O2 and O3 calls need a flag on that run plus a confirmation
   typed at an interactive terminal. No config file, environment variable or `--yes` replaces the
   typed confirmation. Agents run `order stage` only with `--dry-run`; this rule goes into
   `AGENTS.md`.
4. **Credentials.** A vendor token (O3) is read at run time from an environment variable or the OS
   keychain. It is never written to disk, logs, manifests, cards, exceptions or the command line.
5. **Native KiCad DRC is the judge.** `fab check` runs `kicad-cli pcb drc` under the vendor's
   rules. It never relaxes or suppresses a finding.
6. **Headless KiCad only, every subprocess time-bounded.** kicad-cli is found the way the atopile
   frontend finds it (`yapnr.frontends.atopile.kicad.find_cli`, which refuses GUI bundles). Later
   `yapnr.kicad.toolchain` (PR6a) takes over.
7. **Public repository.** Vendor facts are committed only as data with a source and an access date.
   No vendor-API data and no part data are committed (the part-cache rule). Bundles carry no
   absolute paths and no local timezone.

## 4. Vendor facts that shape the design

### 4.1 Files each vendor takes

| Vendor   | Gerbers                                                                                                                                                                                                                                                                                                                                                                     | Drill                                                                                                                                                                                                                                                                                              | Assembly                                                                                                                                                                                                                                                                                                      |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OSH Park | **Zip of gerbers.** Documented names: `.GTL .GBL .G2L .G3L .GTO .GBO .GTS .GBS .GKO`, not case-sensitive [O-name]. X2 is recommended, and the layers are F/B Cu, In1 to In4, silk, mask and Edge.Cuts; paste is optional [O-kgerb]. OSH Park also reads `.kicad_pcb` with "KiCad 9.x" and does not refill zones [O-kicad]. **Our boards are KiCad 10, so we send gerbers.** | `.XLN` [O-name]. One page asks for "INCHES, 2:4, zero suppression None or Leading" [O-drill]; the KiCad page asks for the alternate drill mode, absolute origin, "inches and millimeters work", and prefers decimal [O-kgerb]. Holes up to 6.604 mm are drilled; larger ones are milled [O-drill]. | None (no assembly service)                                                                                                                                                                                                                                                                                    |
| JLCPCB   | Zip or rar, up to 100 MB [J-quote]. Layers F/B Cu, inner, paste, silk and mask, plus Edge.Cuts. Check zone fills, tent vias, **Protel extensions**, and subtract mask from silk [J-kicad].                                                                                                                                                                                  | mm, decimal, absolute, alternate oval mode, optional map [J-kicad]                                                                                                                                                                                                                                 | BOM: `Comment, Designator, Footprint, JLCPCB Part #` [J-bomkicad] (csv/xls/xlsx [J-bom]). CPL: `Designator, Mid X, Mid Y, Layer, Rotation`, in mm, counter-clockwise positive, Top/Bottom [J-cpl]. Rotation must match JLC's own footprint orientation; the open tools keep correction tables [FT] [JT] [KK]. |
| PCBWay   | Zip of gerbers, IPC-D-356, BOM and pick-and-place. This is what PCBWay's own plugin sends [P-plug].                                                                                                                                                                                                                                                                         | Excellon in the zip                                                                                                                                                                                                                                                                                | BOM with manufacturer part numbers; KiKit's column set is `Item #, Designator, Qty, Manufacturer, Mfg Part #, Description / Value, Footprint, Type, Your Instructions / Notes` [KK]; plus a centroid file [P-asm]                                                                                             |

### 4.2 What kicad-cli 10.0.6 does (measured)

These runs used the repository's `splanc_dev` test board and a 6-layer copy of it.

- **Two runs three seconds apart differ only in timestamps.** Each gerber has two (`%TF.CreationDate`
  and `G04 Created by KiCad ... date`), the drill file two, and `.gbrjob` one. The IPC-D-356 and the
  position CSV are byte-identical. `%TF.ProjectId` is derived from the file name.
- **kicad-cli ignores `SOURCE_DATE_EPOCH`.** The timestamps carry the local UTC offset; `TZ=UTC`
  makes it `+00:00`. yapnr therefore runs kicad-cli with `TZ=UTC` and rewrites those lines (§6.4).
- **Protel extensions (the default):** `F.Cu .gtl`, `B.Cu .gbl`, `In1.Cu .g1` to `In4.Cu .g4`,
  `Edge.Cuts .gm1`, `.gto/.gbo`, `.gts/.gbs`, `.gtp/.gbp`. The X2 `FileFunction` names the copper
  layer as `L1`..`L6`.
- **Drill header.** `-u in --excellon-zeros-format suppressleading` writes
  `FORMAT={2:4/ absolute / inch / suppress leading zeros}`. PTH and NPTH are merged
  (`MixedPlating`) unless `--excellon-separate-th` is given.
- **`kicad-cli pcb export stats --format json`** gives what the checks need without `pcbnew`:

  - the outline, its size and area;
  - via counts by type (through, blind, buried, micro);
  - pad counts, castellated pads included;
  - a drill table (shape, size, plated, source, layer span);
  - minimum track, clearance and drill;
  - board thickness.

  It does not give the copper layer count; the `.gbrjob` file and the board's `(layers ...)`
  section do.

### 4.3 Capabilities

| Rule                  | OSH 2L [O-2l]                                     | OSH 4L [O-4l]                             | OSH 6L [O-6l]              | JLC 4L (`jlc-4l`, from `jlc-pofv`'s 5A rows) | PCBWay std 4L [P-cap]                    |
| --------------------- | ------------------------------------------------- | ----------------------------------------- | -------------------------- | -------------------------------------------- | ---------------------------------------- |
| Trace/space           | 6/6 mil                                           | 5/5 mil                                   | 5/5 mil                    | 0.127/0.127 mm (abs. 0.09 [J-cap])           | 5/6 mil outer (4/4 headline)             |
| Min drill             | 10 mil [O-drill]                                  | 10 mil [O-drill]                          | 8 mil [O-drill]            | 0.20 mm (0.15 abs.)                          | 0.20 mm free (0.15 at cost)              |
| Annular ring          | 5 mil [O-drill]                                   | 4 mil [O-drill]                           | 4 mil [O-drill]            | 0.075 mm via                                 | 0.15 mm                                  |
| Copper to board edge  | 15 mil                                            | 15 mil                                    | 15 mil                     | 0.30 mm                                      | not transcribed (draft)                  |
| Hole to copper        | not published                                     | 10 mil drill to inner copper              | not published              | 0.20 via, 0.35 PTH, 0.20 NPTH                | not transcribed (draft)                  |
| Thickness             | 1.6 mm                                            | 1.6 mm                                    | 1.6 mm                     | 0.4 to 4.5 mm                                | 0.2 to 3.2 mm                            |
| Copper (out/in)       | 1 oz                                              | 1 oz plated (1.7 mil) / 0.5 oz (0.68 mil) | 1 oz / 0.5 oz              | 1 oz / 0.5 oz (15.2 µm finished)             | 1 oz / 1 oz                              |
| Finish, mask          | ENIG, purple (fixed)                              | ENIG, purple (fixed)                      | ENIG, purple               | HASL/ENIG/OSP, 7 colours                     | many                                     |
| Via fill / via-in-pad | none [O-2l]                                       | none documented                           | none documented            | POFV is an option (`jlc-pofv`)               | resin and cap, about $95–104 extra       |
| Controlled impedance  | no service                                        | no service                                | no service                 | ±10 % (±5 % on request), named stackups      | ±10 %, or ±5 Ω at 50 Ω and below [P-adv] |
| Size                  | not stated (assumed as 4L)                        | 0.25 in to 16×22 in                       | not stated (assumed as 4L) | –                                            | –                                        |
| Quantity              | multiples of 3                                    | multiples of 3                            | multiples of 3             | 5 or more                                    | 5 or more                                |
| Castellation          | "allowed, but not guaranteed" [O-cast]            | same                                      | same                       | hole ≥ 0.5 mm, board ≥ 10×10 mm              | ≥ 0.4 mm                                 |
| Panels                | 0.1 in between outlines, frame ≥ 0.2 in [O-panel] | same                                      | same                       | –                                            | –                                        |

**Boards routed for JLC do not transfer to OSH Park as they are.** `jlc-pofv`'s default via is
0.45/0.30 mm, a 0.075 mm ring. OSH Park's 4-layer minimum ring is 4 mil (0.1016 mm). Its 0.20 mm
escape and in-pad vias are also below OSH Park's 10 mil minimum drill, and OSH Park rounds smaller
holes up [O-drill]. A board for OSH Park is routed under an OSH Park profile from the start, and
`fab check` reports the mismatch otherwise.

### 4.4 Prices and lead times for a 50×50 mm board (3.875 sq in)

- **OSH Park:**
  - 2-layer: $5/sq in for 3 boards, $19.38, ships in 9–12 days.
  - 4-layer: $10/sq in, $38.75, 9–14 days. Super Swift doubles the price ($77.50) and ships in 5–6
    business days.
  - 6-layer: $15/sq in, $58.13, 12–21 days.
  - Free USPS First Class shipping in the US [O-svc] [O-2l] [O-4l] [O-6l] [O-ship].
  - **Coupons:** the coupon research's 100×120 mm 4-layer coupon board would cost $186 for 3 at
    OSH Park.
- **JLCPCB:**
  - FR-4 "from $2 / 5 pcs", and Rogers/PTFE "from $47 / 5 pcs" [J-home].
  - Economic assembly: $8.18 setup, $1.53 stencil, $3.07 for each extended part [J-pcba]. Confirm in
    the live quote.
- **PCBWay:**
  - 4-layer, 5 pcs: $31.16 (a live quote on 2026-09-26).
  - Assembly from "$29 for 1–20 pcs" [P-home].
  - Rogers RO4003C (Dk 3.38, Df 0.0027 at 10 GHz) and RO4350B (3.48 and 0.0037). These build in
    7–10 days [P-hf].
- **Import duties (not re-verified here).** US duties apply to boards shipped from China since the
  US ended its de minimis exemption in 2025. OSH Park avoids them.

The order card prints an estimate only where the price is a public formula (OSH Park). For the
other vendors it says "see the vendor's quote".

## 5. Data: profiles, stackups and vendors (F2)

### 5.1 Where the data lives

```text
yapnr/fab/data/
├── profiles/<name>.json      capability profile: engine rules, vendor limits, sources
├── stackups/<id>.json        stackup: layers, dielectrics, copper, mask, tolerances, sources
└── vendors/<vendor>.json     file sets, layer naming, drill format, BOM/CPL columns,
                              order options, staging URLs, sources
```

- **The data is JSON, not TOML.** The engine's KiCad-side workers import `fab_profile`. They run
  under KiCad's Python, which is 3.9 in the macOS headless bundle, and `tomllib` needs 3.11.
- **The loader is `yapnr/fab/capability.py`.** It is stdlib-only and works on Python 3.9 and later.
  It validates every file against its schema (`yapnr-fab-profile-v1`, `yapnr-fab-stackup-v1`,
  `yapnr-fab-vendor-v1`).
- **Every value has provenance.** It is the `src` of the object that holds it (a source key with a
  URL and access date), or `derived` with the reason, or a `*_prior` with its own `*_prior_src`
  for a stand-in value (§5.3).

**A profile** is a process capability; **a stackup** is a dielectric build. They are chosen
separately. The three OSH Park 4-layer stackups share one rule set. A profile names the stackups
it allows and its default. For example, "JLC 4-layer 7628" is `--profile jlc-4l` with
`--stackup jlc04161h-7628`, which is that profile's default.

### 5.2 Profiles

| Profile        | Status     | Vendor, service                            | Default stackup      | Other stackups                                                                                            |
| -------------- | ---------- | ------------------------------------------ | -------------------- | --------------------------------------------------------------------------------------------------------- |
| `legacy`       | unchanged  | – (the fixtures' own rules)                | –                    | –                                                                                                         |
| `jlc-pofv`     | unchanged  | JLCPCB 4L, POFV on every via               | `jlc04161h-7628`     | `jlc04161h-2116`, `-3313`, `-1080`                                                                        |
| `jlc-4l`       | new        | JLCPCB 4L standard: tented vias, no in-pad | `jlc04161h-7628`     | `jlc04161h-2116`, `-3313`, `-1080`                                                                        |
| `jlc-6l`       | new, draft | JLCPCB 6L (filled vias are standard)       | `jlc06161h-3313`     | `jlc06161h-2116c`, `jlc06161h-7628`                                                                       |
| `oshpark-2l`   | new        | OSH Park 2 Layer                           | `oshpark-2l-fr4`     | –                                                                                                         |
| `oshpark-4l`   | new        | OSH Park 4 Layer (and Super Swift)         | `oshpark-4l-fr408hr` | `oshpark-4l-em528` (alternate offered at checkout since 2026-09-21 [O-alt]), `oshpark-4l-mixed` [O-mixed] |
| `oshpark-6l`   | new        | OSH Park 6 Layer                           | `oshpark-6l-fr408hr` | –                                                                                                         |
| `pcbway-std`   | new, draft | PCBWay standard 4L FR-4                    | `pcbway-4l-7628`     | –                                                                                                         |
| `pcbway-hf-2l` | new, draft | PCBWay Rogers 2L                           | none: must be chosen | `pcbway-ro4003c-0.813`, `pcbway-ro4003c-0.508`, `pcbway-ro4003c-1.524`, `pcbway-ro4350b-0.508`            |

**Draft profiles** still have values that are published but not yet transcribed. `fab check` and
`order stage` refuse a draft profile unless `--allow-draft` is given, and the card then says
"draft profile".

**A profile file holds four blocks:**

- `engine`: the `fab` and `copper` blocks and the `qualification` text that `fab_profile.py` uses
  today, with the same keys.
- `vendor_limits`: limits KiCad DRC cannot express. These are board size, layer counts, quantity
  multiple, maximum drilled hole, via types, castellation, panel rules, fixed finish and mask, and
  whether impedance control is offered.
- `order_defaults`: what the card proposes. These are thickness, finish, colour, via covering and
  service.
- `sources`.

The OSH Park engine values (all in mm):

| Key                                                    | `oshpark-2l`                      | `oshpark-4l`                       | `oshpark-6l`                        | Provenance                                                                                                        |
| ------------------------------------------------------ | --------------------------------- | ---------------------------------- | ----------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `min_track_width_mm`, `clearance_mm`                   | 0.1524                            | 0.127                              | 0.127                               | [O-2l] [O-4l] [O-6l]                                                                                              |
| `smd_pad_clearance_mm`                                 | 0.1524                            | 0.127                              | 0.127                               | derived: equal to the clearance (OSH Park publishes no separate pad rule)                                         |
| `edge_clearance_mm`                                    | 0.381                             | 0.381                              | 0.381                               | "15 mil edge keepout" [O-2l] [O-4l] [O-6l]                                                                        |
| `hole_clearance_mm`, `pth_hole_…`, `npth_hole_…`       | 0.254                             | 0.254                              | 0.254                               | 4L: "10 mil drill-to-internal-copper" [O-4l]; 2L and 6L derived from it                                           |
| `hole_to_hole_mm` / `pth_hole_to_hole_mm`              | 0.254 / 0.50                      | 0.254 / 0.50                       | 0.254 / 0.50                        | derived: not published; 0.50 copies JLC's stricter PTH value                                                      |
| `hole_to_edge_mm`                                      | 0.50                              | 0.50                               | 0.50                                | derived: not published (the JLC/PCBWay value)                                                                     |
| `min_through_drill_mm`                                 | 0.254                             | 0.254                              | 0.2032                              | [O-drill]                                                                                                         |
| `via_annular_mm`                                       | 0.127                             | 0.1016                             | 0.1016                              | [O-drill]                                                                                                         |
| `min_via_diameter_mm`                                  | 0.508                             | 0.4572                             | 0.4064                              | drill + 2 × ring                                                                                                  |
| `min_npth_drill_mm`                                    | 0.254                             | 0.254                              | 0.2032                              | derived: OSH Park publishes one minimum for all holes                                                             |
| `via_to_smd_pad_mm`                                    | 0.127                             | 0.127                              | 0.127                               | derived: no filled vias at OSH Park [O-2l], so no via goes in a pad                                               |
| `component_pth_min_drill_mm`                           | 0.30                              | 0.30                               | 0.30                                | derived: a classification threshold, as in `jlc-pofv`                                                             |
| via classes `default` / `escape` / `power` (dia/drill) | 0.60/0.30, 0.508/0.254, 0.70/0.40 | 0.55/0.30, 0.4572/0.254, 0.70/0.40 | 0.55/0.30, 0.4064/0.2032, 0.70/0.40 | derived; `escape` sits exactly at the minimum ring; there is no `in_pad` class                                    |
| copper: outer / inner / via plating (µm)               | 35 / – / 25.4                     | 35 / 17.3 / 25.4                   | 35 / 17.3 / 25.4                    | outer: nominal 1 oz, stricter than the published 1.7 mil plated; inner: 0.68 mil [O-4l]; plating: 1 mil [O-drill] |

The `jlc-4l` values are `jlc-pofv`'s 5A rows without the 5B filled-via rows: no
`filled_via_hole_to_hole_mm` and no `in_pad` class.

### 5.3 Stackups

A stackup file lists every layer from top to bottom:

- **Mask:** thickness, εr.
- **Copper:** KiCad layer name, finished thickness, nominal weight, plated or not, finish.
- **Dielectric:** prepreg or core, material and glass style, thickness, εr with the frequency it was
  quoted at, and Df.

It also gives the board thickness and tolerances, the vendor's own name for the stackup, and the
instruction the human must follow at checkout. Appendix A has the data for every stackup in §5.2.

**Unpublished values stay `null`.** A `prior` may sit beside a `null` value, with its source. For
example, OSH Park gives no Df for the 4-layer 2113 prepreg, and its 6-layer page gives 0.009 for a
2113 core. A consumer must ask for the prior explicitly, and the prior is recorded wherever it is
used.

The views:

```python
from yapnr.fab import stackups

s = stackups.load("oshpark-4l-fr408hr")
m = s.microstrip("F.Cu")             # reference: the next copper layer (In1.Cu)
m.h_mm, m.er, m.er_freq_hz, m.t_um   # 0.1999, 3.61, 1e9, 43.2
m.tan_delta                          # None (not published); m.tan_delta_prior == 0.009
spec = m.rf_stackup_spec(f_ref_ghz=10.0, use_prior=True)
# -> {"er": 3.61, "tan_delta": 0.009, "h_mm": 0.1999, "f_ref_ghz": 10.0}  (yapnr.rf StackupSpec)
rules = stackups.rf_rules("oshpark-4l")    # -> {"min_width_mm": 0.127, "min_space_mm": 0.127}
xs = s.cross_section(signal="In2.Cu")      # every layer, for the 2D solver (coupons, FEA)
```

- **For `yapnr.rf`:** `StackupSpec` is a uniform single substrate (εr, tan δ, h, f_ref).
  `microstrip()` refuses a reference plane more than one dielectric away. Its `f_ref` is the
  design's reference frequency (copper sheet conductance, loss matching). It is not the frequency
  the vendor quoted Dk at. That frequency travels separately and is written into the RF spec's
  provenance. `yapnr.rf.cases` gains `--stackup <id>[:<layer>]` and `--fab-profile <name>`, which
  set `StackupSpec` and `Rules(min_width_mm, min_space_mm)`.
- **For the coupon work:** `cross_section()` gives the full layer list with priors and tolerances.
  This is the nominal model that the coupon fit starts from. A measured overlay
  (`yapnr-fab-stackup-measured-v1`) names its base stackup, the order or lot, the fitted values
  with their covariance, and the method. `stackups.load(id, measured=path)` applies it. The coupon
  work defines the fit; this design only reserves the format.
- **Board stackup comparison:** `fab check` compares the board's own KiCad stackup (from `.gbrjob`
  `MaterialStackup` and the board's `(stackup ...)` section) with the chosen stackup. A difference
  in layer count is an error; differences in thickness or εr are warnings.

### 5.4 Changes to `fab_profile.py`

- **Built-in profiles stay byte-identical.** `legacy` and `jlc-pofv` stay Python literals.
  `apply_rules`, `dru_text`, `board_constraints` and `geometry` must give byte-identical output
  for both; a golden test pins this.
- **Other names resolve lazily.** For a name that is not built in, `PROFILES` resolves through
  `yapnr.fab.capability.engine_profile(name)`. KiCad-side workers get `yapnr/__init__.py`,
  `yapnr/fab/{__init__,capability}.py` and `yapnr/fab/data/` staged next to `pnr_kicad_srcs`. This
  is the pure-Python import path PR6a plans. When PR3 moves the engine to `yapnr/core`, the import
  becomes direct.
- **`dru_text` becomes data-driven.**
  - Each rule is written only when the profile has its key. Today a missing
    `filled_via_hole_to_hole_mm` raises.
  - The rule comments cite the profile's own sources instead of the "5A/5B" rows that `jlc-pofv`
    cites.
  - `jlc-pofv` keeps its exact text.
- **The default does not change.** It stays `jlc-pofv` (`PNR_FAB_PROFILE` unset), so the engine
  behaves as before. Selecting a new profile is an explicit choice: the environment variable, the
  manifest's `fab_profile`, or `--fab-profile`. No A/B is needed, because no default changes. The
  first ladder run under `oshpark-4l` is still recorded in the commit body as evidence that the
  profile routes.

## 6. `yapnr fab build` (F1)

### 6.1 Command

```sh
yapnr fab build BOARD.kicad_pcb --vendor {oshpark,jlcpcb,pcbway} \
    [--profile P] [--stackup S] [--qty N] [--assembly | --no-assembly] \
    [--parts-lock yapnr-parts.lock.json] [--cache <part cache>] \
    [--out DIR] [--source-date-epoch T] [--public] [--json]
```

- **Profile.** `--profile` defaults to the vendor's profile for the board's copper layer count:
  - OSH Park: 2, 4 or 6 layers map to `oshpark-2l`, `-4l`, `-6l`.
  - JLCPCB: `jlc-pofv` when the board has in-pad vias, else `jlc-4l`; `jlc-6l` for 6 layers.
  - PCBWay: `pcbway-std`.
- **Stackup.** `--stackup` defaults to the profile's default. It is required when the board carries
  RF footprints (§6.6).
- **`.kicad_pro`.** The board's `.kicad_pro` must sit beside it; it carries the netclasses that DRC
  needs. `--no-project` is an override and gives a warning.
- **Check first.** `build` always runs `fab check` first (§7.1), and an error stops it.
- **From a project (PR3d).** `yapnr.toml` keeps `fab_profile` under `[rules]` (the rules the board
  is routed by) and gains a `[fab]` table with `vendor`, `stackup`, `qty` and `options` (the card's
  choices, such as `service = "super-swift"`). `yapnr fab build --run <run-id>` then takes the
  run's best candidate from the store. Until PR3d, the flags above do the same.

### 6.2 Pipeline

1. **Copy the board to scratch.** The copy goes into `<out>/.work/` under a stable name,
   `<board>.kicad_pcb`, with its `.kicad_pro`. The vendor profile's rules are written into the
   copy (§7.1).
2. **DRC and zone refill.** `kicad-cli pcb drc` runs on the scratch copy with `--refill-zones`,
   `--save-board`, `--format json` and `--severity-all`. The DRC report goes into the bundle. The
   export then uses that refilled copy, so the files are exactly what DRC judged, with zones filled
   under the vendor's clearances.
3. **Board facts.** `kicad-cli pcb export stats --format json`, plus the `(layers ...)`,
   `(stackup ...)` and footprint fields of the board file. A small stdlib S-expression reader
   handles the board file; it needs no `pcbnew`.
4. **Exports,** all with `TZ=UTC` and a timeout:
   - `pcb export gerbers --subtract-soldermask --layers <vendor set>`, with X2 on;
   - `pcb export drill` with the vendor's format (§6.3);
   - `pcb export ipcd356` (PCBWay);
   - `pcb export pos --format csv --units mm --exclude-dnp` (assembly only).
5. **Normalize.** §6.4.
6. **Rename** to the vendor's layer names (§6.3), using a map checked against each file's X2
   `FileFunction`.
7. **Assembly outputs** (§6.5).
8. **Write the README, the order card data, the manifest and the zips** (§6.7).

Each step's result is cached under `.work/` by the sha256 of its inputs. The board, the rules,
the kicad-cli version and the yapnr version all count as inputs. Re-running `build` with an
unchanged board skips KiCad entirely.

### 6.3 Per-vendor file sets

| Vendor   | Layers exported                                          | Names                                                                                                                                                                     | Drill                                                                                    | Extra                                                   |
| -------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| OSH Park | Cu (all), F/B Mask, F/B Silkscreen, Edge.Cuts            | `<b>.GTL .GBL .GTS .GBS .GTO .GBO .GKO`; inner layers `.G2L .G3L` (4L, documented [O-name]); 6L `.G2L`..`.G5L` (extrapolated: the X2 `FileFunction` also identifies them) | `<b>.XLN`: inches, decimal, absolute, alternate oval mode, PTH and NPTH merged           | none                                                    |
| JLCPCB   | Cu (all), F/B Mask, F/B Silkscreen, F/B Paste, Edge.Cuts | KiCad's Protel extensions as kicad-cli writes them (`.gtl .g1 … .gm1`)                                                                                                    | `<b>-PTH.drl`, `<b>-NPTH.drl`: mm, decimal, absolute, alternate oval mode; map as `.gbr` | BOM and CPL kept separate from the gerber zip           |
| PCBWay   | as JLCPCB                                                | KiCad names, as PCBWay's plugin sends                                                                                                                                     | as JLCPCB                                                                                | `<b>.d356` (IPC-D-356) in the zip; BOM and CPL separate |

**OSH Park drill format.** The two OSH Park pages disagree (§4.1). The bundle uses inches, as the
drill page asks, and decimal format with alternate oval mode, as the KiCad page asks. A KiCad-lane
test pins the header. The first OSH Park order checks the drill preview. That fixture board
includes one NPTH hole and one slot, because OSH Park's NPTH handling in a merged file is not
documented on the pages read.

**The vendor zip holds fab files only.** Unknown files in an upload are a risk with every vendor.
The BOM, CPL, README, card, manifest and DRC report sit beside the zip, and in a separate archive
zip (§6.7).

### 6.4 Determinism

- **Normalization.** kicad-cli runs with `TZ=UTC`, and a fixed set of lines is rewritten:
  - `%TF.CreationDate,…*%`, `G04 Created by KiCad (…) date …*`, `; DRILL file … date …`,
    `; #@! TF.CreationDate,…` and the `.gbrjob` `"CreationDate"`;
  - the replacement time is `--source-date-epoch` / `SOURCE_DATE_EPOCH`, else the board's yapnr
    provenance time, else `1980-01-01T00:00:00+00:00`.
- **Unknown lines.** A normalizer test fails when a kicad-cli update adds a line that differs
  between two runs.
- **Zips.**
  - Entries are sorted, timestamps fixed (1980-01-01), external attributes `0o644`, no extra
    fields, no comment.
  - Entries are **stored**, not deflated. Deflate output can differ between zlib builds; a stored
    zip is byte-identical across platforms. Gerbers are small, and every vendor limit is far
    larger.
- **What is reproducible.** The same board, rules, KiCad version and yapnr version give identical
  bytes on one platform. Zone fills and coordinates come from KiCad, so cross-platform identity is
  not claimed. The manifest records the platform, and the KiCad-lane test checks two builds on one
  platform.

### 6.5 Assembly outputs (F1b)

- **Where parts come from.** Every footprint with a `Reference`, not DNP and not
  `exclude_from_bom`.
  - The **LCSC id** comes from the footprint's `LCSC` field, or `lcsc_id`. atopile writes both,
    with `Manufacturer`, `Partnumber` and `atopile_address`.
  - It is cross-checked against the parts lock: each locked part's `lcsc`, else its part-cache
    manifest's. A conflict is an error.
  - A part with no id is an error for JLCPCB unless `--consign REF,...` names it. It is then listed
    on the card as "not assembled by the vendor".
- **RF footprints are excluded.** `yapnr.rf` footprints set `exclude_from_bom` and
  `exclude_from_pos_files`.
- **JLCPCB BOM** (`<b>-jlcpcb-bom.csv`):
  - columns `Comment, Designator, Footprint, JLCPCB Part #` [J-bomkicad];
  - one row per (LCSC id, value, footprint), designators sorted naturally;
  - `Footprint` is the catalog's `package` when present, else the KiCad footprint name without its
    library.
- **JLCPCB CPL** (`<b>-jlcpcb-cpl.csv`):
  - columns `Designator, Mid X, Mid Y, Layer, Rotation`, mm, counter-clockwise positive,
    `Top`/`Bottom` [J-cpl];
  - built from the kicad-cli pos CSV and transformed in yapnr.
- **JLCPCB rotation corrections.**
  - They come from the part cache, **per locked part**, as new manifest metadata outside the part
    id (like `licence.distribution`): `placement.jlcpcb`, with `rotation_deg`, `dx_mm`, `dy_mm`,
    `checked` and `by`.
  - They are set with `yapnr part-cache set-placement <id> --vendor jlcpcb --rotation 90`, by a
    human who compared JLC's preview.
  - No correction database is copied into yapnr (decisions: no vendor data in the repository).
  - Parts without a checked correction are listed on the card ("check rotation in JLC's preview: U3,
    J1").
  - **Bottom side:** the conversion follows Fabrication Toolkit's published maths [FT]: rotation
    `180 − r` as seen from the top. It is pinned by a golden test. Until one bottom-side assembled
    order confirms it, the card warns on every bottom part.
- **PCBWay BOM** (`<b>-pcbway-bom.csv`): KiKit's column set [KK]. `Manufacturer` and `Mfg Part #`
  come from the footprint fields, and the LCSC id goes in the notes as a sourcing hint.
  `Type` is SMD or THT.
- **PCBWay CPL:** the same columns as JLC's, with no corrections. PCBWay's engineers check
  placement.
- **Part-picker facts are not order facts.** Basic or extended status comes from the picker
  catalog (`basic`). Stock is not known offline (`"unknown"`). The card says that stock and price
  are checked on the vendor's BOM page.

### 6.6 README, stackup and impedance notes

`README.md` in each bundle covers:

- **The board:** name, size and area, layers, the stackup with its layers and the instruction for
  the human, thickness, finish, mask and via covering.
- **Impedance-relevant items:** every footprint with a `yapnr.rf` description, and every netclass
  with a target impedance. For each it gives the width, the reference layer and a nominal
  microstrip estimate.
- **The vendor's impedance service:** none at OSH Park ("nominal stackup only"); JLC's named stackup
  and its ±10 %; PCBWay's ±10 %.
- **The fab-check summary and the provenance.**

An **RF footprint** is one that `yapnr.rf.export.kicad` wrote. Its description names the stackup
the design assumed and the spec sha256. If the assumed εr and h differ from the chosen stackup's
signal-to-reference dielectric by more than 1 %, `fab check` stops with `FAB-RF`, and the
stackup must then be named explicitly. Without this check, an RF demo could be ordered on the
wrong substrate.

### 6.7 Bundle layout, manifest and provenance

```text
<out>/<board>-<profile>/
├── <board>-<vendor>-gerbers.zip     the file to upload (fab files only, stored, deterministic)
├── <board>-<vendor>-bom.csv         assembly vendors, with --assembly
├── <board>-<vendor>-cpl.csv
├── README.md                        §6.6
├── order-card.json, order-card.md   §8.2 (the data; `order stage` adds the run's choices)
├── drc.json                         kicad-cli DRC under the vendor profile
├── fab-check.json                   every check, with severity and source
├── manifest.json
└── <board>-<vendor>-bundle.zip      everything above, for records or a release
```

`manifest.json` has the schema `yapnr-fab-manifest-v1`. It records:

- **Files:** the sha256 and size of every file, and of every member of the gerber zip.
- **The board:** its sha256, and the **input id** (the sha256 with UUIDs normalized, as atopile
  builds do).
- **The run,** if any: its id, candidate and engine id, from `yapnr-provenance.json` when the board
  came from `yapnr export kicad`.
- **Inputs:** the parts lock and catalog sha256; the profile, stackup and vendor data file names and
  sha256; the oldest source access date in them.
- **Tools:** the yapnr version and source revision, the kicad-cli version, the platform.
- **The command line,** with relative paths only.
- **The time:** the normalized time, never the wall clock.

`--public` adds a privacy pass over every text file of the bundle. It refuses absolute paths, home
directories and e-mail addresses, using the rules of `tools/privacy_scan.py` once they are
packaged. A minimal built-in check runs until then. Use it before attaching a bundle to a public
release.

## 7. Checks (F2)

### 7.1 Rules applied for the check

`fab check BOARD --profile P [--stackup S]` runs on a scratch copy:

- **`.kicad_pro`.** `board.design_settings.rules` is set, for each key, to the stricter of the
  project's own value and the profile's (`fab_profile.board_constraints`). Netclasses stay the
  project's.
- **`.kicad_dru`.** The profile's `dru_text` replaces a generated rules file (one with the
  `# Generated by pnr.fab_profile` header). Hand-written rules are kept and appended after it.
- **Then `kicad-cli pcb drc --refill-zones --severity-all --format json`.**

### 7.2 Check list

Each finding has a code, a severity (error, warning or info) and the source of its limit.

| Code                | What                                                                                                                                  | Severity                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `FAB-DRC`           | KiCad DRC errors under the vendor rules (the judge)                                                                                   | error per DRC error; DRC warnings are warnings |
| `FAB-LAYERS`        | Copper layer count differs from the stackup's                                                                                         | error                                          |
| `FAB-OUTLINE`       | No outline, or more than one closed outline (a panel); OSH Park's panel rules [O-panel]                                               | error / warning                                |
| `FAB-SIZE`          | Outside the vendor's minimum or maximum board size                                                                                    | error                                          |
| `FAB-VIA-TYPE`      | Blind, buried or micro vias on a profile without them                                                                                 | error                                          |
| `FAB-DRILL`         | A hole above the vendor's maximum drill (OSH Park mills it [O-drill]); slots present (vendor slot notes)                              | warning                                        |
| `FAB-CASTELLATED`   | Castellated pads: OSH Park "allowed, but not guaranteed" [O-cast]; JLC needs the option and hole ≥ 0.5 mm                             | warning / error                                |
| `FAB-STACKUP`       | The board's KiCad stackup differs from the chosen stackup (thickness, εr)                                                             | warning                                        |
| `FAB-RF`            | An RF footprint's assumed substrate differs from the order's stackup (§6.6)                                                           | error                                          |
| `FAB-IMPEDANCE`     | Impedance-relevant items on a vendor or service without impedance control                                                             | warning (the card states "not controlled")     |
| `FAB-ALTERNATE`     | The vendor has an alternate substrate at checkout (OSH Park EM528) and the board has RF items                                         | info; the card tells the human what to pick    |
| `FAB-QTY`           | Quantity not allowed (OSH Park: multiples of 3; JLC and PCBWay: 5 or more)                                                            | error                                          |
| `FAB-MARKING`       | Assembly vendors print an order number on the silkscreen unless the human chooses otherwise (to be confirmed on the live quote pages) | info on the card                               |
| `FAB-ASSEMBLY`      | Missing LCSC id or MPN, unchecked rotation, bottom-side parts, DNP parts                                                              | error / warning                                |
| `FAB-PROFILE-DRAFT` | A draft profile                                                                                                                       | error without `--allow-draft`                  |
| `FAB-PRIVACY`       | With `--public`: absolute paths or e-mail addresses in bundle text                                                                    | error                                          |

`fab check` exits non-zero on any error. `--json` prints the full finding list.

## 8. `yapnr order stage` (O1 to O3)

### 8.1 Command and flow

```sh
yapnr order stage --vendor {oshpark,jlcpcb,pcbway} (BOARD | --bundle DIR) \
    [--profile P] [--stackup S] [--qty N] [--service S] [--option KEY=VALUE ...] \
    [--dry-run] [--print-only] [--yes] [--reveal] \
    [--via import-url --url URL [--verify-url]]      # O1, OSH Park, public zips
    [--upload]                                       # O2, opt-in (OSH Park only)
    [--api]                                          # O3, opt-in (granted access only)
yapnr order vendors [--json]                         # mechanisms, sources, status
```

1. **Validate:** run `fab check` (`--bundle` re-checks the manifest hashes instead).
2. **Build:** run `fab build` if no bundle was given.
3. **Print the order card** (§8.2) and write it into the bundle with the run's choices.
4. **Stage.** The mechanism comes from the flags (§8.3):
   - by default yapnr asks `Open <url> in your browser? [y/N]`;
   - `--yes` answers yes for opening a page only;
   - `--print-only` prints the URL and the file and opens nothing;
   - `--reveal` also shows the zip in the file manager.
5. **Log** `staged.jsonl` in the bundle. It records the time, vendor, mechanism, URL, the gerber
   zip's sha256 and the opt-in confirmations. It holds no tokens and no vendor response bodies
   beyond an upload id.

**Dry run** is `--dry-run`, or forced when `CI` or `YAPNR_ORDER_DRY_RUN=1` is set. It does steps 1
to 3, prints what step 4 would do, and exits 0. It never opens a browser and never opens a
network connection. A CI dry run on fixture boards keeps the whole path tested.

### 8.2 The order card

It is the per-order confirmation summary. Example for an OSH Park 4-layer RF demo (values
illustrative):

```text
ORDER CARD  staging only: yapnr uploads nothing, orders nothing, pays nothing
vendor      OSH Park (US), 4 Layer service (Super Swift: no)
board       rf-divider   50.00 x 50.00 mm   3.875 sq in   input id 3f2a…
layers      4: F.Cu, In1.Cu, In2.Cu, B.Cu        thickness 1.6 mm (fixed)
stackup     oshpark-4l-fr408hr: 1.7 mil Cu / FR408HR 2113 7.87 mil, Dk 3.61 @ 1 GHz /
            0.68 mil Cu / 39 mil core / 0.68 mil Cu / 7.87 mil / 1.7 mil Cu
CHECKOUT    substrate: keep FR408-HR. Do NOT pick the EM528 alternate: this board's RF
            structures assume Dk 3.61 on 0.200 mm (EM528: Dk 3.76 on 0.195 mm, -1.7 ohm,
            -1.9 % wavelength).
finish/mask ENIG / purple (fixed)        via covering: tented (no fill)
impedance   not controlled (OSH Park has no impedance service); nominal stackup only
qty         3 (multiples of 3)
estimate    $38.75 (3.875 sq in x $10, 3 boards; docs.oshpark.com/services/four-layer, 2026-10-02)
checks      oshpark-4l: DRC 0 errors, 0 warnings; FAB-ALTERNATE (info)
file        out/rf-divider-oshpark-4l/rf-divider-oshpark-gerbers.zip
            sha256 9c41…  (12 files, 214 kB)
next        drop the file on https://oshpark.com/, check the preview, choose options, pay there.
Open https://oshpark.com/ in your browser? [y/N]
```

- **JLCPCB cards** name the stackup to choose under "Specify Stackup" (for example
  `JLC04161H-7628`). They list the options to set (layers, thickness, finish, via covering, order
  number marking) and, for assembly, the BOM and CPL files and the parts needing a rotation check.
- **PCBWay cards** list the options, the remark text to paste (for example "no copper thieving or
  silkscreen on the RF structures on L1"), and the assembly files.
- **`--json`** prints the same card as data for agents and scripts.

### 8.3 Mechanisms per vendor

| Vendor   | `manual` (O1, default)                                                                                                  | `import-url` (O1)                                                                                                                                                                                                                                                     | `upload` (O2)                                                                                                                                                                                                                                                                                                                               | `api` (O3)                                                                                                                                                                                                                                                                                                                                                 |
| -------- | ----------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OSH Park | Open `https://oshpark.com/`; drop the zip                                                                               | `https://oshpark.com/import?url=<quote(url, safe=":/")>` [O-api]. `--url` must be `https`. `--verify-url` downloads it (read-only) and compares its sha256 with the bundle's zip. The URL is published by the human or by a release workflow, never by `order stage`. | After decision D1. `--upload` plus typing `oshpark` at a TTY prompt sends one multipart POST, field `file`, to `https://oshpark.com/import.json`. It reads `{"token","url"}` and opens `url` [O-api]. The upload is anonymous, with no account, preview or warnings [O-api]. No redirects followed; timeout 60 s; the zip is the only body. | none                                                                                                                                                                                                                                                                                                                                                       |
| JLCPCB   | Open `https://cart.jlcpcb.com/quote` [J-quote]                                                                          | –                                                                                                                                                                                                                                                                     | none (no documented anonymous upload)                                                                                                                                                                                                                                                                                                       | If access is granted [J-api]: quote endpoints only. Ordering through the API is out of scope; the cart site is never scripted.                                                                                                                                                                                                                             |
| PCBWay   | Open `https://www.pcbway.com/orderonline.aspx` [P-order]; with assembly, `https://www.pcbway.com/quotesmt.aspx` [P-smt] | –                                                                                                                                                                                                                                                                     | Not implemented. The plugin endpoint (`/Common/KiCadUpFile/`, from the plugin's source [P-plug]) is not documented as an API (D1).                                                                                                                                                                                                          | After access and decision D3 [P-api]. Allowlist: `api/Pcb/PcbQuotation`, `api/SMT/SMTQuotation`, `api/Pcb/GetFreightByOrder`, and `api/Pcb/PlaceOrder` ("Add to Cart") only with `--add-to-cart`. **Never reachable:** `ConfirmOrder` ("Confirm orders/Make Payment"), `PayTGroupOrder`, `QueryBalance`, `CancelOrder` and every endpoint not on the list. |

### 8.4 Network, credentials and refusal paths

- **`yapnr/order/http.py` is the only module that opens connections.** Everything else is offline
  by construction, and a test proves it (§10).
  - Requests are built only from `(vendor, endpoint)` pairs on an allowlist.
  - Only https, to the vendor's documented host; redirects are refused; every request has a
    timeout.
  - Headers and bodies are never logged.
- **Tokens (O3):**
  - Read from `YAPNR_PCBWAY_API_KEY` or `YAPNR_JLCPCB_API_KEY`, else from the OS keychain.
  - The keychain lookup runs `security find-generic-password -s yapnr-<vendor> -w` on macOS or
    `secret-tool lookup service yapnr-<vendor>` on Linux, through the time-bounded `proc.run`.
  - Never accepted as a command-line argument, which would be visible in `ps`.
  - Held in a `Secret` wrapper whose `repr` and `str` are redacted. Exception handlers in `order`
    scrub the token value from messages.
- **Nothing about payment or accounts.** yapnr never asks for or stores card details, addresses,
  account passwords or session cookies. A page that asks for any of them is the vendor's page in
  the human's browser.

## 9. RF demo boards and coupons: what this means for the first orders

- **The existing RF cases are not OSH Park designs.** `yapnr.rf.cases` optimized them on S1
  (εr 3.55, tan δ 0.0027, h 0.813 mm) and S2 (the same material, 1.524 mm). Those are Rogers
  RO4003C's design Dk (3.55) and Df (0.0027 at 10 GHz) [R-4003] at 32 mil and 60 mil. As built
  today, they match **PCBWay's RO4003C 2-layer** service, `pcbway-hf-2l` (draft; available
  thicknesses must be confirmed on the quote).
- **On OSH Park they must be re-optimized.** OSH Park offers no Rogers material. On its 4-layer
  FR408HR stackup (F.Cu over In1.Cu: h 0.200 mm, Dk 3.61 at 1 GHz, Df prior 0.009) the 50 Ω width
  drops from 1.82 mm to 0.44 mm. The min width and space become 0.127 mm.
- **Compute cost (est.).** Scaling the grid with h (pitch 0.3 mm to about 0.074 mm, the same
  substrate cell count) costs roughly 16× in-plane and 4× in time steps, so **about 65× per forward
  run** over the same window. Measure it on the smoke variant before planning the demo schedule.
- **Antenna cases.** On OSH Park 4L, an antenna like case S2 would need a multi-dielectric
  substrate (L1 to In2 or B.Cu), which `yapnr.rf` does not model. OSH Park 2L, about 1.53 mm of
  KB6167F, is the closest single-dielectric match. Its Dk is published only at 10 MHz (4.5), so the
  coupons must pin it.
- **Pin the substrate.** OSH Park's EM528 alternate (Dk 3.76 on 7.67 mil) moves a FR408HR-designed
  50 Ω line to 48.3 Ω and shortens its guided wavelength by 1.9 % (Hammerstad–Jensen, uncoated).
  That is enough to detune narrow-band demos. The RF spec, the board's RF footprints, the fab
  profile and the order card all carry the stackup id, and `FAB-RF` stops a mismatch.
  - OSH Park posted FR408HR stock delays on 2026-09-21, with resolution expected around 2026-09-28
    to 2026-10-02 [O-alt].
  - Decision D4 (§12): design for FR408HR and wait, or design for EM528 and order now. The data
    supports either.
- **Coupons characterize one vendor's stackup.** The coupon research targets JLC stackups. An OSH
  Park demo needs OSH Park coupons from the same order, as one panel or a second design: same
  material, same lot. OSH Park charges by area ($10/sq in on 4L), so a reduced set fits the budget
  better than the 100×120 mm board. A 50×100 mm coupon strip costs $77.50 for 3. The stackup data
  for every vendor is in Appendix A, so the coupon generator can target any of them.
- **Solder mask.** OSH Park masks the lines (0.6 mil); its mask Dk is not published (prior 3.8,
  JLC's calculator value). Exposed copper is ENIG, and its nickel adds loss at GHz. The
  masked/unmasked coupon pairs measure both effects.

## 10. Tests

All tests are hermetic unless tagged. None contacts a vendor.

**Unit tests** (`//tests/unit/fab/...`, `//tests/unit/order/...`):

- **Data:**
  - every data file validates against its schema;
  - every numeric value has provenance;
  - every source key resolves to a URL and an access date;
  - the profile copper matches each allowed stackup's copper;
  - no profile names a stackup that does not exist.
- **Engine parity:**
  - `legacy` and `jlc-pofv` give byte-identical `apply_rules`, `dru_text`, `board_constraints` and
    `geometry` output to the recorded goldens;
  - new profiles give a `dru_text` that parses (a KiCad-lane test runs it).
- **Stackup views:**
  - `microstrip()` thickness and εr against Appendix A;
  - `rf_stackup_spec()` refuses a `null` Df without `use_prior`;
  - the Hammerstad–Jensen widths in Appendix A are recomputed with `yapnr.rf.stackup` to 0.005 mm.
- **Normalizer:** recorded kicad-cli 10.0.6 headers in, fixed lines out. Unknown differing lines
  are reported.
- **Zips:** two builds from the same inputs give identical bytes, sorted entries, fixed times and
  stored entries.
- **Manifest:** it hashes every file and zip member, and contains no absolute path.
- **Assembly:**
  - BOM grouping and natural designator sort;
  - LCSC from fields and lock, with conflicts reported;
  - the CPL transform (rotation offsets, bottom-side rule) against a golden that follows [FT];
  - consigned parts; RF footprints excluded.
- **Checks:** synthetic board facts (stats JSON fixtures) trigger every `FAB-*` code, and nothing
  else.
- **Order card:** golden text and JSON for each vendor.
- **Staging:**
  - `import?url=` encoding (query strings, `&`, spaces);
  - `--dry-run` and `CI=1` never call `webbrowser` and never connect;
  - a fixture patches `socket.socket.connect` and `socket.create_connection` to fail for the
    entire O1 test run.
- **O2:**
  - the upload path runs against a **loopback mock** that serves the documented response shape
    `{"token","url"}`;
  - it refuses without a TTY, with `--yes` alone, and with a wrong typed word;
  - the request body is exactly the zip.
- **O3:**
  - `ConfirmOrder`, `PayTGroupOrder` and `QueryBalance` cannot be built: the allowlist test fails
    on any new endpoint constant that is not on the list;
  - a fake token never appears in stdout, stderr, logs, `staged.jsonl`, cards or exception text;
  - the token is refused on the command line;
  - a redirect is refused.

**KiCad lane** (`requires-kicad`, headless kicad-cli):

- **Fixture boards.** Synthetic 2L, 4L and 6L boards are generated by a test helper; Splanc data is
  not used. They include an NPTH hole, a slot, a castellated pad variant, a bottom-side part, a
  `yapnr.rf` footprint and LCSC fields.
- `fab build` for each vendor gives the expected file names, matching X2 `FileFunction`s, the drill
  header formats (§6.3), and byte-identical results from two builds.
- **Planted violations.** A 0.14 mm track passes `oshpark-4l` (5 mil) and fails `oshpark-2l`
  (6 mil). A 0.45/0.30 via fails `oshpark-4l` (ring) and passes `jlc-4l`. A 0.35/0.20 in-pad via
  fails `jlc-4l` and passes `jlc-pofv`.
- **RF mismatch.** An RF footprint designed for S1 fails `FAB-RF` against `oshpark-4l-fr408hr`.

**Manual end-to-end** (`manual`): `yapnr order stage --dry-run` for each vendor on a ladder case
and on the first RF demo board, before its order.

## 11. Module layout, Bazel and docs

```text
yapnr/fab/
├── __init__.py
├── data/{profiles,stackups,vendors}/*.json
├── capability.py   stdlib, Python ≥ 3.9: load and validate data; engine_profile(name)
├── stackups.py     stackup model, microstrip/stripline/cross_section views, rf adapters
├── board.py        board facts: kicad-cli stats, .gbrjob, a small S-expression reader
├── check.py        fab check (§7)
├── export.py       kicad-cli runs (time-bounded, TZ=UTC), normalization, renaming
├── assembly.py     BOM and CPL, part-cache placement corrections
├── bundle.py       README, manifest, deterministic zips, privacy pass
└── cli.py          `yapnr fab profiles|show|check|build`
yapnr/order/
├── __init__.py
├── card.py         order card (text, markdown, JSON)
├── stage.py        flow, dry run, mechanisms
├── vendors.py      per-vendor URLs and mechanisms (from vendors/*.json)
├── http.py         the only network module: allowlist, https, no redirects, timeouts
├── secrets.py      environment and keychain lookup, redacted Secret
└── cli.py          `yapnr order stage|vendors`
```

- **Bazel.**
  - `//yapnr/fab` and `//yapnr/order` are stdlib-only `py_library` targets, with the data as
    `data`, registered in `yapnr/cli.py` as the atopile commands are.
  - `//hardware/pnr:pnr` gains a dependency on `//yapnr/fab:capability`.
  - The wheel includes `yapnr/fab/data`.
- **Docs.**
  - `docs/fab-and-ordering.md`, the user guide, added to the "Using yapnr" toctree. It covers a
    quick start per vendor, choosing a profile and stackup, what `fab check` checks, the bundle,
    the order card, what yapnr never does, credentials for O3, the RF stackup pinning, and a table
    of vendor sources with access dates and a "re-verify before relying" note.
  - `docs/engine/fab-profiles.md` (planned in the migration plan) gets the profile reference,
    generated from the data files.
  - `decisions.md` gains the decisions of §12 once made.
  - `AGENTS.md` gains the `--dry-run` rule (§3).
- **Data refresh.** The vendor data is dated, and vendors change pages and prices. A yearly or
  per-order "re-verify" step lists every source with its access date (`yapnr fab show --sources`).
  A source older than 180 days gives a warning on the card.

## 12. Decisions for the owner, and open items

**Decisions:**

- **D1. Do opt-in uploads count as staging?**
  - Recommendation: **yes for OSH Park's documented `/import.json`** [O-api]. It creates an
    anonymous upload page and no order, after a typed per-run confirmation.
  - **No for PCBWay's plugin endpoint** for now. It works the same way but is not documented as an
    API, so it can change without notice. The manual page costs the human one drag.
- **D2. Public "Order on OSH Park" links for the public demo boards.**
  - The bundle zip would be published at a stable public URL (the docs site or a release asset), and
    the docs would carry an `oshpark.com/import?url=…` link.
  - yapnr itself never uploads, and anyone can then order the same board.
  - Recommendation: yes, per board, after its first order is checked.
- **D3. Vendor API applications** (PCBWay partner, JLCPCB). This is the owner's account decision.
  O1 works without them. If PCBWay access is granted, should `PlaceOrder` ("Add to Cart") be
  allowed at all, or only quotes? Recommendation: quotes, plus add-to-cart behind
  `--add-to-cart`.
- **D4. The substrate of the first OSH Park RF demo:** FR408HR (wait for stock) or EM528 (now).
  Also whether the first demo is a 4L FR408HR re-optimization (§9: compute cost) or a 2L FR-4
  board.
- **D5. The derived values** in §5.2: those marked "derived" in the OSH Park table, and the 6-layer
  inner layer names `.G4L`/`.G5L`.

**Unverified:**

- **Df values:** OSH Park's 4-layer prepreg Df (prior 0.009 from its 6-layer 2113 core); the OSH
  Park 2L Dk above 10 MHz and its Df.
- **Mask Dk:** for OSH Park and PCBWay.
- **Live prices:** JLC's and PCBWay's prices and their order-number marking options (`FAB-MARKING`).
  PCBWay's Rogers thicknesses and 2-layer rules (draft profiles). JLC's default 6-layer stackup
  when none is specified.
- **OSH Park file handling:** NPTH handling in a merged drill file, and whether the 6-layer inner
  layer names are recognized. The X2 attributes make these low risk. The first order's preview is
  the check.
- **That a KiCad 10 `.kicad_pcb` fails at OSH Park.** Not tested, and not needed: the bundle sends
  gerbers.
- **US import duty details.**

## 13. Sources

Pages accessed 2026-10-02 unless noted. Vendor pages change; the data files carry these keys.

- **OSH Park:**
  - services: [O-svc], [O-2l], [O-4l], [O-6l];
  - stackup changes: [O-alt], [O-mixed];
  - ordering and files: [O-home], [O-api], [O-kicad], [O-kgerb], [O-name], [O-drill];
  - design notes and shipping: [O-panel], [O-cast], [O-ship].
- **JLCPCB:**
  - ordering and files: [J-quote], [J-kicad], [J-bom], [J-bomkicad], [J-cpl];
  - API and capabilities: [J-api], [J-imp], [J-cap];
  - prices: [J-home], [J-pcba]. The JLC stackup values come from [J-imp] as read on 2026-10-02
    for the coupon research.
- **PCBWay:**
  - ordering: [P-order], [P-smt], [P-plug] (MIT; read in `plugins/thread.py`), [P-api];
  - capabilities and materials: [P-cap], [P-adv], [P-lam], [P-hf];
  - assembly and prices: [P-asm], [P-home]. The PCBWay 4-layer price is from a live quote on
    2026-09-26.
- **Materials:** [R-4003].
- **Open tools** (all AGPL-compatible licences; facts used, no code copied): Fabrication Toolkit
  [FT] (Apache-2.0), KiKit [KK] (MIT), kicad-jlcpcb-tools [JT] (MIT), Aisler Push [AISLER] (MIT).
- **Other US shops,** for later: AdvancedPCB [ADV] (quote portal needs a login; $99 each on RF
  material), Bay Area Circuits [BAC], Sierra Circuits [SC] (Rogers, controlled impedance).

## 14. As built (2026-10-02)

**Built:** F2a, F1a, O1a, F1b and O1b of §2.1, in `yapnr/fab` and `yapnr/order` as laid out in
§11. The ladder routes under the new profiles (`run.py --fab-profile`), and `yapnr order stage
--dry-run` stages every vendor from those boards:

| Ladder case          | Profile      | Route                         | KiCad DRC under the profile | Bundle                       |
| -------------------- | ------------ | ----------------------------- | --------------------------- | ---------------------------- |
| `04-inverter-leds-8` | `oshpark-2l` | 363 tracks, 7 vias, 0 opens   | 0 errors, 0 warnings        | 8 files, `.GTL`..`.XLN`      |
| `08-chaser-20-plane` | `oshpark-4l` | 1077 tracks, 28 vias, 0 opens | 0 errors, 0 warnings        | 10 files, `.G2L`, `.G3L`     |
| `08-chaser-20-plane` | `jlc-4l`     | 1087 tracks, 28 vias, 0 opens | 0 errors, 0 warnings        | 13 files, KiCad Protel names |
| `08-chaser-20-plane` | `pcbway-std` | 1102 tracks, 28 vias, 0 opens | 0 errors, 0 warnings        | 13 files, IPC-D-356 included |

**Not built:**

- **O2 and O3**, so neither `yapnr/order/http.py` nor `secrets.py` exists. `yapnr/order/net.py`
  holds the one network call of O1, `--verify-url`. It follows redirects to https only (release
  assets redirect to their storage host); the no-redirect rule of §8.4 stays for O2 and O3.
- **`yapnr.rf.cases --stackup/--fab-profile`:** `yapnr.rf` is not in this repository yet. Its
  adapter is ready: `stackups.load(id).microstrip(layer).rf_stackup_spec(...)` and
  `stackups.rf_rules(profile)`.
- **Part-cache placement corrections** (`placement.jlcpcb`, `yapnr part-cache set-placement`).
  Until they exist, the JLCPCB card asks for a rotation check of every placed part.
- **The project `[fab]` table (PR3d), `--run`, and the `yapnr_fab` Bazel rule:** later, as planned.
  The measured stackup overlay is reserved; `stackups.load(..., measured=...)` refuses it.

**Differences from the design:**

- **The default profile** is first the profile the board was routed under, when its generated
  `.kicad_dru` names one of the chosen vendor's profiles for its layer count. Otherwise it
  follows §6.1, with JLCPCB's in-pad case found by a via on an SMD pad.
- **PCBWay drill files are in inches,** as PCBWay's own plugin writes them [P-plug], not in mm as
  §6.3 proposed. They stay decimal, with PTH and NPTH separate.
- **`yapnr fab preview`** (not in §6): a stdlib reader of the gerber and Excellon files in the zip.
  It renders each layer and a top and bottom composite to SVG, so the files to be uploaded can be
  looked at without KiCad.
- **`FAB-SOURCES`** (not in §7.2) warns when a source of the data is older than 180 days (§11,
  data refresh).
- **`order stage`** writes its card as `order-card.staged.{md,json}` beside the build's card, so
  the manifest's hashes stay valid. A dry run writes that card but no `staged.jsonl`.
- **The bundle archive** also keeps KiCad's job file (`kicad/<board>-job.gbrjob`).

**Confirmed on the live pages** (2026-10-02, read only):

- **JLCPCB's quote page** has "Add gerber file", "Mark on PCB" (Remove Mark, Order Number, 2D
  barcode) and "SAVE TO CART" [J-quote].
- **PCBWay's quote page** has "Single pieces", "Calculate", "Other special request" (200
  characters), "Remove product No." (No, Yes for USD 1.50, Specify a location) and "Save to Cart"
  [P-order].
- **OSH Park's upload area** reads "Drag and drop your KiCAD, EagleCAD, or zipped Gerber files",
  with a "Browse for files" button [O-home].

`FAB-MARKING` and the cards name these options. Which marking JLCPCB selects by default is still
unconfirmed.

## Appendix A. Stackup data

**Schema** (`yapnr-fab-stackup-v1`). One full file:

```json
{
  "schema": "yapnr-fab-stackup-v1",
  "id": "oshpark-4l-fr408hr",
  "vendor": "oshpark",
  "title": "OSH Park 4 layer, FR408HR (standard)",
  "vendor_name": "4 Layer, FR408-HR",
  "checkout": "Keep the standard FR408-HR substrate; EM528 is the alternate offered at checkout since 2026-09-21.",
  "thickness_mm": { "value": 1.6, "src": "O-4l" },
  "layers": [
    {
      "kind": "mask",
      "side": "F",
      "thickness_mm": 0.01524,
      "er": null,
      "er_prior": 3.8,
      "er_prior_src": "J-imp calculator",
      "src": "O-4l"
    },
    {
      "kind": "copper",
      "name": "F.Cu",
      "thickness_mm": 0.04318,
      "weight_oz": 1.0,
      "plated": true,
      "finish": "ENIG",
      "src": "O-4l"
    },
    {
      "kind": "dielectric",
      "type": "prepreg",
      "material": "FR408HR 2113",
      "thickness_mm": 0.1999,
      "er": 3.61,
      "er_freq_hz": 1e9,
      "df": null,
      "df_prior": 0.009,
      "df_prior_src": "O-6l (2113 core)",
      "src": "O-4l"
    },
    {
      "kind": "copper",
      "name": "In1.Cu",
      "thickness_mm": 0.01727,
      "weight_oz": 0.5,
      "plated": false,
      "src": "O-4l"
    },
    {
      "kind": "dielectric",
      "type": "core",
      "material": "FR408HR",
      "thickness_mm": 0.9906,
      "er": null,
      "df": null,
      "src": "O-4l"
    },
    {
      "kind": "copper",
      "name": "In2.Cu",
      "thickness_mm": 0.01727,
      "weight_oz": 0.5,
      "plated": false,
      "src": "O-4l"
    },
    {
      "kind": "dielectric",
      "type": "prepreg",
      "material": "FR408HR 2113",
      "thickness_mm": 0.1999,
      "er": 3.61,
      "er_freq_hz": 1e9,
      "df": null,
      "df_prior": 0.009,
      "df_prior_src": "O-6l (2113 core)",
      "src": "O-4l"
    },
    {
      "kind": "copper",
      "name": "B.Cu",
      "thickness_mm": 0.04318,
      "weight_oz": 1.0,
      "plated": true,
      "finish": "ENIG",
      "src": "O-4l"
    },
    {
      "kind": "mask",
      "side": "B",
      "thickness_mm": 0.01524,
      "er": null,
      "er_prior": 3.8,
      "er_prior_src": "J-imp calculator",
      "src": "O-4l"
    }
  ],
  "tolerances": { "impedance": null, "thickness_pct": null, "width_pct": null },
  "sources": {
    "O-4l": { "url": "https://docs.oshpark.com/services/four-layer/", "accessed": "2026-10-02" },
    "O-6l": { "url": "https://docs.oshpark.com/services/six-layer/", "accessed": "2026-10-02" },
    "J-imp": { "url": "https://jlcpcb.com/impedance", "accessed": "2026-10-02" }
  }
}
```

**All stackups.** Thicknesses are in mm, from the top down to and including the middle core;
every stackup listed is symmetric. Cu is the finished thickness where published. W50 is the 50 Ω
microstrip width on F.Cu over the first reference layer: Hammerstad–Jensen, zero-thickness strip,
no mask, computed with the formula of `yapnr.rf.stackup`. Copper thickness and mask each lower Z0
by about 1 to 3 Ω at this width, so the 2D solver and the coupons set the real numbers.

| Id                                          | Layers, top to middle core (mm)                                                                         | Dk (Df), quoted at                                                       | W50 (mm)           | Source                    |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------ | ------------------------- |
| `oshpark-2l-fr4`                            | Cu 1 oz; KB6167F about 1.53 (derived: 1.6 nominal minus copper)                                         | 4.5 at 10 MHz (Df null, prior 0.017, literature)                         | 2.88               | [O-2l]                    |
| `oshpark-4l-fr408hr`                        | mask 0.0152; Cu 0.0432; 2113 pp 0.1999; Cu 0.0173; core 0.9906                                          | pp 3.61 at 1 GHz (Df null, prior 0.009); core null                       | 0.44               | [O-4l]                    |
| `oshpark-4l-em528`                          | Cu 1 oz; EM528 pp 0.1948 (7.67 mil); Cu 0.5 oz; EM528 core 0.9906 (39 mil)                              | pp 3.76 (0.005); core 4.11 (0.005); frequency not stated                 | 0.42               | [O-alt]                   |
| `oshpark-4l-mixed`                          | FR408HR prepreg as `-fr408hr`; EM528 core                                                               | pp 3.61; core 4.11 (0.005)                                               | 0.44               | [O-mixed]                 |
| `oshpark-6l-fr408hr`                        | mask 0.0152; Cu 1 oz; 106 pp 0.1107; Cu 0.5 oz; 2113 core 0.1016; Cu 0.5 oz; core 0.9256                | 106 pp 3.23 (0.011) at 1 GHz; 2113 core 3.66 (0.009) at 1 GHz            | 0.27               | [O-6l]                    |
| `jlc04161h-7628`                            | Cu 0.035; 7628 0.2104; Cu 0.0152; core 1.065                                                            | pp 4.4, core 4.6, mask 3.8; frequency and Df not published (prior 0.017) | 0.40               | [J-imp]                   |
| `jlc04161h-2116` / `-3313` / `-1080`        | Cu 0.035; pp 0.1164 / 0.0994 / 0.0764; Cu 0.0152; core 1.265                                            | pp 4.16 / 4.1 / 3.91; core 4.6                                           | 0.23 / 0.20 / 0.16 | [J-imp]                   |
| `jlc06161h-2116c`                           | Cu 0.035; 2116 0.127 + 0.1194; Cu 0.0152; core 0.30; Cu 0.0152; middle 2116 ×3: 0.1194 + 0.127 + 0.1194 | pp 4.16; core 4.6                                                        | –                  | [J-imp] (coupon research) |
| `jlc06161h-7628`                            | Cu 0.035; 7628 0.2104; Cu 0.0152; core 0.40; Cu 0.0152; middle 7628 0.2028                              | pp 4.4; core 4.6                                                         | 0.40               | [J-imp] (coupon research) |
| `jlc06161h-3313`                            | Cu 0.035; 3313 0.0994; Cu 0.0152; core 0.55; Cu 0.0152; middle 2116 0.1088                              | pp 4.1 / 4.16; core 4.6                                                  | 0.20               | [J-imp] (coupon research) |
| `pcbway-4l-7628`                            | Cu 0.5 oz base plated to 1 oz; 7628 RC46 % 0.196 (0.1855 after lamination); Cu 1 oz; core 1.03          | pp 4.74; core 4.6; frequency and Df not published                        | 0.34               | [P-lam]                   |
| `pcbway-ro4003c-0.813` (`-0.508`, `-1.524`) | Cu 1 oz; RO4003C 0.813                                                                                  | process 3.38 ± 0.05, **design 3.55**; Df 0.0027 at 10 GHz                | 1.82               | [P-hf] [R-4003]           |
| `pcbway-ro4350b-0.508`                      | Cu 1 oz; RO4350B 0.508                                                                                  | 3.48 (0.0037) at 10 GHz (process)                                        | 1.15               | [P-hf]                    |

For `pcbway-ro4350b-0.508`, the design Dk is still to be taken from the Rogers datasheet.

**Notes on the table:**

- **Which Dk the RF solver uses.** For Rogers material it uses the design Dk, which is what
  `yapnr.rf.cases` S1 and S2 use. The process Dk is kept in the file as well.
- **Rogers thicknesses.** The standard Rogers thicknesses (0.203 to 1.524 mm) are offered by PCBWay
  only through the quote, so the `pcbway-ro*` ids list what the RF cases need. Each must be
  confirmed on the quote before use; the profile is draft.
- **Typical FR-4 Df.** The prior 0.017 is the middle of the coupon research's 0.015 to 0.02 range
  for FR-4 near 1 GHz. It is a literature value, not a vendor figure.

[O-svc]: https://docs.oshpark.com/services/
[O-2l]: https://docs.oshpark.com/services/two-layer/
[O-4l]: https://docs.oshpark.com/services/four-layer/
[O-6l]: https://docs.oshpark.com/services/six-layer/
[O-alt]: https://docs.oshpark.com/troubleshooting/alternate-4-layer-stackup/
[O-mixed]: https://docs.oshpark.com/troubleshooting/mixed-material-4-layer-stackup/
[O-api]: https://docs.oshpark.com/tips+tricks/api/
[O-kicad]: https://docs.oshpark.com/design-tools/kicad/
[O-kgerb]: https://docs.oshpark.com/design-tools/kicad/generating-kicad-gerbers/
[O-name]: https://docs.oshpark.com/troubleshooting/naming-pattern/
[O-drill]: https://docs.oshpark.com/submitting-orders/drill-specs/
[O-panel]: https://docs.oshpark.com/troubleshooting/panelized-designs/
[O-cast]: https://docs.oshpark.com/tips+tricks/castellation/
[O-ship]: https://docs.oshpark.com/submitting-orders/shipping-information/
[O-home]: https://oshpark.com/
[J-quote]: https://cart.jlcpcb.com/quote
[J-kicad]: https://jlcpcb.com/help/article/how-to-generate-gerber-and-drill-files-in-kicad-8
[J-bom]: https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly
[J-bomkicad]: https://jlcpcb.com/help/article/how-to-generate-the-bom-and-centroid-file-from-kicad
[J-cpl]: https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly
[J-api]: https://api.jlcpcb.com/
[J-imp]: https://jlcpcb.com/impedance
[J-cap]: https://jlcpcb.com/capabilities/pcb-capabilities
[J-home]: https://jlcpcb.com/
[J-pcba]: https://jlcpcb.com/help/article/pcb-assembly-price
[P-order]: https://www.pcbway.com/orderonline.aspx
[P-smt]: https://www.pcbway.com/quotesmt.aspx
[P-plug]: https://github.com/pcbway/PCBWay-Plug-in-for-Kicad
[P-api]: https://api-partner.pcbway.com/Help
[P-cap]: https://www.pcbway.com/capabilities.html
[P-adv]: https://www.pcbway.com/advanced-pcb-capabilities.html
[P-lam]: https://www.pcbway.com/multi-layer-laminated-structure.html
[P-hf]: https://www.pcbway.com/pcb_prototype/What_is_High_Frequency__HF__PCB_.html
[P-asm]: https://www.pcbway.com/pcb-assembly.html
[P-home]: https://www.pcbway.com/
[R-4003]: https://www.rogerscorp.com/advanced-electronics-solutions/ro4000-series-laminates/ro4003c-laminates
[FT]: https://github.com/bennymeg/Fabrication-Toolkit
[KK]: https://github.com/yaqwsx/KiKit
[JT]: https://github.com/Bouni/kicad-jlcpcb-tools
[AISLER]: https://github.com/AislerHQ/PushForKiCad
[ADV]: https://www.advancedpcb.com/en-us/
[BAC]: https://bayareacircuits.com/
[SC]: https://www.protoexpress.com/
