"""Push-and-shove rungs: small boards where nets routed late must move earlier copper.

``11-shove-channel-N`` is a two-layer board split by a copper wall (a keep-out for
tracks and vias on both layers) with one narrow channel. A 10-net bus runs from a
connector on the west edge to one on the east edge, straight through the channel; four
late nets run from a connector north-west of the wall to one south-east of it, so they
cross the bus and share the channel with it. The channel holds nine tracks per layer
at the fab's pitch (0.25 mm tracks, 0.2 mm clearance) and fourteen nets need it: a
router that commits the first nets it routes to the middle of the channel on both
layers blocks the rest, and must push them aside (or rip them up) to finish.

``11-shove-channel-lm-N`` moves the east connector north, so the bus turns after the
channel, and declares the bus a ``length_match`` group (0.5 mm, judged by KiCad's
``skew`` rule): the matched nets need meander room beside the channel exit, where the
late nets also pass.

Every part is fixed: the rungs test routing alone. The walls are judged by
``no_copper`` checks on the saved board, the group by KiCad's DRC. The channel is
routable by construction: even on a 0.25 mm routing grid (track centres 0.5 mm apart)
eight tracks fit per layer, sixteen in all for fourteen nets.

Both rungs sit in the manual lane with a ``ci.target``: the current router (negotiated
rip-up and reroute) leaves nets open on them, so they wait for the queued push-and-shove
router rather than gate anything.
"""

from copy import deepcopy

from designs import circuit
from hard_rungs import base_checks, connected_pads, hard, pinned

SHOVE_SIZE = (40, 34)
BUS = ["B%d" % k for k in range(10)]
CROSS = ["X%d" % k for k in range(4)]
WALL_X = (18.0, 22.0)
TRACKS_PER_LAYER = 9
# Nine 0.25 mm tracks with 0.2 mm between them and from each wall: 4.25 mm.
CHANNEL_MM = TRACKS_PER_LAYER * 0.25 + (TRACKS_PER_LAYER + 1) * 0.2
CHANNEL_Y = SHOVE_SIZE[1] / 2
LM_TOLERANCE_MM = 0.5


def walls():
    x0, x1 = WALL_X
    h = SHOVE_SIZE[1]
    lo, hi = CHANNEL_Y - CHANNEL_MM / 2, CHANNEL_Y + CHANNEL_MM / 2
    return {"wall-south": [x0, 0.0, x1, lo], "wall-north": [x0, hi, x1, h]}


def _poly(rect):
    x0, y0, x1, y1 = rect
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def shove_parts():
    # JST SH 12 side-entry: pads 1-12 along local x. Rot 90 on the west edge and rot 270
    # on the east edge put pin 1 at opposite ends, so pin k meets pin 13 - k straight.
    j1 = {str(k + 2): n for k, n in enumerate(BUS)}
    j2 = {str(11 - k): n for k, n in enumerate(BUS)}
    for pins in (j1, j2):
        pins.update({"1": "", "12": "", "MP": ""})
    j3 = {**{str(k + 1): "" for k in range(8)}, "MP": ""}
    j4 = dict(j3)
    for k, n in enumerate(CROSS):
        j3[str(k + 3)] = n
        j4[str(6 - k)] = n
    return [
        pinned("J1", "jst_sh_12", "Bus in", j1),
        pinned("J2", "jst_sh_12", "Bus out", j2),
        pinned("J3", "jst_sh_8", "Late in", j3),
        pinned("J4", "jst_sh_8", "Late out", j4),
    ]


def shove_channel(*, east_y=CHANNEL_Y, name="11-shove-channel"):
    parts = shove_parts()
    spec = circuit(
        "%s-%d" % (name, len(BUS) + len(CROSS)),
        "A 10-net bus and four crossing late nets through one narrow channel in a copper "
        "wall on two layers: nine tracks per layer fit, fourteen nets need the channel.",
        parts,
        SHOVE_SIZE,
    )
    cons = spec["constraints"]
    cons["net_class"] = {}
    w, h = SHOVE_SIZE
    cons["fixed"] = {
        "J1": dict(at=[4.0, CHANNEL_Y], rot=90, side="top"),
        "J2": dict(at=[w - 4.0, east_y], rot=270, side="top"),
        "J3": dict(at=[10.0, h - 4.0], rot=0, side="top"),
        "J4": dict(at=[w - 9.0, 4.0], rot=180, side="top"),
    }
    cons["copper_keepout"] = [
        dict(name=k, rect=list(r), layers=["F.Cu", "B.Cu"], items=["tracks", "vias"])
        for k, r in sorted(walls().items())
    ]
    cons["keepout"] = [dict(name=k, polygon=_poly(r)) for k, r in sorted(walls().items())]
    spec["supply"] = dict(voltage_v=3.3, max_current_a=0.01)
    spec = hard(spec, "shove-channel", ["push-and-shove", "narrow-channel"], "manual", 30)
    # A target for the queued push-and-shove router: the current router (negotiated
    # rip-up and reroute on a grid) leaves two nets open (module docstring; 2026-10-06).
    spec["ci"]["target"] = dict(
        feature="push-and-shove",
        reason="the rip-up-and-reroute router leaves two of the fourteen channel nets open",
    )
    spec["checks"] = base_checks(spec) + [
        dict(
            id="no-copper-" + k,
            kind="no_copper",
            polygon=_poly(r),
            layers=["F.Cu", "B.Cu"],
            items=["tracks", "vias"],
            allow_nets=[],
            engine="copper_keepout",
        )
        for k, r in sorted(walls().items())
    ]
    spec["expected_connected_pads"] = connected_pads(spec["parts"])
    return spec


def shove_channel_lm():
    """The bus turns north after the channel to a connector 7 mm higher, matched to
    0.5 mm."""
    spec = shove_channel(east_y=CHANNEL_Y + 7.0, name="11-shove-channel-lm")
    cons = spec["constraints"]
    cons["length_match"] = [dict(name="bus", nets=list(BUS), tolerance_mm=LM_TOLERANCE_MM)]
    spec["description"] = (
        "The channel board with the bus turning north after the channel and matched to "
        "0.5 mm: its meanders need the room the late nets cross."
    )
    spec["features"] = sorted(set(spec["features"]) | {"length-match"})
    spec["dims"]["constraints"] = "length-match"
    return spec


def rungs():
    return deepcopy([shove_channel(), shove_channel_lm()])
