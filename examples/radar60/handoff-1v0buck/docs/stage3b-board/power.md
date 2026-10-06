<!-- markdownlint-disable -->

# radar60 stage 3b — track power (A5 partial fanout, A6 plane partition, A7 IR drop)

2026-10-04. Design: `stage3b-board/design.md` §1 A5–A7 and §1.9.

- **Branch:** `claude/s3b-power` in `yapnr-wt/s3b-power`, off `origin/main` 21042fb.
  It is **local and not pushed**.
- **Records:** run records, scripts and renders are in `stage3b-board/power/`.
- **Where it ran:** on the Mac only, through `gapfix/heavy.sh`. **No GCP spend.**

## Commits

Merge after `s3b-clear` and `s3b-fanout`.

| Commit  | What                                                                                                                    |
| ------- | ----------------------------------------------------------------------------------------------------------------------- |
| f82a22f | A5 partial fanouts: `fanout[*].partial: true \| {bridge, retry}`                                                        |
| 88be2a8 | A7 `ir_drop:` section: `pnr.ir_extract` (KiCad) and `pnr.ir_drop` (numpy, CG)                                           |
| 1d5f5eb | A6 `plane_partition:` section: `pnr.plane_partition`, regions with holes, drawn by writeback and by the staged append   |
| 7ab1e11 | Hard rungs `11-ufbga201-fanout-6L-SGSGPS-partial` and `-rails`; checker kinds `unconnected`, `rail_zones` and `ir_drop` |
| 716faf0 | A partition on a layer not typed power is skipped, with a warning                                                       |
| db95f7f | IR raster defaults to 0.1 mm; `two_point` is optional and the checker skips it                                          |
| e98c729 | A terminal inside another terminal's disc counts as reached                                                             |
| 729ca56 | Steiner trees are 4-connected; the budget width uses the root-to-farthest path, capped at 10 mm                         |
| bcb8339 | `partial: {retry: false}` keeps failed balls out of the generic escapes                                                 |
| 01d2392 | A rail's region is only the copper joined to its tree                                                                   |

Every feature is opt-in. When it is not declared, rules.json, the fanout plan's digest,
routes.json and BoardRoute stay byte-identical. The new keys are emitted only when
declared; `Region.holes`, `partial_open` and `plane_regions` are empty by default.

## The features

**A5 — partial fanout.** A failed ball no longer blocks its net. A ball fails when it has
no plan, conflicts with another part's pad, has no access cell, or loses its access cell
to another fanout's tail.

1. With `bridge`, the ball gets a straight surface stub to an adjacent ball of the same
   net whose escape stands. The stub uses the planned width and is checked exactly
   against foreign pads (at class clearance), escape copper and vias, keepouts, rule areas
   and `reserved` areas.
2. Otherwise it gets a second try through the board's generic escape planner, unless
   `retry: false`.
3. A ball that is still open becomes a failure site and appears in `partial_open`, so
   `fully_routed` is false. Its net routes among the remaining terminals; a net left with
   fewer than two terminals stays blocked.

The report lists `partial: {ball: {net, reason, outcome}}`.

**A6 — plane partition.** It runs after the fanouts are planned.

1. **Raster.** The layer is rasterized at 0.1 mm. Foreign through copper, fixed copper and
   pour keepouts are blocked.
2. **Terminals.** A rail's terminals are its planned via lands, plus a 0.8 mm reach disc
   for each pad that has no via yet.
3. **Connect.** Each rail is connected at minimum width by a Steiner tree (shortest-path
   heuristic), 4-connected. The order with the fewest unreached terminals wins; any rail
   with an unreached terminal is also tried first.
4. **Widen.** Each trunk is widened to max(min_width, IPC-2221 internal width,
   `R_sq · L_path / R_share`), at most 10 mm, wherever there is room.
5. **Grow and carve.** The territories grow breadth-first over the free cells and are
   carved back to the 0.3 mm split gap.
6. **Polygons.** Each territory becomes polygons with holes, and the fill net goes under
   them all.

`PlaneAccess` keeps every drop inside its own rail. `core_no_vias` keeps foreign vias
out of each trunk's minimum-width core. The report gives, per rail:

- width, tree length and path length;
- reached and unreached terminals (unreached ones are failure sites);
- pieces and area;
- narrowest trunk width;
- the smallest gap to another rail.

**A7 — IR drop.** The extraction and solve:

- **Plane copper.** Zones and pads are rasterized at 0.1 mm.
- **Tracks.** Tracks and arcs are exact 1-D resistors, including T joins.
- **Vias.** Each via is a barrel chain `ρΔz/(π(d+t)t)` with t = 20 µm.
- **Sources and sinks.** The sources are held at 0 V and the sinks draw I/n.
- **Solve.** Jacobi-preconditioned CG solves to ≤ 1e-10, after a union-find pass that
  reports an unreachable sink as an **open**.

`ir.json` holds:

- the drop per sink;
- R_eff and the two-point R per sink;
- I²R;
- peak A/mm per layer, with an IPC neck flag;
- pass, fail or open against the budget;
- quantified-assumption warnings;
- PNG heat maps.

`pnr.staged_signal` runs it after the refill when the section is declared. It gates only
with `hard: true`.

## Tests

- **Unit tests (Bazel targets):**
  - New: `fanout_partial_test`, `ir_drop_test`, `plane_partition_test`.
  - Updated: `hard_rungs_test`, `stack_test`.
  - IR analytic checks: a track within 0.1 %; parallel paths halve the resistance; a tree
    equals its path sums; the strip measures 1.000 and the annulus 1.010 against analytic;
    a via barrel within 0.1 %; an island reports open.
- **Bazel locally** (`--local_cpu_resources=2`, lowmem): 17 targets passed.
  - The 6 new targets and those of the changed modules: rerun at the final commits.
  - fanout, fanout_route, fanout_reserve, stack, stack_route, constraints, writeback,
    fixed_copper, regression_contract, quality and detail_escape: passed on db95f7f.
- **KiCad 10 Python, by hand:** `ir_extract_kicad_test`, `plane_partition_kicad_test`,
  `test_stack_kicad` and `test_check_constraints` all pass. They show that:
  - the drawn zones fill as one piece each;
  - there is zero overlap between rails, or with the fill;
  - the checker's three new kinds give the right verdicts.
- **prek:** clean on every commit.

## Rung A/B

Run on the Mac with seeds 0 and 1. "Off" is the same rung with the flag removed.

| Arm                           | KiCad opens              | DRC | Engine unrouted | Place-route CPU | Checks                                  |
| ----------------------------- | ------------------------ | --- | --------------- | --------------- | --------------------------------------- |
| `-partial` on                 | **1** (U1.K4, by design) | 0   | —               | 12 s / 12 s     | all pass                                |
| `-partial` off                | 2 (C8, K4)               | 0   | VCC             | 28 s / 31 s     | `unconnected` violated                  |
| `-rails` on (bcb8339)         | **0**                    | 0   | —               | 11 s / 32 s     | all pass                                |
| `-rails` off (bounding boxes) | 8                        | 0   | VDD             | 41 s / 40 s     | VDD in 9 pieces; IR **open** at 6 balls |

- **`-partial` on:** C8 is "bridged to C7" with a 0.65 mm F.Cu stub that is DRC-clean.
  run.py lists the designed open as `native_unconnected_items`; the rung's verdict is its
  `unconnected` check, and run.py was not touched.
- **`-rails` on:**
  - one piece per rail;
  - all 27 partition terminals reached;
  - gap of 0.31 mm or more;
  - IR at 33 mV budget: VDD 0.39 / 0.40 mV, VDDA 0.14 / 0.28 mV, VBAT 0.011 / 0.009 mV;
  - checker CPU 3 s, down from 426–491 s before db95f7f.
- **Partition cost:** 1.3 s per route on this rung.
- **Renders** (in `power/renders/`):
  - `rails-seed0-zones.png`: the partition;
  - `rails-off-seed0-zones.png`: the bounding boxes;
  - `rails-*-ir/`: IR heat maps.

## Radar probe

The probe runs route_board only, on trial 2's stage-2 placement exported with the macro
(`power/radar_probe.py`; the engine commit is given per row). In3 is typed power, and each rail's root is its
ferrite or regulator pad.

| Run (engine)                      | In3 rails                  | Partition: terminals reached (pieces)                                                                | Rail drop failure sites                          | All nets unrouted | Route CPU |
| --------------------------------- | -------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------ | ----------------- | --------- |
| 7 rails (e98c729)                 | all seven                  | 1V0_RF1 2/10, 1V0_RF2 12/12 (35), 1V2 17/17 (2), 1V8 17/19, 3V3_RADIO_IO 4/30, 3V3 3/6, 5V_SYS 18/22 | 62                                               | 50                | 425 s     |
| 4 rails, partition (729ca56)      | 1V0_RF1, 1V0_RF2, 1V2, 1V8 | 2/10, 12/12, **1/17**, 19/19                                                                         | 6 / 2 / 15 / 1                                   | 53                | 343 s     |
| 4 rails, bounding boxes (729ca56) | same four                  | (no partition)                                                                                       | 5 / 7 / 1 / 10                                   | 43                | 331 s     |
| **2 rails, partition (01d2392)**  | 1V0_RF1, 1V0_RF2           | **10/10 (1), 12/12 (1)**                                                                             | 2 / 2, all bottom-site pads (C57, C55, C60, R46) | 47                | 356 s     |

The renders are `power/renders/radar-in3-{7,4,2}rails-*.png`. The engine's acceptance
fails in every run, because the stage-2 placement predates the stage 3b re-place.

- **2 rails, partition (01d2392).** The territories cover In3 between them, with GND only
  in the gaps.
  - The trunk paths are long: 1V0_RF1 runs 85 mm from FB3 and 1V0_RF2 60 mm from FB4.
  - So the budget widths (17 mm and 12 mm) exceed the 10 mm cap.
  - The core widths fall to 0.1 mm in the BGA field.
- **Bottom-site pads.** The remaining failures are bottom-site pads under U1 whose plane
  drops find no site. Their fanout stubs to the ball vias would be their connection, but
  the drop planner does not count those yet. This is an open follow-up for the engine.
- **Partition and signal routing.** `core_no_vias` along long trunks removes signal via
  sites. With the partition, 47 nets were unrouted against 43 with bounding boxes, but
  the two sets differ, so this is not attributed.

### Findings

- **Partial fanout on radar60.** Across the 4- and 7-rail runs, it gives the following:
  - **Bridge:** 1V2 P14→P15, as design §1 A5 predicted.
  - **Retried:** 11–13 failed balls get the board's escapes, five of them LVDS
    J14–L15 that had lost their access cells.
  - **Still open:** 13–15, mostly GND balls in the PA corner (A1–C1, A3–A7, G1, J1, L1).
    They wait for macro v2 (D14) and the bottom sites, and are now reported per ball
    instead of blocking GND.
  - Use `retry: false` for the LVDS fanout if the generic escapes break the
    outer-layer rule; A3 `dru_routing` is the other fix.
- **A single In3 partition cannot join the four core rails on this placement.**
  - Their balls interleave around and inside U1: 1V2 at G15/H15, N11, P14/P15 and R6;
    1V8 at B11/B12, D15, F5, K5 and R9; 1V0 at G5/H5/J5 and C2/D2.
  - Their ferrites sit 20–40 mm away on different sides.
  - Whichever rail connects first encloses the others' balls; three orders were tried.
  - The partition says so: unreached terminals become failure sites instead of floating
    islands.
- **What stage 3b should do.**
  - Partition In3 only for the two 2.5 A rails (1V0_RF1, 1V0_RF2). On this placement
    the partition joins every terminal of both, one piece each.
  - Keep 1V2 and 1V8 as `drop_nets` with wide tracks. Their budgets of 12 and 21 mΩ allow
    it: a 1 mm track at 1 oz is 0.49 mΩ/mm, so the budgets allow about 24 mm and 43 mm
    [D].
  - Re-place with the design §2.4 ferrite anchors (within 10–12 mm of the U1 ball they
    feed), so each 1.0 V tree stays short. On the probe, 1V0_RF2's root-to-farthest path
    was 136 mm, which needs a width of 27 mm (above the 10 mm cap) to meet 4 mΩ.
  - If the two 1.0 V rails still miss 4 mΩ after the re-place, that is the design §2.6 (c)
    trigger to go to 8 layers.

## How stage 3b should declare it

```yaml
net_class: # gen_board.py: In3 typed power
  rail_1v0_rf1: { nets: [1V0_RF1], plane_layer: In3.Cu }
  rail_1v0_rf2: { nets: [1V0_RF2], plane_layer: In3.Cu }
fanout:
  [
    {
      ...,
      drop_nets: [1V0_PA, 1V2, 1V8, 3V3_RADIO_IO, VBGAP, VOUT_14APLL, VOUT_14SYNTH],
      partial: { bridge: true },
    },
  ]
plane_partition:
  - {
      layer: In3.Cu,
      nets: [1V0_RF1, 1V0_RF2],
      fill: GND,
      split_gap_mm: 0.3,
      min_width_mm: 1.0,
      sources: { 1V0_RF1: { '@pmic.fb_rf1': '2' }, 1V0_RF2: { '@pmic.fb_rf2_a': '2' } },
    }
ir_drop: # design §2.7, 60 C; one entry per rail
  - {
      net: 1V0_RF1,
      sources: { '@pmic.fb_rf1': ['2'] },
      sinks: { '@radio.u1': [G5, H5, J5] },
      budget_mohm: 4,
      temperature_c: 60,
    }
```

The currents come from the `@pnr-current` peaks. The `ir_drop` budgets also set the trunk
widths.

## Notes

- **Expected merge conflicts.** All are appends, none semantic:
  - `router.py`: the fanout hook and the BoardRoute fields;
  - `constraints.py`: `known_keys`, the CompiledConstraints fields and the end of the rules
    dict;
  - `route/detail/fanout.py`;
  - `BUILD.bazel`;
  - `pnr-inputs.md`.
- **The raster is conservative.**
  - Zone polygons are 0.1 mm staircases, and KiCad's fill is the judge.
  - The raster treats antipads conservatively: two antipads 0.1 mm apart may read as
    closed.
  - The IR check measures the real board.
- **IR models copper only.** R46 (design §0.7) and the 4 mΩ versus 3 mV conflict (design
  §4.2) stay owner items.
- **Left to the integration stage:** the GCP identity regression (design §1.10) and the
  WORKLOG update.
