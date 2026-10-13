# PR #100 default activation and validation

All production features introduced by PR #100 are enabled by default. Each Boolean
engine switch accepts an explicit `0` to restore its ablation. Electrical limits,
KiCad DRC, total differential-pair contracts and evidence requirements are unchanged.

| Switch                         | Behavior                                                                                 |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| `PNR_HIER_SAFE_PARTITION`      | Preserve hard relationships across block partitioning                                    |
| `PNR_NATIVE_OUTLINE`           | Preserve the actual board contour and reject inconsistent dimensions                     |
| `PNR_POWER_BRANCHES`           | Cover every carrying branch and require native power screening                           |
| `PNR_POWER_CHANNELS`           | Retain channel demand for shared power nets                                              |
| `PNR_PAD_LOCALITY`             | Attract small components to the actual connected IC pads                                 |
| `PNR_LEGALIZE_SHOVE`           | Transactional neighbor displacement with complete legality checks                        |
| `PNR_PLACEMENT_BANKS`          | Honor authored capacitor banks and their maximum span                                    |
| `PNR_SMPS_LAYOUT`              | Run declared SMPS checks and retain unknown qualification                                |
| `PNR_SMPS_PLACEMENT`           | Include declared SMPS geometric debt in placement                                        |
| `PNR_INGEST_COPPER_PADS`       | Exclude mask/paste-only apertures from copper-pad occupancy                              |
| `PNR_PLANE_ACCESS_COMPACT_BUS` | Keep the full-width collector within its via-bank span                                   |
| `PNR_PLANE_ACCESS_NATIVE_FIT`  | Judge current-sized access with native geometry instead of symmetric courtyard inflation |
| `PNR_HIER_LAYOUT_DIVERSE`      | Preserve distinct valid block layouts in the search portfolio                            |

`PNR_HIER_LAYOUT_SEARCH` defaults to `alternating`: bounded variant swaps and parent
relaxation. `off`, `independent`, `frozen`, and `paired` remain selectable comparison
strategies. Mutually exclusive strategies are not run simultaneously. The existing
fixed evaluation and iteration limits remain active.

New placement-lab runs enable shove and authored banks in the browser, API and CLI.
`--no-shove` and `--no-placement-banks` opt out. Replay of older captures preserves
their recorded feature settings. Unlimited interactive running, legalize-only
replay and diagnostic captures remain explicit user actions; they are execution
modes, not disabled production repairs.

## Native ladder comparison

The full eight-case ladder at seeds 0 and 1 passes **16/16 on main and 16/16 with
these defaults**. Both arms have zero native unconnected items and DRC findings;
source-width, contact and placement checks pass. Identical four-round and
600-second stage budgets were used. Main is `9e19d73e`; candidate is PR #100 with
the documented defaults and transaction-cache fix. Seeds and frozen source hashes
are retained in the experiment evidence.

| Aggregate measure      |       Main | New defaults | Change |
| ---------------------- | ---------: | -----------: | -----: |
| Copper length          | 1705.71 mm |   1551.12 mm |  −9.1% |
| Vias                   |        130 |          108 | −16.9% |
| Place/route phase time |   149.44 s |     277.16 s | +85.5% |
| All stage time         |   491.56 s |     603.39 s | +22.8% |
| CPU time               |   934.45 s |    1038.35 s | +11.1% |
| Campaign elapsed       |      562 s |        672 s | +19.6% |

The default repairs trade runtime for less copper and fewer vias in this ladder;
there is no claim of a universal speedup. Shove's repeated geometry/channel
calculations are cached without changing proposal order or acceptance. A separate
representative 20-part placement fell from 19.64 to 3.84 seconds, with identical
final geometry and transaction decisions; turning shove off took 1.76 seconds.

## Validation in progress

The 310-target combined unit suite passes. A subsequent contextual-search fix
uses the legalizer's authored courtyard gap instead of incorrectly adding the
copper clearance to it; 23 focused hierarchy tests pass and the full suite is
being repeated. Failed search seeds retain diagnostics and allow the caller to
try its remaining declared seeds. Five native showcases and the 20-case hard-lane
inventory are still being checked. Individual failures and timeouts are retained.
Native edge tests cover preserved contours by default and rectangular stamping
with the explicit opt-out. A clean code suite is not a claim that every board is
routed or electrically qualified. The SMPS article remains unqualified as
recorded in [the closure investigation](smps-closure.md).
