"""Small convex quadratic programs for the make-room displacement model.

min 1/2 sum_i w_i x_i^2  subject to  a_k . x >= b_k (inequalities) and
a_k . x == b_k (equalities), solved by Hildreth's dual coordinate ascent, which
needs no linear algebra library (KiCad Python) and warm-starts cheaply. The dual
multipliers identify the binding constraints: the make-room certificate.
"""


def hildreth(cons, weights, iters=6000, tol=2e-6, deadline=None):
    """``(x, lambda, converged, iterations)`` for ``cons`` = [(coeffs {i: a},
    b, is_equality)] and positive ``weights``. ``deadline`` is a
    time.monotonic() bound checked every 50 sweeps."""
    import time

    x = [0.0] * len(weights)
    lam = [0.0] * len(cons)
    inverse = [1.0 / w for w in weights]
    den = [sum(a * a * inverse[i] for i, a in c.items()) or 1e-12 for c, _, _ in cons]
    for it in range(iters):
        worst = 0.0
        for k, (c, b, eq) in enumerate(cons):
            r = b - sum(a * x[i] for i, a in c.items())
            worst = max(worst, abs(r) if eq else r)
            new = lam[k] + r / den[k]
            if not eq and new < 0:
                new = 0.0
            delta = new - lam[k]
            if delta:
                lam[k] = new
                for i, a in c.items():
                    x[i] += inverse[i] * a * delta
        if worst < tol:
            return x, lam, True, it
        if deadline is not None and it % 50 == 49 and time.monotonic() > deadline:
            return x, lam, False, it
    return x, lam, False, iters


def residual(cons, x):
    """Largest violation of ``cons`` at ``x`` (0 when feasible)."""
    worst = 0.0
    for c, b, eq in cons:
        r = b - sum(a * x[i] for i, a in c.items())
        worst = max(worst, abs(r) if eq else r)
    return worst
