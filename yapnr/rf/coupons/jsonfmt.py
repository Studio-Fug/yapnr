"""Stable, compact JSON for generated files: objects indented, lists of numbers or strings on
one line, a final newline. The generated files are excluded from prettier (.prettierignore), so
this is their format."""

from __future__ import annotations

import json
from typing import Any


def dumps(obj: Any, indent: int = 2, _level: int = 0) -> str:
    pad = " " * (indent * (_level + 1))
    end = " " * (indent * _level)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [
            f"{pad}{json.dumps(str(k))}: {dumps(v, indent, _level + 1)}" for k, v in obj.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + end + "}"
    if isinstance(obj, (list, tuple)):
        if not obj:
            return "[]"
        if all(not isinstance(v, (dict, list, tuple)) for v in obj):
            return "[" + ", ".join(json.dumps(v) for v in obj) + "]"
        items = [pad + dumps(v, indent, _level + 1) for v in obj]
        return "[\n" + ",\n".join(items) + "\n" + end + "]"
    return json.dumps(obj, ensure_ascii=False)


def dump(obj: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(obj) + "\n")
