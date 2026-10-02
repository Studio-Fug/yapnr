"""3D Yee FDTD for single-layer microstrip (docs/design/rf-topology-optimization.md §4).

`engine.Simulation` steps the fields; `cpml`, `sources`, `dtft`, `monitors`, `stability` and
`stop` hold the pieces it is built from.
"""
