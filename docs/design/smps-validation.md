# SMPS layout requirements and validation

The SMPS screen converts layout intent into named checks over actual terminals
and copper. Guidance sources are recorded per requirement. Missing inputs,
missing limits, unsupported copper geometry and stale evidence remain explicit
`unqualified` results; native DRC is still required.

The initial profile follows the
[TPS552882 layout guidance, section 10](https://www.ti.com/lit/ds/symlink/tps552882.pdf)
and [ADI AN-139](https://www.analog.com/en/resources/app-notes/an-139.html).
The datasheet separates input and output commutation loops, specifies local
capacitor access on both power and return terminals, and calls for protected
sense routing and ground organization. Those are different requirements from
simply clustering every power component around the controller.

## Checks

| Kind              | Measurement                                              | Important boundary                                 |
| ----------------- | -------------------------------------------------------- | -------------------------------------------------- |
| `pad_distance`    | Nearest specified terminal centers                       | Placement screen only                              |
| `loop_geometry`   | Ordered terminal perimeter and convex-hull area          | Neither routed area nor inductance                 |
| `route_path`      | Copper path length and via count                         | Plane-mediated paths require extraction            |
| `net_geometry`    | Track length, bounding extent, minimum track width, vias | Filled zones and arcs remain unqualified           |
| `separation`      | Conservative projected copper separation                 | No assumed shielding-plane credit                  |
| `paired_routes`   | Bidirectional proximity, local angle and length skew     | Includes endpoint fanout                           |
| `kelvin`          | Other power terminals encountered along a sense lead     | Shared planes require impedance extraction         |
| `reference_plane` | Uncovered area below the declared cell                   | Native filled-polygon subtraction, including holes |
| `via_array`       | Nearby same-net vias spanning required layers            | Count is not ampacity or thermal qualification     |
| `evidence`        | Numerical result, units and operating-corner coverage    | Bound to the exact board hash                      |

The path screen requires native endpoint-to-pad hit tests rather than bounding-box
pad contacts, and deliberately removes plane shortcut edges. A common ground net
or zone island does not imply a short, low-inductance return. Kelvin screening
catches a sense path through another power terminal; passing this necessary
geometric screen does not establish zero shared impedance. The evidence checks
keep return-error, rail-drop, ripple, phase/gain margins and thermal results open
until appropriate extraction, simulation or measurement exists.

## Author and execute

Declare `smps_checks` in constraints YAML; the compiler carries them into routing
rules. Every entry needs a stable `id`, a `kind`, a `source`, its terminals/nets and
explicit limits. Terminal identifiers are `REF:PAD`.

```yaml
smps_checks:
  - id: SMPS-INPUT-ACCESS
    kind: pad_distance
    source: requirements/placement.yaml#SMPS-INPUT-ACCESS
    terminals: [C15:1, Q1:5]
    max_mm: 3
    provisional: true
```

The numerical limits in
[the Mini research profile](../../hardware/pnr/testdata/smps/mini-converter.json)
are **provisional study budgets**, not manufacturer specifications or approved
article requirements. They are not used to claim the Mini is qualified.

Run under the bundled headless KiCad Python:

```sh
python -m pnr.smps board.kicad_pcb --rules rules.json --out smps-report.json
```

The CLI exits nonzero for a failure or an unqualified declared check. Optional
`--evidence receipts.json` provides measurements indexed by check ID. Each receipt
carries `board_sha256`, `artifact_sha256`, `metric`, `unit`, finite `value`,
`status: complete`, `method`, and required `corners`. This validates receipt
applicability and thresholds; it does not authenticate the measurement or replace
review of its model, source artifact, corner coverage and physical validity.

`PNR_SMPS_LAYOUT=1` enables the screen inside the native electrical audit and adds
failure/unknown side fields to native results and ranking. Missing reports rank
as unknown, not zero. Repeat aggregation keeps the worst result. Without the flag,
declared checks appear as unqualified rather than silently passing. Existing
boards with no declared checks keep their previous behavior.

Hierarchy sub-boards retain checks wholly contained in their terminals/nets.
Cross-boundary checks and board-level evidence requirements are recorded as
`smps_deferred_checks` for evaluation on the assembled parent; deferral is not a
pass. The parent retains the original complete contract.

The current implementation does not extract parasitic inductance or capacitance,
prove AGND/PGND star topology, or run a stability/thermal simulation automatically.
It prevents geometric screens from standing in for that missing qualification.

## Regression evidence

Synthetic fault tests exercise distant capacitors, a long thin loop, detoured and
open copper, narrow power tracks, switch-node extent, sensitive-net crossings,
sense-pair divergence, shared Kelvin paths, missing plane coverage, inadequate
via arrays, and stale/unit/corner-incomplete stability evidence.

The actual converter baseline and shove artifacts were screened with the same
research profile. The screen detects input-capacitor access, gate routing,
switch-node extent and ground-reference defects even when native DRC reports
zero geometric violations. Missing ripple, stability and thermal evidence remains
unqualified. See [measured results](assets/mini-pad-locality/layout-choice-study.json).
