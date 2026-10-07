"""Measured sparse-layout broadphase A/B; not a production-board speed claim."""

import json
import time
from dataclasses import replace
from pathlib import Path

from prototype.energy_track import Config, Controller, GateResult, Track
from shapely.geometry import LineString


def run(count):
    tracks = []
    for i in range(count):
        x = (i // 2 % 100) * 20
        y = (i // 200) * 20 + (i % 2) * 2
        tracks.append(Track(str(i), "N" + str(i), ((x, y), (x + 5, y)), layer="F.Cu"))
    cfg = Config(growth_mm=2, spatial_broadphase=True)
    start = time.perf_counter()
    indexed = Controller(tracks, config=cfg)
    build = time.perf_counter() - start
    linear = Controller(tracks, config=replace(cfg, spatial_broadphase=False))
    queries = [tracks[(j * 97) % count] for j in range(30)]
    start = time.perf_counter()
    reference = []
    for t in queries:
        reference.append(
            linear.dirty_neighbors(LineString(t.points), t.layer, GateResult(True), exclude=(t.id,))
        )
    brute = time.perf_counter() - start
    before = dict(indexed._spatial.stats)
    start = time.perf_counter()
    actual = []
    for t in queries:
        actual.append(
            indexed.dirty_neighbors(
                LineString(t.points), t.layer, GateResult(True), exclude=(t.id,)
            )
        )
    elapsed = time.perf_counter() - start
    assert actual == reference
    return dict(
        tracks=count,
        queries=len(queries),
        same_neighbors=True,
        initial_build_seconds=build,
        linear_query_seconds=brute,
        spatial_query_seconds=elapsed,
        linear_track_intersection_checks=count * len(queries),
        indexed_candidate_ids_examined=indexed._spatial.stats["candidate_ids_examined"]
        - before["candidate_ids_examined"],
        indexed_bucket_visits=indexed._spatial.stats["bucket_visits"] - before["bucket_visits"],
        rebuilds=indexed._spatial.stats["rebuilds"],
        fullscan_queries=indexed._spatial.stats["fullscan_queries"],
    )


if __name__ == "__main__":
    result = {
        "layout": "Synthetic separated pairs of tracks, deliberately favorable to a sparse broadphase.",
        "limits": [
            "Initialization is linear in records and occupied cells.",
            "Dense, oversize or huge-query cases can fall back to O(N).",
            "Whole transactions still copy/hash state in O(N); only discovery/lookup is spatially pruned.",
            "No production-board or end-to-end timing claim.",
        ],
        "rows": [run(n) for n in (100, 1000, 10000)],
    }
    Path("results/spatial-benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
