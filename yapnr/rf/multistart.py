"""Multi-start derivation and perturbation (design `multistart.md`).

This module covers §1 of the design only: deriving each start's spec from a base spec's
`starts` block, and the perturbation RNG. Execution (running starts as subprocesses or cloud
waves), successive halving (§2) and final selection (§3) are not implemented here; a spec with
no `starts` key is unaffected (one run, today's behavior).
"""

from __future__ import annotations

import numpy as np

from yapnr.rf.spec import STARTS_VARY_ALLOWED, Spec

_MASK64 = (1 << 64) - 1
_SPLITMIX_GAMMA = 0x9E3779B97F4A7C15


def _splitmix64(state: int) -> int:
    state = (state + _SPLITMIX_GAMMA) & _MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    z = z ^ (z >> 31)
    return z


def _uniform01(seed: int, index: int) -> float:
    """A counter-based SplitMix64 draw in [0, 1), deterministic on (seed, index) alone.

    Not numpy's Generator (`design multistart.md §1`): this is the same on any numpy version or
    machine, since it depends only on 64-bit integer arithmetic.
    """
    state = (int(seed) & _MASK64) ^ ((int(index) & _MASK64) * 0x2545F4914F6CDD1D)
    z = _splitmix64(state & _MASK64)
    # top 53 bits -> a double in [0, 1), as the standard SplitMix64-to-double recipe does.
    return (z >> 11) * (2.0**-53)


def perturb(x0: np.ndarray, amplitude: float, seed: int) -> np.ndarray:
    """x0 + amplitude·(2u − 1), clipped to [0, 1], u from `_uniform01(seed, dof index)`.

    amplitude = 0 returns x0 unchanged (the identity case the design calls out in §1).
    """
    x0 = np.asarray(x0, dtype=np.float64)
    if amplitude == 0:
        return x0.copy()
    u = np.fromiter(
        (_uniform01(seed, i) for i in range(x0.size)), dtype=np.float64, count=x0.size
    ).reshape(x0.shape)
    return np.clip(x0 + amplitude * (2.0 * u - 1.0), 0.0, 1.0)


def _combinations(vary: dict, combine: str) -> list[dict]:
    """The list of override dicts, in plan order, for `combine` (zip|product)."""
    keys = list(vary.keys())
    lists = [vary[k] for k in keys]
    if combine == "product":
        import itertools

        return [dict(zip(keys, combo)) for combo in itertools.product(*lists)]
    n = max(len(v) for v in lists)
    out = []
    for i in range(n):
        out.append({k: (v[i] if len(v) > 1 else v[0]) for k, v in zip(keys, lists)})
    return out


def derive_starts(spec: Spec) -> list[Spec]:
    """The per-start specs from `spec.starts`, in plan order (`s00`, `s01`, ...).

    Each derived spec is the base spec (with `starts` stripped) plus that start's optimizer
    overrides. Raises on anything the design refuses: an unknown `vary` key (already checked by
    `Spec.validate`), start 0 not equal to the base, or two starts sharing a design sha256 (the
    native path is deterministic, so a duplicate would only recompute an identical run).
    """
    if spec.starts is None:
        raise ValueError("spec.starts is None: nothing to derive")
    base = spec.replace(starts=None)
    vary = spec.starts["vary"]
    combine = spec.starts.get("combine", "zip")
    overrides = _combinations(vary, combine)

    derived = []
    seen = {}
    for i, ov in enumerate(overrides):
        bad = set(ov) - set(STARTS_VARY_ALLOWED)
        if bad:
            raise ValueError(f"start {i}: {sorted(bad)} may not be varied across starts")
        s = base.replace(optimizer=base.optimizer.__class__(**{**_opt_dict(base.optimizer), **ov}))
        sha = s.sha256()
        if i == 0 and sha != base.sha256():
            raise ValueError("start 0 must equal the base spec")
        if sha in seen:
            raise ValueError(
                f"start {i} has the same design sha256 as start {seen[sha]} ({sha[:12]}); "
                "the native path is deterministic, so this would only recompute it"
            )
        seen[sha] = i
        derived.append(s)
    return derived


def _opt_dict(optimizer) -> dict:
    from dataclasses import fields

    return {f.name: getattr(optimizer, f.name) for f in fields(optimizer)}
