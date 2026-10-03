"""The kinds of task, by name (``kind = "..."`` in a campaign file)."""

from __future__ import annotations

from typing import Dict

from yapnr.exp.kinds.base import Context, Kind
from yapnr.exp.kinds.bench import BenchCell
from yapnr.exp.kinds.ladder import LadderCell
from yapnr.exp.kinds.mc import McEval, McPlace
from yapnr.exp.kinds.rf import RfRun
from yapnr.exp.kinds.smoke import Smoke

KINDS: Dict[str, Kind] = {
    kind.name: kind for kind in (LadderCell(), BenchCell(), McEval(), McPlace(), RfRun(), Smoke())
}

__all__ = ["KINDS", "Context", "Kind", "get"]


def get(name: str) -> Kind:
    try:
        return KINDS[name]
    except KeyError:
        raise ValueError(
            "no kind %r yet (implemented: %s)" % (name, ", ".join(sorted(KINDS)))
        ) from None
