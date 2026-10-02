"""Export of a binary design (design §10).

- `contour`: marching squares on the binary pixels → loops → nesting → simplified,
  keyholed polygons (one per copper island);
- `raster`: polygons → samples (even-odd), connected components, the exact Euclidean
  distance transform;
- `drc`: the minimum width and space check of the polygons (morphological opening and
  closing on a fine raster);
- `kicad`: the `.kicad_mod` footprint writer (KiCad 10 format, net-tie copper) and a minimal
  reader for round trips;
- `touchstone`: `.sNp` writer and reader;
- `report`: the run directory's footprint, Touchstone file and result JSON with provenance.
"""
