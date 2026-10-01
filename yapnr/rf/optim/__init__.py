"""Optimization (design §8): MMA, the epigraph minimax form and the β schedule.

- `mma`: the method of moving asymptotes (Svanberg 1987, 2002, 2007), written from the
  publications, with the primal-dual interior-point solver of its subproblem;
- `epigraph`: min t s.t. f_k(x) ≤ t (and length-scale constraints) in MMA's native form;
- `schedule`: the β continuation and the end-of-epoch rule.
"""
