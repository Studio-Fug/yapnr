"""RF microstrip inverse design by topology optimization (issue #29).

Design: docs/design/rf-topology-optimization.md. The solver layer:

- `mesh`, `stackup`, `materials`: graded Yee grids, the board stackup, per-edge materials and
  the copper sheet;
- `fdtd`: the Yee stepper (numpy or torch), CPML, sources, DTFT probes, stop rule, stability;
- `ports`, `sparams`, `domain`: line ports and their calibration, resistive lumped ports,
  S-parameters, problem domains (grid, ports, design window, radiated-power box);
- `adjoint`: adjoint runs and the gradient with respect to the copper sheet;
- `numerics`: reproducible complex arithmetic for numpy post-processing.

The optimizer layer:

- `spec`, `objectives`: transfer-function requirements, normalized violations, groups;
- `design`: material grid, conic filter, tanh projection, length-scale constraints;
- `optim`: MMA (and its conservative variant), the epigraph form, the β schedule;
- `problem`, `driver`: spec → solver objects and evaluations; the loop, checkpoints, `design()`;
- `export`: polygons, the width and space check, KiCad footprints, Touchstone, the result JSON;
- `animate` (Pillow, `//yapnr/rf:animate`): the density evolution of a run.
"""
