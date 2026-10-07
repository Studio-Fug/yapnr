# Route-then-compact and hull packing

Status: behind default-off flags (`PNR_ROUTE_COMPACT`, `PNR_MACRO_HULL`, `PNR_HULL_DOVETAIL`).
Code: `hardware/pnr/pnr/place/route_compact.py`, `hardware/pnr/pnr/hier/compact_block.py`,
`hardware/pnr/pnr/place/hull.py`, hooks in `regression/hier_case.py` and
`pnr.route.feedback.route_and_place`.

## The problem

Placement reserves room for routing before anything is routed: global placement spreads the
parts, the legalizer keeps channel room, and hierarchical blocks are placed as rectangles a
few times larger than their parts. Once the board is routed, most of that room turned out to
be unused: gutters between blocks hold a few tracks or none, and a block's rectangle is mostly
empty. Route-then-compact measures what the copper actually uses and gives the rest back.

## The pass

After the router has finished, along x and then along y:

1. **Measure each gutter.** For every pair of bodies that face each other along the axis
   (their extents overlap across it, within the placement clearance, on a shared side), the
   gutter between them holds some routed copper. Tracks running along the gutter each hold a
   lane (their width plus half the copper clearance on either side), vias hold their diameter;
   tracks crossing the gutter need no width, since they shorten with it. The gutter needs the
   union of its lanes on its busiest layer, plus a slack (one routing pitch).
2. **Compact in one dimension, preserving order.** The new positions solve a longest-path
   problem on a constraint graph: each facing pair keeps the gap its copper needs (never less
   than the placement clearance, never more than it has), consecutive bodies keep their order
   along the axis (so every left/right relation of the x pass and every above/below relation
   of the y pass survives, the legalizer principles), fixed parts, parts locked to an edge or
   region and parts carrying a keep-out do not move, keep-outs are obstacles, and line
   groups, rows and aligns move as one body. Bodies close up on an anchor line (the one of
   nine across the span whose result has the least wirelength), all motion in multiples of the
   placement grid, inside the extent the bodies already span: the outline does not change, and
   the outline shrink that becomes possible is reported.
3. **Rip up and route again.** The moved placement is routed again by the driver's own router.
   A step that adds a hard placement violation is not routed (the parts it names are held and
   the axis is planned again); a reroute that is worse (more missing connections, unresolved
   nets, unmatched pairs or groups, a declared pair the coupled router leaves as two legs, a
   plane pad left with no dog-bone site, more vias than 5 %, more copper than 1 % of the board)
   is refused, and the pass backs off: the next attempt closes each gutter half as far. After
   three refusals the axis keeps the routed placement it had.

The reroute is a callback, the seam where the push-and-shove router can later move the copper
instead of routing it again. The pass stops before a reroute that would run past 90 % of the
runner's stage budget, and an error in it keeps the routed result (and is recorded).

## Where it runs

`PNR_ROUTE_COMPACT=1` turns on every part; a comma list picks some:

| Part    | Where                                                       | Bodies                                                          | Reroute                                                                                            |
| ------- | ----------------------------------------------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `TOP`   | hierarchical top level (`hier_case.py`), after the knit     | block macros (their outline, or their hull) and top-level parts | the nets between the blocks are knitted again                                                      |
| `BLOCK` | each block template, after its layout is chosen             | the block's parts                                               | the block outline shrinks to what is left (the macro gets smaller) and every instance routes again |
| `FLAT`  | flat boards (`route_and_place`), after the place-route loop | the parts                                                       | `route_board` on the whole board                                                                   |

`pnr-report.json` carries `route_compact` with each step: the gutters measured (gap, need,
target, after), the bodies, the bounding box and outline shrink before and after, the route
metrics, the result and the seconds spent measuring, checking and rerouting. Each candidate
emits a live-view layout event; on hierarchical runs the trace records every candidate as a
`stage` scope with its poses, and the animation adds a fourth chapter that replays the kept
steps (the blocks move, then the knit routes again).

## Hull packing

`PNR_MACRO_HULL=1` gives each block macro its per-side occupancy hull from its routed copper
(`pnr.hier.extent`; the hierarchical driver measures it from the routes in memory). The
legalizer already packs hulls with FFT slot masks; route-then-compact now slides bodies by the
hull rectangles of each side, so a block closes into another's notch as far as the two
outlines allow. `PNR_HULL_DOVETAIL=W` adds a packing term to global placement over the hull
bodies (their smooth bounding box, weight `W` in wirelength millimetres per millimetre), so
blocks interlock instead of only meeting where wires pull them.

The rung `13-dovetail-blocks-23` (`regression/dovetail_rung.py`) has L-shaped blocks for it.

## Measured

A/B on GCP (2026-10-07), flags off against each arm; the hierarchical rungs on seeds 0-3, the
ladder (initial pool 8/3) and the hard rungs on seeds 0-1. Sums of per-rung means of the
bounding box of the placed bodies and of the routed board's copper and vias; KiCad's DRC
judges every board. The full tables are in the pull request and in
[the ladder's page](../regression-ladder.md#route-then-compact-opt-in).

| Rungs                                                | Arm                                              | Pass  | Bounding box | Copper  | Vias |
| ---------------------------------------------------- | ------------------------------------------------ | ----- | ------------ | ------- | ---- |
| hier-twin-bank, 10-quad-bank, 13-dovetail (12 cells) | off                                              | 12/12 | 5336 mm²     | 2181 mm | 144  |
|                                                      | `--route-compact`                                | 12/12 | -6.7 %       | -4.4 %  | +4 % |
|                                                      | `--macro-hull`                                   | 12/12 | -6.2 %       | -0.8 %  | +3 % |
|                                                      | `--route-compact --macro-hull --hull-dovetail 1` | 12/12 | -19.2 %      | -5.3 %  | +4 % |
| ladder 01-08 (16 cells)                              | off                                              | 16/16 | 3151 mm²     | 1136 mm | 70   |
|                                                      | `--route-compact`                                | 16/16 | -21.5 %      | -11.3 % | -3 % |

Three guards came out of the first runs: a legacy plane pad left without writeback's
dog-bone room (`08-chaser-20-plane`), a coupled pair whose copper the reroute moved
(KiCad's pair gap rule on `11-ufbga201-...-pairs`) and the place-route stage budget
(`12-soc-bga-113`).

## Limits and next steps

- The reroute is a full route of the affected nets: on the MCU rungs a step costs one to two
  minutes. The push-and-shove router can replace it.
- Gutters are measured on the route before the pass; the y pass measures the route the x pass
  left.
- Block compaction refuses most steps on the quad bank (the reroute takes more vias); the
  hull, not the block outline, is what packs those blocks.
