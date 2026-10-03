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
| `layers`               | Copper layer count (2/4). Inner layers are treated as power/ground planes, so routing capacity scales with the **signal** layers. |
| `default_clearance_mm` | Minimum courtyard-to-courtyard gap enforced in legalization, and the track pitch the lookahead router assumes.                    |

The outline is _approximate guidance_: the placer frames the parts within it. Make
it a bit larger than the parts need — an over-tight outline forces congestion and
can leave the place↔route loop unable to reach zero overflow.

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
| `side`         | Preferred side (`top`/`bottom`).                                                     |
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

> Note: in the current MVP `side_pref` compiles to a soft term but legalization
> keeps parts on the top side (single-sided legalize); two-sided placement is a
> tracked follow-on. `fixed.side` is honored end-to-end.

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
the report). A group member boxed in by its own neighbours (a bus routed at its
pins' pitch round a corner, where the inner members are the short ones) gets room
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
   barriers, `edge_align`/`side_pref`/`group` are penalty gradients. Orientation
   is co-optimized (§9.3).
3. **Legalization** snaps to a strictly non-overlapping, in-outline layout that
   still honors the fixed poses and keep-outs.
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
