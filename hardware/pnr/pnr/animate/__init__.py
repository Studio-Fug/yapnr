"""Animations of the place-and-route process from traces (``pnr.trace``).

The pipeline: sources -> provenance DAG (:mod:`pnr.provenance`) -> critical path ->
storyboard (:mod:`.storyboard`) -> timeline of frames with durations (:mod:`.timeline`) ->
raster frames (:mod:`.render`, Pillow) -> encoders (:mod:`.encode`: animated WebP, GIF,
optional MP4 through ffmpeg). One straight line from the unplaced board to KiCad's verdict;
the candidates a selection rejected appear as short montages where the engine chose. Pure
Python and Pillow (no numpy, torch or KiCad), deterministic for the same trace, options and
Pillow version. Command line: ``python -m pnr.animate SOURCE --out PATH``.

Placement constraints are highlighted (:mod:`.highlight`); two runs can be shown side by side
(:mod:`.compare`, ``--compare LEFT RIGHT``); a hierarchical case is shown in chapters (blocks,
top level, knitting: :mod:`.hier`).
"""
