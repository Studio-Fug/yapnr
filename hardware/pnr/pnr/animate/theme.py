"""Colours, sizes and the fixed display strings of the animations.

The colours are the viewer's (``yapnr/viewer/static/app.js``: background, copper layers,
vias, pads, ratsnest, new/ripped/provisional copper, accent); ``SUBSTRATE`` and
``COURTYARD`` are new here.
"""

BACKGROUND = "#10191e"
SUBSTRATE = "#15232a"  # new: the board inside its outline
OUTLINE = "#acc7ce"
LAYERS = {"F.Cu": "#e89a73", "In1.Cu": "#aa9de9", "In2.Cu": "#dfbd62", "B.Cu": "#6eb6e5"}
ZONE_ALPHA = 0.15  # the viewer's "25" alpha suffix
VIA_RING = "#b3c1c9"
VIA_HOLE = "#15252c"
PAD = "#cdd2be"
COURTYARD = "#3b5561"  # new: 1 px part outlines
RATSNEST = "#8da4ba"
RATSNEST_ALPHA = 0.33
NEW = "#62ffad"
RIPPED = "#ff566b"
PROVISIONAL = "#d39af6"
ACCENT = "#9ee6d1"
HEAT_LOW = "#61dbac"
HEAT_HIGH = "#f07769"
TEXT = "#dce5e9"
MUTED = "#8a9ea7"
FAIL = "#ff566b"

HEADER_PX = 34
FOOTER_PX = 40
MARGIN = 0.04  # of the outline, around the board
SUPERSAMPLE = 2

# Phase keys of pnr.trace events and scenes -> overlay text.
PHASE_TEXT = {
    "title": "",
    "source": "Unplaced board",
    "pool": "Initial placement pool",
    "global-placement": "Global placement",
    "legalization": "Legalization",
    "placement": "Placement",
    "attempts": "Placement attempt failed legalization",
    "negotiation": "Routing: negotiation",
    "commit": "Routing: commit",
    "rip-up": "Routing: rip-up and reroute",
    "routed": "Routed",
    "congestion": "Congestion feedback",
    "selection": "",  # montages carry their own caption
    "writeback": "KiCad: writeback",
    "planes": "KiCad: planes",
    "refill": "KiCad: zone refill",
    "drc": "KiCad DRC",
    "result": "KiCad DRC verdict",
}

CRITERION_TEXT = {
    "capacity-proxy": "capacity proxy",
    "route-objective": "route objective",
    "missing-connections": "missing connections",
    "first-legal": "first legal attempt",
}

MONTAGE_TEXT = {
    "capacity-proxy": "{among} legal starts, {selected} shortlisted by capacity proxy",
    "route-objective": "{among} finalists routed, the best route chosen",
    "missing-connections": "{among} rounds, the fewest missing connections chosen",
}
