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

One switch guards the initial pool's own screen, not the legalizer itself:

``PNR_POOL_RELOCATION_SCREEN``
    Unset, it follows :func:`legalize_keep` (on with the displacement-minimizing legalizer).
    ``1`` on, ``0`` off. On, :func:`pnr.place.initial_pool.select_initial_placement` keeps a
    start whose own legalization relocated more than one part and more than
    :data:`pnr.place.initial_pool.POOL_RELOCATION_FRACTION_THRESHOLD` of the movable parts
    (:func:`pnr.place.initial_pool.wholesale_relocation`) out of the proxy shortlist, its
    diversity fill and the routed finalists, unless no other legal start exists:
    PNR_LEGALIZE_KEEP relocates only an occluded part, so a start that forces it to relocate
    most of the board is a pool defect, not a part property. (It replaces the
    ``PNR_POOL_MOTION_PENALTY`` cheap_score factor, which every start outran: the proxy budget
    equals the start count, and the diversity fill re-admitted the far start.)

Two switches are on by default and turn off with ``0`` (for A/B runs):

``PNR_DETAIL_PLACE``
    Detailed placement after legalization (:mod:`pnr.place.detail`): an order-preserving
    compaction toward each part's nets, then turns, slides, swaps and wrong-side moves under a
    movement budget, in place of the KEEP-restricted ``TURN``. Unset, it follows
    ``PNR_LEGALIZE_KEEP`` (on by default); ``0`` turns it off for an A/B, ``1`` on.
``PNR_LEGALIZE_KEEP``
    Displacement-minimizing legalization (:mod:`pnr.place.keep`): a part legal at its
    global-placement pose keeps it (a grid snap) and its turn, a slightly overlapping one and its
    neighbours are pushed apart in their global order (the overlap first; routing channels as far
    as the push reaches), a part the push cannot clear or whose slot is taken takes the nearest
    free slot around its pose, and only a mostly occluded part is relocated by the packer's cost
    and turn search; with
    ``PNR_LEGALIZE_REORIENT`` (or ``TURN``) only the parts the legalizer moved turn afterwards
    (with detailed placement off; on, its turns take that pass's place).
    ``0`` restores the plain nearest-free-slot packer for every part.

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
    "PNR_LEGALIZE_KEEP",
    "PNR_DETAIL_PLACE",
    "PNR_CHANNEL_LAYERS",
    "PNR_POOL_RELOCATION_SCREEN",
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


def legalize_keep() -> bool:
    """``PNR_LEGALIZE_KEEP``: displacement-minimizing legalization (:mod:`pnr.place.keep`), on
    unless the variable is ``0``."""
    raw = (os.environ.get("PNR_LEGALIZE_KEEP") or "").strip()
    if raw in ("", "1"):
        return True
    if raw == "0":
        return False
    raise ValueError("PNR_LEGALIZE_KEEP takes 0 or 1, got %r" % (raw,))


def detail_place() -> bool:
    """``PNR_DETAIL_PLACE``: detailed placement after legalization (:mod:`pnr.place.detail`):
    ``1`` on, ``0`` off; unset, it follows :func:`legalize_keep` (on with the displacement-
    minimizing legalizer, whose legal parts it moves toward their nets; off with the packer,
    which already re-sites every part for wirelength)."""
    raw = (os.environ.get("PNR_DETAIL_PLACE") or "").strip()
    if raw == "":
        return legalize_keep()
    if raw == "0":
        return False
    if raw == "1":
        return True
    raise ValueError("PNR_DETAIL_PLACE takes 0 or 1, got %r" % (raw,))


def channel_layers() -> bool:
    """True with ``PNR_CHANNEL_LAYERS=1``: the channel model credits the other signal layers."""
    return os.environ.get("PNR_CHANNEL_LAYERS") == "1"


def pool_relocation_screen() -> bool:
    """``PNR_POOL_RELOCATION_SCREEN``: the initial pool keeps a start whose legalization
    relocated a high share of the movable parts out of its shortlists
    (:mod:`pnr.place.initial_pool`). ``1`` on, ``0`` off; unset, it follows
    :func:`legalize_keep` (only PNR_LEGALIZE_KEEP reports ``relocated``)."""
    raw = (os.environ.get("PNR_POOL_RELOCATION_SCREEN") or "").strip()
    if raw == "":
        return legalize_keep()
    if raw == "0":
        return False
    if raw == "1":
        return True
    raise ValueError("PNR_POOL_RELOCATION_SCREEN takes 0 or 1, got %r" % (raw,))


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
    if not legalize_keep():
        out["LEGALIZE_KEEP"] = False  # the default (on) is not listed
    if detail_place() != legalize_keep():
        out["DETAIL_PLACE"] = detail_place()  # the default (as KEEP) is not listed
    if channel_layers():
        out["CHANNEL_LAYERS"] = True
    if pool_relocation_screen() != legalize_keep():
        # The default (as KEEP) is not listed.
        out["POOL_RELOCATION_SCREEN"] = pool_relocation_screen()
    return out
