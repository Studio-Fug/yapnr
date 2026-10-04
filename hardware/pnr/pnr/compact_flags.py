"""PNR_COMPACT / PNR_SHRINK switches (stdlib only; docs/design/compact-placement.md).

The compact-placement logic lives in :mod:`pnr.place.compact`; this module only reads the
environment, so stdlib-only modules (:mod:`pnr.place.geometry`, the trace header) ask the
same question as the placer without importing torch or numpy.

``PNR_COMPACT=1`` turns on every part:

``GP``
    global placement at spread 1.0 with its random starts drawn in a cluster box;
``RANK``
    a compactness tie-break in the Monte Carlo selections, after every completion key;
``LEGALIZE``
    the courtyard gap instead of the routing clearance, a per-part copper margin, a
    0.125 mm slot grid and pads kept off the outline;
``COURTYARD``
    offset courtyards: a part occupies its real body box (``Component.body``), which
    lies off its origin for a pin-1-origin header, instead of the origin-symmetric
    envelope;
``DROPS``
    a ``plane_layer`` net class without a declared stack (the legacy plane path) plans
    its surface pads' through-via drops with the signal escapes, before routing, as a
    declared stack does, instead of leaving them to writeback's dog-bones after
    routing, where routed copper can enclose a pad;
``WIRE``
    the legalizer weighs each part's wirelength (weight 4) and picks its turn with its slot
    (``PNR_LEGALIZE_HPWL``, :mod:`pnr.legalize_flags`);
``TURN``
    after legalization, parts turn in place where that shortens their wires and stays legal
    (``PNR_LEGALIZE_REORIENT=wire``);
``SATELLITES``
    a line group carries each member's satellite, such as an LED's series resistor, flush
    beside it (``PNR_LINE_SATELLITES``).

An explicit ``PNR_LEGALIZE_HPWL``, ``PNR_LEGALIZE_REORIENT`` or ``PNR_LINE_SATELLITES``
(``0`` included) wins over its part. ``PNR_COMPACT_<PART>=0`` drops one part (an ablation). ``PNR_SHRINK=1`` (the flat
driver's shrink-to-fit outline search) is separate and never on by default. Unset, every
caller takes its unchanged path and writes no new JSON keys.
"""

from __future__ import annotations

import os

PARTS = ("GP", "RANK", "LEGALIZE", "COURTYARD", "DROPS", "WIRE", "TURN", "SATELLITES")


def enabled(part=None) -> bool:
    """True with ``PNR_COMPACT=1`` (and, for ``part``, unless ``PNR_COMPACT_<PART>=0``)."""
    if os.environ.get("PNR_COMPACT") != "1":
        return False
    if part is None:
        return True
    if part not in PARTS:
        raise ValueError("unknown PNR_COMPACT part %r" % (part,))
    return os.environ.get("PNR_COMPACT_" + part, "1") != "0"


def shrink_enabled() -> bool:
    """True with ``PNR_SHRINK=1``: the flat driver searches a smaller outline."""
    return os.environ.get("PNR_SHRINK") == "1"


def active() -> dict:
    """The active compact parts and shrink, for provenance (empty when all are off)."""
    out = {part: True for part in PARTS if enabled(part)}
    if shrink_enabled():
        out["SHRINK"] = True
    return out
