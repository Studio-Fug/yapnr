# PnR guidance inputs — the constraint language

The algorithmic place-and-route system (FUG-138, design:
[`pnr-system.md`](pnr-system.md)) is **constraint-first**: its value is honoring
your mechanical and electrical _intent_ — connectors on a particular edge, an
antenna keep-out, a button reachable from the enclosure, decouplers on the back —
not just minimizing wirelength. You express that intent in a **sidecar
`constraints.yaml`** next to the board; nothing in the `.ato` changes.

This document is the reference for that file: every section, what it means, and
how it steers placement. For the algorithms behind it see the design doc; for a
working example see
[`hardware/splanc_dev/constraints.yaml`](../../hardware/splanc_dev/constraints.yaml).

## Where it plugs in

```text
bazel build //hardware/splanc_dev:splanc_dev.fab
    ├─ atopile resolves the .ato        →  row-placed .kicad_pcb (netlist + footprints)
    ├─ constraints.yaml  ───────────────┐
    │                                   ▼
    ├─ ingest → place+route loop  (honors the constraints below)
    ├─ writeback → detailed route (FreeRouting)
    └─ DRC + export               →  Gerbers / drill / BOM / pick-place
```

The board target names the file:

```python
atopile_pnr(
    name = "splanc_dev.fab",
    layout = ":splanc_dev",          # the atopile_project base target
    constraints = "constraints.yaml",
)
```

## Coordinate frame and units

- **Units:** millimetres everywhere; `rot` is **degrees CCW**.
- **Origin:** the board-outline **bottom-left** corner. `x` grows right, `y`
  grows up. (KiCad's own y grows _down_; ingest/writeback convert for you.)
- **Edges** are named by compass direction: `south` = `y=0`, `north` = `y=H`,
  `west` = `x=0`, `east` = `x=W`.

## Hard vs. soft

Every constraint compiles to one of two things (design §3):

- **Hard** — a feasibility barrier the result must satisfy: fixed poses,
  keep-outs, the outline. Violations are illegal, and the acceptance tests fail.
- **Soft** — a weighted penalty expressing a preference: edge pulls, side bias,
  grouping. The optimizer trades these off against wirelength; a higher `weight`
  makes the preference stronger. Soft constraints are _intent_, not guarantees.

## The file

```yaml
schema: v0 # required; the schema is versioned so it can grow

board: # global: approximate size + design rules
  outline: { w: 60, h: 50 }
  layers: 4
  default_clearance_mm: 0.3

fixed: # hard: pin a part's pose
  USB1: { edge: south, align: center, rot: 0, side: top }
  U5: { edge: north, align: center, rot: 0, side: top }

edge_align: # soft: pull a part to a board edge
  SW1: { edge: south, side: top }
  CN1: { edge: east, side: top }

region: # hard: keep parts inside an area
  - { name: supply, refs: [U3, L1, C10], rect: [40, 0, 60, 20] }

align: # hard: parts share one coordinate
  - { name: buttons, refs: [SW1, SW2], axis: y }

keepout: # hard: no parts/copper in a region
  - { name: esp32_antenna, ref: U5, extent: { edge: north, depth_mm: 6 } }

side_pref: # soft: bias a set of parts to a side
  bottom: [C*, R*]

group: # soft: cluster parts near an anchor
  - { members: [U2, L2, L3], anchor: U2, radius_mm: 8 }
```

Unknown component references are **warnings, not errors** (the file can name a
part that a build variant drops), and globs (`C*`, `R?`, `U[13]`) expand against
the real netlist. Unknown top-level sections are ignored with a warning, so a
newer schema stays readable by an older engine.

### `board` — size and rules

The approximate board you're targeting.

| Key                    | Meaning                                                                                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `outline: {w, h}`      | Placement region (mm). Parts are kept inside it; it becomes the `Edge.Cuts` rectangle. Omit to use the board's own outline.       |
| `layers`               | Copper layer count (2 to 32). Without a declared stack (below), inner layers are treated as power/ground planes, so routing capacity scales with the **signal** layers. |
| `default_clearance_mm` | Minimum courtyard-to-courtyard gap enforced in legalization, and the track pitch the lookahead router assumes.                    |
| `sides`                | Side policy: `single` (default; every part stays on its source side, and `side_pref` is ignored) or `double` (placement chooses the side of every part nothing holds; needs 2 or more layers; see `side_pref`). |
| `plane_fallback_drops` | `true` (default): writeback and the plane stage (`pnr.planes`) drop a via from every plane pad the router left without a through contact (dog-bone fallback). `false`: neither adds copper nobody routed; the unreached plane pads are listed on stderr (`unreached plane pads`). Use `false` for a placement-only writeback and for boards whose plane access another stage owns (a BGA fanout). |
| `class_clearance`      | `maze` or `repair` (opt-in). `maze`: the grid router keeps net class clearances in the maze itself. Each net's track halo and via keep-out are sized from its own clearance (its class `clearance_mm` where larger than the fab's), every routed net judges pads and escape copper through tables at its own width and clearance (a class net's copper at the class clearance for every other net too), the routed nets are then checked with the exact pairwise rule (any pair still too close is ripped and routed again around the rest; a net that cannot be is left open, not emitted), and an mm audit of all emitted copper against other nets and pads is reported in the PnR report (`escape_diagnostics.class_clearance`: `pairs`, `static`, `ripped`, `rerouted`, `audit`). `repair`: the route is the one without the switch; then the routed nets too close to static copper at their class clearances, and the fewest that part every pair the exact rule finds too close, are ripped and routed again exactly around the rest (the class tables now in force), with the same audit. `maze` costs routing capacity on a grid whose pitch is one signal track and clearance (each class net's halo grows by a cell); `repair` costs only the nets it reroutes. Off (default): every reservation uses the fab clearance and only the escape and drop checks read the classes. |
| `edge`                 | `exact` (opt-in): the router judges the board edge on the board's own `Edge.Cuts` outline (lines, arcs, circles; rounded corners and notches count) instead of the placement rectangle: tracks keep `edge_clearance_mm + ½width` from its centre line and vias the larger of `edge_clearance_mm + via radius` and the hole-to-edge rule as KiCad measures it (to the stroke's edge: `limit + ½stroke + ½drill`, with `limit` the board's own `physical_hole_clearance` to `Edge.Cuts` when `dru_routing` reads one), 1 µm margin. The drivers (`route_case.py`, `pnr.staged_signal`) attach the outline to the rules (`board_edges`, `pnr.board_edge`); an outline that does not frame the `outline` region falls back to the rectangle (warned, `escape_diagnostics.board_edge`). Implies `keep_outline`. |
| `keep_outline`         | `true` (opt-in): writeback keeps the source board's `Edge.Cuts` (moved to its frame) instead of stamping the 0.15 mm rectangle, when it frames the placement region (else the rectangle, with a warning). |
| `dru_routing`          | `true` (opt-in): the router reads the board's custom rules (`<board>.kicad_dru`) where they constrain routing (`pnr.dru_rules`): `disallow via` for a net (a no-via class) keeps it on its pads' one layer; `disallow track` on layers takes those layers from the net; a `clearance` between two kinds of nets (`A.hasNetclass('SW') && B.hasNetclass('XTAL')`) raises one side's clearance when it is 1 mm or less, else keeps each side's tracks and vias that far from the other side's pads, escapes and fixed copper; `length (max)` is reported against the routed length; `physical_hole_clearance` / `edge_clearance` against `Edge.Cuts` feed `edge: exact`. Conditions are judged for each net's tracks and vias (`A/B.Type`, `NetClass`, `NetName`, `Layer`, `hasNetclass`, a via's `Hole`, `&& \|\| !`); every rule or constraint it cannot state exactly (areas, courtyards, pad properties, sizes, pair geometry) is listed with its reason in the PnR report (`escape_diagnostics.dru`: `applied`, `unmodelled`, `length`, `pair_keepouts`, `audit`), never dropped. |

The outline is _approximate guidance_: the placer frames the parts within it. Make
it a bit larger than the parts need — an over-tight outline forces congestion and
can leave the place↔route loop unable to reach zero overflow.

**Declared copper stack.** A board whose KiCad file declares a physical stackup
(Board Setup > Physical Stackup) is routed on its own stack (`pnr.stack`), for any
layer count, when it types at least one layer `power` or `mixed` (Board Setup >
Board Editor Layers) or keeps tracks off an inner layer by a custom rule
(`.kicad_dru`: `(layer ...)`, `(constraint disallow track)`), or when it declares no
`plane_layer` class and draws no zone on a signal-typed inner layer:

- a `power` inner layer (or one a custom rule keeps free of tracks) is a
  **dedicated plane**: no tracks. Its nets are the `plane_layer` classes naming it
  plus the nets of zones already drawn on it, so a second ground plane is just a
  zone in the source board. Every surface pad of
  such a net drops a through via to it, planned together with the signal escapes
  and sized for that pad's own entry width. The via must land where the net's
  copper fills on one of its planes; a pad without such a site is reported
  unrouted at the pad. Write-back keeps the zones already drawn and forms the
  rest: the whole outline for a layer's only net; on a layer shared by several
  nets, the outline for the net with the most pads and, at a higher fill
  priority, its pads' bounding box plus 2 mm for each other net. The outline
  net's drops then stay out of those boxes, so on a shared layer whose nets'
  pads interleave, give each net a layer of its own or draw the zones;
- a `mixed` layer, or a `signal` layer named by a `plane_layer` class, is a split
  plane: the class nets' pads' bounding box, with signals in the gaps;
- every other `signal` layer is routed, inner ones included; zones on it refill
  around its tracks. A class with `current_a` stays off an inner layer whose
  declared copper thickness would need a wider track (IPC-2221 internal) than the
  class width;
- the native KiCad loop's power paths use the routed layers (never a dedicated
  plane), and a pair without its own `reference_layer` takes the dedicated plane
  nearest F.Cu as its reference.

A board without a declared stack keeps the behaviour above, and so does a declared
stack whose planes come only from `plane_layer` classes on signal-typed layers, or
whose only plane hints are zones on signal-typed inner layers (KiCad's default
type). A declared stack that cannot be used as declared also keeps it: one copper
layer, an unknown layer type, a `jumper` outer layer, another copper layer count
than `layers`, or a stackup block whose copper rows are not the board's layers
(KiCad keeps the old block when the layer count changes). Write-back keeps a
declared stack's layer types. Each such decision, and each ambiguity (a `power`
layer no class or zone gives a net, a shared plane layer, zones on a signal
layer), is a warning in the run's log and in the PnR report
(`escape_diagnostics.stack_warnings`).

**Via kinds (blind, buried, micro).** Every via is a through via unless the routing
rules carry a `via_policy` (`pnr.via_policy`). The ladder drivers resolve it from
the design's declared kinds (`via_policy.allowed`: `through`, `blind`, `buried`,
`micro`, with an optional `microvia: {diameter_mm, drill_mm}`) less every kind the
board's `.kicad_dru` disallows (`blind_via`, `buried_via`, `micro_via` or `via`; a
ban limited by a layer or condition counts everywhere), on the board's stackup
block (copper layers, dielectric thickness and kind, `core` or `prepreg`).
Nothing declared, every other kind banned, or no other span worth its drill pair
on this board means through vias only, exactly as before. Under a policy:

- a span is only used when it can be built on the declared stack: a laser
  microvia joins two adjacent layers, one of them outer, through a dielectric no
  deeper than its drill (aspect ratio 1:1; KiCad accepts any pair, the engine
  keeps to these); a controlled-depth blind via is drilled from an outer layer no
  deeper than its drill; a laminated blind or buried via is the through hole of a
  sub-laminate, so each end faces a prepreg bond line or the board's face, never
  the other face of a core. On the 6-layer rungs (prepreg / core In1-In2 /
  prepreg / core In3-In4 / prepreg) F.Cu-In3.Cu, In1.Cu-In3.Cu and In2.Cu-B.Cu
  cannot be built;
- the board's spans form one build (`via_policy.build`, in `rules.json` and the PnR
  report's `escape_diagnostics.via_build`): its laminated spans nest or are
  disjoint (one sequential-lamination tree) and every span is one more drill pair,
  priced as two through vias (`via_policy.drill_pair_cost` overrides it). The
  build is the cheapest for what the parts need (`pnr.via_policy.board_needs`:
  plane drops, signal layer changes, return ties), so a span enters only where
  its vias save more than its drill pair; on the 6-layer chaser rungs that is
  F.Cu-In1.Cu (a microvia, or a controlled-depth blind via) and F.Cu-In2.Cu;
- a via spans two copper layers and occupies only the layers between them: the
  router tests, reserves and prices it there (blind F.Cu to In2.Cu leaves B.Cu
  free), at a keep-out from its own diameter. Its price is the via cost times
  `0.5 + 0.5 * depth / board thickness` (through: 1.0). A layer change takes the
  cheapest span of the build covering both layers; hole spacing is kept between
  all vias whatever their spans, two nets' vias never share a site, and same-net
  vias at one site whose spans share a layer are one barrel. A microvia's size is
  the declared one, else the project's net class microvia, widened to the board's
  minimum annular width (which KiCad applies to microvias too); blind and buried
  vias take the routed via size;
- a plane pad drops to its net's plane nearest the pad. A signal via whose ends
  are referenced (the plane nearest above and below each) to two plane layers of
  one net (two ground planes) gets a via of that net joining both within
  `return_tie.max_mm`: the distance whose return detour, out and back, is delayed
  no more than `0.1 * t_rise` (the stub rule's k), with `t_rise` the design's
  `via_policy.t_rise_ns` or 1 ns (an assumption, reported) and the stack's
  dielectric constant (7.07 mm at 1 ns and 4.5). A drop nearby is deepened where
  clear, else a tie via of the build's span goes at the clear site nearest the
  signal via; every plane layer of such a net is joined at least once. The PnR
  report gives each plane layer's connections
  (`escape_diagnostics.plane_layer_connections`) and the rule, the ties needed,
  met and added, any unmet with its nearest tie, and the signal vias whose two
  references are planes of different nets (`escape_diagnostics.return_ties`). A
  plane layer with no connection at all keeps its fill in KiCad 10, and its DRC
  reports it as isolated copper;
- `routes.json` lists each non-through via in `via_spans` (`[net, x, y, top,
  bottom, kind]`); write-back emits the KiCad via type and layer pair, and fixed
  copper keeps blind, buried and micro vias (each reserves only its span). The
  packed and native maze kernels do not model spans: on such a board every search
  runs on the reference kernel (the same routes, slower; stderr says so once), so
  the exact-separation recovery, which needs the packed kernel's fields, does not
  run there. The length tuner adds meanders there but routes no member again
  (its new vias would lose their spans). The native KiCad repair loop and the
  hierarchical driver add through vias only; the hierarchical driver says so on
  stderr.

### `fixed` — lock a pose (hard)

Pins a part so downstream steps can't move it — the right tool for anything with a
mechanical interface (a USB connector that must protrude, a module whose antenna
must point off-board). Fixed parts are held out of the position gradient but still
pull their nets (so nearby parts cluster around them).

| Key           | Meaning                                                                                                                                                                                                                                                               |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `edge`        | Sit flush against `north`/`south`/`east`/`west`.                                                                                                                                                                                                                      |
| `align`       | Position along the free axis: `left`/`right`/`center` (default center).                                                                                                                                                                                               |
| `at`          | Explicit `[x, y]` centre (mm). Overrides `edge`/`align` when you know the exact spot.                                                                                                                                                                                 |
| `rot`         | Orientation (degrees CCW); snapped to 0/90/180/270.                                                                                                                                                                                                                   |
| `side`        | `top` or `bottom`.                                                                                                                                                                                                                                                    |
| `overhang_mm` | Protrude this far _past_ the edge (with `edge`) — for a connector whose mating face must clear an enclosure wall so a cable seats fully. Negative insets it inward. The board outline is cut at the edge, so the connector body pokes out while its pads stay inside. |

For example, an edge USB-C is `USB1: { edge: south, overhang_mm: 1.5 }` — the
connector body extends 1.5 mm past the board edge, its pads on-board.

### `edge_align` — pull to an edge (soft, or hard on request)

Attracts a part toward a board edge without nailing it there. Use for user-facing
controls and edge connectors that should be reachable but whose exact position
along the edge the optimizer may choose. `edge_align` does not turn the part: set
its facing with `orientation` (for example, the long axis along the edge).

By default the pull acts during global placement only, so legalization may still
move the part off the edge. With `hard: true` the part's courtyard also stays
within `tolerance_mm` of the edge through legalization, and a placement where it
does not is illegal. Parts on the same edge slide along it and may change order.

| Key            | Meaning                                                                              |
| -------------- | ------------------------------------------------------------------------------------ |
| `edge`         | Target edge (required).                                                              |
| `side`         | `top`/`bottom`: with `board.sides: double` the part is placed and held on that side; a single-sided board keeps its source side. |
| `weight`       | Penalty weight (default 5.0); higher pulls harder.                                   |
| `hard`         | `true` keeps the part at the edge through legalization (default `false`).            |
| `tolerance_mm` | With `hard`: largest courtyard-to-edge distance (default 1.0, at least 0.5).        |

```yaml
edge_align:
  SW1: { edge: south, hard: true, tolerance_mm: 1.0 }
orientation:
  SW1: 0 # the long axis along the south edge
```

### `keepout` — exclude a region (hard)

A region where no part (and, at detailed-route time, no copper) may go — antenna
clearance, a mounting-hole boss, a shield footprint. Two forms:

- **Relative to a part** — `ref` + `extent: {edge, depth_mm}`: a band `depth_mm`
  deep hanging off the named part's courtyard edge. Moves with that part, so an
  RF module's antenna clearance stays correct wherever the module ends up.
- **Absolute** — `polygon: [[x,y], ...]`: a fixed region in board coordinates
  (taken as its bounding box in v0).

Give each keep-out a `name` so warnings and reports are legible.

### `copper_keepout` — no foreign copper in an area (hard, routing)

Copper keep-outs restrict tracks, vias and pours (placement ignores them; use
`keepout` for parts). The router keeps its copper out, and writeback writes a KiCad
rule area named `PNR keepout:<name>` so KiCad's DRC enforces the same area.

```yaml
copper_keepout:
  - { ref: U5, rect_mm: [-3, 8, 3, 12] } # v0: every copper layer, every net
  - name: rf_region # v1
    polygon: [[15, 33], [30, 33], [30, 46], [15, 46]] # or rect: [x0, y0, x1, y1]
    layers: [F.Cu, In1.Cu, In2.Cu] # default: every copper layer
    items: [tracks, vias] # default: tracks, vias, pours
    exempt_groups: [RFM1_MACRO] # a fixed_block's group never violates it
  - name: rf_guard
    rect: [10, 28, 21, 46]
    layers: [F.Cu, In1.Cu, In2.Cu]
    allow_classes: [RF, PWR, GND, ANALOG] # declared net_class names (or dp_<pair>)
    allow_nets: ["VREF*"] # literal names or globs
```

- **v0** (`ref` + `rect_mm`, nothing else): a rectangle in the part's own frame that
  follows the part (mirrored with its pads on the bottom side), on every copper
  layer; the rule area forbids tracks, vias and fills. Unchanged.
- **v1** (any of the keys below): the area is `polygon` or `rect` in board
  coordinates, or `ref` + `rect_mm` in the part's frame; it needs a unique `name`.
  - `layers` lists copper layers of the board. Tracks are barred on the listed layers
    the router uses; vias wherever any listed layer bars them, as in KiCad (a through
    via crosses every layer), so an inner plane layer in the list keeps vias out. A
    cell is barred when its centre comes within half the widest track (the via
    radius) plus half a cell diagonal of the polygon.
  - `items` chooses what is barred: `tracks`, `vias`, `pours` (fills; writeback only).
  - `allow_classes` / `allow_nets` exempt nets: the router bars only the other nets
    (per-net masks the maze, its dense kernels and the escape planner all read).
  - `exempt_groups` names `fixed_block` groups whose copper may sit in the area.

A v1 keep-out without allow lists or exempt groups is a rule area with KiCad's own
flags on its layers. With them, the rule area forbids nothing itself and writeback
adds one custom rule per layer to the board's `.kicad_dru`, in a block between
`# >>> yapnr copper_keepout` and `# <<< yapnr copper_keepout`:

```text
(rule "yapnr keepout rf_guard F.Cu"
  (layer "F.Cu")
  (condition "A.intersectsArea('PNR keepout:rf_guard') && !A.hasNetclass('RF') && ... && A.NetName != 'VREF1' && !A.memberOfGroup('RFM1_MACRO')")
  (constraint disallow track via))
```

Only that block is replaced on a later writeback; the fab profile's generated rules
and a hand-written file's own rules stay (the profile regenerates its rules around
the block). Pours become `disallow zone`, which KiCad's DRC judges but its zone
filler does not, so such a keep-out with `pours` (the default) is also cut out of
every plane zone the engine forms (`pnr.writeback.clip_keepout_pours`) whose net it
does not allow. A zone drawn in the source is never changed: one such a keep-out
would flag is listed on stderr (`drawn zones inside a keepout that bars their
pours`). A keep-out on a layer the router does not route (a dedicated plane)
affects vias and pours only.

### `fixed_block` — copper kept exactly as drawn (hard, routing)

A fixed block is copper the engine must not change: an RF macro, an antenna feed, a
matched meander. Draw it on the source board and put it in a KiCad group; the group
may hold tracks, **arcs**, vias, zones, rule areas and footprints.

```yaml
fixed_block:
  - name: rfm1
    group: RFM1_MACRO # the KiCad group on the source board
    anchor: U1 # optional; a part with a fixed pose: the frame of the digest
    sha256: 8301dcd5... # optional: the copper digest, checked at export and validate
    solid_layers: [In2.Cu] # its copper zones on these layers are obstacles
    refs: [RFM1] # its footprints (also read from the group at export)
```

- **Placement.** The block's footprints are held out of the placement graph
  (`pnr.fixed_block.hold_out`), as mounting holes are; declare a `keepout` over the
  block so parts stay off it. Its nets keep their other pins; KiCad sees them joined
  through the block's copper.
- **Routing.** The copper is exported to `fixed.json` (schema 2: `arcs` and
  `blocks`, see `pnr.fixed_block`) and reserved as copper its own nets own: other
  nets keep their clearance from every track, arc (chords within 1 um), via, pad and
  solid zone, its own nets may join it. A solid zone on a layer the router does not
  route keeps foreign vias out. A net with pads in the circuit joins the block at a
  **port**: for each connected piece of the net's block copper that no pad already
  reaches, the free end of its tracks and arcs nearest the net's pads (a piece without
  a free end, such as a ground rail between fence vias, joins at its nearest via). The
  plane nets join through their planes instead. Rule areas in
  the group bar what their flags say. The regression runner carries the copper in
  `rules.json` (`fixed_copper`), so every route of the placement loop sees it.
- **Writeback** keeps the group's tracks, arcs and vias (they are not preview
  routing); zones and footprints stay where the source has them.
- **Checks.** `pnr.fixed_copper --validate` requires the same digest (and the
  declared one), the same lock state of every block item and the same block zones,
  besides `fixed_copper_preserved` (which now covers arcs).
- **Digest.** `python -m pnr.fixed_copper BOARD --digest GROUP [--anchor REF]
  [--rename MAP.json]` prints it: every track, arc and via of the group, to the
  nanometre, in the anchor's frame (position and orientation), with layers, widths,
  via sizes and nets.

v0 limits: the series topology of a block with two ports on one net is not imposed
(the router joins the net's pads and the port nearest them, so the second end may be
left as a stub); placement does not see block ports (only its footprints' absence and
your `keepout`).

### `side_pref` — top/bottom bias (soft)

Biases a set of parts toward a side. The classic use is pushing decoupling caps
and passives to the back (`bottom`) to keep the top clean for the parts a user
sees or that need access. Keyed by side, valued by refs/globs.

```yaml
side_pref:
  bottom: [C*, R*] # all caps and resistors prefer the back
  top: [U*, J*] # ICs and connectors prefer the front
```

A `side_pref` takes effect only on a double-sided board (`board.sides:
double`), where placement chooses sides: there the preference is a cost (`weight`
x 5 mm of wirelength for a part on the other side), not a lock. On a single-sided
board (the default) every part stays on its source side and the compiler warns
that the `side_pref` is ignored.

With `board.sides: double` every part nothing holds is free. A part stays on its
source side when a hard `side` rule, a `fixed` pose, a source lock, a line group
or row, a drilled pad, a keep-out or copper keep-out tied to it, a plane-access
intent, a landing reserve or a pad on a `diff_pair` or `length_match` net holds it
(the pair router keeps a pair on one layer, and a part flipped on one leg would
lengthen that leg alone). An `edge_align` with a `side` puts the part on that side.

For free parts, global placement relaxes the side with the position and
rotation, the legalizer may take a slot on the other side, and a seeded detail
pass tries flips and pairwise swaps. Every side choice is costed in wirelength
millimetres: 3 mm for each non-plane net whose surface pins end up on both
sides without a drilled pin (a layer change), the `side_pref` cost, and 0.5 mm
for each part off its source side. On a double-sided board two parts with three
or more connected pads (ICs, not two-terminal passives) may not overlap on
opposite sides: a through via under such a stack would land on the far part's
pads, so neither could fan out there. A capacitor under an IC is allowed.
Writeback flips a bottom part as KiCad's
Flip does (mirrored footprint, every pad, graphic and text on `B.*`).
`fixed.side` and `side` are honored end-to-end.

### `group` — cluster a subsystem (soft)

Pulls members within `radius_mm` of an `anchor`, so a functional block (a
switching regulator and its inductor + caps, a crystal and its load caps) lands
together — shorter loops, less noise.

| Key          | Meaning                                                                                    |
| ------------ | ------------------------------------------------------------------------------------------ |
| `members`    | Refs/globs to cluster.                                                                     |
| `anchor`     | The ref they cluster around (usually the main IC).                                         |
| `radius_mm`  | Target radius (default ~5 mm).                                                             |
| `weight`     | Penalty weight (default 2.0).                                                              |
| `hard`       | `true`: each member's centre must lie within `radius_mm` (a placement outside is illegal). |
| `anchor_pad` | With `hard`: measure from the centre of this pad of the anchor, not from its origin.       |

A hard group measures each member's centre from the anchor's origin. For a part that
must sit at one pad of a bigger one (a snubber at an inductor's switch-node pad, a
decoupling capacitor at its ball), the origin can be several millimetres from the pad
that matters, so `anchor_pad` measures from that pad instead, at the anchor's pose,
rotation and side (a bottom-side anchor's pads are mirrored with it). The legalizer
places the anchor before its members and bounds each member by a disc about the pad;
the hard check (`group_outside`) and the benchmark checker's `proximity` check
(`anchor_pad`) measure the same point. A pad name the anchor does not have is
refused by name. The soft pull of global placement still aims at the anchor's
origin; the legalizer applies the pad.

```yaml
group:
  - { members: [C31], anchor: L2, anchor_pad: "2", radius_mm: 2.5, hard: true }
```

### `line_group` — hold parts in one rigid line (hard)

Keeps an ordered set of parts in one straight, evenly spaced line, all turned the
same way: a row of indicator LEDs, a bank of buttons. The placer moves and turns
the whole line as one rigid body (one position, one of four rotations); it may
turn the line by 180°, which reverses the order on the board.

| Key        | Meaning                                                                                  |
| ---------- | ---------------------------------------------------------------------------------------- |
| `name`     | Unique name (required).                                                                  |
| `members`  | Literal refs in line order (at least two; no globs).                                     |
| `pitch_mm` | Centre-to-centre spacing along the line.                                                 |
| `gap_mm`   | Courtyard-to-courtyard gap instead (default: `board.default_clearance_mm`).              |
| `rot`      | Every member's rotation in the line's frame (0/90/180/270, default 0).                   |
| `edge`     | `none` (default) or an edge: a soft pull of the whole line toward it.                    |
| `reason`   | Free text for reports.                                                                   |

```yaml
line_group:
  - name: chaser_leds
    members: [D1, D2, D3, D4, D5]
    pitch_mm: 3.0
    rot: 90
    reason: Chaser LEDs in one row, so the sequence reads as a line
```

A member may not also be `fixed`, in a `row`, `edge_align`, `orientation` or
`side`, the part of a ref-relative `keepout`, or in a hard `group`: a rigid line
cannot honour those. A member locked in the source board is refused too, when
placement starts. A soft `group` pulls the whole line. Members stay on the top
side and carry no plane-access intents. The line occupies the sides its members
occupy: a line of SMD parts may sit above a bottom-side part, and a drilled
member reserves both sides of the whole line.

### `region` — confine parts to an area (hard, or soft on request)

Keeps the courtyards of the listed parts inside an allowed area in board
coordinates: a clock section in one half of the board, a regulator in a corner,
an analog front end away from the switching supply. The area is one rectangle, one
polygon, or the union of several (`areas`). Several regions on one part all apply.

| Key       | Meaning                                                                                |
| --------- | -------------------------------------------------------------------------------------- |
| `name`    | Unique name (required).                                                                |
| `refs`    | Refs, globs or `@addresses` (at least one known part).                                 |
| `rect`    | `[x0, y0, x1, y1]` with `x0 < x1`, `y0 < y1`.                                          |
| `polygon` | `[[x, y], ...]`: at least three points, nonzero area, simple (concave is fine).        |
| `areas`   | A list of `{rect: ...}` / `{polygon: ...}` pieces; the area is their union.            |
| `hard`    | `true` (default): a placement outside is illegal. `false`: a penalty on the protrusion. |
| `weight`  | Soft penalty weight (default 10).                                                      |
| `reason`  | Free text for reports.                                                                 |

```yaml
region:
  - name: clock
    refs: [U1, "R[12]", C1, C2, C3]
    rect: [0, 0, 21, 32] # the west half of a 42 x 32 mm board
```

The part's body is its courtyard widened to its pads and silkscreen, where it
really lies about the footprint origin: a pin header measured from pin 1 or a
connector with an offset shell is not padded out to a box centred on its origin. A
rectangle, a single polygon, and a union whose pieces have only horizontal and
vertical edges are tested exactly. Other unions are tested on a raster whose grid
lines are the pieces' own coordinates plus a 0.25 mm grid; only the cells a sloped
edge cuts are refused, so the test is conservative by at most one cell along a
sloped edge. The legalizer uses that raster for every polygon and union. A region
on a part in a `line_group` or a hierarchical block acts on the member's body
inside the rigid line or block, at every rotation.

A region no placement can meet is refused before placement, with the region and
the part named: a fixed part outside it, or a part that fits nowhere inside it at
any allowed rotation within the outline, its keep-outs and its hard edge band.
Self-crossing polygons are refused when the file is read.

### `align` — share one coordinate (hard, or soft on request)

Makes the listed parts share an `x` (a vertical line) or a `y` (a horizontal
line): two ICs on one centre line, a row of buttons at one height, connectors flush
along one edge. Each part is measured at its `anchor`, evaluated at the part's
rotation and side, so a rotation moves a pad or edge anchor but never `origin`.

| Key      | Meaning                                                                                      |
| -------- | -------------------------------------------------------------------------------------------- |
| `name`   | Unique name (required).                                                                      |
| `refs`   | Refs, globs or `@addresses` (at least two known parts).                                      |
| `axis`   | `y`: the anchors share one y (a horizontal line); `x`: one x.                                |
| `anchor` | One for every ref, or a `{ref: anchor}` map (unlisted refs: `origin`); see below.            |
| `tol_mm` | With `hard`: the largest spread of the anchors (default 0.25; 0 asks for one exact line).   |
| `hard`   | `true` (default): a larger spread is illegal. `false`: a penalty on each anchor's deviation. |
| `weight` | Soft penalty weight (default 5).                                                             |
| `reason` | Free text for reports.                                                                       |

Anchors: `origin` (the footprint origin, as KiCad stores the position; the
default), `centre` (the centre of the pad bounding box), `pad1` or `pad:<name>`,
and a body edge: `south`/`north` with `axis: y`, `west`/`east` with `axis: x`. The
body is the one a region measures (above), so an edge anchor finds the real edge of
an off-centre part.

```yaml
align:
  - name: ics
    refs: [U1, U2]
    axis: y # one horizontal line through both origins
    tol_mm: 0.25
  - name: connector_faces
    refs: [J2, J3]
    axis: x
    anchor: east # their east courtyard edges flush
```

An align works with `row`, `line_group` and `edge_align`: a member of a rigid line
carries its anchor in the line's frame (two refs of one line are refused, as the
line already fixes their offsets), and a hard edge band and an align band both
bound the part. The compiler refuses an edge anchor that does not measure the axis.

The legalizer keeps each member within the band the members already placed leave
(at least 0.15 mm wide, since its slots are 0.25 mm apart), narrowed to where the
members not yet placed can still reach. A `tol_mm` under that band (0, say) is met
afterwards: the members move onto one exact line wherever every move stays legal.
An align already within its `tol_mm` keeps the legalized poses, which stay on the
placement grid that the router's grid follows. A band that holds no slot is
backtracked, and the legalizer fails with the part named if no arrangement fits.

An align no placement can meet is refused before placement, naming the align:
fixed members farther apart than `tol_mm`, members whose regions or edge bands keep
their anchors apart, or a `pad:<name>` the part does not have. Aligned parts stay
top-level parts in hierarchical placement. `PNR_POWER_FIRST=1` refuses a design
with a `region` or an `align`.

Both work with `board.sides: double`: a part free to take either side keeps its
regions and aligns there, measured with its pads, anchors and body mirrored on
the bottom (so a `pad1` anchor moves when the part flips, an `origin` never).

### `fanout` — escape an area-array part (BGA, LGA)

Plans every ball of a named area-array part before routing: signal balls escape
out of the array, balls of a net with a dedicated plane get their drop via, each
by the rules and the via classes you give. Without the section nothing changes.

```yaml
fanout:
  - name: u1
    ref: U1 # the part (fix it: its fanout is planned at its pose)
    skip_pads: [B4, B6] # balls left alone (copper another input owns, RF launches)
    via_classes: # first class naming a net wins; default takes the rest
      ground: { diameter_mm: 0.35, drill_mm: 0.15, nets: [GND], sites: [interstitial] }
      default: { diameter_mm: 0.40, drill_mm: 0.20, sites: [vacant, outside] }
    surface_rings: 2 # rings 0-1 may escape on the part's own layer
    escape_layers: [In2.Cu, B.Cu] # where a dog-bone hands a signal over
    forbidden_exits: [north, east] # or {F.Cu: [north]}: per layer
    reserved: # corridors kept free, in the part's frame (frame: board for absolute)
      - { rect: [-5.4, -0.2, -4.4, 0.2], layers: [F.Cu] }
    neck_mm: 0.10 # signal tracks inside the fanout (default: the net's width)
    neck_classes: [QSPI] # classes whose own minimum width the neck may go below
    drop_nets: [1V0_PA, "VDD*"] # routed nets whose balls take a via instead of an exit
    partial: { bridge: true } # a failed ball does not block its net (default: it does)
    lock: true # write the fanout copper locked (default)
```

| Key               | Meaning                                                                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `ref`             | The part. Its lands of the most common size form the lattice (pitch per axis, rings, vacant sites); other lands are obstacles. |
| `name`            | Unique name (default: the ref); the report and `fanout-<name>.json` use it.                                                     |
| `pads`            | Pad names or globs to fan out (default `*`, every netted ball).                                                                |
| `skip_pads`       | Balls the fanout leaves alone. Their lands stay obstacles.                                                                     |
| `via_classes`     | `name: {diameter_mm, drill_mm, nets, sites}`; `sites` from `interstitial` (a lattice cell centre), `vacant` (a lattice point without a ball), `outside` (beyond the array), `in_pad` (a filled via in the ball; needs the fab profile's in-pad class); optional `layers`, the only copper layers the class's nets may use (a board rule that keeps LVDS on the outer layers, say). Checked against the fab rules. |
| `surface_rings`   | Balls in rings below this may escape on the surface; deeper rings need a via (default 2).                                      |
| `escape_layers`   | Routing layers a dog-bone may change to (default: every routing layer of the stack but the surface).                           |
| `ring_layers`     | `{ring: [layers]}`: the only exit layers of that ring (the surface included by naming it).                                     |
| `forbidden_exits` | Board compass edges of the part no escape may leave across: a list (every layer) or `{layer: [edges]}`.                        |
| `reserved`        | `{rect or polygon, layers, frame}` areas no fanout copper enters (`frame`: `part`, the default, or `board`).                    |
| `neck_mm`         | The signal track width inside the fanout; the router continues at the net's own width from the exit. It narrows a signal below the fab's default track width, never below a minimum the net has of its own (a class `width_mm`, a width from `current_a`, an electrical outer width or terminal budget) unless `neck_classes` names one of its classes, never below a terminal width contract, and never widens; `fanout.check` refuses a value under `min_track_width_mm`. The validator takes each declared neck as an authorized short escape: the pad's required entry width is the neck's (`pnr.pad_entry.fanout_neck`), and the plan lists them (`diagnostics.necks`). |
| `neck_classes`    | Net classes (or `dp_<pair>`) whose own minimum width `neck_mm` may go below (needs `neck_mm`; an unknown name is a warning). |
| `drop_nets`       | Routed (non-plane) nets, names or globs, whose balls drop a via of their class beside the ball, as a plane ball does, instead of escaping across the array edge (no exit, no neck): a supply decoupled under the array or fed from another layer. The router takes each via as the net's terminal on the layer opposite the part and routes on from it. |
| `partial`         | `true` or `{bridge: true, retry: false}`: a ball whose escape fails (no plan, a conflict on the board, no access cell, or an access cell another fanout's tail took) no longer blocks its net, which routes among its other terminals (below). Default: the net is blocked. |
| `lock`            | Write the fanout copper locked (default `true`), so later passes leave it alone.                                               |
| `bottom_sites`    | `{parts, max_stub_mm, zone, rotations}`: decoupling sites under the array on the bottom side (below).                         |
| `variant`         | A seeded permutation of the planner's tie-breaks (default 0, none).                                                            |

How it works (`pnr/fanout`): the lattice gives the sites; tracks run on the half
lattice (through the channel between two balls, the interstitial sites and the
vacant ones, orthogonal or at 45 degrees) and every object is judged on exact
geometry against the lands, fixed copper, `copper_keepout`s (a v1 keepout bars
only its `items` on its `layers`, and lets its allowed nets and classes through, as
the router does), a fixed block's rule areas (tracks on their own `layers` only),
mounting holes, reserved corridors, the outline and the fab's via, hole and edge
rules, each with 1 µm added (the native Oracle's margin, so the engine's own gate
accepts every planned item). All balls
are assigned together by negotiated congestion, so signals and drops share the
sites: most signals escaped first, then most drops, then the least length and vias.
A ball with no legal path is reported `failed`, with its reason. At 0.65 mm pitch
with 0.32 mm lands and 0.10/0.10 rules a 0.35/0.15 via fits an interstitial site
and a 0.40/0.20 one does not, and one 0.10 mm track fits between two balls.

The router reserves the planned copper, routes each escaped signal on from the
first free grid cell beyond its exit, straight out or, when a part closes that
way, turning up to 75 degrees within 2 mm (the tail keeps each foreign pad at the
larger of the two nets' class clearances and is judged against class keepouts
exactly, as the plan was; the fanout copper keeps other nets' vias at the largest
class clearance), and leaves the fanned-out balls to the plan.
A ball whose exit finds no such cell is not emitted, and its planned copper goes
back to the maze;
the drop via of a plane ball is its connection. The fanout's vias keep their class
(`routes.json` `via_sizes`), and its copper is written locked (`locked`). The
route report's escape diagnostics carry a `fanout` block per fanout (escaped
signals, drops, via sites, failures, balls without an access cell).

With `partial`, a ball whose escape fails no longer blocks its net. With `bridge`,
the router first tries a straight surface stub, at the ball's planned width, to the
nearest adjacent ball of its net (orthogonal, then diagonal) whose escape stands,
judged exactly against foreign pads at the larger class clearance, the escape copper
and vias, keepouts, rule areas and the fanout's `reserved` areas; the stub is written
with that ball's escape (locked with it). At 0.65 mm pitch with 0.32 mm lands an
orthogonal stub keeps 0.44 mm from the side balls and a diagonal one crosses the
interstitial site (so it fits only where no other net's via sits there). Otherwise
the ball goes back to the board's escape planner for a second try, unless
`retry: false` (that planner knows none of the fanout's via classes, class layers or
necks, so a pair or a class kept to the outer layers may want to stay out of it).
A ball neither joins stays open: its net routes among its other terminals (a net left with fewer
than two is still blocked), the ball is a failure site, the route's `partial_open`
names it (`{net: {"U1.P14": reason}}`, and the route is not fully routed), and the
fanout report lists every such ball under `partial` (`{ball: {net, reason,
outcome}}`, the outcome `bridged to P15`, `escaped by the board's escapes` or
`open`). KiCad's DRC reports the open ball as unconnected.

A plane ball that already touches fixed copper of its own net (a pour or track of
a fixed block, `fixed_copper` `polygons`) is joined by it and gets no drop; other
nets keep their clearance from that copper. Each pair of nets keeps the larger of
their class clearances, as KiCad's DRC judges them, and a via class must meet the
judge's minimum via (`min_via_diameter_mm`, else `via_diameter_mm`).

`bottom_sites: {parts: [C50, C56], max_stub_mm: 0.5, zone: interior}` puts each
listed part (in priority order) on the bottom side under the array, where every pad
clears the fanout's vias and bottom tracks and lies within `max_stub_mm` of a
fanout via of its net; `interior` keeps the sites inside the array's outermost
fully vacant ring (else inside ring 2), `shadow` allows the whole array. Placement
takes the sites as fixed bottom poses; parts no site fits are reported. Other bottom
parts are kept out of the array by the side policy only. A site part's pad on a net
with a dedicated plane may drop by a stub on its own layer to a fanout via of its
net within `max_stub_mm` of its land (the via reaches the plane): the drop planner
offers that first, before drilling a via of its own.

`python -m pnr.fanout plan GRAPH --rules RULES --out DIR` writes the plan
(`fanout-<name>.json`: copper, every ball's terminal, diagnostics) without routing;
`python -m pnr.fanout verify BOARD --rules RULES --out DIR --kicad-cli CLI` (KiCad's
Python) adds it to a copy of the board and judges it with the native Oracle and
KiCad's DRC. Balls on a net with a dedicated plane drop only on a board that
declares its copper stack; without one the plane stage drops them.

### `plane_partition` — supply rails sharing a plane layer

Divides one dedicated plane layer (typed `power`) among the rails whose net classes
name it as `plane_layer`. Without it, every rail but the one with the most pads gets
its pads' bounding box plus 2 mm, and the boxes of rails whose balls spread over
one package overlap. Without the section nothing changes.

```yaml
plane_partition:
  - layer: In3.Cu
    nets: [1V0_RF1, 1V0_RF2, 1V2, 1V8] # names or globs; each a plane net of the layer
    order: current # by peak current, then terminal count (or: listed)
    split_gap_mm: 0.3 # copper gap between two rails
    min_width_mm: 1.0 # a rail's narrowest trunk
    fill: GND # what is left (a zone over the outline at priority 0), or absent
    core_no_vias: true # other nets' vias stay out of each trunk's core
    terminal_reach_mm: 0.8 # a pad without a drop yet: where its drop will land
    currents: { 1V2: 1.0 } # A; default the @pnr-current peak, else the class current_a
    budgets_mohm: { 1V2: 12 } # widens a trunk for its IR budget (default: its ir_drop budget)
    sources: { 1V2: { "@pmic.fb_1v2": "2" } } # the trunk's root (default: the central terminal)
    h_mm: 0.1 # the raster
    protect_fanouts: true # declared fanouts' access cells stay open (default false)
    fixed_lands: true # own-net fixed pads and zones on the layer are terminals (default false)
```

How it works (`pnr/plane_partition.py`, run by the router after the declared
fanouts are planned): the layer is rasterized inside the outline less the edge
clearance, with every other net's through copper blocked at the larger class
clearance of the pair, as KiCad's fill keeps it (planned fanout vias, fixed vias,
plated holes, mounting holes), fixed copper of other nets on
the layer, and the `copper_keepout`s that bar pours there (except for the nets they
allow). A rail's terminals are its planned drop vias (fanout and fixed vias) and,
for a surface pad without one, the disc within `terminal_reach_mm` of it. First
every rail is connected at its minimum width: a Steiner tree over its terminals
(Dijkstra from the tree to the nearest remaining terminal; a cell costs more where
the trunk would be narrower than it should be and inside another rail's pad disc),
keeping `split_gap_mm` of copper from every other rail and leaving the pad discs of
the rails still to come free; a tree passes only where a zone of the fab track width
(the minimum width writeback gives the zones) fills, so a neck KiCad would not fill
leaves its terminals unreached instead of on an island. A rail left with an unreached terminal is tried first
in turn, and the order with the fewest unreached terminals wins. Then each trunk
widens, the higher current first, to the largest of `min_width_mm`, the IPC-2221
internal width for its current (at the layer's copper) and `R_sq L / R_share` for
its IR budget (`R_share` the budget less two via barrels, at least a quarter of
it), where no other rail's copper or pad disc is. The territories then grow over
the free cells round the board (breadth first), grown copper is carved back to keep
the split gap, and each territory becomes polygons with holes (a hole holding only
another net's antipad is filled: KiCad clears it). The regions replace the
bounding boxes on that layer for every drop the router plans (`pnr.stack.
PlaneAccess`), and `routes.json` carries them (`plane_regions`) to writeback, which
replaces that layer's zones of those nets with one zone per region (its holes and
priority; the fill net under them all). With `core_no_vias`, other nets' vias keep
via radius plus clearance beyond half the minimum width of each trunk's centre line,
so a row of vias cannot cut a rail's neck.

A fixed block's vias count where their copper is: a through via of a rail is one of
its terminals (a macro's feed vias, say), another net's blocks it, and a blind, buried
or micro via whose span misses the layer does neither. With `fixed_lands: true` a
fixed block's own-net pads and zones on the layer (a macro's tie on the plane layer)
are land terminals too, by their outlines.

With `protect_fanouts: true` the plane machinery leaves a declared fanout's planned
access cells (where each ball's tail meets the maze) open: no other net's exit or
plane drop is planned whose copper or via keep-out takes such a cell (a cap's drop
via beside a ball's tail closed its only way out), and the trunk cores' via
keepouts spare a via site at each of them. Without the key nothing changes.

**Outer pours (`region`).** An entry with a `region` partitions a routed layer (an
outer layer, typed signal) inside that region instead: a power stage whose hot-rod
lands (0.25 x 1.82 mm at 0.5 mm pitch, say) no track can enter at its class
clearance, connected by copper that overlaps each land along its length.

```yaml
plane_partition:
  - layer: F.Cu
    nets: [5V_SYS, SW_B0, SW_B1, 1V0_BUCK, GND] # any nets of the board
    region: { refs: [U2, L1, L2, C30, C31], margin_mm: 0.5 } # or a polygon [[x, y], ...]
    terminals: pad # whole lands (default reach: discs as on a plane layer)
    connect: solid # the zones' pad connection (or thermal; default: the zone's)
    stitch_vias: 4 # through vias into each pour of a net with a dedicated plane (default 1)
    split_gap_mm: 0.2
    min_width_mm: 0.25
```

The region is the polygon, or the bounding box of the parts' courtyards at their
placed poses plus `margin_mm`; the raster is that region. A rail's terminals are its
surface pads on the layer inside the region (with `terminals: pad` the whole land,
claimed before any tree like a via land), its plated holes and its planned or fixed
vias there. Other nets' lands, escape tracks and vias and fixed copper on the layer
are blocked at the pair's clearance, and every other net's land in the region keeps a
way out: before the territories grow, the shortest corridor of free cells no rail
claims from the land to the region's edge, a track with its clearance wide, is held
out of the growth (a land without one is reported under `walled_in`). The zones are
drawn at priority 100 and up, above the layer's other zones (which writeback keeps),
with `connect` as their pad connection; `fill` does not go with a region.

The router (`pnr/route/detail/pour.py`) claims each territory for its net on its layer
(tracks and vias of other nets keep out; a cell another net's pad already owns is left
to it, and KiCad's fill keeps the clearance there), plans no escape or plane drop for
the pads a territory covers, gives a net whose other pads still route one access cell
in its territory (the maze joins them to the pour), and drills `stitch_vias` through
vias inside each pour of a net with a dedicated plane, at sites where a drop of that
net may land. The escape diagnostics list each pour's pads and stitches (`pours`).

The route's escape diagnostics carry a `plane_partition` report per layer: per rail
its current, width (and the IPC and budget widths), tree length, terminals reached,
the unreached ones (also failure sites; a pad's drop then fails in the drop planner,
as a pad outside its region does), connected pieces, area, the narrowest width along
its trunk (`core_min_mm`), and the smallest gap between rails. `ir_drop` (below)
measures the result on the routed board.

### `pour` — an outer-layer pour whose pads count as connected after the refill

A net poured on an outer layer (a GND flood on B.Cu, the fallback for bottom-side
decoupling caps under an array and for connector returns) joins that net's pads on
the layer itself: the router plans no escape and no plane drop for them (the escape
diagnostics list them as `pour_pads`), and writeback's fallback drops leave them
alone.

```yaml
pour:
  - layer: B.Cu # an outer layer
    net: GND
    stitch: true # stitch islands with pads to the net's other zones (default true)
    connect: thermal # the zone's pad connection: solid or thermal (default)
    clearance_mm: 0.2 # default: the fab clearance
    min_width_mm: 0.15 # default: the fab track width
```

`pnr.planes` (KiCad's Python; both the planes and the refill steps run it) draws the
pour over the outline where the board has no zone of that name yet, at priority 0 so
every other zone of the layer fills first, fills it, and judges it after the fill
(`pnr/pour.py`): each filled island that holds a pad of the net but no via or plated
hole of it gets a through via at the site nearest its pads where the via's land lies
inside the island, it clears every other net's copper on every layer by the
clearance, and another zone of the net (its plane) fills there; then the board is
refilled. An island without such a site is reported (`planes: pours {...}`, a
warning per island). KiCad's DRC remains the judge of what is connected. Without the
section nothing changes.

### `ir_drop` — the DC drop of a supply rail

Asks for a report of a rail's copper resistance on the routed board. It is a
report: it changes no copper, and it fails a run only with `hard: true`.

```yaml
ir_drop:
  - net: 1V0_RF1
    sources: { "@pmic.fb_rf1": ["2"] } # {part: [pads]} or ["FB3:2", ...]
    sinks: { "@radio.u1": [G5, H5, J5] } # the same forms, or all (default: every other pad)
    current_a: 2.5 # default: the net's @pnr-current peak, else its class current_a
    split: equal # each sink draws I/n; area: by pad area
    budget_mohm: 4.0 # or budget_mv
    temperature_c: 60 # copper resistivity at this temperature
    h_mm: 0.1 # the plane raster
    two_point: true # also each sink's resistance with the others open (a solve per sink)
    hard: false
```

`python -m pnr.ir_extract BOARD --rules RULES --out DIR [--heatmaps]` (KiCad's
Python with numpy; `pnr.staged_signal` runs it after the refill when the rules
carry `ir_drop`) reads the rail's copper from the board: the filled zones per
layer, tracks and arcs with their width, vias with drill and span, every pad of the
net, and the copper thickness and depth of each layer from the board's stackup
block. Fixed-block copper counts like any other. `pnr.ir_drop` then builds a
resistive network: zones and pads rasterized at `h_mm` (one square of copper,
`t / rho`, between neighbouring cells), tracks as exact resistors `rho L / (w t)`
(joined at end points, to the cell under each end and at T joins), each via a
chain of barrel segments `rho dz / (pi (d + t) t)` with 20 um plating and its land
one node per layer. The source pads are held at 0 V and each sink draws its share
over its pad; Jacobi-preconditioned conjugate gradients solve it to a relative
residual of 1e-10, after a connectivity pass that reports a sink no copper reaches
as **open** instead of a number. `ir.json` gives, per rail: the drop at each sink,
the effective resistance (worst drop / current), the two-point resistance of each
sink with the others open (`two_point`), the I²R loss, the largest current per mm of width on
each layer with its location and a `neck` flag where it is above what an IPC-2221
trace carrying the whole current would carry per mm, and `status` (`pass`, `fail`
against the budget, `open`, or `unsolved` when the solve stops short of its
tolerance: then no drop or resistance is given). Warnings take the quantified-assumption form, for
instance what the worst sink's drop would be if the whole current went to it.
With `--heatmaps` each layer's potential is written as a PNG. The extraction runs
under KiCad's Python; where that Python has no numpy (the container image's), the
solve runs in the numeric Python named by `PNR_PYTHON`, which `regression/run.py` and
`pnr.staged_signal` set. Copper only: the
resistance of parts in the path (ferrites, sense resistors) is not modelled. On the
`-rails` rung's VDD (320 thousand nodes at 0.05 mm) 0.1 mm gives 2.590 against
2.574 mOhm (0.6 %) in 4 s against 57 s of one solve.

### `net_class` / `diff_pair` / `length_match` — routing rules

These describe how nets are _routed_ rather than how parts are _placed_ — they
drive the detailed router (trace widths) and the post-route **quality pass**
(`pnr/quality.py`), which measures routed length, via count, differential-pair
skew, and length-match compliance. They are keyed by **net name** (not component
ref); net-name globs (`*hv`) expand against the real netlist.

```yaml
net_class:
  power: { width_mm: 0.4, clearance_mm: 0.3, nets: [lv, '*hv', GND, '*-GND'] }

diff_pair:
  - { name: usb, p: USB_DP, n: USB_DM, width_mm: 0.2, gap_mm: 0.15, skew_mm: 0.3 }

length_match:
  - { name: rgmii, nets: [TXD0, TXD1, TXD2, TXD3], tolerance_mm: 1.0 }
```

- **`net_class`** — a named width/clearance rule over a set of nets. Applied to
  the board's net settings in write-back, so **FreeRouting routes those nets at
  the given width** (e.g. power rails wider). The quality report rolls up total
  routed length per class. A class `clearance_mm` larger than the fab's holds
  between its nets and every other net, as KiCad's DRC judges two nets (the larger
  of their clearances): the grid router's exact escape and drop checks against pads
  and escape copper use it, and so does a fanout's hand-over; with
  `board.class_clearance: maze` the maze keeps it too. A pad whose footprint sets
  its own clearance or solder mask margin (a fiducial's `(clearance 0.6)
  (solder_mask_margin 0.5)`) keeps foreign copper the larger of the two away (the
  margin plus 1 µm: copper inside the aperture is a mask bridge); margins of 0.05 mm
  or less (KiCad's BGA land default) are not read. A class may also set
  **`plane_layer`** (e.g.
  `In1.Cu`): its net is **poured as a copper plane** on that layer instead of
  being trace-routed — the right home for a high-fanout ground or power net on a
  multilayer board (each pad reaches it with a short via, and the router only has
  to route signals). Use **one net per plane layer** (a full-board pour is a
  single net, or they short). Example:

  ```yaml
  net_class:
    gnd: { nets: [GND], plane_layer: In1.Cu } # ground plane on inner layer 1
    v3v3: { nets: [3V3], plane_layer: In2.Cu } # 3V3 plane on inner layer 2
  ```

- **`diff_pair`** — two nets (`p`/`n`) with `width_mm`/`gap_mm`; the quality pass
  reports their routed-length **skew** and flags it if it exceeds `skew_mm`
  (default 0.5). `skew_ps` gives the budget as a delay instead (each layer's
  propagation delay from the board's stackup); a pair gives one or the other. The
  native electrical flow routes a pair coupled; the own grid router routes its two
  legs as two nets and matches their lengths (the route report says how much of the
  P leg runs beside the N leg).
- **`length_match`** — a group of nets whose routed lengths must agree within
  `tolerance_mm` or `tolerance_ps` (not both); the quality pass reports the group
  **spread** and flags it if it exceeds the tolerance.
- **`tuning`** (optional) — the meander rules for both: `gap_mm` (edge to edge,
  at least the clearance and the track width; without it three track widths where
  that is enough, else the minimum), `amplitude_max_mm`, `min_segment_mm`,
  `max_added_mm` (the meander length one net may gain), `style` (`auto`,
  `trombone`, `serpentine`, `accordion`), `mitre` (45-degree corners, default on),
  and two switches, both on by default: `meanders` (the router tunes the sets after
  routing) and `placement` (placement keeps the members' estimated lengths even).

The own detailed router **tunes** every declared pair and group after routing
(`pnr/route/detail/tune.py`): each member shorter than the longest gets meanders on
straight runs of its own path and layer, legal by the router's own clearance rules,
until the spread is within the budget. Lengths are measured as KiCad's DRC measures
them (`pnr/length_model.py`: merged track lines straightened inside pads and vias,
plus each via's span through the stackup), so a KiCad `skew` or `length` rule in mm
sees the same numbers. A budget in ps is judged by the engine's own audit
(`pnr.quality`): KiCad 10.0.6's `kicad-cli pcb drc` reads every delay as 0 ps
(KiCad issue 23868), so it cannot judge a time-domain rule. The stackup and the
exact lands of the matched nets' pads (a through-hole pad's circle or square, which
the graph does not record) come from the board itself: `python -m pnr.route ...
--board BOARD.kicad_pcb` (the `atopile_pnr` rule passes its source board); without
it the tuner takes KiCad's default stack for the board's layer count and a rounded
square for through-hole lands, and a board stackup whose copper layers are not the
board's layer count is not used.

The per-set result (status, lengths, layers, margin, meanders and their gap) is in
the route report and in `routes.json` as `length_tuning`; a set left outside its
budget is `length_unmatched` and named on stderr. When the short members have no
room left for meanders, the longest member is routed again around the other nets
(vias priced high, so it may change layer) and kept if it is shorter (`rerouted` in
the report; never on a board with blind, buried or micro vias, see above). A
group member boxed in by its own neighbours (a bus routed at its pins' pitch round
a corner, where the inner members are the short ones) gets room
instead: from the route as it was before tuning, each member is routed again with
the others in place, steps close to another member priced a little higher, so the
bus fans out where the board has room; the set is tuned again and whichever attempt
ends closer is kept (`spaced` in the report). A pair's legs are never routed apart.
A set that still misses its budget keeps its meanders only when they closed at
least half of the gap; otherwise it goes back to the route as routed (`reverted`).

Pairs are tuned before groups. A group may lengthen a pair's legs (both, toward its
longest member); a pair it puts out of its budget is tuned again, and if that fails
the group's tuning is undone (`conflicts`). A net in two sets is never routed again.
Copper the route keeps as it is (a hierarchical block's, or pairs routed before the
grid) counts in its net's length; a set whose nets leave a hierarchical block is
tuned on the whole board, not in the block (`partial`).

Placement prepares for this: global placement pulls each set's members toward equal
estimated lengths, and after legalization the small parts on matched nets (series
resistors and the like) move to the legal slot that keeps the legs even
(`pnr/place/matched.py`), so two series resistors of a pair do not end up at
different distances from the connector. The candidate routes of the initial
placement pool, the Monte-Carlo screen and the hierarchical knit rank a route with
fewer sets outside their budgets ahead of fewer vias and less copper.

```yaml
diff_pair:
  - { name: usb, p: USB_DP, n: USB_DM, width_mm: 0.2, gap_mm: 0.15, skew_ps: 2.0 }
tuning: { gap_mm: 0.3, amplitude_max_mm: 1.0, style: serpentine }
```

The quality report ships in the fab bundle as `quality.txt`. Its diff-pair /
length-match checks are **advisory** by default (reported, not enforced); set
`quality_gate = True` on the `atopile_pnr` target to fail the build on a miss.

**Routing completeness is always enforced.** If FreeRouting leaves any net
unrouted, the build **fails** with the count — a partially-routed board is not a
board. (`route_max_passes = 0` lets the router run to completion; set
`require_routed = False` only to inspect a deliberately-partial result.) `drc_gate`
similarly turns DRC violations into a build failure.

### `legalize` — legalizer options (opt-in)

The legalizer snaps the global placement onto a grid of slots (0.25 mm) and packs
the parts one by one. These options change how; each is off unless the file sets it,
and a design without the section is legalized exactly as before.

| Key         | Values                         | Meaning                                                                                        |
| ----------- | ------------------------------ | ---------------------------------------------------------------------------------------------- |
| `outline`   | `raster` (default), `exact`    | `exact`: every part's courtyard stays inside the outline by the test the hard check uses.      |
| `order`     | `blocks` (default), `scarcity` | `scarcity`: a part held by a hard region or edge band competes with the hard-group blocks.     |
| `lookahead` | `none` (default), `regions`    | `regions`: a slot that strands a held part with few slots left is refused (when another fits). |

```yaml
legalize:
  outline: exact
  order: scarcity
  lookahead: regions
```

`outline: exact` matters when the outline is not a whole number of slots (a 46.3 mm
board on the 0.25 mm grid): the slot raster then has a partial last row or column
that reaches past the edge, and a part packed there keeps its courtyard inside the
raster but up to a slot minus half the clearance outside the board, which the
placement's hard check refuses (`outside_outline`). With `exact` each slot centre is
bounded by the box in which the part's courtyard, at the tried rotation and side, lies
inside the outline, and the legalizer checks the result with the hard check itself.
The length-matching pass after legalization keeps off those cells too. The outline is
the `board.outline` rectangle; rounded corners are not modelled.

`order: scarcity` changes the order parts are packed in. The legalizer packs the
parts of hard groups (and aligns) block by block, picking next the block with the
fewest free slots per square millimetre of its parts, and only then every other part,
fewest free slots first. A part held only by a narrow hard `region` (a connector's one
window) therefore comes after every block and can find its window full. With
`scarcity` each part held by a hard region or a hard `edge_align` band, and in no
group, is a block of its own and takes its turn by the same measure, so a window part
goes before a roomy block; and once such a part has fewer free slots left than every
part of the block being packed (a roomy region a block is filling), it goes next.

`lookahead: regions` checks each slot before taking it: a greedy trial pack of the
parts still to place that are held by a hard group, region or edge band, can still
reach the slot and would keep at most 64 free slots once it is taken (a part that does
not fit even without the slot is stranded anyway and left out). When one of them no
longer fits, the next nearest slot is tried (up to 40), then the part's other turn;
when no turn has a slot that strands nothing, the nearest slot of the first is kept
and backtracking deals with the part. It is
the look-ahead of power-first placement (`PNR_POWER_FIRST=1`, which refuses regions)
for the default flow, and costs the trial packs: it is meant for boards with narrow
regions.

### Legalizer and global-placement switches (environment, opt-in)

Besides the per-design `legalize:` options above, seven engine switches change how global
placement hands parts to the legalizer and how the legalizer turns them. They are environment
variables (`pnr/legalize_flags.py`), off by default and independent of `PNR_COMPACT`; a run with
none set is placed exactly as before. The regression runner sets them from its own options
(it strips ambient `PNR_*` variables), and `provenance.json` records them.

| Variable                  | Runner option                 | Meaning                                                                                                                       |
| ------------------------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `PNR_GP_POLISH=1`         | `--gp-polish`                 | A final global-placement phase (200 iterations, turns frozen) whose overlap term uses the legalizer's own slots.            |
| `PNR_GP_CHANNELS=<l>`     | `--gp-channels L`             | The polish (implied) also weighs the legalizer's routing-channel cost by `l` (1 or 0.5 in the A/B).                          |
| `PNR_POOL_SOURCE_CLAMP=1` | `--pool-source-clamp`         | The initial pool's source start begins inside the outline (the cluster box under compact placement).                         |
| `PNR_LEGALIZE_HPWL=<w>`   | `--legalize-hpwl W`           | The legalizer weighs `w` times each part's wirelength (mm² per mm) and picks its turn among all four with the slot.          |
| `PNR_LEGALIZE_REORIENT=1` | `--legalize-reorient [wire]`  | After legalization, parts turn in place where that shortens their wires, stays legal and keeps their channels (`wire`: legal only). |
| `PNR_LEGALIZE_CHANNEL_CLEARANCE=fab` | `--legalize-channel-clearance fab` | The legalizer's routing-channel model spaces nets without a class at the fab clearance instead of the board's `default_clearance_mm`. |
| `PNR_LINE_SATELLITES=1`   | `--line-satellites`           | A `line_group` carries each member's satellite (a free two-pad part on a two-pin net to one member pad, such as an LED's series resistor) flush beside it. |

`PNR_LEGALIZE_HPWL` respects the turns a design pins: a part with an `orientation` (or a `fixed`
`rot`) keeps it, and a `line_group` turns only as one rigid line; parts on a `diff_pair` or
`length_match` net are legalized as without it. The in-place turns never touch
fixed, locked or oriented parts, `row` and `line_group` members, hierarchical blocks, or parts on
a `diff_pair` or `length_match` net, and run only where the placer may turn parts. A line
satellite is a part no constraint names, on the top side and on no matched net; a board with
`sides: double` has none. Design and measurements:
[compact placement, section 11](../design/compact-placement.md).

## How intent becomes a layout

1. **Ingest** reads the resolved board into a neutral graph (components, pads,
   nets, courtyards).
2. **Placement** (differentiable, design §4) minimizes smooth wirelength +
   spreading + your constraint penalties; `fixed`/`keepout`/outline are hard
   barriers, `edge_align`/`side_pref`/`group`/`region`/`align` are penalty
   gradients. Orientation is co-optimized (§9.3).
3. **Legalization** snaps to a strictly non-overlapping, in-outline layout that
   still honors the fixed poses and keep-outs, and the hard edge bands, groups,
   regions and alignments.
4. **Place↔route loop** (design §6) global-routes the placement, and where copper
   demand exceeds capacity it inflates those parts' spacing and re-places — until
   the board is routable, then FreeRouting does the detailed route.

So: **hard** constraints define the feasible region; **soft** constraints shape
the objective within it; and wirelength + routability do the rest.

## Tips

- Start minimal — fix only the parts with a real mechanical interface, add one or
  two `group`s for the noisy subsystems, and let the optimizer do the rest. Then
  tighten with `edge_align`/`side_pref` if the result needs nudging.
- If the place↔route loop won't converge (overflow won't reach 0), the outline is
  probably too small or a keep-out too large — give it more room.
- Weights are relative; bump one `weight` up a few× to make that preference win
  against wirelength, rather than hand-placing.
- The result is a first-spin layout for an EE to review, not a substitute for
  one. Fixed poses and keep-outs are trustworthy; soft preferences are advisory.

### Current-aware native routing and differential endpoint topology

An optional `electrical_fab` JSON on `pcb_pnr` enables the native electrical
routing stage after placement, signal routing and plane fill. Source annotations
are resolved by atopile instance address and pin number; never encode generated
reference designators. The report records source path, line and digest.

```ato
# @pnr-current {"target":"board.supply","pads":["1"],"rms_current_a":2,"peak_current_a":3}
# @pnr-current {"target":"board.sensor","pads":["1"],"scope":"terminal","rms_current_a":0.05,"peak_current_a":0.1}
```

A net-wide annotation sizes a distribution trunk. A terminal annotation sizes
only the isolated pad group it completely covers; it does not reduce the net's
trunk width. Branch allocations are explicit, not inferred from component names.
`neck_max_length_mm` authorizes only a short, pad-centered escape with checked
loss/drop budgets and a full-width continuation. Missing authorization does not
permit automatic neck-down. The compiler takes the maximum of fabrication,
explicit class width and current-derived minimum. Inner and outer copper use
separate current-width calculations. Via banks use RMS heating and peak-drop
budgets; protected source arrays remain protected during cleanup.

The fabrication model must declare copper weight, allowed temperature rise,
board thickness, minimum via plating and barrel loss/drop budgets. These are
engineering assumptions, not a thermal or fabrication qualification. The current
width calculation is an IPC-2221 screening approximation; validate against the
actual stackup, cooling environment and fabrication process.

`@pnr-pair` annotations supply an ordered terminal chain and bounded local
auxiliary branches; see the USB annotation in `splanc_mini.ato` for the JSON
schema. Width, gap and skew remain in the normal `diff_pair` rule. The native
adapter routes an envelope and offsets both conductors together, checks exact
mate clearance, tunes bounded length mismatch, and measures connected endpoint
paths including known via travel. It rejects ambiguous cycles and missing layer
heights. A pair-support placement proposal is accepted only with a complete
native-checked reroute of both polarities and preserved return connectivity.

Current limitations: coupled trunks use F.Cu; local branches may change layers.
The planner does not yet repair already-connected but poorly matched pairs.
Impedance qualification requires actual stackup dimensions and a separate
validated impedance calculation. The electrical audit reports these limitations
and cannot turn zero native opens into an electrical PASS by itself.
