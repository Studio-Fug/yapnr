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
# New, used only for traces whose header lists placement constraints, for block macros and in
# comparisons (docs/design/constraint-and-hier-animations.md, section 6.2).
CONSTRAINT = "#f2c14e"  # a constrained part's target: the line, the edge, the rigid body
CONSTRAINT_OK = "#62ffad"  # within its tolerance (the "new copper" mint)
CONSTRAINT_BAD = "#ff566b"  # beyond it (the failure red)
BLOCK_OUTLINE = "#d5dde1"  # a block macro's outline (dashed)
REFERENCE = "#6f8792"  # another panel's constraint, drawn as a neutral target

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
    "gloss": "Gloss",
    "gloss-before": "Gloss: before",
    "gloss-after": "Gloss: after",
    "drc": "KiCad DRC",
    "native-phase": "Native loop",
    "result": "KiCad DRC verdict",
    # Hierarchical chapters (pnr.animate.hier).
    "chapter": "",
    "blocks": "Blocks: placed and routed on their own boards",
    "reuse": "Blocks: one layout per template",
    "lift": "Blocks become macros",
    "knit": "Knitting: routing the nets between blocks",
}

CRITERION_TEXT = {
    "capacity-proxy": "capacity proxy",
    "route-objective": "route objective",
    "missing-connections": "missing connections",
    "first-legal": "first legal attempt",
}


def criterion_text(criterion):
    """Display name of a selection criterion (a rung's is its name: "rung1 objective")."""
    return CRITERION_TEXT.get(criterion, str(criterion or "").replace("-", " "))


MONTAGE_TEXT = {
    "capacity-proxy": "{among} legal starts, {selected} shortlisted by capacity proxy",
    "route-objective": "{among} finalists routed, the best route chosen",
    "missing-connections": "{among} rounds, the fewest missing connections chosen",
}
