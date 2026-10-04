"""Constraint schema + compiler (design doc §3).

Parses the sidecar ``constraints.yaml`` that sits next to a board and compiles it
into the terms the placer consumes: every constraint becomes either a **hard
barrier** (a feasibility region the optimizer must not violate — fixed poses,
keep-outs) or a **soft penalty** (a weighted gradient expressing intent — edge
pulls, side preference, grouping). See the design doc for why "hard as barriers,
soft as penalties" lets one relaxation engine handle both.

This module is the *front end* only: it validates the file, expands globs against
the real netlist, and emits structured, weighted :class:`Constraint` objects. The
actual penalty/barrier *math* (turning these into torch terms) lives in the
placement package added in a later phase — keeping the schema pure Python means
it is unit-testable with no torch/pcbnew in the loop.

Coordinate frame matches :mod:`pnr.graph`: mm, origin bottom-left, ``rot`` CCW.
Schema is versioned (``v0``); unknown top-level keys and unknown component refs
are warnings, not errors, so the file can grow without breaking older boards.
"""

from __future__ import annotations

import fnmatch
import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

SCHEMA_VERSION = "v0"

EDGES = ("north", "south", "east", "west")
SIDES = ("top", "bottom")
# The board's side policy (``board.sides``): ``single`` keeps every part on the side
# it arrives on (the default, and the behaviour of every board without the key);
# ``double`` lets placement choose either side for each part nothing else holds
# (pnr.place.sides).
SIDE_POLICIES = ("single", "double")

# The board-outline default when the file omits one; the placer reframes to the
# real Edge.Cuts once ingested.
DEFAULT_CLEARANCE_MM = 0.2


class Enforcement(Enum):
    """How a constraint acts in the optimizer."""

    HARD = "hard"  # feasibility barrier
    SOFT = "soft"  # weighted penalty gradient


# Default weights for soft terms (relative; tuned later against real boards).
DEFAULT_WEIGHTS = {
    "edge_align": 5.0,
    "side_pref": 1.0,
    "group": 2.0,
}


@dataclass
class FabProfile:
    """The manufacturable design-rule set (a ``fab:`` block) the router targets and
    the emitted board is checked against — a knob for **local DRC relaxation**.

    The defaults are a conservative JLCPCB-class 4-layer set; a fab house that
    supports finer geometry (advanced/2-layer) lets you tighten these, which lowers
    the DRC-clean grid-pitch floor (``track_width + clearance``) and so lets tracks
    escape between finer-pitch pads. Every value flows into the grid pitch, the
    router's via/track geometry, and the ``.kicad_pro`` DRC rules — one source of
    truth. ``min_through_drill`` / ``via_annular`` are loosened only to *tolerate
    source footprints* (vendor parts with sub-spec drills / annuli), not routing.

    The optional fields (None = not distinguished, the pre-profile behaviour) are
    the per-hole-kind rules a fab capability profile adds; they are emitted into
    ``rules.json`` only when set. The selected profile (``pnr.fab_profile``,
    ``PNR_FAB_PROFILE``) overrides these capability values at the pipeline
    boundaries; ``track_width_mm`` (the default signal track) stays the design's.
    """

    track_width_mm: float = 0.15
    clearance_mm: float = 0.13
    via_diameter_mm: float = 0.45
    via_drill_mm: float = 0.25
    hole_clearance_mm: float = 0.20
    edge_clearance_mm: float = 0.20
    min_through_drill_mm: float = 0.20
    via_annular_mm: float = 0.0
    min_track_width_mm: Optional[float] = None
    smd_pad_clearance_mm: Optional[float] = None
    pth_hole_clearance_mm: Optional[float] = None
    npth_hole_clearance_mm: Optional[float] = None
    hole_to_hole_mm: Optional[float] = None
    pth_hole_to_hole_mm: Optional[float] = None
    filled_via_hole_to_hole_mm: Optional[float] = None
    hole_to_edge_mm: Optional[float] = None
    min_via_diameter_mm: Optional[float] = None
    min_npth_drill_mm: Optional[float] = None
    component_pth_min_drill_mm: Optional[float] = None
    via_to_smd_pad_mm: Optional[float] = None

    @property
    def pitch_floor_mm(self) -> float:
        """Smallest DRC-clean grid pitch: two adjacent tracks clear iff
        ``pitch ≥ track + clearance``. The router grid may not go finer."""
        return self.track_width_mm + self.clearance_mm


@dataclass
class BoardSpec:
    """The ``board:`` block — approximate outline + global rules."""

    width: Optional[float] = None
    height: Optional[float] = None
    layers: int = 2
    default_clearance_mm: float = DEFAULT_CLEARANCE_MM
    references_on_fab: bool = False
    sides: str = "single"  # side policy, one of SIDE_POLICIES (pnr.place.sides)
    # Courtyard-to-courtyard gap (mm) of the compact legalizer (PNR_COMPACT LEGALIZE,
    # pnr.place.compact.courtyard_gap); None (not authored) takes its default. A plain
    # class attribute, not a dataclass field, so asdict() is unchanged.
    courtyard_clearance_mm = None


@dataclass
class Constraint:
    """A single compiled constraint.

    ``kind`` is the YAML section it came from (``fixed``/``edge_align``/
    ``keepout``/``side_pref``/``group``); ``enforcement`` says whether the placer
    treats it as a barrier or a penalty; ``refs`` are the *resolved* component
    references it applies to (globs already expanded); ``params`` carries the
    kind-specific fields; ``weight`` is the penalty weight for soft terms
    (``None`` for hard).
    """

    kind: str
    enforcement: Enforcement
    refs: Tuple[str, ...]
    params: Dict = field(default_factory=dict)
    weight: Optional[float] = None
    name: Optional[str] = None


def width_for_current(
    current_a: float, *, copper_oz: float = 1.0, delta_t_c: float = 10.0, external: bool = True
) -> float:
    """Minimum trace width (mm) to carry ``current_a`` within a ``delta_t_c`` rise,
    per **IPC-2221**: ``A[mils²] = (I / (k·ΔT^0.44))^(1/0.725)``, then
    ``width = A / (thickness[mils] · 1.378·copper_oz)``. ``k`` = 0.048 external /
    0.024 internal. Lets a net class be specified by *amperage* instead of a raw
    width, so power rails get sized for their expected current."""
    from pnr.electrical import current_width

    return current_width(current_a, copper_oz, delta_t_c, external)


@dataclass
class NetClass:
    """A named routing rule set (trace width / clearance) over a set of nets.

    ``nets`` are net-name globs, resolved against the real netlist at route time
    (nets are not component refs, so they can't be expanded at compile time).
    ``plane_layer`` (e.g. ``In1.Cu``) pours the class's nets as a copper plane on
    that layer instead of trace-routing them — the right home for high-fanout
    ground / power nets on a multilayer board. ``current_a`` sizes the trace width
    from the expected current (IPC-2221) when ``width_mm`` is not given directly —
    so a class is defined by *type/amperage* (signal vs power) rather than a raw
    width."""

    name: str
    width_mm: Optional[float] = None
    clearance_mm: Optional[float] = None
    nets: Tuple[str, ...] = ()
    plane_layer: Optional[str] = None
    current_a: Optional[float] = None
    copper_oz: float = 1.0
    delta_t_c: float = 10.0
    external: bool = True

    def resolved_width_mm(self, floor_mm: float) -> Optional[float]:
        """Maximum of the explicit width, current screen and fabrication floor."""
        candidates = [floor_mm]
        if self.width_mm is not None:
            from pnr.plane_intent import positive

            candidates.append(positive(self.width_mm, "width_mm"))
        if self.current_a is not None:
            candidates.append(
                width_for_current(
                    self.current_a,
                    copper_oz=self.copper_oz,
                    delta_t_c=self.delta_t_c,
                    external=self.external,
                )
            )
        return max(candidates) if self.width_mm is not None or self.current_a is not None else None


@dataclass
class DiffPair:
    """A differential pair: two nets routed together, length-skew-checked."""

    name: str
    p: str
    n: str
    width_mm: Optional[float] = None
    gap_mm: Optional[float] = None
    skew_mm: float = 0.5  # max acceptable + / - routed-length difference
    # A skew budget in time (ps), judged on delay (per-layer propagation delay from
    # the board's stackup). A pair gives ``skew_mm`` or ``skew_ps``, not both.
    skew_ps: Optional[float] = None
    # PNR_BUS_CLASSES=1 only: ``_defaulted`` (a plain attribute, not a dataclass
    # field, so asdict() is unchanged) names the fields the constraint file left to
    # their defaults; a bus class (pnr.si.bus_classes) may derive those.


@dataclass
class LengthMatch:
    """A group of nets whose routed lengths must agree within a tolerance."""

    name: str
    nets: Tuple[str, ...] = ()
    tolerance_mm: float = 1.0
    tolerance_ps: Optional[float] = None  # a time budget; never with tolerance_mm


@dataclass
class CompiledConstraints:
    """The whole file, compiled and validated."""

    board: BoardSpec
    constraints: List[Constraint] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    schema: str = SCHEMA_VERSION
    # The manufacturable design-rule set (a ``fab:`` block) — see :class:`FabProfile`.
    fab: FabProfile = field(default_factory=FabProfile)
    # Routing rules (design §9.6). Kept separate from placement constraints — the
    # placer ignores them; writeback + the quality pass consume them.
    net_classes: List[NetClass] = field(default_factory=list)
    diff_pairs: List[DiffPair] = field(default_factory=list)
    length_matches: List[LengthMatch] = field(default_factory=list)
    # Meander rules for pair / group length tuning (``tuning:``); None = defaults.
    tuning: Optional[Dict] = None
    # Opt-in legalizer options (``legalize:``, :mod:`pnr.place.legal_options`); None = off.
    legalize: Optional[Dict] = None
    copper_keepouts: List[Dict] = field(default_factory=list)
    mounting_holes: List[Dict] = field(default_factory=list)
    # Fixed copper blocks (``fixed_block``, pnr.fixed_block): KiCad groups kept as drawn.
    fixed_blocks: List[Dict] = field(default_factory=list)
    # ``board.plane_fallback_drops`` (None: not declared, the default true behaviour).
    plane_fallback_drops: Optional[bool] = None

    @property
    def hard(self) -> List[Constraint]:
        return [c for c in self.constraints if c.enforcement is Enforcement.HARD]

    @property
    def soft(self) -> List[Constraint]:
        return [c for c in self.constraints if c.enforcement is Enforcement.SOFT]

    def for_ref(self, ref: str) -> List[Constraint]:
        return [c for c in self.constraints if ref in c.refs]

    @property
    def locked_refs(self) -> Tuple[str, ...]:
        """Refs held out of the position gradient (fixed poses)."""
        out: List[str] = []
        for c in self.constraints:
            if c.kind == "fixed":
                out.extend(c.refs)
        return tuple(out)


class ConstraintError(ValueError):
    """A structural problem that cannot be a warning (bad enum, malformed block)."""


def _expand_refs(
    patterns: Iterable[str], known: Sequence[str], warnings: List[str], where: str
) -> Tuple[str, ...]:
    """Expand a ref or glob against the netlist.

    A literal ref that matches nothing is a warning (kept, so the intent is
    visible, but the placer ignores it); a glob (``*``/``?``/``[``) that matches
    nothing is also a warning. Order is stable and de-duplicated.
    """

    resolved: List[str] = []
    seen = set()
    for pat in patterns:
        is_glob = any(ch in pat for ch in "*?[")
        if is_glob:
            matches = [r for r in known if fnmatch.fnmatchcase(r, pat)]
            if not matches:
                warnings.append(f"{where}: glob {pat!r} matched no components")
            hits = matches
        else:
            if pat in known:
                hits = [pat]
            else:
                warnings.append(f"{where}: unknown component ref {pat!r}")
                hits = [pat]  # keep it; the placer will skip refs it can't find
        for r in hits:
            if r not in seen:
                seen.add(r)
                resolved.append(r)
    return tuple(resolved)


def _require_enum(value, allowed, where: str):
    if value is not None and value not in allowed:
        raise ConstraintError(f"{where}: {value!r} not one of {tuple(allowed)}")
    return value


def _opt_float(value):
    return None if value is None else float(value)


def _positive(value, where: str) -> Optional[float]:
    """An optional positive finite number (None when absent)."""
    if value is None:
        return None
    if not _finite_number(value) or float(value) <= 0:
        raise ConstraintError(f"{where} must be a positive number")
    return float(value)


TUNING_STYLES = ("auto", "trombone", "serpentine", "accordion")


TUNING_NUMBERS = ("gap_mm", "amplitude_max_mm", "min_segment_mm", "max_added_mm")
TUNING_SWITCHES = ("mitre", "meanders", "placement")


def _parse_tuning(raw) -> Optional[Dict]:
    """The ``tuning:`` block (meanders for pair / group length matching): ``gap_mm``
    (edge to edge, at least the clearance and the track width), ``amplitude_max_mm``,
    ``min_segment_mm``, ``max_added_mm`` (meander length one net may gain), ``style``
    (auto, trombone, serpentine, accordion), ``mitre`` (45-degree corners for the fine
    step), and the switches ``meanders`` (the router tunes the sets after routing) and
    ``placement`` (placement keeps the members' estimated lengths even), both on by
    default. None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConstraintError("tuning must be a mapping")
    out: Dict = {}
    for key in TUNING_NUMBERS:
        if raw.get(key) is not None:
            out[key] = _positive(raw[key], f"tuning.{key}")
    if raw.get("style") is not None:
        _require_enum(raw["style"], TUNING_STYLES, "tuning.style")
        out["style"] = str(raw["style"])
    for key in TUNING_SWITCHES:
        if raw.get(key) is not None:
            if not isinstance(raw[key], bool):
                raise ConstraintError(f"tuning.{key} must be true or false")
            out[key] = raw[key]
    unknown = sorted(set(raw) - set(TUNING_NUMBERS) - set(TUNING_SWITCHES) - {"style"})
    if unknown:
        raise ConstraintError("tuning: unknown key(s) %s" % ", ".join(unknown))
    return out


# ``legalize:`` (pnr.place.legal_options): each key's values, the first the default.
LEGALIZE_OPTIONS = {
    "outline": ("raster", "exact"),
    "order": ("blocks", "scarcity"),
    "lookahead": ("none", "regions"),
}


def _parse_legalize(raw) -> Optional[Dict]:
    """The ``legalize:`` block: opt-in legalizer options, each a name from
    :data:`LEGALIZE_OPTIONS` (``outline: exact`` keeps every courtyard inside the board
    outline by the same test as the hard check, ``order: scarcity`` orders parts held to
    a region or an edge band with the hard-group blocks by remaining slots,
    ``lookahead: regions`` refuses a slot that strands a scarce region or group part).
    None when absent; only the keys given, so a default-valued key is kept as written."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConstraintError("legalize must be a mapping")
    unknown = sorted(set(raw) - set(LEGALIZE_OPTIONS))
    if unknown:
        raise ConstraintError("legalize: unknown key(s) %s" % ", ".join(map(str, unknown)))
    out: Dict = {}
    for key, allowed in LEGALIZE_OPTIONS.items():
        if key in raw:
            out[key] = _require_enum(raw[key], allowed, f"legalize.{key}")
            if out[key] is None:
                raise ConstraintError(f"legalize.{key} must be one of {allowed}")
    return out


def _anchor_pad(value, hard: bool) -> str:
    """A hard group's ``anchor_pad``: the anchor's pad name (number or string)."""
    if not hard:
        raise ConstraintError("group.anchor_pad requires hard: true")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == "":
        raise ConstraintError("group.anchor_pad must be a pad name")
    return str(value)


def _expand_nets(patterns: Iterable[str], net_names: Sequence[str]) -> Tuple[str, ...]:
    """Expand net-name literals/globs against the real netlist (order-stable)."""
    resolved: List[str] = []
    seen = set()
    for pat in patterns:
        is_glob = any(ch in pat for ch in "*?[")
        hits = (
            [n for n in net_names if fnmatch.fnmatchcase(n, pat)]
            if is_glob
            else ([pat] if pat in net_names else [])
        )
        for n in hits:
            if n not in seen:
                seen.add(n)
                resolved.append(n)
    return tuple(resolved)


def compile_routing_rules(compiled: "CompiledConstraints", net_names: Sequence[str]) -> Dict:
    """Resolve net-class / diff-pair / length-match rules against the real net
    names into a plain (JSON-serializable) dict — the ``rules.json`` seam the
    pcbnew steps (writeback, quality) consume without pyyaml/torch. Net globs are
    expanded here (where the netlist is known); unknown literal nets are dropped.
    """
    names = set(net_names)
    fab = compiled.fab
    return {
        "layers": int(compiled.board.layers),
        "references_on_fab": compiled.board.references_on_fab,
        "copper_keepouts": [
            _keepout_rules(k, compiled, net_names) for k in compiled.copper_keepouts
        ],
        "mounting_holes": compiled.mounting_holes,
        "default_clearance_mm": float(compiled.board.default_clearance_mm),
        "fab": {
            "track_width_mm": fab.track_width_mm,
            "clearance_mm": fab.clearance_mm,
            "via_diameter_mm": fab.via_diameter_mm,
            "via_drill_mm": fab.via_drill_mm,
            "hole_clearance_mm": fab.hole_clearance_mm,
            "edge_clearance_mm": fab.edge_clearance_mm,
            "min_through_drill_mm": fab.min_through_drill_mm,
            "via_annular_mm": fab.via_annular_mm,
            # Per-hole-kind distinctions only when the fab block sets them.
            **{k: getattr(fab, k) for k in _OPTIONAL_FAB if getattr(fab, k) is not None},
        },
        "net_classes": [
            {
                "name": nc.name,
                # Use every applicable minimum, including the current-derived width.
                "width_mm": nc.resolved_width_mm(fab.track_width_mm),
                "current_a": nc.current_a,
                "copper_oz": nc.copper_oz,
                "delta_t_c": nc.delta_t_c,
                "external": nc.external,
                "clearance_mm": nc.clearance_mm,
                "plane_layer": nc.plane_layer,
                "nets": list(_expand_nets(nc.nets, net_names)),
            }
            for nc in compiled.net_classes
        ],
        "diff_pairs": [
            {
                "name": dp.name,
                "p": dp.p,
                "n": dp.n,
                "width_mm": dp.width_mm,
                "gap_mm": dp.gap_mm,
                "skew_mm": dp.skew_mm,
                **({"skew_ps": dp.skew_ps} if dp.skew_ps is not None else {}),
                **({"defaulted": list(dp._defaulted)} if getattr(dp, "_defaulted", ()) else {}),
            }
            for dp in compiled.diff_pairs
            if dp.p in names and dp.n in names
        ],
        "length_match": [
            {
                "name": lm.name,
                "nets": list(_expand_nets(lm.nets, net_names)),
                "tolerance_mm": lm.tolerance_mm,
                **({"tolerance_ps": lm.tolerance_ps} if lm.tolerance_ps is not None else {}),
            }
            for lm in compiled.length_matches
        ],
        **({"tuning": dict(compiled.tuning)} if compiled.tuning is not None else {}),
        # Declared only: a board without them keeps its rules.json bytes.
        **(
            {"fixed_blocks": [dict(b) for b in compiled.fixed_blocks]}
            if compiled.fixed_blocks
            else {}
        ),
        **(
            {"plane_fallback_drops": compiled.plane_fallback_drops}
            if compiled.plane_fallback_drops is not None
            else {}
        ),
    }


# copper_keepout v1 (layers, items, allow lists, exempt groups); a v0 entry is
# ``{name, ref, rect_mm}`` exactly and keeps that form everywhere (pnr.fixed_block).
from pnr.fixed_block import (  # noqa: E402 - stdlib-only helpers shared with pcbnew steps
    KEEPOUT_ITEMS,
    KEEPOUT_V1_KEYS,
    keepout_is_v1,
)


def _keepout_rules(spec: Dict, compiled: "CompiledConstraints", net_names: Sequence[str]) -> Dict:
    """The rules.json form of one compiled keepout: v0 unchanged; v1 with its
    exempt nets resolved (``allowed_nets``: the allow-list globs and the nets of the
    allowed classes, against the real netlist)."""
    if not keepout_is_v1(spec):
        return spec
    out = dict(spec)
    allowed = set(_expand_nets(spec.get("allow_nets") or (), net_names))
    classes = set(spec.get("allow_classes") or ())
    for nc in compiled.net_classes:
        if nc.name in classes:
            allowed.update(_expand_nets(nc.nets, net_names))
    for dp in compiled.diff_pairs:  # a pair is the class "dp_<name>" (writeback)
        if "dp_" + dp.name in classes:
            allowed.update(n for n in (dp.p, dp.n) if n in set(net_names))
    out["allowed_nets"] = sorted(allowed)
    return out


def _parse_board(raw: Dict) -> BoardSpec:
    outline = raw.get("outline") or {}
    board = BoardSpec(
        width=outline.get("w"),
        height=outline.get("h"),
        layers=int(raw.get("layers", 2)),
        default_clearance_mm=float(raw.get("default_clearance_mm", DEFAULT_CLEARANCE_MM)),
        references_on_fab=bool(raw.get("references_on_fab", False)),
        sides=_require_enum(raw.get("sides") or "single", SIDE_POLICIES, "board.sides"),
    )
    gap = _courtyard_clearance(raw.get("courtyard_clearance_mm"))
    if gap is not None:
        board.courtyard_clearance_mm = gap
    if board.sides == "double" and board.layers < 2:
        raise ConstraintError("board.sides: double needs at least 2 copper layers")
    return board


def _courtyard_clearance(value) -> Optional[float]:
    """``board.courtyard_clearance_mm``: None when absent, else a finite number >= 0."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConstraintError("board.courtyard_clearance_mm must be a finite number")
    if value < 0:
        raise ConstraintError("board.courtyard_clearance_mm must not be negative")
    return float(value)


_OPTIONAL_FAB = (
    "min_track_width_mm",
    "smd_pad_clearance_mm",
    "pth_hole_clearance_mm",
    "npth_hole_clearance_mm",
    "hole_to_hole_mm",
    "pth_hole_to_hole_mm",
    "filled_via_hole_to_hole_mm",
    "hole_to_edge_mm",
    "min_via_diameter_mm",
    "min_npth_drill_mm",
    "component_pth_min_drill_mm",
    "via_to_smd_pad_mm",
)


def _parse_fab(raw: Dict) -> FabProfile:
    """Parse the optional ``fab:`` block; any omitted field keeps its default."""
    d = FabProfile()
    fields = (
        "track_width_mm",
        "clearance_mm",
        "via_diameter_mm",
        "via_drill_mm",
        "hole_clearance_mm",
        "edge_clearance_mm",
        "min_through_drill_mm",
        "via_annular_mm",
    ) + _OPTIONAL_FAB
    for k in fields:
        if k in raw:
            setattr(d, k, float(raw[k]))
    return d


def _resolve_addresses(doc, addresses, pin_nets):
    """Resolve @source.path selectors, failing closed on stale/ambiguous paths.

    Lists may contain globs (e.g. @board.converter.*). Scalar values and mapping
    keys must resolve to one component. Never silently drop a source constraint.
    """

    def matches(value):
        result = [ref for path, ref in addresses.items() if fnmatch.fnmatchcase(path, value[1:])]
        if not result:
            raise ConstraintError(f"unknown component address {value!r}")
        return sorted(set(result))

    def visit(value):
        if isinstance(value, str) and value.startswith("net@"):
            net = pin_nets.get(value[4:])
            if not net:
                raise ConstraintError(f"unknown or unconnected net endpoint {value!r}")
            return net
        if isinstance(value, str) and value.startswith("@"):
            refs = matches(value)
            if len(refs) != 1:
                raise ConstraintError(f"ambiguous component address {value!r}: {refs}")
            return refs[0]
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, str) and item.startswith("@"):
                    result.extend(matches(item))
                else:
                    result.append(visit(item))
            return result
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                resolved = visit(key)
                if resolved in result:
                    raise ConstraintError(f"duplicate resolved component key {resolved!r}")
                result[resolved] = visit(item)
            return result
        return value

    return visit(doc)


def _expand_layout_arrays(doc):
    """Instantiate one local placement template at evenly spaced module origins."""
    import copy
    import math

    doc = copy.deepcopy(doc)
    arrays = doc.pop("layout_array", [])
    if not isinstance(arrays, list):
        raise ConstraintError("layout_array must be a list")
    fixed = doc.setdefault("fixed", {})
    if not isinstance(fixed, dict):
        raise ConstraintError("fixed must be a mapping")

    def xy(value, label):
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in value
            )
        ):
            raise ConstraintError(f"{label} requires two finite coordinates")
        return value

    for array in arrays:
        if not isinstance(array, dict):
            raise ConstraintError("layout_array entry must be a mapping")
        instances = array.get("instances")
        members = array.get("members")
        if (
            not isinstance(instances, list)
            or not instances
            or any(
                not isinstance(s, str) or not s or any(c in s for c in "@*?[]") for s in instances
            )
            or len(set(instances)) != len(instances)
        ):
            raise ConstraintError("layout_array requires unique, literal module paths")
        if not isinstance(members, dict) or not members:
            raise ConstraintError("layout_array requires a nonempty members template")
        origin = xy(array.get("origin"), "layout_array.origin")
        step = xy(array.get("step"), "layout_array.step")
        if len(instances) > 1 and all(v == 0 for v in step):
            raise ConstraintError("layout_array instances require nonzero spacing")
        for index, instance in enumerate(instances):
            for member, spec in members.items():
                if (
                    not isinstance(member, str)
                    or not member
                    or any(c in member for c in "@*?[]")
                    or not isinstance(spec, dict)
                ):
                    raise ConstraintError("layout_array members require literal paths and poses")
                local = xy(spec.get("at"), "layout_array member.at")
                selector = "@" + instance + "." + member
                if selector in fixed:
                    raise ConstraintError(f"layout_array duplicates fixed pose {selector}")
                pose = copy.deepcopy(spec)
                pose["at"] = [origin[k] + index * step[k] + local[k] for k in range(2)]
                fixed[selector] = pose
    return doc


LINE_GROUP_EDGES = ("none",) + EDGES
# Hard edge alignment: the largest courtyard-to-edge distance, default and floor (mm).
EDGE_TOLERANCE_MM = 1.0
MIN_EDGE_TOLERANCE_MM = 0.5


def _finite_number(value) -> bool:
    import math

    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _parse_line_groups(raw, known_refs, board, prior) -> List[Constraint]:
    """The ``line_group`` section: ordered members held in one rigid line.

    A line group is HARD: its members keep the layout of :mod:`pnr.place.line_group`
    under one common position and cardinal rotation. Members may carry no relation a
    rigid body cannot honour (a fixed pose, a row, edge alignment, orientation, a hard
    side, a ref-relative keepout, a hard group); a soft group pulls the whole line.
    ``prior`` holds the constraints parsed before this section.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConstraintError("line_group must be a list")
    known = set(known_refs)
    conflicting = {}
    for con in prior:
        if con.kind in ("fixed", "row", "edge_align", "orientation", "side"):
            refs = con.refs
        elif con.kind == "keepout" and con.params.get("extent") and not con.params.get("polygon"):
            refs = con.refs
        elif con.kind == "group" and con.enforcement is Enforcement.HARD:
            refs = tuple(con.refs) + (con.params.get("anchor"),)
        else:
            continue
        for ref in refs:
            if ref:
                conflicting.setdefault(ref, "hard group" if con.kind == "group" else con.kind)
    out: List[Constraint] = []
    owner: Dict[str, str] = {}
    clearance = float(board.default_clearance_mm)
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConstraintError("line_group entry must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConstraintError("line_group requires a non-empty name")
        where = "line_group %r" % name
        if any(c.name == name for c in out):
            raise ConstraintError(where + ": duplicate name")
        members = entry.get("members")
        if (
            not isinstance(members, list)
            or len(members) < 2
            or any(not isinstance(m, str) or any(ch in m for ch in "*?[") for m in members)
            or len(set(members)) != len(members)
            or any(m not in known for m in members)
        ):
            raise ConstraintError(where + ": requires at least two known, unique, literal refs")
        for ref in members:
            if ref in owner:
                raise ConstraintError(
                    "%s: %s is already a member of line_group %r" % (where, ref, owner[ref])
                )
            if ref in conflicting:
                raise ConstraintError(
                    "%s: member %s also has a %s constraint, which a rigid line cannot honour"
                    % (where, ref, conflicting[ref])
                )
        pitch, gap = entry.get("pitch_mm"), entry.get("gap_mm")
        if pitch is not None and gap is not None:
            raise ConstraintError(where + ": give pitch_mm or gap_mm, not both")
        if pitch is None and gap is None:
            gap = clearance
        for label, value in (("pitch_mm", pitch), ("gap_mm", gap)):
            if value is not None and (not _finite_number(value) or value <= 0):
                raise ConstraintError("%s: %s must be finite and positive" % (where, label))
        if gap is not None and gap < clearance - 1e-12:
            raise ConstraintError(where + ": gap_mm must meet the placement clearance")
        rot = entry.get("rot", 0)
        if not _finite_number(rot) or abs(rot / 90 - round(rot / 90)) > 1e-8:
            raise ConstraintError(where + ": rot must be a finite cardinal rotation")
        edge = entry.get("edge", "none")
        if edge not in LINE_GROUP_EDGES:
            raise ConstraintError("%s: edge %r not one of %s" % (where, edge, LINE_GROUP_EDGES))
        reason = entry.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ConstraintError(where + ": reason must be a string")
        for ref in members:
            owner[ref] = name
        out.append(
            Constraint(
                "line_group",
                Enforcement.HARD,
                tuple(members),
                dict(
                    pitch_mm=None if pitch is None else float(pitch),
                    gap_mm=None if gap is None else float(gap),
                    rot=float(round(rot / 90) * 90 % 360),
                    edge=edge,
                    reason=reason,
                ),
                name=name,
            )
        )
    return out


REGION_WEIGHT = 10.0  # soft region: penalty weight default
ALIGN_WEIGHT = 5.0  # soft align: penalty weight default
ALIGN_TOLERANCE_MM = 0.25
AXIS_EDGES = {"x": ("west", "east"), "y": ("south", "north")}
POINT_ANCHORS = ("origin", "centre", "pad1")


def _hard_flag(entry, where):
    hard = entry.get("hard", True)
    if not isinstance(hard, bool):
        raise ConstraintError(where + ": hard must be a boolean")
    return hard


def _weight(entry, default, where):
    weight = entry.get("weight", default)
    if not _finite_number(weight) or weight <= 0:
        raise ConstraintError(where + ": weight must be finite and positive")
    return float(weight)


def _entry_refs(entry, known_refs, warnings, where, minimum):
    refs = entry.get("refs")
    if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r for r in refs):
        raise ConstraintError(where + ": refs must be a nonempty list of refs or globs")
    resolved = _expand_refs(refs, known_refs, warnings, where)
    known = [r for r in resolved if r in known_refs]
    if len(known) < minimum:
        raise ConstraintError(
            "%s: needs at least %d known component%s" % (where, minimum, "s" * (minimum > 1))
        )
    return resolved


def _area(spec, where):
    """One area piece, ``{"rect": [x0, y0, x1, y1]}`` or ``{"polygon": [[x, y], ...]}``."""
    if not isinstance(spec, dict) or len([k for k in ("rect", "polygon") if k in spec]) != 1:
        raise ConstraintError(where + ": an area is exactly one of rect or polygon")
    if "rect" in spec:
        rect = spec["rect"]
        if (
            not isinstance(rect, (list, tuple))
            or len(rect) != 4
            or not all(_finite_number(v) for v in rect)
            or not (rect[0] < rect[2] and rect[1] < rect[3])
        ):
            raise ConstraintError(where + ": rect needs [x0, y0, x1, y1] with x0 < x1 and y0 < y1")
        return {"rect": [float(v) for v in rect]}
    poly = spec["polygon"]
    if (
        not isinstance(poly, (list, tuple))
        or len(poly) < 3
        or any(
            not isinstance(p, (list, tuple)) or len(p) != 2 or not all(_finite_number(v) for v in p)
            for p in poly
        )
    ):
        raise ConstraintError(where + ": polygon needs at least three finite [x, y] points")
    pts = [[float(x), float(y)] for x, y in poly]
    twice = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))  # shoelace
    if abs(twice) < 1e-9:
        raise ConstraintError(where + ": polygon has no area")
    crossing = _self_intersection(pts)
    if crossing is not None:
        raise ConstraintError(
            "%s: polygon is not simple (edges %d and %d meet)" % (where, crossing[0], crossing[1])
        )
    return {"polygon": pts}


def _self_intersection(pts):
    """The first pair of polygon edges (by index) that touch or cross other than at
    the vertex adjacent edges share, else None. A repeated point is a zero-length
    edge, which also counts."""
    n = len(pts)
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) <= 1e-12 else (1 if v > 0 else -1)

    def on(a, b, c):  # c on segment a-b, given collinear
        return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(
            a[1], b[1]
        )

    def meet(p, q):
        (a, b), (c, d) = p, q
        o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
        if o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4):
            return True
        return (
            (o1 == 0 and on(a, b, c))
            or (o2 == 0 and on(a, b, d))
            or (o3 == 0 and on(c, d, a))
            or (o4 == 0 and on(c, d, b))
        )

    for i, (a, b) in enumerate(edges):
        if a == b:
            return (i, i)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                # Adjacent edges share one vertex; they may not fold back onto each other.
                shared = edges[i][1] if j == i + 1 else edges[i][0]
                p = edges[i][0] if j == i + 1 else edges[i][1]
                q = edges[j][1] if j == i + 1 else edges[j][0]
                if orient(p, shared, q) == 0 and (
                    (p[0] - shared[0]) * (q[0] - shared[0])
                    + (p[1] - shared[1]) * (q[1] - shared[1])
                    > 0
                ):
                    return (i, j)
                continue
            if meet(edges[i], edges[j]):
                return (i, j)
    return None


def _parse_regions(raw, known_refs, warnings) -> List[Constraint]:
    """The ``region`` section: the courtyards of ``refs`` lie inside an area (the
    union of ``rect``/``polygon`` pieces, board coordinates). Hard by default; a soft
    region is a penalty on the protrusion. See :mod:`pnr.place.regions`."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConstraintError("region must be a list")
    out: List[Constraint] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConstraintError("region entry must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConstraintError("region requires a non-empty name")
        where = "region %r" % name
        if any(c.name == name for c in out):
            raise ConstraintError(where + ": duplicate name")
        refs = _entry_refs(entry, known_refs, warnings, where, 1)
        given = [k for k in ("rect", "polygon", "areas") if k in entry]
        if len(given) != 1:
            raise ConstraintError(where + ": give exactly one of rect, polygon or areas")
        if given[0] == "areas":
            pieces = entry["areas"]
            if not isinstance(pieces, list) or not pieces:
                raise ConstraintError(where + ": areas must be a nonempty list")
            areas = [_area(p, where) for p in pieces]
        else:
            areas = [_area({given[0]: entry[given[0]]}, where)]
        hard = _hard_flag(entry, where)
        reason = entry.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ConstraintError(where + ": reason must be a string")
        out.append(
            Constraint(
                "region",
                Enforcement.HARD if hard else Enforcement.SOFT,
                refs,
                dict(areas=areas, reason=reason),
                weight=_weight(entry, REGION_WEIGHT, where),
                name=name,
            )
        )
    return out


def _anchor(value, axis, where):
    """Validate one align anchor name for ``axis``; ``center`` reads as ``centre``."""
    if value == "center":
        value = "centre"
    if value in POINT_ANCHORS:
        return value
    if isinstance(value, str) and value.startswith("pad:") and len(value) > 4:
        return value
    if value in EDGES:
        if value not in AXIS_EDGES[axis]:
            raise ConstraintError(
                "%s: edge anchor %r does not measure axis %s (use %s)"
                % (where, value, axis, " or ".join(AXIS_EDGES[axis]))
            )
        return value
    raise ConstraintError(
        "%s: anchor %r not one of origin, centre, pad1, pad:<name>, %s"
        % (where, value, ", ".join(AXIS_EDGES[axis]))
    )


def _parse_aligns(raw, known_refs, warnings, prior) -> List[Constraint]:
    """The ``align`` section: the anchors of ``refs`` share one ``axis`` coordinate
    (``y``: one horizontal line). Hard by default (the largest anchor spread is at
    most ``tol_mm``, 0 for exact); a soft align is a penalty on the spread. ``prior``
    holds the constraints parsed before (the line groups)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConstraintError("align must be a list")
    line_of = {r: c.name for c in prior if c.kind == "line_group" for r in c.refs}
    out: List[Constraint] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConstraintError("align entry must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConstraintError("align requires a non-empty name")
        where = "align %r" % name
        if any(c.name == name for c in out):
            raise ConstraintError(where + ": duplicate name")
        refs = _entry_refs(entry, known_refs, warnings, where, 2)
        axis = entry.get("axis")
        if axis not in AXIS_EDGES:
            raise ConstraintError(where + ": axis must be x or y")
        spec = entry.get("anchor", "origin")
        if isinstance(spec, dict):
            unknown = sorted(set(spec) - set(refs))
            if unknown:
                raise ConstraintError(where + ": anchor names refs outside refs: %s" % unknown)
            anchors = {r: _anchor(spec.get(r, "origin"), axis, where) for r in refs}
        else:
            default = _anchor(spec, axis, where)
            anchors = {r: default for r in refs}
        tol = entry.get("tol_mm", ALIGN_TOLERANCE_MM)
        if not _finite_number(tol) or tol < 0:
            raise ConstraintError(where + ": tol_mm must be finite and not negative")
        lines = {}
        for ref in refs:
            if ref in line_of:
                lines.setdefault(line_of[ref], []).append(ref)
        for line, members in lines.items():
            if len(members) > 1:
                raise ConstraintError(
                    "%s: %s are members of one line_group %r, which already fixes their "
                    "relative position" % (where, " and ".join(members), line)
                )
        hard = _hard_flag(entry, where)
        reason = entry.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ConstraintError(where + ": reason must be a string")
        out.append(
            Constraint(
                "align",
                Enforcement.HARD if hard else Enforcement.SOFT,
                refs,
                dict(axis=axis, anchors=anchors, tol_mm=float(tol), reason=reason),
                weight=_weight(entry, ALIGN_WEIGHT, where),
                name=name,
            )
        )
    return out


def compile_constraints(
    doc: Dict, known_refs: Sequence[str], addresses=None, pin_nets=None
) -> CompiledConstraints:
    """Compile a parsed constraints document against a netlist's refs.

    ``doc`` is the already-parsed YAML mapping (see :func:`load_constraints` for
    the file entry point); ``known_refs`` is the component references from the
    ingested :class:`pnr.graph.BoardGraph`. Returns validated, glob-expanded
    :class:`Constraint` objects plus non-fatal warnings.
    """

    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ConstraintError("top level must be a mapping")

    doc = _resolve_addresses(_expand_layout_arrays(doc), addresses or {}, pin_nets or {})
    warnings: List[str] = []
    board = _parse_board(doc.get("board") or {})
    fab = _parse_fab(doc.get("fab") or {})
    constraints: List[Constraint] = []

    known_keys = {
        "schema",
        "board",
        "fab",
        "fixed",
        "orientation",
        "row",
        "edge_align",
        "keepout",
        "side_pref",
        "side",
        "group",
        "line_group",
        "region",
        "align",
        "net_class",
        "diff_pair",
        "length_match",
        "tuning",
        "legalize",
        "copper_keepout",
        "fixed_block",
    }
    for key in doc:
        if key not in known_keys:
            warnings.append(f"unknown top-level section {key!r} (ignored)")

    # fixed: HARD — locked pose, held out of the position gradient.
    for ref, spec in (doc.get("fixed") or {}).items():
        spec = spec or {}
        refs = _expand_refs([ref], known_refs, warnings, f"fixed.{ref}")
        _require_enum(spec.get("edge"), EDGES, f"fixed.{ref}.edge")
        _require_enum(spec.get("side"), SIDES, f"fixed.{ref}.side")
        constraints.append(
            Constraint(
                kind="fixed",
                enforcement=Enforcement.HARD,
                refs=refs,
                params={
                    "edge": spec.get("edge"),
                    "align": spec.get("align"),
                    "rot": spec.get("rot"),
                    "side": spec.get("side"),
                    "at": spec.get("at"),  # explicit (x, y) pose, optional
                    # protrude past the edge (mm) for a mating connector; -inset
                    "overhang_mm": spec.get("overhang_mm"),
                },
            )
        )

    # Orientation is independent of XY; sensor axes do not require fixed centres.
    import math

    for selector, spec in (doc.get("orientation") or {}).items():
        angle = spec.get("rot") if isinstance(spec, dict) else spec
        if (
            isinstance(angle, bool)
            or not isinstance(angle, (int, float))
            or not math.isfinite(angle)
            or abs(angle / 90 - round(angle / 90)) > 1e-8
        ):
            raise ConstraintError("orientation requires a finite cardinal rotation")
        refs = _expand_refs([selector], known_refs, warnings, "orientation")
        for con in constraints:
            if (
                con.kind == "fixed"
                and set(refs).intersection(con.refs)
                and (con.params.get("rot") or 0) % 360 != angle % 360
            ):
                raise ConstraintError("orientation conflicts with fixed rotation")
        constraints.append(
            Constraint(
                "orientation",
                Enforcement.HARD,
                refs,
                {
                    "rot": angle % 360,
                    "reason": spec.get("reason") if isinstance(spec, dict) else None,
                },
            )
        )

    # edge_align: SOFT — pull the part to a board edge during global placement (the
    # facing is set with ``orientation``). Opt-in ``hard: true`` also keeps the part's
    # courtyard within ``tolerance_mm`` of that edge through legalization.
    for ref, spec in (doc.get("edge_align") or {}).items():
        spec = spec or {}
        refs = _expand_refs([ref], known_refs, warnings, f"edge_align.{ref}")
        edge = _require_enum(spec.get("edge"), EDGES, f"edge_align.{ref}.edge")
        if edge is None:
            raise ConstraintError(f"edge_align.{ref}: 'edge' is required")
        _require_enum(spec.get("side"), SIDES, f"edge_align.{ref}.side")
        hard = spec.get("hard", False)
        if not isinstance(hard, bool):
            raise ConstraintError(f"edge_align.{ref}.hard must be a boolean")
        tolerance = spec.get("tolerance_mm", EDGE_TOLERANCE_MM)
        if not _finite_number(tolerance) or tolerance < MIN_EDGE_TOLERANCE_MM:
            raise ConstraintError(
                f"edge_align.{ref}.tolerance_mm must be finite and at least "
                f"{MIN_EDGE_TOLERANCE_MM} mm"
            )
        params = {"edge": edge, "side": spec.get("side")}
        if hard:
            params.update(hard=True, tolerance_mm=float(tolerance))
        constraints.append(
            Constraint(
                kind="edge_align",
                enforcement=Enforcement.HARD if hard else Enforcement.SOFT,
                refs=refs,
                params=params,
                weight=float(spec.get("weight", DEFAULT_WEIGHTS["edge_align"])),
            )
        )

    # keepout: HARD — no parts/copper in a region (poly or relative-to-component).
    for entry in doc.get("keepout") or []:
        entry = entry or {}
        name = entry.get("name")
        ref = entry.get("ref")
        refs = _expand_refs([ref], known_refs, warnings, f"keepout.{name or ref}") if ref else ()
        if "extent" not in entry and "polygon" not in entry:
            raise ConstraintError(
                f"keepout {name!r}: needs an 'extent' (rel-to-ref) or a 'polygon'"
            )
        constraints.append(
            Constraint(
                kind="keepout",
                enforcement=Enforcement.HARD,
                refs=refs,
                name=name,
                params={
                    "extent": entry.get("extent"),
                    "polygon": entry.get("polygon"),
                },
            )
        )

    # side: HARD — constrain the copper side while leaving XY/rotation movable.
    # This is deliberately separate from fixed, whose absent XY resolves to the
    # board centre, and from side_pref, which is only a soft preference.
    for side, patterns in (doc.get("side") or {}).items():
        if side not in SIDES:
            raise ConstraintError(f"side: {side!r} not one of {SIDES}")
        refs = _expand_refs(patterns or [], known_refs, warnings, f"side.{side}")
        for ref in refs:
            prior = [
                c.params["side"]
                for c in constraints
                if ref in c.refs and c.kind in ("fixed", "side") and c.params.get("side")
            ]
            if any(value != side for value in prior):
                raise ConstraintError(f"conflicting hard side rules for {ref}")
        constraints.append(
            Constraint(kind="side", enforcement=Enforcement.HARD, refs=refs, params={"side": side})
        )

    # side_pref: SOFT — bias a set of parts to a side. Only a double-sided board
    # (board.sides: double) lets placement choose sides; elsewhere it has no effect.
    if doc.get("side_pref") and board.sides != "double":
        warnings.append("side_pref has no effect unless board.sides is double (ignored)")
    for side, patterns in (doc.get("side_pref") or {}).items():
        _require_enum(side, SIDES, "side_pref key")
        refs = _expand_refs(patterns or [], known_refs, warnings, f"side_pref.{side}")
        constraints.append(
            Constraint(
                kind="side_pref",
                enforcement=Enforcement.SOFT,
                refs=refs,
                params={"side": side},
                weight=DEFAULT_WEIGHTS["side_pref"],
            )
        )

    # Relative rows have no global origin. Each global start samples a joint
    # legal edge configuration; the final source guard validates the relation.
    row_members = set()
    for entry in doc.get("row") or []:
        members = tuple(entry.get("members") or ())
        if (
            not members
            or len(set(members)) != len(members)
            or any(r not in known_refs for r in members)
        ):
            raise ConstraintError("row requires known unique ordered members")
        if row_members.intersection(members):
            raise ConstraintError("component occurs in multiple rows")
        if any(c.kind == "fixed" and set(members).intersection(c.refs) for c in constraints):
            raise ConstraintError("row cannot silently override an authored absolute pose")
        row_members.update(members)
        if entry.get("edge") != "any":
            raise ConstraintError("row currently requires edge: any")
        facing = entry.get("facing")
        if facing not in (None, "north", "south"):
            raise ConstraintError("row facing must be a local north/south normal")
        gap = entry.get("gap_mm", float(board.default_clearance_mm))
        if (
            isinstance(gap, bool)
            or not isinstance(gap, (int, float))
            or not math.isfinite(gap)
            or gap < float(board.default_clearance_mm)
        ):
            raise ConstraintError("row gap must meet placement clearance")
        constraints.append(
            Constraint(
                "row",
                Enforcement.HARD,
                members,
                dict(
                    edge="any",
                    facing=facing,
                    gap_mm=gap,
                    reason=entry.get("reason", "User-authored relative row"),
                ),
                name=entry.get("name"),
            )
        )

    # Groups are soft by default; hard groups survive legalization.
    for entry in doc.get("group") or []:
        entry = entry or {}
        members = entry.get("members") or []
        warnings_before_group = len(warnings)
        refs = _expand_refs(members, known_refs, warnings, "group.members")
        anchor = entry.get("anchor")
        hard = entry.get("hard", False)
        if not isinstance(hard, bool):
            raise ConstraintError("group.hard must be a boolean")
        if hard:
            if not refs or len(warnings) != warnings_before_group:
                raise ConstraintError("hard group requires known, nonempty members")
            radius = entry.get("radius_mm")
            if (
                isinstance(radius, bool)
                or not isinstance(radius, (int, float))
                or not 0 < radius < float("inf")
            ):
                raise ConstraintError("hard group requires a finite positive radius_mm")
            if anchor not in known_refs:
                raise ConstraintError("hard group requires a known anchor")
        if anchor is not None and anchor not in known_refs:
            warnings.append(f"group.anchor: unknown component ref {anchor!r}")
        params = {"anchor": anchor, "radius_mm": entry.get("radius_mm")}
        if entry.get("anchor_pad") is not None:
            # A hard group measured from one pad of its anchor (pnr.place.legal_options).
            params["anchor_pad"] = _anchor_pad(entry["anchor_pad"], hard)
        constraints.append(
            Constraint(
                kind="group",
                enforcement=Enforcement.HARD if hard else Enforcement.SOFT,
                refs=refs,
                params=params,
                weight=float(entry.get("weight", DEFAULT_WEIGHTS["group"])),
            )
        )

    # line_group: HARD — ordered members held in one rigid line (pnr.place.line_group).
    constraints.extend(_parse_line_groups(doc.get("line_group"), known_refs, board, constraints))

    # region / align: HARD by default — allowed placement areas and shared
    # coordinates (pnr.place.regions); a soft one is a weighted penalty.
    constraints.extend(_parse_regions(doc.get("region"), known_refs, warnings))
    constraints.extend(_parse_aligns(doc.get("align"), known_refs, warnings, constraints))

    # net_class: routing rule sets over net-name globs (resolved at route time).
    net_classes: List[NetClass] = []
    for name, spec in (doc.get("net_class") or {}).items():
        spec = spec or {}
        nets = spec.get("nets") or []
        net_classes.append(
            NetClass(
                name=str(name),
                width_mm=_opt_float(spec.get("width_mm")),
                clearance_mm=_opt_float(spec.get("clearance_mm")),
                nets=tuple(str(n) for n in nets),
                plane_layer=spec.get("plane_layer"),
                current_a=_opt_float(spec.get("current_a")),
                copper_oz=float(spec.get("copper_oz", 1.0)),
                delta_t_c=float(spec.get("delta_t_c", 10.0)),
                external=bool(spec.get("external", True)),
            )
        )

    # diff_pair: two nets routed together + skew-checked.
    diff_pairs: List[DiffPair] = []
    for entry in doc.get("diff_pair") or []:
        entry = entry or {}
        if not entry.get("p") or not entry.get("n"):
            raise ConstraintError(f"diff_pair {entry.get('name')!r}: needs 'p' and 'n' nets")
        if entry.get("skew_mm") is not None and entry.get("skew_ps") is not None:
            raise ConstraintError(
                f"diff_pair {entry.get('name')!r}: give skew_mm or skew_ps, not both"
            )
        dp = DiffPair(
            name=str(entry.get("name") or f"{entry['p']}/{entry['n']}"),
            p=str(entry["p"]),
            n=str(entry["n"]),
            width_mm=_opt_float(entry.get("width_mm")),
            gap_mm=_opt_float(entry.get("gap_mm")),
            skew_mm=float(entry.get("skew_mm", 0.5)),
            skew_ps=_positive(entry.get("skew_ps"), f"diff_pair {entry.get('name')!r}.skew_ps"),
        )
        if os.environ.get("PNR_BUS_CLASSES") == "1":
            dp._defaulted = tuple(k for k in ("skew_mm",) if k not in entry)
        diff_pairs.append(dp)

    # length_match: groups whose routed lengths must agree within a tolerance.
    length_matches: List[LengthMatch] = []
    for entry in doc.get("length_match") or []:
        entry = entry or {}
        nets = entry.get("nets") or []
        if len(nets) < 2:
            raise ConstraintError(f"length_match {entry.get('name')!r}: needs >= 2 nets")
        if entry.get("tolerance_mm") is not None and entry.get("tolerance_ps") is not None:
            raise ConstraintError(
                f"length_match {entry.get('name')!r}: give tolerance_mm or tolerance_ps, not both"
            )
        length_matches.append(
            LengthMatch(
                name=str(entry.get("name") or "group"),
                nets=tuple(str(n) for n in nets),
                tolerance_mm=float(entry.get("tolerance_mm", 1.0)),
                tolerance_ps=_positive(
                    entry.get("tolerance_ps"), f"length_match {entry.get('name')!r}.tolerance_ps"
                ),
            )
        )
    tuning = _parse_tuning(doc.get("tuning"))
    legalize = _parse_legalize(doc.get("legalize"))

    # Mechanical fastener envelopes reserve both faces and every copper layer.
    import math

    mounting_holes = []
    hole_names = set()
    for entry in doc.get("mounting_hole") or []:
        name = entry.get("name")
        at = entry.get("at", [])
        drill = entry.get("drill_mm")
        diameter = entry.get("clearance_diameter_mm")
        values = list(at) + [drill, diameter] if isinstance(at, (list, tuple)) else []
        if (
            not isinstance(name, str)
            or not name
            or name in hole_names
            or len(values) != 4
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in values
            )
            or not 0 < drill < diameter
        ):
            raise ConstraintError(
                "mounting_hole requires unique name, finite XY and 0 < drill < clearance diameter"
            )
        radius = diameter / 2
        if board.width is None or board.height is None:
            raise ConstraintError("mounting_hole requires explicit board dimensions")
        if (
            at[0] - radius < 0
            or at[1] - radius < 0
            or at[0] + radius > board.width
            or at[1] + radius > board.height
        ):
            raise ConstraintError("mounting_hole clearance envelope must fit inside board")
        hole_names.add(name)
        mounting_holes.append(
            {"name": name, "at": list(at), "drill_mm": drill, "clearance_diameter_mm": diameter}
        )
        x, y = at
        constraints.append(
            Constraint(
                kind="keepout",
                enforcement=Enforcement.HARD,
                refs=(),
                name="mount:" + name,
                params={
                    "polygon": [
                        [x - radius, y - radius],
                        [x + radius, y - radius],
                        [x + radius, y + radius],
                        [x - radius, y + radius],
                    ]
                },
            )
        )

    fixed_blocks = _parse_fixed_blocks(doc.get("fixed_block"), constraints, known_refs, warnings)
    copper_keepouts = []
    v1_names = set()
    for entry in doc.get("copper_keepout") or []:
        if not isinstance(entry, dict):
            raise ConstraintError("copper_keepout: each entry is a mapping")
        if keepout_is_v1(entry):
            spec = _parse_keepout_v1(
                entry, board, net_classes, diff_pairs, fixed_blocks, known_refs
            )
            if spec["name"] in v1_names:
                raise ConstraintError(f"copper_keepout: duplicate name {spec['name']!r}")
            v1_names.add(spec["name"])
            copper_keepouts.append(spec)
            continue
        ref = entry.get("ref")
        rect = entry.get("rect_mm", [])
        if ref not in known_refs:
            raise ConstraintError(f"copper_keepout: unknown ref {ref!r}")
        if len(rect) != 4 or not all(isinstance(x, (int, float)) for x in rect):
            raise ConstraintError("copper_keepout: rect_mm needs four numbers")
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise ConstraintError("copper_keepout: rectangle must have positive area")
        copper_keepouts.append(
            {"name": str(entry.get("name") or ref), "ref": ref, "rect_mm": list(rect)}
        )
    fallback = (doc.get("board") or {}).get("plane_fallback_drops")
    if fallback is not None and not isinstance(fallback, bool):
        raise ConstraintError("board.plane_fallback_drops must be true or false")

    return CompiledConstraints(
        board=board,
        constraints=constraints,
        warnings=warnings,
        schema=str(doc.get("schema", SCHEMA_VERSION)),
        fab=fab,
        net_classes=net_classes,
        diff_pairs=diff_pairs,
        length_matches=length_matches,
        tuning=tuning,
        legalize=legalize,
        copper_keepouts=copper_keepouts,
        mounting_holes=mounting_holes,
        fixed_blocks=fixed_blocks,
        plane_fallback_drops=fallback,
    )


def copper_layer_names(count: int) -> List[str]:
    """KiCad's copper layer names of a ``count``-layer board, top to bottom."""
    count = max(2, int(count))
    return ["F.Cu"] + ["In%d.Cu" % i for i in range(1, count - 1)] + ["B.Cu"]


def _finite_points(raw, where: str, minimum: int) -> List[List[float]]:
    if not isinstance(raw, (list, tuple)) or len(raw) < minimum:
        raise ConstraintError(f"{where} needs at least {minimum} points")
    out = []
    for pt in raw:
        if (
            not isinstance(pt, (list, tuple))
            or len(pt) != 2
            or not all(_finite_number(v) for v in pt)
        ):
            raise ConstraintError(f"{where}: each point is two finite numbers")
        out.append([float(pt[0]), float(pt[1])])
    return out


def _string_list(raw, where: str) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ConstraintError(f"{where} must be a list")
    if not all(isinstance(v, str) and v for v in raw):
        raise ConstraintError(f"{where}: every entry is a non-empty string")
    return list(raw)


def _parse_keepout_v1(entry, board, net_classes, diff_pairs, fixed_blocks, known_refs) -> Dict:
    """One ``copper_keepout`` entry in the v1 form (pnr.route.detail.router,
    pnr.writeback.apply_copper_keepouts)."""
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ConstraintError("copper_keepout: a v1 entry needs a name")
    where = f"copper_keepout {name!r}"
    unknown = sorted(set(entry) - set(KEEPOUT_V1_KEYS) - {"name", "ref", "rect_mm", "reason"})
    if unknown:
        raise ConstraintError(f"{where}: unknown key(s) {', '.join(unknown)}")
    shapes = [k for k in ("polygon", "rect", "rect_mm") if k in entry]
    if len(shapes) != 1:
        raise ConstraintError(f"{where}: give exactly one of polygon, rect, or ref + rect_mm")
    out: Dict = {"name": name}
    if "polygon" in entry:
        out["polygon"] = _finite_points(entry["polygon"], f"{where}.polygon", 3)
    else:
        key = shapes[0]
        rect = entry[key]
        if (
            not isinstance(rect, (list, tuple))
            or len(rect) != 4
            or not all(_finite_number(v) for v in rect)
        ):
            raise ConstraintError(f"{where}.{key} needs four numbers")
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise ConstraintError(f"{where}: rectangle must have positive area")
        if key == "rect_mm":
            if entry.get("ref") not in known_refs:
                raise ConstraintError(f"{where}: unknown ref {entry.get('ref')!r}")
            out["ref"] = entry["ref"]
            out["rect_mm"] = [float(v) for v in rect]
        else:
            x0, y0, x1, y1 = (float(v) for v in rect)
            out["polygon"] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    if "ref" in entry and "rect_mm" not in entry:
        raise ConstraintError(f"{where}: ref goes with rect_mm (the part's frame)")
    names = copper_layer_names(board.layers)
    layers = _string_list(entry.get("layers"), f"{where}.layers") or list(names)
    bad = [la for la in layers if la not in names]
    if bad:
        raise ConstraintError(f"{where}: not copper layers of this board: {', '.join(bad)}")
    out["layers"] = [la for la in names if la in set(layers)]
    items = _string_list(entry.get("items"), f"{where}.items") or list(KEEPOUT_ITEMS)
    bad = [i for i in items if i not in KEEPOUT_ITEMS]
    if bad:
        raise ConstraintError(f"{where}.items: {bad} not in {KEEPOUT_ITEMS}")
    out["items"] = [i for i in KEEPOUT_ITEMS if i in set(items)]
    classes = _string_list(entry.get("allow_classes"), f"{where}.allow_classes")
    declared = {nc.name for nc in net_classes} | {"dp_" + dp.name for dp in diff_pairs}
    bad = [c for c in classes if c not in declared]
    if bad:
        raise ConstraintError(f"{where}.allow_classes: undeclared net class(es) {bad}")
    groups = _string_list(entry.get("exempt_groups"), f"{where}.exempt_groups")
    known_groups = {b["group"] for b in fixed_blocks}
    bad = [g for g in groups if g not in known_groups]
    if bad:
        raise ConstraintError(f"{where}.exempt_groups: not fixed_block groups {bad}")
    out["allow_classes"] = classes
    out["allow_nets"] = _string_list(entry.get("allow_nets"), f"{where}.allow_nets")
    out["exempt_groups"] = groups
    return out


def _parse_fixed_blocks(raw, constraints, known_refs, warnings) -> List[Dict]:
    """The ``fixed_block`` list (pnr.fixed_block): name, KiCad group, optional fixed
    anchor (the frame of the copper digest), digest, solid layers and footprints."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConstraintError("fixed_block must be a list")
    fixed = {r for c in constraints if c.kind == "fixed" for r in c.refs}
    out, names, groups = [], set(), set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConstraintError("fixed_block: each entry is a mapping")
        name, group = entry.get("name"), entry.get("group")
        if not isinstance(name, str) or not name or name in names:
            raise ConstraintError("fixed_block: each block needs a unique name")
        where = f"fixed_block {name!r}"
        if not isinstance(group, str) or not group or group in groups:
            raise ConstraintError(f"{where}: needs a KiCad group name of its own")
        unknown = sorted(
            set(entry) - {"name", "group", "anchor", "sha256", "solid_layers", "refs", "reason"}
        )
        if unknown:
            raise ConstraintError(f"{where}: unknown key(s) {', '.join(unknown)}")
        spec: Dict = {"name": name, "group": group}
        anchor = entry.get("anchor")
        if anchor is not None:
            if anchor not in known_refs:
                raise ConstraintError(f"{where}: unknown anchor {anchor!r}")
            if anchor not in fixed:
                raise ConstraintError(f"{where}: anchor {anchor!r} must have a fixed pose")
            spec["anchor"] = anchor
        digest = entry.get("sha256")
        if digest is not None:
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise ConstraintError(f"{where}.sha256 must be 64 lowercase hex digits")
            spec["sha256"] = digest
        spec["solid_layers"] = _string_list(entry.get("solid_layers"), f"{where}.solid_layers")
        refs = _expand_refs(
            _string_list(entry.get("refs"), f"{where}.refs"), known_refs, warnings, where
        )
        if set(refs) & fixed:
            raise ConstraintError(f"{where}: a block footprint is held out, it cannot be fixed")
        spec["refs"] = list(refs)
        names.add(name)
        groups.add(group)
        out.append(spec)
    return out


def load_constraints(path: str, known_refs: Sequence[str]) -> CompiledConstraints:
    """Load and compile a ``constraints.yaml`` file (see :func:`compile_constraints`)."""

    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return compile_constraints(doc, known_refs)
