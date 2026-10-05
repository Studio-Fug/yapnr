"""The design parameterization (design §7): design variables → the physical density ρ̄.

    x (DOF) ─ material grid ─▶ ρ ─ ring ─▶ ρ_ext ─ conic filter ─▶ ρ̃ ─ tanh(β) ─▶ ρ̄

- `material_grid`: the 2D design window on the copper plane, fixed pixels (port pads,
  keepouts), mirror symmetry on the DOF and the fixed exterior ring the filter sees;
- `filters`: the conic filter;
- `projection`: the tanh projection and its β = ∞ limit (Heaviside);
- `lengthscale`: the minimum width and space indicator constraints of Zhou et al.;
- `metrics`: the gray-level measure M_nd;
- `parameterization`: the whole chain in torch (float64), with its vector-Jacobian products.
"""
