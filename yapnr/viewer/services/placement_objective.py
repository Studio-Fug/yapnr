"""Small linear objective DSL. Parse arithmetic, never execute user code."""

import ast
import math

TERMS = {
    "wirelength": "w_wire",
    "spread_overlap": "w_spread",
    "outline": "w_bound",
    "keepout": "w_keep",
    "plane_area": "w_plane",
    "plane_separation": "w_plane_sep",
    "pad_locality": "w_locality",
    "source_constraints": None,
}
DEFAULT = (
    "wirelength + spread_overlap + 20*outline + 40*keepout + "
    "0.05*plane_area + 0.35*plane_separation + 4*pad_locality + source_constraints"
)


def parse(expression):
    if not isinstance(expression, str) or len(expression) > 2000:
        raise ValueError("Objective must be at most 2000 characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("Use a sum of nonnegative weights times named terms") from exc
    if sum(1 for _ in ast.walk(tree)) > 200:
        raise ValueError("Objective is too complex")

    def visit(n):
        if isinstance(n, ast.Name) and n.id in TERMS:
            return {n.id: 1.0}
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            v = float(n.value)
            if not math.isfinite(v) or not 0 <= v <= 1000:
                raise ValueError("Weights must be finite and between 0 and 1000")
            return {None: v}
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Mult)):
            a, b = visit(n.left), visit(n.right)
            if isinstance(n.op, ast.Add):
                return {k: a.get(k, 0) + b.get(k, 0) for k in a.keys() | b.keys()}
            if set(a) == {None}:
                return {k: a[None] * v for k, v in b.items()}
            if set(b) == {None}:
                return {k: b[None] * v for k, v in a.items()}
        raise ValueError("Allowed: named terms, nonnegative numbers, +, *, and parentheses")

    weights = visit(tree.body)
    if None in weights or any(not math.isfinite(v) or v > 1000 for v in weights.values()):
        raise ValueError("Use weighted terms with coefficients at most 1000")
    if weights.get("source_constraints") != 1:
        raise ValueError("Include source_constraints exactly once; authored intent stays enforced")
    return {key: weights.get(term, 0.0) for term, key in TERMS.items() if key}
