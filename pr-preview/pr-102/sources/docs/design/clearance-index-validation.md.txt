# Clearance index: integration validation

PR #92 now contains only the clearance-index/native-cache optimization and
library-provenance changes on main `c57077df`. Its already merged #88 stack
was removed by replaying those two commits; the engine changes applied
without manual conflict resolution. No validation rule or engine default
was relaxed.

## Verified campaign receipts

The existing GCP summaries were fetched again on 2026-10-07:

- Base: `20261007-ladder-c15db5`, bundle commit
  `2215cc8f2091e7ea705fdc2cb98b0bb30fdc6abb`, whose parent is #88 head
  `540784c7d0a16b2aa80b0ddb9790a1b84832132d`.
- New: `20261007-ladder-d3b3dd`, bundle commit
  `b2c92f52ce4a1c5cea715612fab655701f0aa427`, whose parent is optimization
  commit `c6c1765d51e56dc0e8719a51b58d8d6110ea7b48`.
- Each bundle-only commit adds the same prebuilt Linux library; neither
  commit was published. Both campaigns completed, with all 96 cell receipts
  present in each arm.
- All 96 pairs agree on verdict, opens, DRC violations, unconnected items,
  vias and copper hash. There are 94 nonempty copper hashes, and 85/96 cells
  pass in each arm. The other eleven cells remain failures in both arms.
- Summed task wall time is 2369 → 2041 seconds (-14%); summed place-route
  time is 1834 → 1467 seconds (-20%).
- Every native provenance receipt names `libpnr_maze.so`, source `prebuilt`,
  SHA-256 `d3f7b82a5fcd0d2139a848b14e59844fd1df99625597f5b8e41aeb06d8abd48a`.

These are the original pre-integration campaigns, not a new A/B of the
rebased head. They do not include the later dovetail rungs or main's newer
changes. No new paid run was started.

## Animation applicability

This optimization has no intended visual behavior: the index skips distant
obstacles before applying the same exact tests, and the native wrapper
reuses buffers. The verified copper hashes are identical for every cell
with copper. An animation would repeat the same board geometry and cannot
demonstrate the measured runtime improvement; the receipt comparison is
the appropriate evidence for this PR.

## Integration checks

Nine focused Bazel targets passed: `obstacle_index_test`,
`dense_maze_native_test`, `exact_route_native_test`, `hard_rungs_test`,
`tests/unit/exp:test_kinds`, and the four release tests. Full CI still needs
to pass on the pushed head.
