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

| Key         | Meaning                                            |
| ----------- | -------------------------------------------------- |
| `members`   | Refs/globs to cluster.                             |
| `anchor`    | The ref they cluster around (usually the main IC). |
| `radius_mm` | Target radius (default ~5 mm).                     |
| `weight`    | Penalty weight (default 2.0).                      |

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
| `neck_mm`         | The signal track width inside the fanout; the router continues at the net's own width from the exit.                         |
| `lock`            | Write the fanout copper locked (default `true`), so later passes leave it alone.                                               |
| `bottom_sites`    | `{parts, max_stub_mm, zone, rotations}`: decoupling sites under the array on the bottom side (below).                         |
| `variant`         | A seeded permutation of the planner's tie-breaks (default 0, none).                                                            |

How it works (`pnr/fanout`): the lattice gives the sites; tracks run on the half
lattice (through the channel between two balls, the interstitial sites and the
vacant ones, orthogonal or at 45 degrees) and every object is judged on exact
geometry against the lands, fixed copper, `copper_keepout`s, mounting holes,
reserved corridors, the outline and the fab's via, hole and edge rules. All balls
are assigned together by negotiated congestion, so signals and drops share the
sites: most signals escaped first, then most drops, then the least length and vias.
A ball with no legal path is reported `failed`, with its reason. At 0.65 mm pitch
with 0.32 mm lands and 0.10/0.10 rules a 0.35/0.15 via fits an interstitial site
and a 0.40/0.20 one does not, and one 0.10 mm track fits between two balls.

The router reserves the planned copper, routes each escaped signal on from the
first free grid cell beyond its exit, and leaves the fanned-out balls to the plan;
the drop via of a plane ball is its connection. The fanout's vias keep their class
(`routes.json` `via_sizes`), and its copper is written locked (`locked`). The
route report's escape diagnostics carry a `fanout` block per fanout (escaped
signals, drops, via sites, failures, balls without an access cell).

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
parts are kept out of the array by the side policy only.

`python -m pnr.fanout plan GRAPH --rules RULES --out DIR` writes the plan
(`fanout-<name>.json`: copper, every ball's terminal, diagnostics) without routing;
`python -m pnr.fanout verify BOARD --rules RULES --out DIR --kicad-cli CLI` (KiCad's
Python) adds it to a copy of the board and judges it with the native Oracle and
KiCad's DRC. Balls on a net with a dedicated plane drop only on a board that
declares its copper stack; without one the plane stage drops them.

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
  routed length per class. A class may also set **`plane_layer`** (e.g.
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
