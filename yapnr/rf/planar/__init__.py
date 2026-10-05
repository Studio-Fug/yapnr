"""Solver-neutral layered planar RF structures (schema ``yapnr-planar-v1``, millimetres).

One geometry description feeds every field solver (Palace now; openEMS through the same document
later), so a comparison between two solvers never compares two geometry exports.

- ``model``: the document, its validation, exact-arc rings, port faces and the geometry hash;
- ``stackups``: stack presets (the radar60 RO4835 stack) and the Hammerstad roughness factor;
- ``clean``: union, crop, snap, simplify and exact-arc refitting of copper (shapely);
- ``adapters``: generated lines, the radar60 patch from an rfmacro record, the radar60 feed
  models, and a region of a zone-filled KiCad board.

User guide: docs/rf-palace.md.
"""

SCHEMA = "yapnr-planar-v1"
