"""Legalizer and global-placement polish switches (stdlib only; docs/design/compact-placement.md
section 11, "Spacing and turns at legalization").

The logic lives in :mod:`pnr.place.gp_polish` (global placement), :func:`pnr.place.legalize.legalize`
and :mod:`pnr.place.reorient`; this module only reads the environment, so stdlib-only modules
ask the same question as the placer without importing torch or numpy. Every switch is off by
default and works with or without ``PNR_COMPACT``:

``PNR_GP_POLISH=1``
    A final global-placement phase (:data:`POLISH_STEPS` more iterations of
    :func:`pnr.place.model.global_place`'s loop) with the turns and sides frozen, whose overlap
    term uses the legalizer's own slot (courtyard grown by the spreading factor, the clearance and
    the copper margin, rounded up to the slot grid), so snapping cannot create overlaps.
``PNR_GP_CHANNELS=<lambda>``
    The polish (implied) also carries the legalizer's routing-channel cost, made smooth:
    ``lambda * sum shortage^2`` over facing pad rows (:class:`pnr.place.channels.ChannelModel`).
``PNR_POOL_SOURCE_CLAMP=1``
    The initial pool's source start (``start-01``) clamps every movable source position into the
    outline (the cluster box under ``PNR_COMPACT`` ``GP``) before global placement.
``PNR_LEGALIZE_HPWL=<w>``
    The legalizer's slot cost gains ``w`` times the part's wirelength (half-perimeter, plane nets
    left out), and the slot is chosen together with the turn among all four quarter turns.
``PNR_LEGALIZE_REORIENT=1``
    After legalization (and the align snap and the matched-length pass), a greedy in-place pass
    turns parts about their slot centre where that shortens their wirelength, stays legal and does
    not raise the part's routing-channel penalty. ``PNR_LEGALIZE_REORIENT=wire`` drops the channel
    guard (wirelength and legality only, the prototype's rule), for the A/B. Only where the
    placer may turn parts (``orient``).
``PNR_LEGALIZE_CHANNEL_CLEARANCE=fab``
    The legalizer's routing-channel model (:class:`pnr.place.channels.ChannelModel`) spaces the
    tracks of nets without a class at the fab clearance (``fab.clearance_mm``, the router's)
    instead of the board's ``default_clearance_mm``.
``PNR_CHANNEL_LAYERS=1``
    The routing-channel model (:class:`pnr.place.channels.ChannelModel`, used by global
    placement, the legalizer and its push alike) credits the board's other signal layers: a net
    that can drop off the surface asks only its share of the channel (one over the signal-layer
    count), and a drop that needs a via in the channel pays one shared via row there
    (:mod:`pnr.place.channels`, "Layer-aware demand").
``PNR_LINE_SATELLITES=1``
    A line group (:mod:`pnr.place.line_group`) carries its members' satellites: a free two-pad
    part joined to one member pad by a two-pin net sits flush beside that member, in line with
    it, as part of the rigid line (:func:`pnr.place.line_group.satellites`).

Unset (or ``0``), every caller takes its unchanged path and writes no new JSON keys.

Under ``PNR_COMPACT=1`` three of them are compact parts (:mod:`pnr.compact_flags`): ``WIRE``
(``PNR_LEGALIZE_HPWL`` at :data:`COMPACT_WIRE_WEIGHT`), ``TURN`` (``PNR_LEGALIZE_REORIENT=wire``)
and ``SATELLITES`` (``PNR_LINE_SATELLITES=1``). A variable that is set, ``0`` included, wins over
its part; ``PNR_COMPACT_<PART>=0`` drops the part.
"""

from __future__ import annotations

import math
import os
from typing import Optional

from pnr import compact_flags

# Iterations of the global-placement polish phase (fixed: no wall-clock budget).
POLISH_STEPS = 200
# The wirelength weight of the PNR_COMPACT part WIRE (the review-fix A/B's choice, w 4).
COMPACT_WIRE_WEIGHT = 4.0

FLAGS = (
    "PNR_GP_POLISH",
    "PNR_GP_CHANNELS",
    "PNR_POOL_SOURCE_CLAMP",
    "PNR_LEGALIZE_HPWL",
    "PNR_LEGALIZE_REORIENT",
    "PNR_LEGALIZE_CHANNEL_CLEARANCE",
    "PNR_LINE_SATELLITES",
    "PNR_CHANNEL_LAYERS",
)


def _weight(name: str) -> Optional[float]:
    """A non-negative finite weight from ``name``; None when unset, empty or zero."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("%s takes a number, got %r" % (name, raw)) from None
    if not math.isfinite(value) or value < 0:
        raise ValueError("%s takes a non-negative finite number, got %r" % (name, raw))
    return value if value > 0 else None


def gp_channels() -> Optional[float]:
    """``PNR_GP_CHANNELS``: the channel-cost weight lambda of the polish, or None."""
    return _weight("PNR_GP_CHANNELS")


def gp_polish() -> bool:
    """True with ``PNR_GP_POLISH=1`` or a positive ``PNR_GP_CHANNELS``."""
    return os.environ.get("PNR_GP_POLISH") == "1" or gp_channels() is not None


def pool_source_clamp() -> bool:
    """True with ``PNR_POOL_SOURCE_CLAMP=1``."""
    return os.environ.get("PNR_POOL_SOURCE_CLAMP") == "1"


def _explicit(name: str) -> bool:
    """``name`` is set to something (``0`` included): it wins over a compact part."""
    return (os.environ.get(name) or "").strip() != ""


def legalize_hpwl() -> Optional[float]:
    """``PNR_LEGALIZE_HPWL``: the wirelength weight of the legalizer's slot cost, or None
    (unset under ``PNR_COMPACT`` part ``WIRE``: :data:`COMPACT_WIRE_WEIGHT`)."""
    if not _explicit("PNR_LEGALIZE_HPWL") and compact_flags.enabled("WIRE"):
        return COMPACT_WIRE_WEIGHT
    return _weight("PNR_LEGALIZE_HPWL")


REORIENT_MODES = {"1": "guarded", "wire": "wire"}


def legalize_reorient() -> Optional[str]:
    """``PNR_LEGALIZE_REORIENT``: ``"guarded"`` (``1``: wirelength, legality and the channel
    guard), ``"wire"`` (no channel guard) or None (unset or ``0``)."""
    raw = os.environ.get("PNR_LEGALIZE_REORIENT")
    if not _explicit("PNR_LEGALIZE_REORIENT") and compact_flags.enabled("TURN"):
        return "wire"  # the PNR_COMPACT part TURN
    if raw is None or raw in ("", "0"):
        return None
    if raw not in REORIENT_MODES:
        raise ValueError("PNR_LEGALIZE_REORIENT takes 1 or wire, got %r" % (raw,))
    return REORIENT_MODES[raw]


CHANNEL_CLEARANCES = ("fab",)


def legalize_channel_clearance() -> Optional[str]:
    """``PNR_LEGALIZE_CHANNEL_CLEARANCE``: ``"fab"`` or None (unset, empty or ``0``)."""
    raw = os.environ.get("PNR_LEGALIZE_CHANNEL_CLEARANCE")
    if raw is None or raw in ("", "0"):
        return None
    if raw not in CHANNEL_CLEARANCES:
        raise ValueError("PNR_LEGALIZE_CHANNEL_CLEARANCE takes fab, got %r" % (raw,))
    return raw


def line_satellites() -> bool:
    """True with ``PNR_LINE_SATELLITES=1`` (unset: the ``PNR_COMPACT`` part ``SATELLITES``)."""
    if not _explicit("PNR_LINE_SATELLITES"):
        return compact_flags.enabled("SATELLITES")
    return os.environ.get("PNR_LINE_SATELLITES") == "1"


def channel_layers() -> bool:
    """True with ``PNR_CHANNEL_LAYERS=1``: the channel model credits the other signal layers."""
    return os.environ.get("PNR_CHANNEL_LAYERS") == "1"


def active() -> dict:
    """The active switches and their values, for provenance and the trace (empty when off)."""
    out = {}
    if gp_polish():
        out["GP_POLISH"] = POLISH_STEPS
    if gp_channels() is not None:
        out["GP_CHANNELS"] = gp_channels()
    if pool_source_clamp():
        out["POOL_SOURCE_CLAMP"] = True
    if legalize_hpwl() is not None:
        out["LEGALIZE_HPWL"] = legalize_hpwl()
    if legalize_reorient():
        out["LEGALIZE_REORIENT"] = legalize_reorient()
    if legalize_channel_clearance():
        out["LEGALIZE_CHANNEL_CLEARANCE"] = legalize_channel_clearance()
    if line_satellites():
        out["LINE_SATELLITES"] = True
    if channel_layers():
        out["CHANNEL_LAYERS"] = True
    return out
