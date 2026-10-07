# Spatial broadphase follow-on (v0.2)

The v0.1 baseline is preserved separately. This change is opt-in through
`Config(spatial_broadphase=True)` or the real-case driver's `--spatial` option.
It does not enable any production router hook.

## Data structure and updates

`prototype/spatial.py:LayerGrid` is a dynamic, layer-aware uniform grid:

- `(layer, integer_x_cell, integer_y_cell) -> set(record IDs)`
- `record ID -> layer, expanded bounding box`
- `record ID -> occupied cells`, for removal/update without rebuilding
- per-layer record sets and oversize-record sets for bounded fallbacks

Track records store the current path's AABB expanded by half-width plus its
clearance. Fixed-obstacle records store their true shape AABB expanded by the
analytic radius plus clearance. A planning query expands the witness AABB by
growth radius plus the querying track's half-width and clearance. Adding both
clearances conservatively bounds the exact rule's maximum clearance; the
planner still performs the same exact-distance and native legality checks.

On acceptance, only moved track records are removed/reinserted. Rejected
transactions leave geometry and index unchanged. Joint group validation unions
in every changed member explicitly, so it cannot miss a neighbor moving into a
previously empty queried region. Arbitrary external state snapshots without a
changed-ID set deliberately use an O(N) comparison fallback.

A validator can return `updated_obstacles` after refill. Only existing,
unprotected obstacle IDs with unchanged identity/layer/radius/clearance/policy
are accepted. Shape records are updated dynamically. The geometric symmetric
difference automatically expands the dirty region and includes its layer.
A validator-supplied dirty region with unknown layer scope is queried on all
layers; an explicit layer list is combined with the moved track's layer.

## Dirty-neighbor footprint

The initial dirty footprint remains the conservative v0.1 region:

`union(old_path, new_path).buffer(growth_mm + 2 * max(width_mm + clearance_mm))`

The maximum is computed once at construction because width and clearance are
immutable during these path edits. Validator-affected regions are unioned in.
The grid returns broadphase candidates; a final exact LineString intersection
with the dirty region determines the sparse requeue set. Queue membership is
still a deque plus a set, ordered deterministically by track ID.

## Actual complexity

For an update occupying U old/new cells, record maintenance is O(U), with no
whole-index rebuild. A query visits C cells plus their unique candidate IDs K
and oversize records, then sorts/tests the candidates. Typical sparse lookup
is O(C + K log K) plus geometry work. Initial construction is linear in records
and occupied cells, with sorting and the controller's initial fingerprint.

The cell budget defaults to 4096. A larger record is held in a per-layer
oversize set. A larger query scans the requested layers instead of allocating
or visiting an unbounded number of cells. Dense layouts, numerous oversized
objects and broad dirty regions can therefore still cost O(N).

Crucially, whole transactions are NOT sublinear: they still copy the state
mapping and sort/hash the global geometry fingerprint for cycle detection.
That is O(N log N) fingerprint sorting and O(N) copying. This follow-on improves
neighbor discovery and obstacle lookup, not every part of transaction cost.

## Measured A/B

A deliberately sparse synthetic layout places pairs of tracks 20 mm apart.
For 30 queries, indexed and brute-force neighbor lists were identical:

- 100 tracks: 3,000 linear intersection checks vs 60 indexed candidate IDs.
- 1,000 tracks: 30,000 linear intersection checks vs 60 indexed candidate IDs.
- 10,000 tracks: 300,000 linear intersection checks vs 60 indexed candidate IDs.

The 10,000-track run took about 2.10 s for linear queries and 0.0032 s for indexed
queries; one-time index construction took 0.272 s. All three runs had zero
rebuilds and zero full-scan fallbacks. These are one-run timings on a favorable
synthetic fixture, not production-board or end-to-end speed claims. The JSON
report retains full timings and counters.

## Tests and reproduction

The combined suite has 43 tests. Additional coverage includes 1,000 random
dynamic upsert/remove/query steps against a brute-force AABB oracle, negative
coordinates and cell boundaries, layer changes, oversize fallback, large widths
and clearances, randomized potential-blocker completeness, moved-group shapes,
rollback, refill geometry updates and dirty scopes. The original dependency
example has identical accepted moves/routes with the index on and off.

```sh
python3 -m unittest discover -s tests -v
python3 -m prototype.benchmark_spatial
# With the same pinned engine / headless KiCad environment as README:
bash scripts/real_case.sh --spatial
```

The native spatial A/B reproduced the exact same 1-bend route and passed fresh
KiCad/IR no-regression checks. Its obstacle list shrank from 45 to 13, after
examining 21 broadphase candidates across 12 cells. Zero rebuilds or fallbacks.
The native spatial A/B report is separate from the geometry-only oracle tests.
No geometry or native gate is weakened by this lookup layer.
