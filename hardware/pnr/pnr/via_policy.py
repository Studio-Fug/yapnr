"""Via kinds a board may use, and the copper span each one occupies.

A KiCad 10 via has a type (through, blind, buried or micro) and a layer pair; its
copper, hole and plane clearances exist only on the layers from one to the other
(the zone filler cuts antipads only there, and DRC checks clearance only there).

``rules["via_policy"]`` (:func:`resolve`, :func:`board_policy`) says which kinds a
route may use on this board::

    {"allowed": ["through", "blind", "buried", "micro"],
     "layers": ["F.Cu", "In1.Cu", ..., "B.Cu"],       # copper, outer to outer
     "gaps_mm": [0.09, 0.55, ...],                     # dielectric between neighbours
     "sizes": {"blind": [0.6, 0.3], "buried": [0.6, 0.3], "micro": [0.3, 0.1]},
     "warnings": [...]}

It is absent when only through vias are allowed: the board then routes exactly as
before. The allowed kinds are the ones the design declares (through only when it
declares none) minus every kind the board's custom rules (``.kicad_dru``) disallow;
a disallow rule limited by a layer or a condition still counts as a ban everywhere,
which is the safe reading. ``via`` bans every kind but through (through vias stay the
engine's fallback, the judge reports them), and ``buried_via`` bans blind vias too
(KiCad up to 9 named both kinds that way).

Spans (stack indices ``t < b``, outer to outer):

``through``  F to B.
``micro``    two adjacent layers, one of them outer, whose dielectric is no deeper
             than the microvia drill (aspect ratio 1:1, IPC-2226); KiCad itself
             accepts any pair, so the engine keeps to this.
``blind``    an outer layer to an inner one.
``buried``   two inner layers.

A span's cost multiplier (the router's via price) is 1.0 for a through via, so a
through-only board prices exactly as before, and ``0.5 + 0.5 * depth / thickness``
for any other span: a shorter span drills less and blocks fewer layers.

:class:`ViaModel` answers span questions on the stack and :class:`GridVias` on the
detailed router's grid (whose layers are the routed subset of the stack). Pure
stdlib.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

THROUGH, BLIND, BURIED, MICRO = "through", "blind", "buried", "micro"
KINDS = (THROUGH, BLIND, BURIED, MICRO)

# KiCad DRU ``disallow`` item -> the kinds it bans (through is never removed).
_DRU_ITEMS = {
    "via": (BLIND, BURIED, MICRO),
    "blind_via": (BLIND,),
    "buried_via": (BLIND, BURIED),
    "micro_via": (MICRO,),
}

# KiCad's defaults when a project states no microvia minimums.
_MIN_MICRO = (0.2, 0.1)


def dru_via_bans(text: Optional[str]) -> Tuple[set, List[str]]:
    """``(kinds, notes)``: the via kinds a ``.kicad_dru`` disallows, and one note per
    ban that a layer or condition limits (taken as a ban everywhere)."""
    banned: set = set()
    notes: List[str] = []
    if not text:
        return banned, notes
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    for block in re.split(r"\(rule\s", text)[1:]:
        name = re.match(r'\s*"([^"]*)"|\s*(\S+)', block)
        label = (name.group(1) or name.group(2)) if name else "?"
        for items in re.findall(r"\(constraint\s+disallow\s+([^)]*)\)", block):
            kinds = {k for item in items.split() for k in _DRU_ITEMS.get(item, ())}
            if not kinds:
                continue
            banned |= kinds
            if re.search(r"\((layer|condition)\s", block):
                notes.append(
                    "rule %r disallows %s only somewhere; taken as a ban everywhere"
                    % (label, "/".join(sorted(kinds)))
                )
    return banned, notes


def copper_gaps(rows: Sequence[dict], names: Sequence[str]) -> Optional[List[float]]:
    """Dielectric thickness (mm) between each pair of neighbouring copper layers of
    ``names``, from a stackup block's rows (:func:`pnr.stack.stackup_rows`), or None
    when the block does not list exactly these copper layers or a thickness."""
    copper = [r["name"] for r in rows if r["type"] == "copper"]
    if list(copper) != list(names):
        return None
    gaps: List[float] = []
    current = None
    for row in rows:
        if row["type"] == "copper":
            if current is not None:
                gaps.append(round(current, 6))
            current = 0.0
        elif current is not None and (
            row["type"] in ("core", "prepreg") or row["name"].startswith("dielectric")
        ):
            if row["thickness_mm"] is None:
                return None
            current += row["thickness_mm"]
    return gaps if len(gaps) == len(names) - 1 else None


def resolve(
    declared,
    layers: Sequence[str],
    *,
    gaps: Optional[Sequence[float]] = None,
    banned: Iterable[str] = (),
    via_size: Tuple[float, float] = (0.6, 0.3),
    microvia: Optional[Tuple[float, float]] = None,
    min_microvia: Tuple[float, float] = _MIN_MICRO,
    min_annular: float = 0.0,
    notes: Sequence[str] = (),
) -> Optional[dict]:
    """The board's via policy (module doc), or None for through vias only.

    ``declared``: the design's via policy (``{"allowed": [...], "microvia":
    {"diameter_mm", "drill_mm"}}``, or just the list of kinds); None declares
    through vias only. ``banned``: kinds the board's rules disallow
    (:func:`dru_via_bans`). ``via_size``: the routed via (diameter, drill), also the
    size of blind and buried vias. ``microvia``: the project's microvia size, used
    when the design gives none; a microvia under ``min_microvia`` is not used, and
    one whose ring is under ``min_annular`` (the board's minimum annular width,
    which KiCad applies to microvias too) is widened to meet it."""
    if declared is None:
        return None
    kinds = declared.get("allowed", []) if isinstance(declared, dict) else list(declared)
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise ValueError("unknown via kind(s) %s; known: %s" % (unknown, ", ".join(KINDS)))
    warnings = list(notes)
    banned = set(banned)
    allowed = [k for k in KINDS if k in kinds and k not in banned]
    for kind in sorted(set(kinds) & banned - {THROUGH}):
        warnings.append("%s vias are declared but the board's rules disallow them" % kind)
    if THROUGH not in allowed:
        allowed.insert(0, THROUGH)
    layers = list(layers)
    if len(layers) < 3:
        return None
    sizes = {BLIND: list(via_size), BURIED: list(via_size)}
    if MICRO in allowed:
        size = None
        if isinstance(declared, dict) and declared.get("microvia"):
            size = (
                float(declared["microvia"]["diameter_mm"]),
                float(declared["microvia"]["drill_mm"]),
            )
        elif microvia:
            size = (float(microvia[0]), float(microvia[1]))
        if size is None:
            warnings.append("microvias are allowed but no microvia size is given: not used")
            allowed.remove(MICRO)
        elif size[0] < min_microvia[0] - 1e-9 or size[1] < min_microvia[1] - 1e-9:
            warnings.append(
                "microvia %.3g/%.3g mm is under the board's minimum %.3g/%.3g mm: not used"
                % (size + tuple(min_microvia))
            )
            allowed.remove(MICRO)
        elif size[1] >= size[0]:
            raise ValueError("microvia drill must be smaller than its diameter")
        else:
            ring = round(size[1] + 2 * min_annular, 6)
            if size[0] < ring - 1e-9:
                warnings.append(
                    "microvia %.3g/%.3g mm widened to %.3g/%.3g mm: the board's minimum "
                    "annular width is %.3g mm" % (size + (ring, size[1], min_annular))
                )
                size = (ring, size[1])
            sizes[MICRO] = list(size)
    if allowed == [THROUGH]:
        return None
    if gaps is not None and len(gaps) != len(layers) - 1:
        raise ValueError("gaps_mm must list one dielectric per neighbouring copper pair")
    out = dict(
        allowed=allowed,
        layers=layers,
        gaps_mm=None if gaps is None else [float(g) for g in gaps],
        sizes={k: v for k, v in sizes.items() if k in allowed},
    )
    if MICRO in allowed and gaps is None:
        warnings.append("the stack states no dielectric thickness: no microvia span qualifies")
    if warnings:
        out["warnings"] = warnings
    return out


def board_policy(declared, board_path, rules: dict) -> Optional[dict]:
    """:func:`resolve` for the board file ``board_path``: its stackup block (copper
    layers and dielectrics), the custom rules beside it (``.kicad_dru``) and the
    project's microvia size and minimums (``.kicad_pro``). ``rules`` are the routing
    rules (layer count, the fab via size and the minimum annular width that
    writeback stamps into the project)."""
    from pnr.stack import copper_names, stackup_rows

    if declared is None:
        return None
    path = str(board_path)
    stem = path[: -len(".kicad_pcb")] if path.endswith(".kicad_pcb") else path
    text = _read(path) or ""
    rows = stackup_rows(text)
    names = [r["name"] for r in rows if r["type"] == "copper"]
    count = int(rules.get("layers", 2))
    if len(names) != count:
        names = copper_names(count)
    gaps = copper_gaps(rows, names)
    banned, notes = dru_via_bans(_read(stem + ".kicad_dru"))
    microvia = None
    minimum = _MIN_MICRO
    project = _read(stem + ".kicad_pro")
    if project:
        try:
            doc = json.loads(project)
        except ValueError:
            doc = {}
        design = (doc.get("board") or {}).get("design_settings") or {}
        limits = design.get("rules") or {}
        minimum = (
            float(limits.get("min_microvia_diameter", _MIN_MICRO[0])),
            float(limits.get("min_microvia_drill", _MIN_MICRO[1])),
        )
        for nc in (doc.get("net_settings") or {}).get("classes") or []:
            if nc.get("name") == "Default" and nc.get("microvia_diameter"):
                microvia = (float(nc["microvia_diameter"]), float(nc["microvia_drill"]))
    fab = rules.get("fab") or {}
    return resolve(
        declared,
        names,
        gaps=gaps,
        banned=banned,
        via_size=(float(fab.get("via_diameter_mm", 0.6)), float(fab.get("via_drill_mm", 0.3))),
        microvia=microvia,
        min_microvia=minimum,
        min_annular=float(fab.get("via_annular_mm") or 0.0),
        notes=notes,
    )


def _read(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ the stack model


class ViaModel:
    """Spans on the stack of ``policy`` (stack indices, 0 = F.Cu)."""

    def __init__(self, policy: dict, through: Tuple[float, float]):
        self.policy = policy
        self.layers = list(policy["layers"])
        self.n = len(self.layers)
        self.allowed = set(policy["allowed"]) | {THROUGH}
        self.gaps = policy.get("gaps_mm")
        self.sizes = {k: tuple(v) for k, v in (policy.get("sizes") or {}).items()}
        self.sizes[THROUGH] = tuple(through)
        self._cover: Dict[Tuple[int, int], Tuple[int, int, str]] = {}

    def index(self, name: str) -> int:
        return self.layers.index(name)

    def micro_ok(self, t: int, b: int) -> bool:
        """A microvia may join stack layers ``t < b``: adjacent, one of them outer,
        and a dielectric no deeper than its drill."""
        if MICRO not in self.allowed or b != t + 1 or not (t == 0 or b == self.n - 1):
            return False
        if self.gaps is None:
            return False
        return self.gaps[t] <= self.sizes[MICRO][1] + 1e-9

    def kind_of(self, t: int, b: int) -> Optional[str]:
        """The kind a via spanning exactly ``t..b`` is (microvia when one qualifies),
        or None when that kind is not allowed."""
        if not 0 <= t < b < self.n:
            raise ValueError("a via spans two distinct copper layers")
        if t == 0 and b == self.n - 1:
            return THROUGH
        if self.micro_ok(t, b):
            return MICRO
        kind = BLIND if t == 0 or b == self.n - 1 else BURIED
        return kind if kind in self.allowed else None

    def cost(self, t: int, b: int, kind: str) -> float:
        if kind == THROUGH:
            return 1.0
        if self.gaps:
            total = sum(self.gaps)
            depth = sum(self.gaps[t:b])
        else:
            total, depth = float(self.n - 1), float(b - t)
        return 0.5 + 0.5 * (depth / total if total > 0 else 1.0)

    def cover(self, t: int, b: int) -> Tuple[int, int, str]:
        """``(top, bottom, kind)``: the cheapest allowed span containing ``t..b``
        (a through via always does)."""
        if t > b:
            t, b = b, t
        key = (t, b)
        if key not in self._cover:
            best = None
            for top in range(t, -1, -1):
                for bottom in range(max(b, top + 1), self.n):
                    kind = self.kind_of(top, bottom)
                    if kind is None:
                        continue
                    rank = (self.cost(top, bottom, kind), bottom - top, top)
                    if best is None or rank < best[0]:
                        best = (rank, (top, bottom, kind))
            self._cover[key] = best[1]
        return self._cover[key]

    def size(self, kind: str) -> Tuple[float, float]:
        return self.sizes.get(kind, self.sizes[THROUGH])


@dataclass(frozen=True)
class Span:
    """A via on the router's grid: copper layers ``t..b`` of the stack (indices)
    named ``top``/``bottom``, the grid layers ``lo..hi`` it occupies, its kind, size
    (mm), keep-out (grid cells) and price multiplier."""

    t: int
    b: int
    lo: int
    hi: int
    top: str
    bottom: str
    kind: str
    diameter: float
    drill: float
    keepout: int
    cost: float

    @property
    def radius(self) -> float:
        return self.diameter / 2.0

    @property
    def through(self) -> bool:
        return self.kind == THROUGH

    def layers(self):
        return range(self.lo, self.hi + 1)


class GridVias:
    """:class:`ViaModel` on a detailed-routing grid whose layers ``grid_layers`` are
    a subset of the stack (the plane layers between them are not grid layers).

    ``through_keepout`` is the grid's own via keep-out radius: a through span keeps
    it exactly, so the through path of the router is unchanged; any other kind's
    radius follows the same rule for its own diameter against the largest via."""

    def __init__(
        self,
        policy: dict,
        grid_layers: Sequence[str],
        pitch: float,
        clearance: float,
        through: Tuple[float, float],
        through_keepout: int,
    ):
        self.model = ViaModel(policy, through)
        self.grid_layers = list(grid_layers)
        self.stack_index = [self.model.index(n) for n in self.grid_layers]
        if self.stack_index != sorted(self.stack_index) or len(set(self.stack_index)) != len(
            self.stack_index
        ):
            raise ValueError("grid layers must be distinct copper layers in stack order")
        self.pitch = float(pitch)
        self.clearance = float(clearance)
        self.through_keepout = int(through_keepout)
        self.max_radius = max(self.model.size(k)[0] / 2.0 for k in self.model.allowed)
        self._spans: Dict[Tuple[int, int], Span] = {}
        self.full = self.stack_span(0, self.model.n - 1)

    def stack_span(self, t: int, b: int) -> Span:
        """The cheapest allowed via containing stack layers ``t..b``."""
        key = (min(t, b), max(t, b))
        span = self._spans.get(key)
        if span is None:
            top, bottom, kind = self.model.cover(*key)
            covered = [g for g, s in enumerate(self.stack_index) if top <= s <= bottom]
            diameter, drill = self.model.size(kind)
            if kind == THROUGH:
                keepout = self.through_keepout
            else:
                keepout = max(
                    1,
                    math.ceil((diameter / 2.0 + self.max_radius + self.clearance) / self.pitch) - 1,
                )
            span = self._spans[key] = Span(
                top,
                bottom,
                covered[0] if covered else -1,
                covered[-1] if covered else -2,
                self.model.layers[top],
                self.model.layers[bottom],
                kind,
                diameter,
                drill,
                keepout,
                self.model.cost(top, bottom, kind),
            )
        return span

    def span(self, a: int, b: int) -> Span:
        """The via joining grid layers ``a`` and ``b``."""
        return self.stack_span(self.stack_index[a], self.stack_index[b])

    def to_layer(self, a: int, name: str) -> Span:
        """The via from grid layer ``a`` to copper layer ``name`` (a plane drop)."""
        return self.stack_span(self.stack_index[a], self.model.index(name))

    def reaches(self, span: Span, name: str) -> bool:
        """``span`` connects copper layer ``name``."""
        return span.t <= self.model.index(name) <= span.b

    def merged(self, spans: Iterable[Span]) -> List[Span]:
        """Same-net vias at one place: spans sharing a copper layer are one barrel
        (two holes there would be co-located); disjoint spans stay apart."""
        return [self.stack_span(t, b) for t, b in merge_ranges((s.t, s.b) for s in spans)]


def merge_ranges(ranges: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge ranges ``(t, b)`` that share an index; disjoint ranges stay separate."""
    out: List[List[int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]
