"""Fab outputs: vendor profiles and stackups as data, fab checks and per-vendor bundles.

docs/design/fab-and-ordering.md (F1, F2) and docs/fab-and-ordering.md (the user guide).

- ``capability``: the data in ``data/`` (profiles, stackups, vendors), validated; stdlib only and
  Python 3.9 compatible, because KiCad-side engine workers import it through ``pnr.fab_profile``.
- ``stackups``: stackup views (microstrip, cross-section) and the ``yapnr.rf`` adapter.
- ``board``, ``kicad``, ``check``, ``export``, ``bundle``, ``assembly``, ``build``: ``yapnr fab``.

Nothing here opens a network connection.
"""
