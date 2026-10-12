"""Read-only surface arithmetic over named cost arrays; no Python execution."""

import ast
import math

NAMES = {
    "wirelength",
    "spread_overlap",
    "outline",
    "keepout",
    "plane_area",
    "plane_separation",
    "pad_locality",
    "edge_alignment",
    "group_radius",
    "region",
    "alignment",
    "length_matching",
    "source_constraints",
    "objective",
}


def expression(text):
    if not isinstance(text, str) or not 0 < len(text) <= 2000:
        raise ValueError("Surface expression must be 1–2000 characters")
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("Invalid surface expression") from exc
    if sum(1 for _ in ast.walk(tree)) > 150:
        raise ValueError("Surface expression is too complex")

    def check(n):
        if isinstance(n, ast.Name) and n.id in NAMES:
            return
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            if abs(n.value) <= 1e6 and math.isfinite(n.value):
                return
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            return check(n.operand)
        if isinstance(n, ast.BinOp) and isinstance(
            n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
        ):
            if isinstance(n.op, ast.Pow) and not (
                isinstance(n.right, ast.Constant)
                and type(n.right.value) in (int, float)
                and 0 <= n.right.value <= 4
            ):
                raise ValueError("Powers require a constant exponent between 0 and 4")
            check(n.left)
            return check(n.right)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and not n.keywords:
            counts = {"abs": 1, "sqrt": 1, "log1p": 1, "min": 2, "max": 2}
            if n.func.id in counts and len(n.args) == counts[n.func.id]:
                for arg in n.args:
                    check(arg)
                return
        raise ValueError("Use named terms, numbers, + - * / **, abs, sqrt, log1p, min or max")

    check(tree.body)
    return tree.body


def evaluate(text, terms):
    import numpy as np

    def visit(n):
        if isinstance(n, ast.Name):
            return terms[n.id]
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.UnaryOp):
            v = visit(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            a, b = visit(n.left), visit(n.right)
            return {
                ast.Add: np.add,
                ast.Sub: np.subtract,
                ast.Mult: np.multiply,
                ast.Div: np.divide,
                ast.Pow: np.power,
            }[type(n.op)](a, b)
        return {
            "abs": np.abs,
            "sqrt": np.sqrt,
            "log1p": np.log1p,
            "min": np.minimum,
            "max": np.maximum,
        }[n.func.id](*(visit(a) for a in n.args))

    with np.errstate(all="ignore"):
        out = np.asarray(visit(expression(text)), dtype=float)
    if not np.all(np.isfinite(out)):
        raise ValueError(
            "Surface expression is non-finite at some sites (check division or function domains)"
        )
    return out
