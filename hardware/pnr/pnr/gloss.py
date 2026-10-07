"""PNR_GLOSS: transactional gloss, dekink and corridor-coalescing pass.

Design: docs/design/gloss.md.

Three layers, one module:

* settings (``enabled``, ``settings``): environment only, no KiCad import; nothing
  here is read unless ``PNR_GLOSS=1``.
* KiCad adapter (``Model`` and the ``--worker`` modes ``inventory``, ``trial``,
  ``facts``, ``metrics``): board -> pure geometry of :mod:`pnr.gloss_geometry`
  with the router's own predicates (native shape checker ``Oracle`` for L1, exact
  KiCad shapes for contact L2, obstacle test points for homotopy L3, pair/open
  terminal guards L7), and transactional board edits that re-check every
  proposal on the applied board. pcbnew is imported inside functions only.
* controller (``GlossPass``, any python; workers run one at a time under the
  KiCad python): inventory -> batches -> trial/refill/facts worker -> cold KiCad
  DRC -> accept, split or blacklist; re-inventory; phase-end gate with bisection
  and replay; whole-pass revert. The native checks are the authority: the
  planner only proposes.
"""

import argparse
import dataclasses
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

STEPS = ("normalize", "dekink", "gloss", "corridor")
# PNR_GLOSS_CLASSES_FROM: rule sources functional groups may be derived from (fixed order)
CLASS_SOURCES = ("netclasses", "pairs", "si", "length_match")
LABELS = ("06g-gloss", "07g-gloss")
NM = 1_000_000
DS_TOLERANCE_MM2 = 1e-3  # polygon approximation noise allowed in a window dead-space comparison
UNION_TOLERANCE_MM2 = 2e-4  # normalize: copper union XOR allowed (1 um arc approximation)
POLY_ERROR = 1000  # 1 um polygon approximation error (design 2.4)
INVENTORY_SAFETY_SECONDS = (
    120.0  # wall-clock safety net of one inventory (normal planning is bounded by
)
# deterministic per-chain budgets; a hit is reported, never silent)


# ---------------------------------------------------------------- settings (design 5.3)
def enabled(env=None):
    return (os.environ if env is None else env).get("PNR_GLOSS") == "1"


@dataclass(frozen=True)
class Settings:
    steps: tuple
    passes: tuple
    si: bool
    cycle: bool
    seconds: float
    max_transactions: int
    batch: int
    hug: bool = True  # same-class adjacency tie-breaks in dekink/gloss (PNR_GLOSS_HUG=0: off)
    sweeps: int = 2  # 2: a final gloss + corridor sweep after the step list (PNR_GLOSS_SWEEPS)
    classes: str = None  # PNR_GLOSS_CLASSES: functional groups JSON {tag: [nets] | {nets: [...]}}
    cross_group_mm: float = 10.0  # PNR_GLOSS_CROSS_GROUP_MM: cross-group parallel-run cap (mm)
    classes_from: tuple = ()  # PNR_GLOSS_CLASSES_FROM: groups derived from rules (CLASS_SOURCES)


def settings(env=None):
    """Sub-flags (read only by callers that checked :func:`enabled`)."""
    env = os.environ if env is None else env

    def items(name, allowed):
        raw = env.get(name)
        if raw is None:
            return tuple(allowed)
        wanted = [v.strip() for v in raw.split(",") if v.strip()]
        bad = [v for v in wanted if v not in allowed]
        if bad:
            raise ValueError(
                "%s: unknown %s (expected %s)" % (name, ",".join(bad), ",".join(allowed))
            )
        return tuple(v for v in allowed if v in wanted)  # the order is fixed

    def number(name, default, kind):
        try:
            value = kind(env.get(name, default))
        except ValueError:
            raise ValueError(
                "%s must be a %s number, got %r" % (name, kind.__name__, env.get(name))
            )
        if not value > 0:
            raise ValueError("%s must be positive" % name)
        return value

    def boolean(name, default):
        raw = env.get(name)
        if raw is None:
            return default
        if raw not in ("0", "1"):
            raise ValueError("%s must be 0 or 1, got %r" % (name, raw))
        return raw == "1"

    if boolean("PNR_GLOSS_CYCLE", False):
        raise ValueError(
            "PNR_GLOSS_CYCLE=1: the in-cycle gloss pass and un-glossing (design v2) are not "
            "implemented; unset it"
        )
    classes = env.get("PNR_GLOSS_CLASSES") or None
    if classes is not None and not Path(classes).is_file():
        raise ValueError("PNR_GLOSS_CLASSES: no such file %r" % classes)
    if classes is not None:
        try:
            load_class_tags(classes)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("PNR_GLOSS_CLASSES: unreadable groups file %r (%s)" % (classes, error))
    classes_from = (
        items("PNR_GLOSS_CLASSES_FROM", CLASS_SOURCES) if env.get("PNR_GLOSS_CLASSES_FROM") else ()
    )
    raw = env.get("PNR_GLOSS_CROSS_GROUP_MM")
    if raw is not None and classes is None and not classes_from:
        raise ValueError(
            "PNR_GLOSS_CROSS_GROUP_MM needs PNR_GLOSS_CLASSES (the functional groups file) or "
            "PNR_GLOSS_CLASSES_FROM"
        )
    try:
        cross_group_mm = float(raw) if raw is not None else 10.0
    except ValueError:
        raise ValueError("PNR_GLOSS_CROSS_GROUP_MM must be a number (mm), got %r" % raw)
    if not 0 <= cross_group_mm < math.inf:
        raise ValueError("PNR_GLOSS_CROSS_GROUP_MM must be a finite length >= 0 mm")
    sweeps = number("PNR_GLOSS_SWEEPS", 2, int)
    if sweeps > 2:
        raise ValueError("PNR_GLOSS_SWEEPS must be 1 or 2")
    return Settings(
        items("PNR_GLOSS_STEPS", STEPS),
        items("PNR_GLOSS_PASSES", LABELS),
        boolean("PNR_GLOSS_SI", False),
        False,
        number("PNR_GLOSS_SECONDS", 240, float),
        number("PNR_GLOSS_MAX_TRANSACTIONS", 48, int),
        number("PNR_GLOSS_BATCH", 16, int),
        boolean("PNR_GLOSS_HUG", True),
        sweeps,
        str(Path(classes).resolve()) if classes else None,
        cross_group_mm,
        classes_from,
    )


def settings_key(conf):
    """A short digest of the settings a pass runs under: every sub-flag's effective value, the
    groups file by its content (sha256), never by its path. The router key's ``gloss`` field
    (pnr.feedback.signals) and native_loop's progress.json carry it, so evaluations of two gloss
    configurations (or of gloss on and off) never mix in one feedback library."""
    fields = {f.name: getattr(conf, f.name) for f in dataclasses.fields(conf)}
    if fields.get("classes"):
        fields["classes"] = "sha256:" + sha256(fields["classes"])
    text = json.dumps(fields, sort_keys=True, default=list)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- small helpers
def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str) + "\n")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def uid(item):
    return item.m_Uuid.AsString()


def pt(v):
    return (int(v.x), int(v.y))


def project_netclass(board_path):
    """Effective netclass name from the project's net settings, as tools/keyhole_region.py
    resolves it (pattern match, lowest priority wins); an explicit non-Default assignment
    also counts (conservative)."""
    import fnmatch

    try:
        ns = json.loads(Path(board_path).with_suffix(".kicad_pro").read_text())["net_settings"]
    except (OSError, ValueError, KeyError, TypeError):
        return lambda net: "Default"
    classes = {c.get("name"): c for c in ns.get("classes") or []}
    patterns = ns.get("netclass_patterns") or []
    assigned = ns.get("netclass_assignments") or {}

    def name(net):
        explicit = assigned.get(net) if isinstance(assigned, dict) else None
        if explicit:
            names = explicit if isinstance(explicit, list) else [explicit]
            if any(n != "Default" for n in names):
                return ",".join(sorted(str(n) for n in names))
        matches = [
            classes.get(p.get("netclass"), {"name": p.get("netclass")})
            for p in patterns
            if fnmatch.fnmatchcase(net, p.get("pattern", ""))
        ]
        return min(matches, key=lambda c: c.get("priority", 999))["name"] if matches else "Default"

    return name


def si_intents(board, rules, sources):
    """The @pnr-si intents: the rules' si_intents, else resolved from the annotation sources
    (parse and resolve, no simulation); (intents, status). Fails closed (intents None)."""
    intents = rules.get("si_intents")
    if intents is None:
        if not any("@pnr-si" in Path(s).read_text(errors="replace") for s in sources):
            return [], "none"
        try:
            from pnr.ingest import build_graph
            from pnr.si.report import resolve_intents

            intents = resolve_intents([str(s) for s in sources], build_graph(board).components)
        except Exception as error:  # fail closed: the pass stops
            return None, "si_unresolved: %r" % (error,)
        return intents, "resolved"
    return intents, "rules"


def si_nets(board, rules, sources):
    """E3: nets of every @pnr-si intent; (nets, status). Fails closed (nets None)."""
    intents, status = si_intents(board, rules, sources)
    if intents is None:
        return None, status
    return {n for i in intents for n in i.get("nets", [])}, status


class Grid:
    """1 mm buckets of (box -> payload)."""

    def __init__(self, size=NM):
        self.size = size
        self.cells = defaultdict(list)

    def add(self, box, payload):
        s = self.size
        for x in range(int(box[0]) // s, int(box[2]) // s + 1):
            for y in range(int(box[1]) // s, int(box[3]) // s + 1):
                self.cells[x, y].append(payload)

    def query(self, box):
        s = self.size
        out, seen = [], set()
        for x in range(int(box[0]) // s, int(box[2]) // s + 1):
            for y in range(int(box[1]) // s, int(box[3]) // s + 1):
                for item in self.cells.get((x, y), ()):
                    if id(item) not in seen:
                        seen.add(id(item))
                        out.append(item)
        return out


def item_box(item):
    box = item.GetBoundingBox()
    return (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom())


def boxes_overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def drc_open_nets(drc, lookup):
    """Nets of the DRC report's unconnected items (KiCad names arbitrary items of an open net
    run to run, but the per-net open count - hence this set - is a function of the board).
    lookup: uuid -> board item; the item description's [net] is the fallback."""
    import re

    nets = set()
    for finding in (drc or {}).get("unconnected_items", []):
        found = None
        for entry in finding.get("items", []):
            item = lookup.get(entry.get("uuid"))
            if item is not None and item.GetNetCode():
                found = item.GetNetname()
                break
        if found is None:
            for entry in finding.get("items", []):
                names = re.findall(r"\[([^\]]+)\]", entry.get("description", ""))
                if names:
                    found = names[0]
                    break
        if found:
            nets.add(found)
    return nets


def freeze_sets(model, drc):
    """A7/F3 and G2 as a deterministic function of the pre-pass board and its DRC report
    (the controller passes B_0's report to every worker of a pass):

    frozen: every track named by a DRC violation (violation keys must survive edits) and every
            track of every net with an unconnected item (the whole open net, not the one item
            KiCad happens to name);
    open_pads: every pad of every such net (G2 guards their escape, not only the named pads).
    """
    lookup = dict(model.by_uid)
    lookup.update(model.pad_by_uid)
    frozen = set()
    for finding in (drc or {}).get("violations", []):
        for entry in finding.get("items", []):
            if entry.get("uuid") in model.by_uid:
                frozen.add(entry["uuid"])
    open_nets = drc_open_nets(drc, lookup)
    frozen |= {uid(t) for t in model.items if t.GetNetCode() and t.GetNetname() in open_nets}
    open_pads = {uid(p) for p in model.pads if p.GetNetCode() and p.GetNetname() in open_nets}
    return frozen, open_pads, open_nets


def load_class_tags(path):
    """PNR_GLOSS_CLASSES functional groups file -> {net: group tag}; None when no file.

    {"classes": {tag: [nets] | {"nets": [nets], ...provenance...}}} (or the classes mapping at
    the top level). A net in no group is a group of its own. With a file, corridor packing and
    gloss hugging at minimum pitch are unlimited within a group and capped between groups
    (PNR_GLOSS_CROSS_GROUP_MM, gloss_geometry.allowed_parallel_mm)."""
    if not path:
        return None
    data = json.loads(Path(path).read_text())
    classes = data.get("classes", data)
    out = {}
    for tag, nets in sorted(classes.items()):
        if isinstance(nets, dict):
            nets = nets["nets"]
        if isinstance(nets, str) or not all(isinstance(n, str) for n in nets):
            raise ValueError("PNR_GLOSS_CLASSES: group %r must list net names" % tag)
        for net in nets:
            if net in out and out[net] != tag:
                raise ValueError(
                    "PNR_GLOSS_CLASSES: net %r in classes %r and %r" % (net, out[net], tag)
                )
            out[net] = tag
    return out


def derive_class_tags(rules, kinds, intents=()):
    """PNR_GLOSS_CLASSES_FROM: functional groups from the rules -> {net: group tag}.

    kinds (CLASS_SOURCES order): ``netclasses`` (each rules net class, tag ``netclass:NAME``),
    ``pairs`` (each differential pair, ``pair:NAME``), ``si`` (the nets of each @pnr-si intent,
    ``si:NAME``), ``length_match`` (each length-match group, ``length_match:NAME``). Derived
    groups with the same member set are one group (the first source's tag, e.g. a pair that is
    also its own net class). A net derived into groups with different members joins the most
    specific one: the fewest members, then the first in source order (a differential pair inside
    a wider net class is a group of its own; the class keeps its other nets)."""
    groups = []  # (tag, frozenset of nets), in source order
    for kind in [k for k in CLASS_SOURCES if k in kinds]:
        if kind == "netclasses":
            rows = [
                ("netclass:" + c["name"], c.get("nets") or [])
                for c in rules.get("net_classes") or []
            ]
        elif kind == "pairs":
            rows = [("pair:" + d["name"], [d["p"], d["n"]]) for d in rules.get("diff_pairs") or []]
        elif kind == "si":
            rows = [
                ("si:" + str(i.get("name", k)), i.get("nets") or [])
                for k, i in enumerate(intents or [])
            ]
        else:
            rows = [
                ("length_match:" + m["name"], m.get("nets") or [])
                for m in rules.get("length_match") or []
            ]
        for tag, nets in rows:
            if isinstance(nets, str) or not all(isinstance(n, str) for n in nets):
                raise ValueError("PNR_GLOSS_CLASSES_FROM: group %r must list net names" % tag)
            if nets:
                groups.append((tag, frozenset(nets)))
    unique = []  # the same group derived again (e.g. a pair declared as its own net class) is one
    for tag, nets in groups:
        if not any(members == nets for _, members in unique):
            unique.append((tag, nets))
    out = {}
    for _, (tag, nets) in sorted(enumerate(unique), key=lambda row: (len(row[1][1]), row[0])):
        for net in nets:
            out.setdefault(net, tag)
    return dict(sorted(out.items()))


def group_args(conf):
    """Worker arguments for the functional groups of ``conf`` ([] without groups)."""
    out = []
    if getattr(conf, "classes", None):
        out += ["--classes", str(conf.classes)]
    if getattr(conf, "classes_from", ()):
        out += ["--classes-from", ",".join(conf.classes_from)]
    if out:
        out += ["--cross-group-mm", repr(float(getattr(conf, "cross_group_mm", 10.0)))]
    return out


def check_groups(conf, rules):
    """The functional groups a pass would use, from its settings and the rules alone (``si``
    needs the board and is resolved by the pass): raises ValueError at controller start when
    the groups file or the rules cannot give groups, rather than when a pass runs."""
    if not getattr(conf, "classes", None) and not getattr(conf, "classes_from", ()):
        return None
    try:
        return class_tags(conf.classes, tuple(k for k in conf.classes_from if k != "si"), rules)
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        raise ValueError("PNR_GLOSS functional groups: %s" % (error,))


def class_tags(classes=None, classes_from=(), rules=None, intents=()):
    """Functional groups {net: tag} of a pass: derived ones (PNR_GLOSS_CLASSES_FROM) overlaid by
    the groups file (PNR_GLOSS_CLASSES; a file entry wins for its net); None when neither."""
    if not classes and not classes_from:
        return None
    tags = derive_class_tags(rules or {}, classes_from, intents) if classes_from else {}
    tags.update(load_class_tags(classes) or {})
    return tags


# ---------------------------------------------------------------- KiCad adapter (design 1.1-1.4)
class Model:
    """Board facts the planner and the trial checks need, in nm (KiCad side).

    base: a Model of the same board before an edit; net eligibility, protection,
    SI resolution and DRC-derived sets are reused (geometry is re-indexed).
    """

    def __init__(
        self,
        board,
        rules,
        *,
        path=None,
        sources=(),
        drc=None,
        gloss_si=False,
        base=None,
        deadline=math.inf,
        guard_open_nets=False,
        hug=True,
        classes=None,
        cross_group_mm=None,
        noise_allowance=None,
        classes_from=(),
    ):
        import pcbnew as k

        from pnr import gloss_geometry as g

        self.k, self.g, self.b, self.rules, self.path = k, g, board, rules, path
        self.deadline = deadline
        self.layers = list(board.GetEnabledLayers().CuStack())
        self.lname = {la: board.GetLayerName(la) for la in self.layers}
        self.lid = {n: la for la, n in self.lname.items()}
        self.items = list(board.GetTracks())
        self.pads = [p for f in board.GetFootprints() for p in f.Pads()]
        self.zones = list(board.Zones()) + [z for f in board.GetFootprints() for z in f.Zones()]
        self.by_uid = {uid(t): t for t in self.items}
        self.pad_by_uid = {uid(p): p for p in self.pads}
        from pnr.writeback import outline_bounds

        ob = outline_bounds(board)
        self.box = (ob.GetLeft(), ob.GetTop(), ob.GetRight(), ob.GetBottom())
        if base is not None:
            for name in (
                "netclass",
                "protected",
                "si",
                "si_status",
                "policies",
                "gloss_si",
                "frozen_ids",
                "open_pads",
                "open_nets",
                "contracts",
                "guard_open_nets",
                "hug_enabled",
                "class_tags",
                "cross_group_mm",
                "noise_allowance",
            ):
                setattr(self, name, getattr(base, name))
        else:
            from pnr.via_coalesce import protected

            self.netclass = project_netclass(path) if path else (lambda net: "Default")
            self.protected = set(protected(board, rules, sources)[0])
            self.si, self.si_status = si_nets(board, rules, sources)
            self.policies, self.gloss_si, self.contracts = {}, gloss_si, {}
            self.guard_open_nets, self.hug_enabled = bool(guard_open_nets), bool(hug)
            self.class_tags = class_tags(
                classes,
                classes_from,
                rules,
                (si_intents(board, rules, sources)[0] or []) if "si" in classes_from else (),
            )
            # functional groups (PNR_GLOSS_CLASSES): cap on cross-group parallel runs at minimum
            # pitch; noise_allowance is the noise-budget hook (gloss_geometry.allowed_parallel_mm budget)
            self.cross_group_mm = (
                g.CROSS_GROUP_MM if cross_group_mm is None else float(cross_group_mm)
            )
            self.noise_allowance = noise_allowance
            self.frozen_ids, self.open_pads, self.open_nets = freeze_sets(self, drc)
        self.arcs = {
            (t.GetNetname(), t.GetLayer()) for t in self.items if t.GetClass() == "PCB_ARC"
        }
        self.net_items = defaultdict(list)  # (net, layer) -> items on the layer
        self.layer_items = defaultdict(list)  # layer -> every copper item on it
        self.groups = defaultdict(list)  # (net, layer) -> PCB_TRACK
        for t in self.items:
            for la in self.layers:
                if t.IsOnLayer(la):
                    self.net_items[t.GetNetname(), la].append(t)
                    self.layer_items[la].append(t)
            if t.GetClass() == "PCB_TRACK" and t.GetNetCode():
                self.groups[t.GetNetname(), t.GetLayer()].append(t)
        for p in self.pads:
            for la in self.layers:
                if p.IsOnLayer(la):
                    self.net_items[p.GetNetname(), la].append(p)
                    self.layer_items[la].append(p)
        self.fills = defaultdict(list)  # layer -> filled copper zones
        self.keepouts = defaultdict(list)  # layer -> track keepout rule areas
        for z in self.zones:
            for la in self.layers:
                if not z.IsOnLayer(la):
                    continue
                if z.GetIsRuleArea():
                    if z.GetDoNotAllowTracks():
                        self.keepouts[la].append(z)
                else:
                    self.fills[la].append(z)
                    self.net_items[z.GetNetname(), la].append(z)
        self._shapes, self._grids, self._points, self._oracle, self._item_grid = (
            {},
            {},
            {},
            None,
            {},
        )
        self._chainsets, self._segs, self._info, self._rays = {}, {}, {}, {}
        self._coupling, self._totals = {}, {}

    # -- policy and eligibility (E1-E5, design 1.1)
    def policy(self, net):
        if net not in self.policies:
            from pnr.electrical import net_policy

            self.policies[net] = net_policy(net, self.rules)
        return self.policies[net]

    def info(self, net, la):
        if (net, la) not in self._info:
            self._info[net, la] = self._net_info(net, la)
        return self._info[net, la]

    def _net_info(self, net, la):
        g = self.g
        pol = self.policy(net)
        cls = self.netclass(net)
        ok, si, reason = g.eligibility(
            net,
            mode=pol["mode"],
            protected=self.protected,
            si_nets=self.si or (),
            netclass=cls,
            arc_layers={x for n, x in self.arcs if n == net},
            layer=la,
            gloss_si=self.gloss_si,
        )
        width = round(
            (pol["outer_width_mm"] if la in (self.k.F_Cu, self.k.B_Cu) else pol["inner_width_mm"])
            * NM
        )
        clearance = round(pol["clearance_mm"] * NM)
        # corridor class: every condition but E3 (an SI net is a stack boundary, never a member)
        corridor_ok = (
            pol["mode"] == "signal"
            and net not in self.protected
            and cls == "Default"
            and (net, la) not in self.arcs
        )
        key = g.class_key(self.lname[la], cls, width, clearance)
        # PNR_GLOSS_CLASSES: the functional group does not split the class (owner decision
        # 2026-09-30): cross-group packing is allowed up to the parallel-run cap (cap_check)
        tag = self.class_tags.get(net) if self.class_tags else None
        return dict(
            net=net,
            eligible=ok,
            si=si,
            reason=reason,
            mode=pol["mode"],
            netclass=cls,
            width=width,
            clearance=clearance,
            corridor=corridor_ok,
            key=key,
            tag=tag,
        )

    def eligible_groups(self, corridor=False):
        out = []
        for net, la in sorted(self.groups, key=lambda k: (self.lname[k[1]], k[0])):
            i = self.info(net, la)
            if i["corridor"] if corridor else i["eligible"]:
                out.append((net, la))
        return out

    # -- shapes and indices
    def shape(self, item, la):
        key = (uid(item), la)
        if key not in self._shapes:
            if item.GetClass() == "ZONE":
                self._shapes[key] = item.GetFilledPolysList(la)
            else:
                self._shapes[key] = item.GetEffectiveShape(la)
        return self._shapes[key]

    def net_grid(self, net, la):
        key = (net, la)
        if key not in self._grids:
            grid = Grid()
            for item in self.net_items.get(key, ()):
                grid.add(item_box(item), item)
            self._grids[key] = grid
        return self._grids[key]

    def layer_grid(self, la):
        if la not in self._item_grid:
            grid = Grid()
            for item in (
                self.layer_items.get(la, ()) + self.fills.get(la, []) + self.keepouts.get(la, [])
            ):
                grid.add(item_box(item), item)
            self._item_grid[la] = grid
        return self._item_grid[la]

    def vec(self, p):
        return self.k.VECTOR2I(int(round(p[0])), int(round(p[1])))

    def contains(self, item, la, p):
        return self.shape(item, la).Collide(self.vec(p), 1)

    @property
    def oracle(self):
        if self._oracle is None:
            from pnr.native_electrical import Oracle

            self._oracle = Oracle(self.b, self.rules, deadline=self.deadline)
        return self._oracle

    # -- frozen copper (F1-F3) and anchors (A1-A7)
    def contract_pads(self, net):
        if net not in self.contracts:
            from pnr.electrical import terminal_policy
            from pnr.pad_entry import required_width

            out = []
            for p in self.pads:
                if p.GetNetname() != net or not p.GetNetCode():
                    continue
                ref = p.GetParentFootprint().GetReference()
                try:
                    pol = terminal_policy(ref, [p.GetNumber()], net, self.rules)
                except (ValueError, KeyError):
                    pol = dict(terminal_sources=[])  # unreadable contract: treat as contracted
                try:
                    required = round(required_width(p, self.rules) * NM)
                except (ValueError, KeyError):
                    required = math.inf
                records = (pol or {}).get("terminal_sources") or []
                neck = max([r.get("neck_max_length_mm", 0) for r in records] or [0])
                out.append((uid(p), pol is not None, required, round(neck * NM)))
            self.contracts[net] = out
        return self.contracts[net]

    def frozen(self, track):
        """F2/F3 for one PCB_TRACK (F1 locked is Seg.locked)."""
        if uid(track) in self.frozen_ids:
            return True
        la = track.GetLayer()
        for pad_id, contracted, required, neck in self.contract_pads(track.GetNetname()):
            pad = self.pad_by_uid[pad_id]
            if not pad.IsOnLayer(la) or not (contracted or required > track.GetWidth()):
                continue
            if self.shape(pad, la).Collide(self.shape(track, la), int(neck if contracted else 0)):
                return True
        return False

    def segs(self, net, la):
        key = (net, la)
        if key not in self._segs:
            g = self.g
            self._segs[key] = [
                g.Seg(
                    uid(t),
                    pt(t.GetStart()),
                    pt(t.GetEnd()),
                    t.GetWidth(),
                    bool(t.IsLocked()),
                    self.frozen(t),
                )
                for t in self.groups.get(key, ())
            ]
        return self._segs[key]

    def terminals(self, net, la):
        return [x for x in self.net_items.get((net, la), ()) if x.GetClass() in ("PAD", "PCB_VIA")]

    def pinned(self, net, la):
        """A1/A2/A6: inside a same-net pad, via pad or filled zone on the layer."""
        terms = [
            x
            for x in self.net_items.get((net, la), ())
            if x.GetClass() in ("PAD", "PCB_VIA", "ZONE")
        ]
        cache = {}

        def inside(p):
            if p not in cache:
                cache[p] = any(
                    boxes_overlap(item_box(x), (p[0], p[1], p[0], p[1])) and self.contains(x, la, p)
                    for x in terms
                )
            return cache[p]

        return inside

    def pin_points(self, net, la):
        """A1: projections of pad/via centres onto segments touching them (track_graph rule)."""
        g = self.g
        out = set()
        tracks = self.groups.get((net, la), ())
        for term in self.terminals(net, la):
            c = pt(term.GetPosition())
            tb = item_box(term)
            for t in tracks:
                if not boxes_overlap(tb, item_box(t)):
                    continue
                if self.shape(term, la).Collide(self.shape(t, la), 0):
                    a, b = pt(t.GetStart()), pt(t.GetEnd())
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    n2 = dx * dx + dy * dy
                    s = (
                        0
                        if not n2
                        else max(0.0, min(1.0, ((c[0] - a[0]) * dx + (c[1] - a[1]) * dy) / n2))
                    )
                    out.add(g.ipt((a[0] + s * dx, a[1] + s * dy)))
        return sorted(out)

    def class_width(self, net, la):
        return self.info(net, la)["width"]

    def chainset(self, net, la, class_width=True):
        key = (net, la, class_width)
        if key not in self._chainsets:
            self._chainsets[key] = self.g.build_chains(
                self.segs(net, la),
                net=net,
                layer=self.lname[la],
                pinned=self.pinned(net, la),
                pin_points=self.pin_points(net, la),
                class_width=self.class_width(net, la) if class_width else None,
            )
        return self._chainsets[key]

    # -- L2 contact, L3 points, L7 guards
    def anchor_items(self, net, la, anchors):
        out = set()
        for p in anchors:
            for x in self.net_grid(net, la).query((p[0], p[1], p[0], p[1])):
                if self.contains(x, la, p):
                    out.add(uid(x))
        return out

    def touched_terminals(self, net, la, points, width):
        """Same-net pads/vias the polyline's copper (width) already touches. A chain anchored
        just outside a via (pin point clamped to its segment end) touches the via with its
        old end leg; its new end leg may keep that contact (L2). pin_points makes
        every touched terminal an anchor, so these sit at chain ends: the contact graph is
        unchanged."""
        k = self.k
        probe = k.PCB_TRACK(self.b)
        probe.SetLayer(la)
        probe.SetWidth(int(width))
        grid = self.net_grid(net, la)
        out = set()
        for a, b in zip(points, points[1:]):
            probe.SetStart(self.vec(a))
            probe.SetEnd(self.vec(b))
            shape = probe.GetEffectiveShape(la)
            box = (
                min(a[0], b[0]) - width,
                min(a[1], b[1]) - width,
                max(a[0], b[0]) + width,
                max(a[1], b[1]) + width,
            )
            for x in grid.query(box):
                if (
                    x.GetClass() not in ("PAD", "PCB_VIA")
                    or uid(x) in out
                    or not boxes_overlap(item_box(x), box)
                ):
                    continue
                if self.shape(x, la).Collide(shape, 0):
                    out.add(uid(x))
        return out

    def ray_index(self, la):
        """Layer copper as RayItems for the adjacency metric: tracks (eligible same-class
        corridor tracks carry their class key; SI and other tracks only stop rays), vias
        (discs) and pads (stadiums of their bounding box)."""
        if la not in self._rays:
            g = self.g
            items = []
            for x in self.layer_items.get(la, ()):
                cls = x.GetClass()
                if cls in ("PCB_TRACK", "PCB_ARC"):
                    net, key, clearance = x.GetNetname(), None, 0
                    if x.GetNetCode():
                        info = self.info(net, la)
                        clearance = info["clearance"]
                        if info["corridor"] and not info["si"] and x.GetWidth() == info["width"]:
                            key = info["key"]
                    items.append(
                        g.RayItem(
                            pt(x.GetStart()),
                            pt(x.GetEnd()),
                            x.GetWidth() / 2,
                            net,
                            key,
                            clearance,
                            uid(x),
                        )
                    )
                elif cls == "PCB_VIA":
                    c = pt(x.GetPosition())
                    items.append(
                        g.RayItem(c, c, x.GetWidth(la) / 2, x.GetNetname(), None, 0, uid(x))
                    )
                elif cls == "PAD":
                    a, b, r = self.guard_shape(x, la)
                    items.append(g.RayItem(a, b, r, x.GetNetname(), None, 0, uid(x)))
            self._rays[la] = g.RayIndex(items)
        return self._rays[la]

    def hug_fns(self, net, la, width, group_only=False):
        """(hug, snap) callables of the planner's Context for a same-class chain, else (None, None).
        group_only: neighbours of the net's own functional group only (cap fallback)."""
        info = self.info(net, la)
        if not self.hug_enabled or not info["corridor"] or info["si"]:
            return None, None
        if group_only and not self.cap_enabled:
            return None, None
        g = self.g
        index = self.ray_index(la)
        key, clearance = info["key"], info["clearance"]
        mine = self.group(net) if group_only else None
        accept = (lambda it: self.group(it.net) == mine) if group_only else None

        def hug(a, b):
            return g.hug_segment(a, b, width / 2, net, key, clearance, index, accept=accept)

        def snap(box):
            return [
                r
                for r in index.near(box)
                if r.key == key and r.net != net and (accept is None or accept(r))
            ]

        return hug, snap

    # -- functional groups: cross-group parallel-run cap (owner decision 2026-09-30)
    @property
    def cap_enabled(self):
        return self.class_tags is not None

    def group(self, net):
        return self.g.group_of(net, self.class_tags)

    def allowed_nm(self, a, b):
        """Allowed C(a, b) in nm, None = unlimited (gloss_geometry.allowed_parallel_mm: the hook)."""
        mm = self.g.allowed_parallel_mm(
            a, b, self.class_tags, self.cross_group_mm, budget=self.noise_allowance
        )
        return None if mm is None else mm * NM

    def ctrack(self, net, a, b, width, owner):
        return self.g.CTrack(
            net, tuple(a), tuple(b), int(width), round(self.policy(net)["clearance_mm"] * NM), owner
        )

    def coupling_layer(self, la):
        """Every track of the layer (with a net) for the parallel-run measure."""
        if la not in self._coupling:
            self._coupling[la] = self.g.CouplingLayer(
                [
                    self.ctrack(
                        t.GetNetname(), pt(t.GetStart()), pt(t.GetEnd()), t.GetWidth(), uid(t)
                    )
                    for t in self.layer_items.get(la, ())
                    if t.GetClass() in ("PCB_TRACK", "PCB_ARC") and t.GetNetCode()
                ]
            )
        return self._coupling[la]

    def net_totals(self, net):
        """{other net: C(net, other)} in nm, summed over the copper layers (this board)."""
        if net not in self._totals:
            out = Counter()
            for la in self.layers:
                for other, v in self.g.net_runs(self.coupling_layer(la), net).items():
                    out[other] += v
            self._totals[net] = dict(out)
        return self._totals[net]

    def pair_total(self, a, b):
        return self.net_totals(a).get(b, 0.0)

    def cross_group_rows(self):
        """{'a|b': [C mm, allowed mm]} of every cross-group pair with a run (whole board)."""
        total = Counter()
        for la in self.layers:
            for pair, v in self.g.pair_table(self.coupling_layer(la)).items():
                total[pair] += v
        out = {}
        for (a, b), v in sorted(total.items()):
            allowed = self.allowed_nm(a, b)
            if self.group(a) != self.group(b) and v > 1:
                out["%s|%s" % (a, b)] = [
                    round(v / NM, 6),
                    None if allowed is None else round(allowed / NM, 6),
                ]
        return out

    def cap_check(self, delta):
        """None, or 'cap:<a>|<b>' for the first pair the change `delta` ({(a, b): nm}) breaks."""
        bad = self.g.cap_violations(delta, self.pair_total, self.allowed_nm)
        return None if not bad else "cap:%s|%s" % bad[0][:2]

    def chain_cap(self, net, la, chain):
        """Context.cap for one chain: C after replacing its copper by new points, all pairs of net."""
        g = self.g
        segs_by_id = {s.id: s for s in self.segs(net, la)}
        keep = [
            self.ctrack(net, a, b, w, "%s#keep" % sid)
            for sid, a, b, w in g.chain_ops(chain, [], segs_by_id)["keep"]
        ]
        cache = {}

        def cap(points):
            if "edit" not in cache:  # lazy: most chains never reach the cap check
                cache["edit"] = g.EditCoupling(
                    self.coupling_layer(la), chain.seg_ids, keep, margin=g.TUBE
                )
            legs = [
                self.ctrack(net, p, q, chain.width, "#new")
                for p, q in zip(points, points[1:])
                if p != q
            ]
            return self.cap_check(cache["edit"].delta(legs))

        return cap

    def corridor_cap(self, la, meta, base):
        """Corridor.cap: C with every moved chain of the layer at its state geometry."""
        g = self.g
        info = {}

        def parts(cid):
            if cid not in info:
                ch, _ = meta[cid]
                segs_by_id = {s.id: s for s in self.segs(ch.net, la)}
                info[cid] = (
                    ch,
                    [
                        self.ctrack(ch.net, a, b, w, "%s#keep" % sid)
                        for sid, a, b, w in g.chain_ops(ch, [], segs_by_id)["keep"]
                    ],
                )
            return info[cid]

        def cap(cid, new, state):
            moved = {c: pts for c, pts in state.items() if c != cid and pts != base[c]}
            moved[cid] = new
            remove, add = set(), []
            for c, pts in sorted(moved.items()):
                ch, keep = parts(c)
                remove.update(ch.seg_ids)
                add += keep + [
                    self.ctrack(ch.net, p, q, ch.width, "#new")
                    for p, q in zip(pts, pts[1:])
                    if p != q
                ]
            return self.cap_check(g.EditCoupling(self.coupling_layer(la), remove, add).delta())

        return cap

    def contact_fn(self, net, la, width, allowed):
        """L2: True when a new segment touches same-net copper outside `allowed` (exact shapes)."""
        k = self.k
        probe = k.PCB_TRACK(self.b)
        probe.SetLayer(la)
        probe.SetWidth(int(width))
        grid = self.net_grid(net, la)

        def contact(a, b):
            probe.SetStart(self.vec(a))
            probe.SetEnd(self.vec(b))
            shape = probe.GetEffectiveShape(la)
            box = (
                min(a[0], b[0]) - width,
                min(a[1], b[1]) - width,
                max(a[0], b[0]) + width,
                max(a[1], b[1]) + width,
            )
            for x in grid.query(box):
                if uid(x) in allowed or not boxes_overlap(item_box(x), box):
                    continue
                if self.shape(x, la).Collide(shape, 0):
                    return True
            return False

        contact.probe = probe
        return contact

    def points(self, la):
        """L3 test points of every item on the layer: (x, y, owner uuid)."""
        if la not in self._points:
            k = self.k
            rows = []
            for x in self.layer_items.get(la, ()):
                owner = uid(x)
                cls = x.GetClass()
                if cls == "PAD":
                    rows.append(pt(x.GetPosition()) + (owner,))
                    poly = k.SHAPE_POLY_SET()
                    x.TransformShapeToPolygon(poly, la, 0, 5000, k.ERROR_INSIDE)
                    for o in range(poly.OutlineCount()):
                        line = poly.COutline(o)
                        rows += [pt(line.CPoint(i)) + (owner,) for i in range(line.PointCount())]
                elif cls == "PCB_VIA":
                    rows.append(pt(x.GetPosition()) + (owner,))
                else:
                    rows += [pt(x.GetStart()) + (owner,), pt(x.GetEnd()) + (owner,)]
            for z in self.fills.get(la, []) + self.keepouts.get(la, []):
                poly = z.GetFilledPolysList(la) if not z.GetIsRuleArea() else z.Outline()
                for o in range(poly.OutlineCount()):
                    line = poly.COutline(o)
                    rows += [pt(line.CPoint(i)) + (uid(z),) for i in range(line.PointCount())]
                    for h in range(poly.HoleCount(o)):
                        hole = poly.CHole(o, h)
                        rows += [pt(hole.CPoint(i)) + (uid(z),) for i in range(hole.PointCount())]
            for d in self.b.GetDrawings():
                if d.GetLayer() == k.Edge_Cuts:
                    rows += [pt(d.GetStart()) + ("edge",), pt(d.GetEnd()) + ("edge",)]
            grid = Grid()
            for r in rows:
                grid.add((r[0], r[1], r[0], r[1]), r)
            self._points[la] = grid
        return self._points[la]

    def obstacle_points(self, la, box, exclude=()):
        return [
            (r[0], r[1])
            for r in self.points(la).query(box)
            if r[2] not in exclude and box[0] <= r[0] <= box[2] and box[1] <= r[1] <= box[3]
        ]

    def guard_shape(self, x, la):
        cls = x.GetClass()
        if cls == "PCB_TRACK" or cls == "PCB_ARC":
            return (pt(x.GetStart()), pt(x.GetEnd()), x.GetWidth() / 2)
        if cls == "PCB_VIA":
            c = pt(x.GetPosition())
            return (c, c, x.GetWidth(la) / 2)
        box = self.shape(x, la).BBox()
        w, h = box.GetWidth(), box.GetHeight()
        c = (box.GetCenter().x, box.GetCenter().y)
        if w >= h:
            return ((c[0] - (w - h) // 2, c[1]), (c[0] + (w - h) // 2, c[1]), h / 2)
        return ((c[0], c[1] - (h - w) // 2), (c[0], c[1] + (h - w) // 2), w / 2)

    def guards(self, net, la, width, box, si=False):
        """L7: G1 diff-pair copper (cap 3 x pair gap), G2 open terminal pads (1 mm), G3 SI 3W."""
        g = self.g
        out = []
        for pair in sorted(self.rules.get("diff_pairs", []), key=lambda p: p["name"]):
            if net in (pair["p"], pair["n"]):
                continue
            cap = 3 * pair.get("gap_mm", 0.15) * NM
            wide = (box[0] - cap, box[1] - cap, box[2] + cap, box[3] + cap)
            shapes = []
            for n in (pair["p"], pair["n"]):
                for x in self.net_grid(n, la).query(wide):
                    if x.GetClass() != "ZONE":
                        shapes.append(self.guard_shape(x, la))
            if shapes:
                out.append(g.Guard("G1:" + pair["name"], tuple(sorted(shapes)), cap))
        cap = 1.0 * NM
        wide = (box[0] - cap, box[1] - cap, box[2] + cap, box[3] + cap)
        shapes = [
            self.guard_shape(self.pad_by_uid[p], la)
            for p in sorted(self.open_pads)
            if self.pad_by_uid[p].IsOnLayer(la)
            and self.pad_by_uid[p].GetNetname() != net
            and boxes_overlap(item_box(self.pad_by_uid[p]), wide)
        ]
        if self.guard_open_nets:
            # before refinement (06g): all copper of open nets keeps its surroundings
            shapes += [
                self.guard_shape(x, la)
                for x in self.layer_grid(la).query(wide)
                if x.GetClass() in ("PCB_TRACK", "PCB_ARC", "PCB_VIA")
                and x.GetNetCode()
                and x.GetNetname() in self.open_nets
                and x.GetNetname() != net
                and boxes_overlap(item_box(x), wide)
            ]
        if shapes:
            out.append(g.Guard("G2", tuple(sorted(shapes)), cap))
        if si:
            cap = 3 * width
            wide = (box[0] - cap, box[1] - cap, box[2] + cap, box[3] + cap)
            shapes = [
                self.guard_shape(x, la)
                for x in self.layer_grid(la).query(wide)
                if x.GetClass() in ("PCB_TRACK", "PCB_ARC", "PCB_VIA", "PAD")
                and x.GetNetname() != net
            ]
            if shapes:
                out.append(g.Guard("G3", tuple(sorted(shapes)), cap))
        return out

    def allowed_items(self, net, la, chain_points, width, own=()):
        """L2 exemptions of one chain: items at its anchors, its own segments, and the same-net
        pads/vias its old copper already touches."""
        return (
            self.anchor_items(net, la, (chain_points[0], chain_points[-1]))
            | set(own)
            | self.touched_terminals(net, la, chain_points, width)
        )

    def context(self, net, la, chain, cs, tube=None):
        g = self.g
        tube = g.TUBE if tube is None else tube
        info = self.info(net, la)
        width = chain.width
        allowed = self.allowed_items(net, la, chain.points, width, chain.seg_ids)
        oracle = self.oracle
        lmm = width / NM

        def clear(a, b):
            return oracle.clear(net, la, (a[0] / NM, a[1] / NM), (b[0] / NM, b[1] / NM), lmm)

        box = g.bbox(chain.points, tube + NM)
        hug, snap = self.hug_fns(net, la, width)
        extra = {}
        if self.cap_enabled:
            hug_group, snap_group = self.hug_fns(net, la, width, group_only=True)
            extra = dict(
                cap=self.chain_cap(net, la, chain), hug_group=hug_group, snap_group=snap_group
            )
        return g.Context(
            width,
            clear=clear,
            contact=self.contact_fn(net, la, width, allowed),
            obstacles=self.obstacle_points(la, box, allowed),
            guards=self.guards(net, la, width, box, si=info["si"]),
            anchor_legs=g.chain_anchor_legs(cs, chain),
            bounds=self.box,
            tube=tube,
            hug=hug,
            snap=snap,
            clearance=info["clearance"],
            search_budget=g.CHAIN_CALL_BUDGET,
            **extra,
        )

    # -- corridor model (design 2.3)
    def corridor(self, la, planner=True):
        """(Corridor, {chain_id: (chain, chainset)}) for one layer.

        Members: every chain of a corridor-class net (E1, E2, E4, E5); SI nets are
        boundaries (si=True, never movable). Their copper is ignored by the Oracle
        fork and checked by the planner against the current chain state.
        """
        g = self.g
        chains, meta, ignored = [], {}, set()
        for net, la2 in self.eligible_groups(corridor=True):
            if la2 != la:
                continue
            info = self.info(net, la)
            cs = self.chainset(net, la)
            si = net in (self.si or ())
            segs = {s.id: s for s in self.segs(net, la)}
            for ch in cs.chains:
                cid = "%s|%s|%d,%d" % (net, ch.pieces[0][2], ch.points[0][0], ch.points[0][1])
                old_segments = tuple(
                    sorted((s.id, s.a, s.b, s.width) for s in (segs[i] for i in ch.seg_ids))
                )
                chains.append(
                    g.CorridorChain(
                        cid,
                        net,
                        ch.width,
                        info["clearance"],
                        ch.legs,
                        info["key"],
                        movable=not ch.frozen and not si and info["eligible"],
                        si=si,
                        old_segments=old_segments,
                    )
                )
                meta[cid] = (ch, cs)
                ignored.update(ch.seg_ids)
        tracks = [
            (t.GetNetname(), pt(t.GetStart()), pt(t.GetEnd()), t.GetWidth())
            for t in self.layer_items.get(la, ())
            if t.GetClass() in ("PCB_TRACK", "PCB_ARC") and uid(t) not in ignored
        ]
        kwargs = dict(
            layer=self.lname[la],
            tracks=tracks,
            strip_blocked=self.strip_blocked(la),
            ray_items=[r for r in self.ray_index(la).items if r.owner not in ignored],
        )
        if planner:
            from pnr.native_electrical import Oracle

            fork = Oracle(self.b, self.rules, ignored=ignored, deadline=self.deadline)

            def clear(net, width, a, b):
                return fork.clear(
                    net, la, (a[0] / NM, a[1] / NM), (b[0] / NM, b[1] / NM), width / NM
                )

            contacts = {}

            def contact(net, width, a, b, cid):
                if cid not in contacts:
                    ch = meta[cid][0]
                    allowed = self.allowed_items(net, la, ch.points, ch.width, ch.seg_ids)
                    contacts[cid] = self.contact_fn(net, la, width, allowed)
                return contacts[cid](a, b)

            def guards(cid):
                ch = meta[cid][0]
                return self.guards(
                    ch.net, la, ch.width, g.bbox(ch.points, g.CORRIDOR_MAX_SHIFT + NM)
                )

            box = self.box
            kwargs.update(
                clear=clear,
                contact=contact,
                guards=guards,
                obstacles=self.obstacle_points(la, box, ignored),
                window_ok=self.window_ok(la, ignored, {c.id: c for c in chains}),
            )
            if self.cap_enabled:
                kwargs["cap"] = self.corridor_cap(
                    la, meta, {c.id: g.simplify(c.points) for c in chains}
                )
        return g.Corridor(chains, **kwargs), meta, ignored

    def strip_blocked(self, la):
        """Non-track items (pads, vias, fills, keepouts) of other nets intersecting a strip."""
        k = self.k
        grid = self.layer_grid(la)

        def blocked(quad, nets):
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            box = (math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys)))
            poly = None
            for x in grid.query(box):
                cls = x.GetClass()
                if cls in ("PCB_TRACK", "PCB_ARC"):
                    continue
                if cls != "ZONE" or not x.GetIsRuleArea():
                    if x.GetNetname() in nets:
                        continue
                if not boxes_overlap(item_box(x), box):
                    continue
                if poly is None:
                    poly = k.SHAPE_POLY_SET()
                    poly.NewOutline()
                    for p in quad:
                        poly.Append(int(round(p[0])), int(round(p[1])))
                other = x.Outline() if cls == "ZONE" and x.GetIsRuleArea() else self.shape(x, la)
                if cls == "ZONE":
                    test = k.SHAPE_POLY_SET(other)
                    test.BooleanIntersection(poly)
                    if test.OutlineCount() and test.Area() > 0:
                        return True
                elif poly.Collide(other, 0):
                    return True
            return False

        return blocked

    # -- dead space (design 2.4)
    def free_region(self, la, w, c, box, skip=(), extra=()):
        """F within box (+margin): outline - edge clearance - copper inflated by max(c, c_item)+1um
        - track keepouts - holes inflated by their hole clearance. extra: (a, b, width, clearance).
        """
        k = self.k
        from pnr.fab_profile import geometry

        geo = geometry(self.rules)
        edge = round(self.rules.get("fab", {}).get("edge_clearance_mm", 0.2) * NM)
        F = k.SHAPE_POLY_SET()
        if not (self.b.GetBoardPolygonOutlines(F, False) and F.OutlineCount()):
            F = rect(k, self.box)
        F.Inflate(-edge, k.CORNER_STRATEGY_ROUND_ALL_CORNERS, POLY_ERROR)
        F.BooleanIntersection(rect(k, box))
        occupied = k.SHAPE_POLY_SET()
        for x in self.layer_grid(la).query(box):
            if uid(x) in skip or not boxes_overlap(item_box(x), box):
                continue
            cls = x.GetClass()
            if cls == "ZONE":
                if x.GetIsRuleArea():
                    occupied.Append(x.Outline())
                else:
                    fill = k.SHAPE_POLY_SET(x.GetFilledPolysList(la))
                    gap = max(c, round(self.policy(x.GetNetname())["clearance_mm"] * NM)) + 1000
                    fill.Inflate(gap, k.CORNER_STRATEGY_ROUND_ALL_CORNERS, POLY_ERROR)
                    occupied.Append(fill)
                continue
            gap = max(c, round(self.policy(x.GetNetname())["clearance_mm"] * NM)) + 1000
            x.TransformShapeToPolygon(occupied, la, int(gap), POLY_ERROR, k.ERROR_OUTSIDE)
            if cls == "PCB_VIA":
                from pnr.via_in_pad import hole_keepouts

                for layer, _, hole_gap in hole_keepouts(geo, x, [la]):
                    if layer == la:  # bare hole of a removed-pad via: hole clearance
                        disc = k.PCB_TRACK(self.b)
                        disc.SetLayer(la)
                        disc.SetStart(x.GetPosition())
                        disc.SetEnd(x.GetPosition())
                        disc.SetWidth(int(x.GetDrillValue() + 2 * round(hole_gap * NM)))
                        disc.TransformShapeToPolygon(occupied, la, 0, POLY_ERROR, k.ERROR_OUTSIDE)
            if cls == "PAD" and max(x.GetDrillSize().x, x.GetDrillSize().y) > 0:
                npth = x.GetAttribute() == k.PAD_ATTRIB_NPTH
                hole = geo.npth_hole_clearance if npth else geo.pth_hole_clearance
                hole = (
                    hole
                    if hole is not None
                    else self.rules.get("fab", {}).get("hole_clearance_mm", 0.2)
                )
                x.TransformHoleToPolygon(
                    occupied, int(round(hole * NM)), POLY_ERROR, k.ERROR_OUTSIDE
                )
        if extra:
            probe = k.PCB_TRACK(self.b)
            probe.SetLayer(la)
            for a, b, width, cl in extra:
                probe.SetStart(self.vec(a))
                probe.SetEnd(self.vec(b))
                probe.SetWidth(int(width))
                probe.TransformShapeToPolygon(
                    occupied, la, int(max(c, cl) + 1000), POLY_ERROR, k.ERROR_OUTSIDE
                )
        if occupied.OutlineCount():
            F.BooleanSubtract(occupied)
        return F

    def dead_space(self, la, w, c, box, skip=(), extra=(), buses=()):
        """(F, US, DS) mm^2 inside box: US = area(open(F, w/2)), DS = area(F) - US.

        buses: k values; A<k> = area(open(F, (w + (k-1) pitch)/2)), the free area a k-track
        bus at minimum pitch could use (grows when free strips merge)."""
        k = self.k
        margin = int(
            w + 2 * c + 50_000 + (max(buses) - 1) * (w + c + 1000) if buses else w + 2 * c + 50_000
        )
        big = (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
        F = self.free_region(la, w, c, big, skip, extra)
        inner = rect(k, box)

        def opened(r):
            U = k.SHAPE_POLY_SET(F)
            U.Inflate(-int(r), k.CORNER_STRATEGY_ROUND_ALL_CORNERS, POLY_ERROR)
            U.Inflate(int(r), k.CORNER_STRATEGY_ROUND_ALL_CORNERS, POLY_ERROR)
            U.BooleanIntersection(F)
            U.BooleanIntersection(inner)
            return U.Area() / NM / NM

        u = opened(w // 2)
        out = {}
        for n in buses:
            out["A%d" % n] = opened((w + (n - 1) * (w + c + 1000)) // 2)
        F.BooleanIntersection(inner)
        f = F.Area() / NM / NM
        out.update(F=f, US=u, DS=f - u)
        return out

    def window_ok(self, la, ignored, chains):
        """Corridor dead-space hook: dDS(window) <= 0 with chain copper taken from the states."""

        def extra(state, box):
            out = []
            for cid, points in state.items():
                c = chains[cid]
                for a, b in zip(points, points[1:]):
                    if boxes_overlap(
                        (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), box
                    ):
                        out.append((a, b, c.width, c.clearance))
            return out

        def ok(window, before, after):
            any_chain = next(iter(chains.values()))
            w, c = any_chain.width, any_chain.clearance
            margin = int(w + 2 * c + 50_000)
            big = (window[0] - margin, window[1] - margin, window[2] + margin, window[3] + margin)
            d0 = self.dead_space(la, w, c, window, ignored, extra(before, big))
            d1 = self.dead_space(la, w, c, window, ignored, extra(after, big))
            ok.last = (d0, d1)
            return d1["DS"] <= d0["DS"] + DS_TOLERANCE_MM2

        ok.last = None
        return ok


def rect(k, box):
    poly = k.SHAPE_POLY_SET()
    poly.NewOutline()
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])):
        poly.Append(int(x), int(y))
    return poly


# ---------------------------------------------------------------- planning (inventory worker, design 2.0-2.3, 3.4)
def edit_id(spec):
    body = json.dumps(
        [
            spec["step"],
            spec.get("net"),
            spec.get("layer"),
            spec.get("old", {}).get("sha256"),
            spec.get("new_nm"),
            spec.get("ops"),
            [m.get("new_nm") for m in spec.get("members") or []],
        ],
        sort_keys=True,
        default=str,
    )
    return spec["step"] + ":" + hashlib.sha256(body.encode()).hexdigest()[:16]


def chain_spec(g, step, model, net, la, chain, new, segs_by_id, anchor_legs=None):
    edit = g.make_edit(step, chain, new, segs_by_id, anchor_legs=anchor_legs)
    spec = edit.to_spec()
    ops = g.chain_ops(chain, edit.new, segs_by_id)
    spec.update(
        layer=model.lname[la],
        old_nm=[list(p) for p in g.simplify(chain.points)],
        new_nm=[list(p) for p in edit.new],
        old_segments_nm=[[i, list(a), list(b), w] for i, a, b, w in edit.old_segments],
        ops=dict(
            remove=ops["remove"],
            keep=[[i, list(a), list(b), w] for i, a, b, w in ops["keep"]],
            width=ops["width"],
        ),
        nets=[net],
        gain=edit.gain(),
        box=list(edit.box()),
    )
    spec["id"] = edit_id(spec)
    return spec


def chain_signature(net, layer, points):
    return hashlib.sha256(json.dumps([net, layer, [list(p) for p in points]]).encode()).hexdigest()[
        :20
    ]


def plan(model, step, *, deadline=math.inf, skip=(), quiet_skip=(), quiet=None):
    """Proposals of one step on this board (design 2.0-2.3). Returns (specs, stats).

    quiet_skip: signatures of chains planned without a proposal in an earlier round whose
    surroundings have not changed since (the controller tracks accepted edit boxes); quiet,
    when a list, receives (signature, layer, box) of every chain planned without a proposal.
    """
    from pnr import gloss_geometry as g

    stats = Counter()
    specs = []
    if model.si is None:
        return [], dict(status=model.si_status)
    if step == "normalize":
        for net, la in model.eligible_groups():
            segs = model.segs(net, la)
            vias = [x for x in model.terminals(net, la)]

            def covers(p, r, vias=vias, la=la):
                for x in vias:
                    poly = model.k.SHAPE_POLY_SET()
                    x.TransformShapeToPolygon(poly, la, 0, POLY_ERROR, model.k.ERROR_INSIDE)
                    poly.Inflate(-int(r), model.k.CORNER_STRATEGY_ROUND_ALL_CORNERS, POLY_ERROR)
                    if poly.OutlineCount() and poly.Contains(model.vec(p)):
                        return True
                return False

            result = g.normalize(
                segs,
                pinned=model.pinned(net, la),
                pin_points=model.pin_points(net, la),
                covers_disc=covers,
            )
            for key, n in result.counts.items():
                stats[key] += n
            if not result.delete and not result.modify:
                continue
            by = {s.id: s for s in segs}
            touched = sorted(set(result.delete) | set(result.modify))
            old = tuple(sorted((s.id, s.a, s.b, s.width) for s in (by[i] for i in touched)))
            spec = dict(
                step="normalize",
                net=net,
                layer=model.lname[la],
                nets=[net],
                delete=sorted(result.delete),
                modify={i: [list(a), list(b)] for i, (a, b) in sorted(result.modify.items())},
                old=dict(uuids=touched, sha256=g.segments_sha256(old)),
                old_segments_nm=[[i, list(a), list(b), w] for i, a, b, w in old],
                counts=result.counts,
                gain=float(len(result.delete)),
                box=list(g.bbox([p for s in old for p in (s[1], s[2])], g.INFLUENCE_MARGIN)),
            )
            spec["id"] = edit_id(spec)
            if spec["id"] not in skip:
                specs.append(spec)
        return specs, dict(stats)
    if step in ("dekink", "gloss"):
        jobs = []
        why = Counter()  # first failing check of every chain planned without a proposal
        for net, la in model.eligible_groups():
            cs = model.chainset(net, la)
            stats["off_width"] += cs.off_width
            stats["loops"] += cs.loops + cs.closed
            for ch in cs.chains:
                if ch.frozen:
                    stats["frozen"] += 1
                    continue
                legs = ch.legs
                if len(legs) < 3:
                    stats["short"] += 1
                    continue
                if chain_signature(net, model.lname[la], ch.points) in quiet_skip:
                    stats["quiet"] += 1
                    continue
                slack = g.cost(legs) - g.lower_bound(legs[0], legs[-1])
                jobs.append((-slack, net, model.lname[la], ch.points[0], ch.points[-1], la, ch, cs))
        jobs.sort(key=lambda j: j[:5])
        calls = Counter()
        for _, net, _, _, _, la, ch, cs in jobs:
            if time.monotonic() > deadline:
                stats["deadline"] += 1
                break
            stats["chains"] += 1
            try:
                ctx = model.context(net, la, ch, cs)
                new = g.dekink(ch.legs, ctx) if step == "dekink" else g.gloss(ch.legs, ctx)
            except TimeoutError:
                stats["deadline"] += 1
                break
            for key, n in ctx.calls.items():
                calls[key] += n
                calls["max_" + key] = max(calls["max_" + key], n)
            if new is None:
                why[ctx.why or "no_candidate"] += 1
                if quiet is not None:
                    quiet.append(
                        (
                            chain_signature(net, model.lname[la], ch.points),
                            model.lname[la],
                            list(g.bbox(ch.points, g.TUBE + 2 * NM)),
                        )
                    )
                continue
            segs_by_id = {s.id: s for s in model.segs(net, la)}
            spec = chain_spec(
                g, step, model, net, la, ch, new, segs_by_id, anchor_legs=ctx.anchor_legs
            )
            if spec["id"] in skip:
                stats["blacklisted"] += 1
                continue
            specs.append(spec)
        stats["proposed"] = len(specs)
        out = dict(stats)
        out["why"] = dict(sorted(why.items()))
        out["calls"] = dict(sorted(calls.items()))
        return specs, out
    if step == "corridor":
        for la in model.layers:
            if not any(l2 == la for _, l2 in model.eligible_groups()):
                continue
            if time.monotonic() > deadline:
                stats["deadline"] += 1
                break
            try:
                cor, meta, _ = model.corridor(la)
                edits = cor.plan()
            except TimeoutError:
                stats["deadline"] += 1
                break
            for key, n in cor.stats.items():
                stats[key] += n
            for e in edits:
                spec = e.to_spec()
                members = []
                for m, row in zip(e.members, spec["members"]):
                    ch, cs = meta[m["chain"]]
                    segs_by_id = {s.id: s for s in model.segs(m["net"], la)}
                    ops = g.chain_ops(ch, g.simplify(m["new"]), segs_by_id)
                    row.update(
                        old_nm=[list(p) for p in g.simplify(m["old"])],
                        new_nm=[list(p) for p in g.simplify(m["new"])],
                        old_segments_nm=[
                            [i, list(a), list(b), w] for i, a, b, w in m["old_segments"]
                        ],
                        ops=dict(
                            remove=ops["remove"],
                            keep=[[i, list(a), list(b), w] for i, a, b, w in ops["keep"]],
                            width=ops["width"],
                        ),
                    )
                    members.append(row)
                spec.update(
                    members=members,
                    layer=model.lname[la],
                    nets=sorted({m["net"] for m in members}),
                    gain=e.gain(),
                    box=list(e.box()),
                )
                spec["window_nm"] = [round(v * NM) for xy in spec["window_mm"] for v in xy]
                spec["id"] = edit_id(spec)
                if spec["id"] in skip:
                    stats["blacklisted"] += 1
                    continue
                specs.append(spec)
        stats["proposed"] = len(specs)
        return specs, dict(stats)
    raise ValueError("unknown step " + step)


def geometry_key(spec):
    """Order key of a spec from its geometry only (ids hash uuids, which are random for copper
    created by earlier edits; ordering by them would make batching run-dependent)."""
    return json.dumps(
        [
            spec.get("old_nm"),
            spec.get("new_nm"),
            spec.get("modify"),
            [(m.get("old_nm"), m.get("new_nm")) for m in spec.get("members") or []],
        ],
        sort_keys=True,
        default=str,
    )


def batch_specs(specs, limit):
    """Greedy by gain into batches of distinct nets and disjoint influence boxes (design 3.4)."""
    order = sorted(
        specs, key=lambda s: (-s["gain"], s["layer"], s["nets"], geometry_key(s), s["id"])
    )
    out = []
    for s in order:
        for b in out:
            if len(b) < limit and all(
                not (set(s["nets"]) & set(x["nets"]))
                and not (x["layer"] == s["layer"] and boxes_overlap(s["box"], x["box"]))
                for x in b
            ):
                b.append(s)
                break
        else:
            out.append([s])
    return out


# ---------------------------------------------------------------- applying and re-checking (trial worker, T1)
class Stale(Exception):
    pass


def current_segments(model, ids):
    out = []
    for i in ids:
        t = model.by_uid.get(i)
        if t is None or t.GetClass() != "PCB_TRACK":
            raise Stale("missing " + i)
        out.append((i, pt(t.GetStart()), pt(t.GetEnd()), t.GetWidth()))
    return tuple(sorted(out))


def replacements(spec):
    """Chain replacements of a spec: [(net, old_nm, new_nm, old_segments, ops)]."""
    if spec["step"] == "corridor":
        return [
            (m["net"], m["old_nm"], m["new_nm"], m["old_segments_nm"], m["ops"])
            for m in spec["members"]
        ]
    if spec["step"] == "normalize":
        return []
    return [(spec["net"], spec["old_nm"], spec["new_nm"], spec["old_segments_nm"], spec["ops"])]


def precheck(model, spec, gloss_si):
    """Stale (hash guard) and eligibility asserts on the checkpoint board. Returns None or a reason."""
    from pnr import gloss_geometry as g

    la = model.lid.get(spec["layer"])
    if la is None:
        return "stale:layer"
    groups = (
        [(spec["net"], spec["old_segments_nm"])]
        if spec["step"] != "corridor"
        else [(m["net"], m["old_segments_nm"]) for m in spec["members"]]
    )
    for net, rows in groups:
        try:
            now = current_segments(model, [r[0] for r in rows])
        except Stale:
            return "stale:missing"
        want = tuple(sorted((r[0], tuple(r[1]), tuple(r[2]), r[3]) for r in rows))
        if g.segments_sha256(now) != g.segments_sha256(want):
            return "stale:geometry"
        info = model.info(net, la)
        if not info["eligible"]:
            return "ineligible:" + str(info["reason"])
        if info["si"] and (spec["step"] == "corridor" or not gloss_si):
            return "ineligible:si"
        for i in [r[0] for r in rows]:
            t = model.by_uid[i]
            if t.IsLocked() or model.frozen(t) or t.GetNetname() != net or t.GetLayer() != la:
                return "ineligible:frozen"
    return None


def apply_spec(model, spec):
    """Edit the board in place; returns ({created, removed} uuids, wrappers the caller keeps alive).

    Replaced copper is deleted with ``board.Delete`` (never ``Remove``: a detached item
    freed after its board crashes KiCad's Python at teardown). Its uuids are read first and
    its (now invalid) wrapper is never touched again: the re-checks run on a fresh Model."""
    k = model.k
    b = model.b
    la = model.lid[spec["layer"]]
    created, removed, keep_alive = [], [], []

    def add(net, a, z, width):
        t = k.PCB_TRACK(b)
        t.SetStart(k.VECTOR2I(int(a[0]), int(a[1])))
        t.SetEnd(k.VECTOR2I(int(z[0]), int(z[1])))
        t.SetLayer(la)
        t.SetWidth(int(width))
        t.SetNetCode(b.FindNet(net).GetNetCode())
        b.Add(t)
        t.thisown = False
        created.append(uid(t))
        keep_alive.append(t)
        return t

    if spec["step"] == "normalize":
        for i in spec["delete"]:
            b.Delete(model.by_uid.pop(i))  # discarded copper (Delete, not Remove)
            removed.append(i)
        for i, (a, z) in spec["modify"].items():
            t = model.by_uid[i]
            t.SetStart(k.VECTOR2I(int(a[0]), int(a[1])))
            t.SetEnd(k.VECTOR2I(int(z[0]), int(z[1])))
        return dict(created=created, removed=removed, modified=sorted(spec["modify"])), keep_alive
    members = []
    for net, old, new, old_segments, ops in replacements(spec):
        mine = dict(net=net, created=[], keep=[], removed=[])
        for i in ops["remove"]:
            b.Delete(model.by_uid.pop(i))  # replaced by the new path (Delete, not Remove)
            mine["removed"].append(i)
        for _, a, z, w in ops["keep"]:
            mine["keep"].append(uid(add(net, a, z, w)))
        for a, z in zip(new, new[1:]):
            mine["created"].append(uid(add(net, a, z, ops["width"])))
        removed += mine["removed"]
        members.append(mine)
    return dict(created=created, removed=removed, members=members), keep_alive


def anchor_rays(model, net, la, p, exclude):
    """Directions of same-net legs at anchor p on the applied board (L5 input)."""
    out = []
    for x in model.net_grid(net, la).query((p[0], p[1], p[0], p[1])):
        if x.GetClass() != "PCB_TRACK" or uid(x) in exclude:
            continue
        a, b = pt(x.GetStart()), pt(x.GetEnd())
        if a == p:
            out.append((b[0] - p[0], b[1] - p[1]))
        elif b == p:
            out.append((a[0] - p[0], a[1] - p[1]))
        elif model.g.point_segment_distance(p, a, b) <= model.g.TOL:
            out += [(a[0] - p[0], a[1] - p[1]), (b[0] - p[0], b[1] - p[1])]
    return sorted(out)


def recheck(model, spec, applied):
    """T1 on the applied board: LEGAL for every replacement and the edit rule. None or a reason."""
    g = model.g
    la = model.lid[spec["layer"]]
    oracle = model.oracle
    members = applied.get("members", [])
    rows = spec.get("members") or [None] * len(members)
    for j, ((net, old, new, _, ops), mine) in enumerate(zip(replacements(spec), members)):
        old = [tuple(p) for p in old]
        new = [tuple(p) for p in new]
        width = ops["width"]
        if new[0] != old[0] or new[-1] != old[-1]:
            return "L4:anchors"
        old_set = {frozenset(x) for x in zip(old, old[1:])}
        new_set = {frozenset(x) for x in zip(new, new[1:])}
        added = [x for x in zip(new, new[1:]) if frozenset(x) not in old_set]
        removed = [x for x in zip(old, old[1:]) if frozenset(x) not in new_set]
        if any(g.direction(a, b) is None for a, b in added):
            return "L4:octilinear"
        for a, b in added:
            if not oracle.clear(
                net, la, (a[0] / NM, a[1] / NM), (b[0] / NM, b[1] / NM), width / NM
            ):
                return "L1:clear"
        own = set(mine["created"]) | set(mine["keep"])
        allowed = model.allowed_items(net, la, old, width, own)
        contact = model.contact_fn(net, la, width, allowed)
        for a, b in added:
            if contact(a, b):
                return "L2:contact"
        box = g.bbox(old + new, g.TOL)
        exclude, extra = set(allowed), []
        if spec["step"] == "corridor":
            # Members move one after another in the planner's order (seq): a member's sweep is
            # judged against the members moved before it at their new place and the ones moved
            # after it at their old place - the deformation the planner checked (design 2.3).
            for i, (other, row) in enumerate(zip(members, rows)):
                if i == j:
                    continue
                exclude |= set(other["created"]) | set(other["keep"])
                where = (
                    row["new_nm"]
                    if (row.get("seq") or 0) < (rows[j].get("seq") or 0)
                    else row["old_nm"]
                )
                extra += [
                    tuple(p) for p in where if box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]
                ]
        if g.swept_points(old, new, model.obstacle_points(la, box, exclude) + extra):
            return "L3:swept"
        rays = {p: anchor_rays(model, net, la, p, set(mine["created"])) for p in (new[0], new[-1])}
        if not g.turns_ok(old, new, rays):
            return "L5:turn"
        info = model.info(net, la)
        gbox = g.bbox(old + new, g.TUBE + NM)
        for guard in model.guards(net, la, width, gbox, si=info["si"]):
            d_old = g.guard_distance(removed, width, guard)
            d_new = g.guard_distance(added, width, guard)
            if min(d_new, guard.cap) < min(d_old, guard.cap) - g.TOL:
                return "L7:" + guard.name
        if spec["step"] in ("dekink", "gloss"):
            if not g.rule_r(old, new, rays):
                return "rule:R"
            if spec["step"] == "gloss":
                tube = g._Tube(old, g.TUBE)
                if any(not tube.covers(a, b) for a, b in added):
                    return "L8:tube"
        else:
            if g.bends(new) > g.bends(old) or g.sharp_turns(new) > g.sharp_turns(old):
                return "rule:dB"
            if g.length(new) - g.length(old) > g.CORRIDOR_SLACK + g.TOL:
                return "rule:slack"
            if max(g.polyline_distance(p, old) for p in new) > g.CORRIDOR_MAX_SHIFT + g.TOL:
                return "L8:shift"
    return None


def track_union(model, net, la):
    """Union polygon of the net's track copper on the layer (normalize guard)."""
    k = model.k
    poly = k.SHAPE_POLY_SET()
    for t in model.groups.get((net, la), ()):
        t.TransformShapeToPolygon(poly, la, 0, POLY_ERROR, k.ERROR_INSIDE)
    poly.Simplify()
    return poly


def union_xor(model, before, net, la):
    """XOR area (mm^2) between a stored union and the net's current track copper."""
    k = model.k
    x = k.SHAPE_POLY_SET(before)
    x.BooleanXor(track_union(model, net, la))
    return x.Area() / NM / NM


def board_facts(board, rules, text):
    """Native facts of a refilled board (shove.gates.facts on an in-memory board)."""
    from pnr.electrical_audit import audit_board
    from pnr.fab_profile import geometry
    from pnr.native_electrical import reference_failures
    from pnr.pad_entry import snapshot
    from pnr.shove.gates import unjustified_subwidth
    from pnr.via_coalesce import partition
    from pnr.via_in_pad import forbidden_vias

    audit = audit_board(board, rules, text)
    geo = geometry(rules)
    entries = snapshot(board, rules)
    pads = {uid(p): p.GetNetname() for f in board.GetFootprints() for p in f.Pads()}
    pairs = audit.get("pairs", [])
    return dict(
        partition=partition(board),
        entries=entries,
        entry_nets={key: pads.get(key.split(":")[0]) for key in entries},
        bad_entries=sum(not v for v in entries.values()),
        reference=reference_failures(board, rules),
        subwidth=audit["subwidth_track_count"],
        unjustified=unjustified_subwidth(board, rules, audit),
        forbidden=sorted(forbidden_vias(board, geo)) if geo.via_to_smd_pad is not None else [],
        unqualified_pairs=sum(not p.get("length_match_qualified", False) for p in pairs),
        skews={p["name"]: p.get("skew_mm") for p in pairs},
        vias_in_smd_forbidden=(audit.get("vias_in_smd_pads") or {}).get("forbidden"),
    )


# ---------------------------------------------------------------- corridor quality in the trial (design 3.1)
def board_adjacency(model, la, window):
    """X / T of the board's same-class tracks inside window (ray metric on the real copper)."""
    g = model.g
    index = model.ray_index(la)
    reach = int(g.ADJ_REACH + 2 * NM)
    wide = (window[0] - reach, window[1] - reach, window[2] + reach, window[3] + reach)
    return g.adjacency([r for r in index.near(wide) if r.key is not None], index, window=window)


def corridor_before(model0, spec):
    """Pre-apply corridor state of a spec: (Corridor, base state, after state, window, E0, X0, DS0) or None.

    The planned state's excess E1 is computed here too: the Corridor's strip check reads the
    pre-apply board's items, and apply_spec deletes the members' old tracks (board.Delete), so
    nothing may consult this Corridor once the spec is applied."""
    g = model0.g
    la = model0.lid[spec["layer"]]
    cor, _, _ = model0.corridor(la, planner=False)
    base = {cid: g.simplify(c.points) for cid, c in cor.chains.items()}
    after = dict(base)
    for m in spec["members"]:
        if m["chain"] not in base or [list(p) for p in base[m["chain"]]] != m["old_nm"]:
            return None
        after[m["chain"]] = [tuple(p) for p in m["new_nm"]]
    window = tuple(spec["window_nm"])
    some = next(iter(cor.chains.values()))
    return dict(
        cor=cor,
        base=base,
        after=after,
        window=window,
        w=some.width,
        c=some.clearance,
        E=cor.excess(base, window),
        E_after=cor.excess(after, window),
        X=board_adjacency(model0, la, window),
        DS=model0.dead_space(la, some.width, some.clearance, window),
    )


def corridor_after(model1, spec, pre):
    """dX, dE (mm^2) and window dead space after applying, recomputed on the real board (design 3.1;
    the acceptance metric is the ray metric X, E is kept as a diagnostic)."""
    la = model1.lid[spec["layer"]]
    e1 = pre["E_after"]  # computed before apply (corridor_before)
    x1 = board_adjacency(model1, la, pre["window"])
    d1 = model1.dead_space(la, pre["w"], pre["c"], pre["window"])
    return dict(
        dX_mm2=(x1["X"] - pre["X"]["X"]) / NM / NM,
        X_mm2=pre["X"]["X"] / NM / NM,
        X_new_mm2=x1["X"] / NM / NM,
        T_mm=pre["X"]["T"] / NM,
        T_new_mm=x1["T"] / NM,
        dE_mm2=(e1 - pre["E"]) / NM / NM,
        E_mm2=pre["E"] / NM / NM,
        E_new_mm2=e1 / NM / NM,
        DS=pre["DS"],
        DS_new=d1,
    )


# ---------------------------------------------------------------- metrics (design 2.4)
def metrics(model):
    """L, chain L, B by angle class, S, E (exact, survey bbox) and DS/US per layer."""
    g = model.g
    out = dict(classes={}, layers={})
    rows = defaultdict(
        lambda: dict(
            length_mm=0.0,
            chain_length_mm=0.0,
            segments=0,
            chains=0,
            bends={"45": 0, "90": 0, "135": 0, "180": 0, "other": 0},
            anchor_bends={"45": 0, "90": 0, "135": 0, "180": 0, "other": 0},
        )
    )
    corridor_legs = defaultdict(list)
    for (net, la), tracks in sorted(
        model.groups.items(), key=lambda kv: (model.lname[kv[0][1]], kv[0][0])
    ):
        info = model.info(net, la)
        cats = ["all", "mode:" + info["mode"]]
        if info["eligible"]:
            cats.append("eligible")
        if info["corridor"] and not info["si"]:
            cats.append("corridor_class")
        segs = model.segs(net, la)
        cs = model.chainset(net, la, class_width=False)
        # every degree-2 copper vertex: chain interiors plus anchors with exactly two pieces
        # (pads, vias, pin points, frozen ends; the chain-interior count omits them)
        anchor_hist = Counter()
        for anchor, legs in cs.anchor_legs.items():
            if len(legs) == 2:
                t = g.vector_turn((-legs[0][0], -legs[0][1]), legs[1])
                c = g.turn_class(t)
                if c != 0:
                    anchor_hist[str(c)] += 1
        for cat in cats:
            for key in (cat, cat + "@" + model.lname[la]):
                r = rows[key]
                r["length_mm"] += sum(math.dist(s.a, s.b) for s in segs) / NM
                r["segments"] += len(segs)
                for ch in cs.chains:
                    r["chains"] += 1
                    r["chain_length_mm"] += g.length(ch.points) / NM
                    for c, n in g.bend_histogram(ch.points).items():
                        r["bends"][str(c)] += n
                for c, n in anchor_hist.items():
                    r["anchor_bends"][c] += n
        if info["corridor"] and not info["si"]:
            for ch in cs.chains:
                cid = "%s|%s|%s" % (net, ch.pieces[0][2], ch.points[0])
                corridor_legs[la] += g.chain_legs(
                    g.CorridorChain(
                        cid, net, ch.width, info["clearance"], ch.points, info["key"], True, False
                    )
                )
    for r in rows.values():
        r["bends_total"] = sum(r["bends"].values())
        r["bends_all"] = r["bends_total"] + sum(r["anchor_bends"].values())
    out["classes"] = dict(sorted(rows.items()))
    signal_layers = sorted(
        {la for (net, la) in model.groups if model.info(net, la)["eligible"]},
        key=lambda la: model.lname[la],
    )
    w = round(model.rules.get("fab", {}).get("track_width_mm", 0.2) * NM)
    c = round(max(model.rules.get("fab", {}).get("clearance_mm", 0.15), 0) * NM)
    coupled = Counter()
    for la in signal_layers:
        tracks = [
            (t.GetNetname(), pt(t.GetStart()), pt(t.GetEnd()), t.GetWidth(), None)
            for t in model.layer_items.get(la, ())
            if t.GetClass() in ("PCB_TRACK", "PCB_ARC")
        ]
        index = g._TrackIndex(tracks)
        blocked = model.strip_blocked(la)
        e = sum(
            p["excess"] for p in g.corridor_pairs(corridor_legs[la], index, blocked, mode="exact")
        )
        eb = sum(
            p["excess"] for p in g.corridor_pairs(corridor_legs[la], index, blocked, mode="bbox")
        )
        pairs = len(g.corridor_pairs(corridor_legs[la], index, blocked, mode="exact"))
        ds = model.dead_space(la, w, c, model.box, buses=(2, 3))
        rays = model.ray_index(la)
        adj = g.adjacency([r for r in rays.items if r.key is not None], rays)
        for pair, n in adj["pairs"].items():
            coupled[pair] += n
        out["layers"][model.lname[la]] = dict(
            E_mm2=e / NM / NM,
            E_bbox_mm2=eb / NM / NM,
            corridor_pairs=pairs,
            X_mm2=adj["X"] / NM / NM,
            T_mm=adj["T"] / NM,
            free_mm2=ds["F"],
            US_mm2=ds["US"],
            DS_mm2=ds["DS"],
            A2_mm2=ds["A2"],
            A3_mm2=ds["A3"],
        )
    for name in ("E_mm2", "E_bbox_mm2", "X_mm2", "T_mm", "DS_mm2", "US_mm2", "A2_mm2", "A3_mm2"):
        out[name] = sum(v[name] for v in out["layers"].values())
    # coupled length at minimum pitch per net pair (both sides sampled, so halved), longest first
    out["coupled_mm"] = [
        dict(nets=list(k), mm=round(v / 2 / NM, 2))
        for k, v in sorted(coupled.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    ]
    out["class"] = dict(width_mm=w / NM, clearance_mm=c / NM)
    if model.cap_enabled:
        out["cross_group"] = cross_group_summary(model)
    return out


def cross_group_summary(model):
    """Cross-group parallel runs at minimum pitch (functional groups; report): the longest, the
    longest with a net the phase may move, and every pair over its allowance."""
    rows = model.cross_group_rows()
    movable = {net for (net, la) in model.groups if model.info(net, la)["eligible"]}
    pairs = sorted(((v[0], k) for k, v in rows.items()), reverse=True)
    mov = [(mm, k) for mm, k in pairs if set(k.split("|")) & movable]
    over = [
        dict(nets=k.split("|"), mm=v[0], allowed_mm=v[1])
        for k, v in sorted(rows.items())
        if v[1] is not None and v[0] > v[1] + 1e-6
    ]
    return dict(
        cap_mm=model.cross_group_mm,
        pairs=len(rows),
        max_mm=pairs[0][0] if pairs else 0.0,
        max_pair=pairs[0][1].split("|") if pairs else None,
        max_movable_mm=mov[0][0] if mov else 0.0,
        max_movable_pair=mov[0][1].split("|") if mov else None,
        over_cap=over,
        top=[dict(nets=k.split("|"), mm=mm) for mm, k in pairs[:15]],
    )


def compact_metrics(m):
    """The summary fields of :func:`metrics` (eligible-class L, B, S; E; DS/US)."""
    e = m["classes"].get("eligible") or {}
    return dict(
        length_mm=round(e.get("length_mm", 0.0), 3),
        chain_length_mm=round(e.get("chain_length_mm", 0.0), 3),
        segments=e.get("segments", 0),
        bends=e.get("bends"),
        bends_total=e.get("bends_total", 0),
        bends_all=e.get("bends_all", 0),
        X_mm2=round(m.get("X_mm2", 0.0), 3),
        T_mm=round(m.get("T_mm", 0.0), 3),
        A2_mm2=round(m.get("A2_mm2", 0.0), 3),
        A3_mm2=round(m.get("A3_mm2", 0.0), 3),
        E_mm2=round(m["E_mm2"], 3),
        E_bbox_mm2=round(m["E_bbox_mm2"], 3),
        DS_mm2=round(m["DS_mm2"], 3),
        US_mm2=round(m["US_mm2"], 3),
        coupled_mm=m.get("coupled_mm"),
        **(dict(cross_group=m["cross_group"]) if "cross_group" in m else {}),
        layers={
            k: {x: round(y, 3) if isinstance(y, float) else y for x, y in v.items()}
            for k, v in m["layers"].items()
        },
    )


# ---------------------------------------------------------------- workers (KiCad python)
def worker(args):
    import pcbnew as k

    from pnr.fab_profile import load_board

    rules = read(args.rules)
    sources = [Path(s) for s in args.annotation_source]
    drc = read(args.drc) if args.drc else None
    gloss_si = bool(args.gloss_si)
    opts = dict(
        guard_open_nets=bool(getattr(args, "guard_open_nets", False)),
        hug=not getattr(args, "no_hug", False),
        classes=getattr(args, "classes", None),
        cross_group_mm=getattr(args, "cross_group_mm", None),
        classes_from=tuple(
            x for x in (getattr(args, "classes_from", None) or "").split(",") if x.strip()
        ),
    )
    keep = []
    if args.worker == "inventory":
        b = load_board(args.board)
        b.BuildConnectivity()
        t0 = time.monotonic()
        model = Model(
            b, rules, path=args.board, sources=sources, drc=drc, gloss_si=gloss_si, **opts
        )
        if args.step in ("dekink", "gloss"):
            model.oracle  # build the shape checker before the planning clock starts
        started = time.monotonic()
        deadline = (
            started + args.deadline
        )  # wall-clock safety net only; planning work is bounded per chain
        model.deadline = deadline
        if model._oracle is not None:
            model._oracle.deadline = deadline
        skip = read(args.skip) if args.skip else {}
        if isinstance(skip, list):
            skip = dict(edits=skip)
        quiet = []
        specs, stats = plan(
            model,
            args.step,
            deadline=deadline,
            skip=set(skip.get("edits", [])),
            quiet_skip=set(skip.get("quiet", [])),
            quiet=quiet,
        )
        seconds = time.monotonic() - started
        result = dict(
            step=args.step,
            specs=specs,
            stats=stats,
            si_status=model.si_status,
            quiet=quiet,
            seconds=round(seconds, 2),
            setup_seconds=round(started - t0, 2),
            deadline_seconds=args.deadline,
            margin_seconds=round(args.deadline - seconds, 2),
            deadline_hit=bool(stats.get("deadline")),
        )
        keep.append(model)
    elif args.worker == "trial":
        transaction = read(args.spec)
        specs = transaction["specs"]
        dropped, live = {}, []
        for _round in range(4):
            b = load_board(args.board)
            b.BuildConnectivity()
            model0 = Model(
                b, rules, path=args.board, sources=sources, drc=drc, gloss_si=gloss_si, **opts
            )
            keep.extend([b, model0])
            live = []
            for s in specs:
                if s["id"] in dropped:
                    continue
                why = precheck(model0, s, gloss_si)
                if why:
                    dropped[s["id"]] = why
                else:
                    live.append(s)
            if not live:
                break
            pre = {}
            cap_before = {}
            if model0.cap_enabled:  # C(net, *) of every net a copper edit touches, before it
                for s in live:
                    if s["step"] != "normalize":
                        for n in s["nets"]:
                            cap_before[n] = dict(model0.net_totals(n))
            for s in live:  # pre-apply state (the edit mutates wrappers in place)
                if s["step"] == "normalize":
                    pre[s["id"]] = track_union(model0, s["net"], model0.lid[s["layer"]])
                elif s["step"] == "corridor":
                    pre[s["id"]] = corridor_before(model0, s)
                else:  # report-only: same-class adjacency in the edit's window
                    pre[s["id"]] = board_adjacency(model0, model0.lid[s["layer"]], tuple(s["box"]))
            applied = {}
            for s in live:
                applied[s["id"]], alive = apply_spec(model0, s)
                keep.extend(alive)
            b.BuildConnectivity()
            model1 = Model(b, rules, path=args.board, base=model0)
            keep.append(model1)
            bad = {}
            for s in live:
                if s["step"] == "normalize":
                    xor = union_xor(model1, pre[s["id"]], s["net"], model1.lid[s["layer"]])
                    applied[s["id"]]["union_xor_mm2"] = xor
                    if xor > UNION_TOLERANCE_MM2:
                        bad[s["id"]] = "N:union"
                    continue
                why = recheck(model1, s, applied[s["id"]])
                if why is None and s["step"] == "corridor":
                    if pre[s["id"]] is None:
                        why = "stale:corridor"
                    else:
                        q = corridor_after(model1, s, pre[s["id"]])
                        applied[s["id"]]["quality"] = q
                        if q["dX_mm2"] > -model1.g.ADJ_MIN_GAIN / NM / NM:
                            why = "rule:dX"
                        elif q["DS_new"]["DS"] > q["DS"]["DS"] + DS_TOLERANCE_MM2:
                            why = "rule:dDS"
                if why is None and s["step"] in ("dekink", "gloss"):
                    x1 = board_adjacency(model1, model1.lid[s["layer"]], tuple(s["box"]))
                    applied[s["id"]]["adjacency"] = dict(
                        dX_mm2=(x1["X"] - pre[s["id"]]["X"]) / NM / NM,
                        dT_mm=(x1["T"] - pre[s["id"]]["T"]) / NM,
                    )
                if why:
                    bad[s["id"]] = why
            if cap_before:
                # functional-group cap on the applied board (all edits of the batch together):
                # C_after <= max(allowed, C_before) for every cross-group pair of a touched net
                for net in sorted(cap_before):
                    after = model1.net_totals(net)
                    for other in sorted(set(after) | set(cap_before[net])):
                        limit = model1.allowed_nm(net, other)
                        if limit is None:
                            continue
                        c0, c1 = cap_before[net].get(other, 0.0), after.get(other, 0.0)
                        if c1 > max(limit, c0) + model1.g.CAP_TOL:
                            for s in live:
                                if (
                                    s["step"] != "normalize"
                                    and {net, other} & set(s["nets"])
                                    and s["id"] not in bad
                                ):
                                    bad[s["id"]] = "cap:%s|%s" % tuple(sorted((net, other)))
            if not bad:
                break
            dropped.update(bad)
        else:
            live = []
        result = dict(dropped=dropped, applied={})
        if live:
            k.ZONE_FILLER(b).Fill(b.Zones())
            b.BuildConnectivity()
            k.SaveBoard(str(args.out), b)
            result.update(
                applied={s["id"]: applied[s["id"]] for s in live},
                accepted_specs=[s["id"] for s in live],
                facts=board_facts(b, rules, Path(args.out).read_text()),
            )
            if model1.cap_enabled:
                result["facts"]["cross_group"] = model1.cross_group_rows()
    elif args.worker == "facts":
        b = load_board(args.board)
        k.ZONE_FILLER(b).Fill(b.Zones())
        b.BuildConnectivity()
        result = board_facts(b, rules, Path(args.board).read_text())
        keep.append(b)
        if opts["classes"] or opts["classes_from"]:
            model = Model(
                b, rules, path=args.board, sources=sources, drc=drc, gloss_si=gloss_si, **opts
            )
            result["cross_group"] = model.cross_group_rows()
            keep.append(model)
    elif args.worker == "metrics":
        b = load_board(args.board)
        b.BuildConnectivity()
        model = Model(
            b, rules, path=args.board, sources=sources, drc=drc, gloss_si=gloss_si, **opts
        )
        result = dict(metrics=metrics(model))
        if args.full:
            from pnr.electrical_audit import audit_board
            from pnr.pad_entry import repair

            text = Path(args.board).read_text()
            audit = audit_board(b, rules, text)
            from pnr.shove.gates import unjustified_subwidth

            result["audit"] = dict(
                subwidth=audit["subwidth_track_count"],
                unjustified=len(unjustified_subwidth(b, rules, audit)),
                reference_failures=len(audit.get("reference_failures") or []),
                unqualified_pairs=sum(
                    not p.get("length_match_qualified", False) for p in audit["pairs"]
                ),
                skews={p["name"]: p.get("skew_mm") for p in audit["pairs"]},
                vias_in_smd_forbidden=(audit.get("vias_in_smd_pads") or {}).get("forbidden"),
            )
            scratch = load_board(args.board)  # pad-entry repair on a scratch copy, never saved
            scratch.BuildConnectivity()
            entries = repair(scratch, rules)
            result["pad_entry"] = dict(blocked=len(entries["blocked"]), added=len(entries["added"]))
            keep.append(scratch)
        keep.extend([model, b])
    else:
        raise ValueError(args.worker)
    Path(args.report).write_text(json.dumps(result, indent=1, default=str) + "\n")
    # Release borrowed wrappers while their native boards are still alive (KiCad 10 teardown).
    keep.clear()


# ---------------------------------------------------------------- controller (design 3.2-4.2, 5.4)
def run_kicad(cmd, log):
    """Run one KiCad worker under its deadline (pnr.proc), output to ``log``.

    KiCad's Python occasionally dies by signal at teardown and an identical rerun
    succeeds, so a signal exit is retried once; a kill at the worker deadline is not
    (a wedge, not a crash). Raises CalledProcessError on failure (DeadlineExceeded when
    the deadline killed it)."""
    from pnr.proc import DeadlineExceeded, run_status, worker_timeout

    with Path(log).open("w") as out:
        for attempt in (0, 1):
            code, timed_out = run_status(
                cmd, timeout=worker_timeout(cmd), stdout=out, stderr=subprocess.STDOUT
            )
            if timed_out:
                raise DeadlineExceeded(cmd, worker_timeout(cmd))
            if code >= 0 or attempt:
                break
            out.write("\n[retry after signal %d]\n" % -code)
            out.flush()
    if code:
        raise subprocess.CalledProcessError(code, cmd)


def copy_board(source, target):
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    for ext in (".kicad_pro", ".kicad_dru"):
        if (
            source.with_suffix(ext).exists()
            and source.with_suffix(ext).resolve() != target.with_suffix(ext).resolve()
        ):
            shutil.copyfile(source.with_suffix(ext), target.with_suffix(ext))
    table = source.parent / "fp-lib-table"
    if table.exists() and source.parent.resolve() != target.parent.resolve():
        (target.parent / "fp-lib-table").write_text(
            table.read_text().replace("${KIPRJMOD}", str(source.parent.resolve()))
        )


def violation_keys(drc):
    from pnr.via_coalesce import violation_keys as keys

    return keys(drc)


def dangling(drc):
    return {
        i["uuid"]
        for v in drc["violations"]
        if v["type"] in ("track_dangling", "via_dangling")
        for i in v.get("items", [])
    }


def objective(drc, facts):
    return [
        len(drc["violations"]),
        facts["bad_entries"],
        len(facts["reference"] or []),
        facts["subwidth"],
        facts["unqualified_pairs"],
        len(drc["unconnected_items"]),
    ]


def compare(before_drc, before, after_drc, after, *, end=False):
    """Transaction (design 3.2 T2-T4) or phase-end (4.2) gate. Returns (ok, reasons, new_keys)."""
    from pnr.connectivity_restore import lost_connections
    from pnr.via_coalesce import acceptable

    reasons = []
    lost_entries = [
        i for i, v in before["entries"].items() if v and not after["entries"].get(i, False)
    ]
    new_bad = [i for i, v in after["entries"].items() if not v and i not in before["entries"]]
    same_partition = sorted(map(sorted, before["partition"])) == sorted(
        map(sorted, after["partition"])
    )
    lost = lost_connections(before["partition"], after["partition"])
    checks = dict(preserved=same_partition, lost_pad_entries=lost_entries)
    new_keys = Counter(violation_keys(after_drc)) - Counter(violation_keys(before_drc))
    if not same_partition:
        reasons.append("partition")
    if lost:
        reasons.append("lost_connections")
    if lost_entries:
        reasons.append("lost_pad_entries")
    if new_bad:
        reasons.append("new_bad_entries")
    ref_before = {json.dumps(r, sort_keys=True) for r in before["reference"] or []}
    if any(json.dumps(r, sort_keys=True) not in ref_before for r in after["reference"] or []):
        reasons.append("reference")
    if set(after["forbidden"]) - set(before["forbidden"]):
        reasons.append("forbidden_vias")
    if after["subwidth"] > before["subwidth"]:
        reasons.append("subwidth")
    if len(after["unjustified"]) > len(before["unjustified"]):
        reasons.append("unjustified_subwidth")
    if after["unqualified_pairs"] > before["unqualified_pairs"]:
        reasons.append("unqualified_pairs")
    for name, skew in after["skews"].items():
        old = before["skews"].get(name)
        if old is not None and (skew is None or skew > old + 1e-9):
            reasons.append("skew:" + name)
    if new_keys:
        reasons.append("drc:new_violation")
    if len(after_drc["unconnected_items"]) > len(before_drc["unconnected_items"]):
        reasons.append("drc:opens")
    types0 = Counter(v["type"] for v in before_drc["violations"])
    types1 = Counter(v["type"] for v in after_drc["violations"])
    if any(types1[t] > types0[t] for t in ("track_dangling", "via_dangling")):
        reasons.append("drc:dangling")
    if not acceptable(before_drc, after_drc, checks) and not reasons:
        reasons.append("drc:acceptable")
    o0, o1 = objective(before_drc, before), objective(after_drc, after)
    if any(x > y for x, y in zip(o1, o0)):
        reasons.append("objective")
    if cross_group_increase(before.get("cross_group"), after.get("cross_group")):
        reasons.append("cross_group_cap")
    return not reasons, reasons, new_keys


def cross_group_increase(before, after, tol_mm=1e-5):
    """Pairs whose cross-group run rose above max(allowed, before) (functional-group cap; facts
    'cross_group' rows {'a|b': [mm, allowed mm]}, present only with a groups file)."""
    if after is None:
        return []
    before = before or {}
    out = []
    for pair, (mm, allowed) in sorted(after.items()):
        if allowed is None:
            continue
        prev = (before.get(pair) or [0.0])[0]
        if mm > max(allowed, prev) + tol_mm:
            out.append(pair)
    return out


class GlossPass:
    """One pass: steps -> transactions -> end gate. Workers run strictly one at a time."""

    def __init__(self, a, conf):
        self.a, self.conf = a, conf
        self.work = Path(a.work_dir).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.kicad_python = a.kicad_python or os.environ.get("PNR_KICAD_PYTHON") or sys.executable
        self.rules = Path(a.rules).resolve()
        self.started = time.monotonic()
        self.budget_end = self.started + (a.seconds if a.seconds else conf.seconds)
        self.events, self.transactions = [], []
        self.blacklist = set()
        self.seconds = Counter()
        self.counter = 0
        self.inject = []  # test hook (Python API): specs that bypass the planner
        self.quiet = {}  # step -> {chain signature: (layer, box, transaction counter)}
        self.changed = []  # (transaction counter, layer, box) of every accepted edit
        self.sweep = 0  # 0: the step list; 1: the final gloss + corridor sweep
        self.planning = []  # one row per inventory: seconds, safety-deadline margin and hits
        self.base_drc_path = None  # B_0's DRC report (set by run)
        self.step_delta = {}  # summed delta of the accepted transactions of the current round

    # -- processes
    def run_worker(self, mode, board, report, extra=()):
        cmd = [
            self.kicad_python,
            "-m",
            "pnr.gloss",
            str(board),
            "--worker",
            mode,
            "--rules",
            str(self.rules),
            "--report",
            str(report),
        ]
        for s in self.a.annotation_source:
            cmd += ["--annotation-source", str(Path(s).resolve())]
        if self.conf.si:
            cmd += ["--gloss-si"]
        if getattr(self.a, "guard_open_nets", False):
            cmd += ["--guard-open-nets"]
        if not getattr(self.conf, "hug", True):
            cmd += ["--no-hug"]
        cmd += group_args(self.conf)
        cmd += [str(x) for x in extra]
        run_kicad(cmd, Path(report).with_suffix(".log"))
        return read(report)

    def drc(self, board):
        from pnr.native_drc import run_drc

        return run_drc(
            self.a.kicad_cli, Path(board), Path(board).with_suffix(".drc.json"), final=True
        )

    def emit(self, kind, board=None, data=None):
        from pnr.live import emit

        emit(kind, board=board, data=data)

    def left(self):
        return self.budget_end - time.monotonic()

    # -- transactions
    def transaction(self, step, specs):
        """Apply one batch to the checkpoint; returns (accepted, record)."""
        from pnr.profile import span

        self.counter += 1
        folder = self.work / "trials" / ("%03d" % self.counter)
        folder.mkdir(parents=True, exist_ok=True)
        save(folder / "spec.json", dict(step=step, specs=specs))
        trial = folder / "trial.kicad_pcb"
        record = dict(
            transaction=self.counter,
            step=step,
            sweep=self.sweep,
            edits=[s["id"] for s in specs],
            nets=sorted({n for s in specs for n in s["nets"]}),
            accepted=False,
            folder=str(folder),
        )
        t0 = time.monotonic()
        try:
            with span("gloss_trial"):
                result = self.run_worker(
                    "trial",
                    self.current,
                    folder / "trial.json",
                    ["--spec", folder / "spec.json", "--out", trial, "--drc", self.base_drc_path],
                )
        except subprocess.CalledProcessError as error:
            record.update(reason="worker_error", returncode=error.returncode)
            self.seconds["trial"] += time.monotonic() - t0
            return False, record, {}
        record["dropped"] = result["dropped"]
        if not result.get("accepted_specs"):
            record["reason"] = "all_dropped"
            self.seconds["trial"] += time.monotonic() - t0
            return False, record, result
        shutil.copyfile(self.current.with_suffix(".kicad_pro"), trial.with_suffix(".kicad_pro"))
        table = self.current.parent / "fp-lib-table"
        if table.exists():
            shutil.copyfile(table, folder / "fp-lib-table")
        self.seconds["trial"] += time.monotonic() - t0
        t1 = time.monotonic()
        with span("gloss_drc"):
            after_drc = self.drc(trial)
        self.seconds["drc"] += time.monotonic() - t1
        facts = result["facts"]
        save(folder / "facts.json", facts)
        ok, reasons, new_keys = compare(self.current_drc, self.current_facts, after_drc, facts)
        accepted_specs = [s for s in specs if s["id"] in result["accepted_specs"]]
        gain = 0.0
        for s in accepted_specs:
            gain += s["gain"]
        if ok and gain <= 0:
            ok, reasons = False, ["T5:no_gain"]
        record.update(
            accepted=ok,
            reasons=reasons,
            applied_edits=result["accepted_specs"],
            opens=len(after_drc["unconnected_items"]),
            violations=len(after_drc["violations"]),
            objective=objective(after_drc, facts),
            delta=self.delta(accepted_specs, result.get("applied", {})),
        )
        if ok:
            self.history.append(
                dict(
                    board=trial,
                    drc=after_drc,
                    facts=facts,
                    specs=accepted_specs,
                    applied=result["applied"],
                    transaction=self.counter,
                )
            )
            self.changed += [(self.counter, s["layer"], s["box"]) for s in accepted_specs]
            self.current, self.current_drc, self.current_facts = trial, after_drc, facts
        else:
            record["attribution"] = self.attribute(result, new_keys, after_drc, facts)
        return ok, record, result

    def delta(self, specs, applied=None):
        """Planned change of the accepted edits: length, bends (end-anchor turns included as
        bends_all), E, and the same-class adjacency X measured on the applied board (dekink and
        gloss: windowed per edit; corridor: the trial's quality window)."""
        out = Counter()
        applied = applied or {}
        for s in specs:
            m = s.get("metrics") or {}
            ap = applied.get(s["id"], {})
            if s["step"] in ("dekink", "gloss"):
                out["length_mm"] += m.get("L_new", 0) - m.get("L", 0)
                out["bends"] += m.get("B_new", 0) - m.get("B", 0)
                out["bends_all"] += m.get("B_all_new", m.get("B_new", 0)) - m.get(
                    "B_all", m.get("B", 0)
                )
                out["adjacency_mm2"] += (ap.get("adjacency") or {}).get("dX_mm2", 0.0)
            elif s["step"] == "corridor":
                out["excess_mm2"] += s.get("dE_mm2", 0)
                out["length_mm"] += sum(x.get("dL", 0) for x in s.get("members") or [])
                out["adjacency_mm2"] += (ap.get("quality") or {}).get(
                    "dX_mm2", s.get("dX_mm2", 0.0)
                )
            else:
                out["segments"] -= len(s.get("delete", []))
        return {k: round(v, 6) for k, v in out.items()}

    def attribute(self, result, new_keys, after_drc, facts):
        """Edits named by new violation keys / new dangling items (owned uuids, else the nets in the
        native item descriptions) and nets of lost or newly bad pad entries (design 3.4)."""
        import re

        owner = {}
        for sid, applied in result.get("applied", {}).items():
            for u in applied.get("created", []) + applied.get("modified", []):
                owner[u] = sid
        named, nets = set(), set()
        fresh = set(new_keys)
        new_dangling = dangling(after_drc) - dangling(self.current_drc)
        for v in after_drc["violations"]:
            key = (v["type"], tuple(sorted(i["uuid"] for i in v.get("items", []))))
            dang = v["type"] in ("track_dangling", "via_dangling") and any(
                i["uuid"] in new_dangling for i in v.get("items", [])
            )
            if key not in fresh and not dang:
                continue
            for i in v.get("items", []):
                if i["uuid"] in owner:
                    named.add(owner[i["uuid"]])
                nets.update(re.findall(r"\[([^\]]+)\]", i.get("description", "")))
        nets |= {
            facts["entry_nets"].get(key)
            for key, v in facts["entries"].items()
            if not v and self.current_facts["entries"].get(key, True)
        }
        nets |= {
            self.current_facts["entry_nets"].get(key)
            for key, v in self.current_facts["entries"].items()
            if v and not facts["entries"].get(key, False)
        }
        return dict(edits=sorted(named), nets=sorted(n for n in nets if n))

    def process(self, step, specs):
        """Run batches with drop/halve/blacklist (design 3.4). Returns accepted edit count."""
        limit = (
            4
            if step == "corridor"
            else (len(specs) or 1) if step == "normalize" else self.conf.batch
        )
        queue = (
            [(b, 0) for b in batch_specs(specs, limit)]
            if step != "normalize"
            else [(list(specs), 0)]
        )
        accepted = 0
        while queue:
            if self.left() <= 0:
                self.stop = "time_budget"
                break
            if self.counter >= self.conf.max_transactions:
                self.stop = "max_transactions"
                break
            batch, depth = queue.pop(0)
            ok, record, result = self.transaction(step, batch)
            self.transactions.append(record)
            self.emit(
                "geometry_result",
                board=self.current if ok else None,
                data=dict(
                    phase="gloss",
                    label=self.a.label,
                    transaction=record["transaction"],
                    step=step,
                    nets=record["nets"],
                    edits=len(batch),
                    accepted=ok,
                    reason=(
                        None
                        if ok
                        else (record.get("reason") or ",".join(record.get("reasons", [])))
                    ),
                    delta=record.get("delta"),
                ),
            )
            print(
                json.dumps(
                    {
                        k: record.get(k)
                        for k in ("transaction", "step", "accepted", "reasons", "reason")
                    }
                    | dict(edits=len(batch))
                ),
                flush=True,
            )
            for sid, why in (record.get("dropped") or {}).items():
                if not why.startswith("stale"):
                    self.blacklist.add(sid)
            if ok:
                accepted += len(record["applied_edits"])
                for key, value in (record.get("delta") or {}).items():
                    self.step_delta[key] = self.step_delta.get(key, 0.0) + value
                continue
            live = [s for s in batch if s["id"] not in (record.get("dropped") or {})]
            if not live:
                continue
            if len(live) == 1:
                self.blacklist.add(live[0]["id"])
                continue
            named = set((record.get("attribution") or {}).get("edits", []))
            nets = set((record.get("attribution") or {}).get("nets", []))
            culprits = [s for s in live if s["id"] in named] or [
                s for s in live if set(s["nets"]) & nets
            ]
            if culprits and len(culprits) < len(live):
                for s in culprits:
                    self.blacklist.add(s["id"])
                queue.insert(0, ([s for s in live if s not in culprits], depth))
                continue
            if depth >= 4:
                for s in live:
                    self.blacklist.add(s["id"])
                continue
            if step == "normalize":
                nets = sorted({n for s in live for n in s["nets"]})
                half = set(nets[: len(nets) // 2])
                parts = [
                    [s for s in live if set(s["nets"]) <= half],
                    [s for s in live if not set(s["nets"]) <= half],
                ]
            else:
                parts = [live[: len(live) // 2], live[len(live) // 2 :]]
            for part in reversed([p for p in parts if p]):
                queue.insert(0, (part, depth + 1))
        return accepted

    def inventory(self, step, round_):
        from pnr.profile import span

        folder = self.work / "inventory"
        folder.mkdir(exist_ok=True)
        name = (
            "%s-%d" % (step, round_)
            if not self.sweep
            else "%s-s%d-%d" % (step, self.sweep + 1, round_)
        )
        skip = folder / (name + ".skip.json")
        quiet = sorted(
            sig
            for sig, (layer, box, at) in self.quiet.get(step, {}).items()
            if not any(
                n > at and lay == layer and boxes_overlap(b, box) for n, lay, b in self.changed
            )
        )
        save(skip, dict(edits=sorted(self.blacklist), quiet=quiet))
        t0 = time.monotonic()
        # The planning result must not depend on wall-clock speed: chain work is bounded by
        # deterministic budgets and this deadline is only a safety net (hits are reported).
        deadline = INVENTORY_SAFETY_SECONDS
        with span("gloss_inventory"):
            result = self.run_worker(
                "inventory",
                self.current,
                folder / (name + ".json"),
                [
                    "--step",
                    step,
                    "--deadline",
                    deadline,
                    "--skip",
                    skip,
                    "--drc",
                    self.base_drc_path,
                ],
            )
        self.seconds["inventory"] += time.monotonic() - t0
        self.planning.append(
            dict(
                step=step,
                sweep=self.sweep + 1,
                round=round_,
                seconds=result.get("seconds"),
                margin_seconds=result.get("margin_seconds"),
                deadline_hit=bool(result.get("deadline_hit")),
                budget_refusals=(result.get("stats", {}).get("calls") or {}).get(
                    "search_refused", 0
                ),
            )
        )
        at = self.counter
        bucket = self.quiet.setdefault(step, {})
        for sig, layer, box in result.get("quiet", []):
            bucket[sig] = (layer, box, at)
        return result

    def run(self):
        a, conf = self.a, self.conf
        source = Path(a.board).resolve()
        self.current = self.work / "checkpoint-000" / "board.kicad_pcb"
        copy_board(source, self.current)
        # B_0's DRC report: every inventory and trial of the pass derives its frozen (A7) and
        # open-net guard (G2) sets from this one report (never a later checkpoint's).
        self.base_drc_path = self.current.with_suffix(".drc.json")
        self.history = []
        self.stop = None
        self.sweep = 0
        self.planning = []
        self.emit(
            "phase_start",
            board=self.current,
            data=dict(
                phase="gloss",
                label=a.label,
                budget_seconds=round(self.budget_end - self.started, 1),
                steps=list(conf.steps),
            ),
        )
        t0 = time.monotonic()
        self.current_drc = self.drc(self.current)
        self.current_facts = self.run_worker(
            "facts", self.current, self.work / "checkpoint-000" / "facts.json"
        )
        self.seconds["initial"] += time.monotonic() - t0
        self.base = dict(board=self.current, drc=self.current_drc, facts=self.current_facts)
        self.metrics_before = self.measure_board(self.current, "metrics-before.json")
        steps_report = {}
        status = "ok"
        plan_steps = [(s, 0) for s in conf.steps]
        if getattr(conf, "sweeps", 1) > 1 and "gloss" in conf.steps and "corridor" in conf.steps:
            # a second gloss + corridor sweep: chains hug what the corridor step packed, and the
            # corridor step packs what the adjacency-aware gloss straightened
            plan_steps += [("gloss", 1), ("corridor", 1)]
        for step, sweep in plan_steps:
            self.sweep = sweep
            t_step = time.monotonic()
            rows = []
            for round_ in range(3 if sweep == 0 else 2):
                if self.left() <= 0 or self.counter >= conf.max_transactions:
                    break
                try:
                    inv = self.inventory(step, round_)
                except subprocess.CalledProcessError as error:
                    rows.append(
                        dict(round=round_, status="inventory_error", returncode=error.returncode)
                    )
                    break
                if inv.get("stats", {}).get("status", "").startswith("si_unresolved") or str(
                    inv.get("si_status", "")
                ).startswith("si_unresolved"):
                    status = "si_unresolved"
                    rows.append(dict(round=round_, status=status, si_status=inv.get("si_status")))
                    break
                specs = [s for s in inv["specs"] if s["id"] not in self.blacklist]
                if round_ == 0 and sweep == 0 and self.inject:
                    specs = [s for s in self.inject if s["step"] == step] + specs
                row = dict(
                    round=round_,
                    proposed=len(specs),
                    stats=inv["stats"],
                    seconds=inv.get("seconds"),
                )
                if not specs:
                    rows.append(row)
                    break
                self.step_delta = {}
                row["accepted_edits"] = self.process(step, specs)
                row["delta"] = {k: round(v, 4) for k, v in self.step_delta.items()}
                rows.append(row)
                if not row["accepted_edits"]:
                    break
            if status != "ok":
                break
            steps_report[step if not sweep else "%s#%d" % (step, sweep + 1)] = dict(
                rounds=rows, seconds=round(time.monotonic() - t_step, 2)
            )
            self.seconds[step] += time.monotonic() - t_step
        end = self.end_gate()
        return self.finish(source, steps_report, end, status)

    def measure_board(self, board, name):
        """--metrics: L, B, S, E, DS/US of a board (report only; failures are recorded)."""
        if not getattr(self.a, "metrics", False):
            return None
        t0 = time.monotonic()
        try:
            m = self.run_worker("metrics", board, self.work / name)["metrics"]
            return compact_metrics(m)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
            return dict(error=repr(error))
        finally:
            self.seconds["metrics"] += time.monotonic() - t0

    # -- phase end (design 4.2)
    def end_check(self, entry):
        """G_end(B_0, B_k): strongest headless checks, against the pre-pass checkpoint."""
        drc = self.drc(entry["board"])
        ok, reasons, new_keys = compare(
            self.base["drc"], self.base["facts"], drc, entry["facts"], end=True
        )
        if ok and self.conf.si and os.environ.get("PNR_SI") == "1":
            si_nets = set()
            for spec in entry.get("specs_so_far", []):
                si_nets.update(spec["nets"])
            ok, why = self.si_check(entry["board"], si_nets)
            if not ok:
                reasons.append(why)
        return not reasons, reasons

    def si_check(self, board, nets):
        """PNR_SI=1 and PNR_GLOSS_SI=1: no more layout-caused SI failures than B_0."""
        rules = read(self.rules)
        intents = rules.get("si_intents") or []
        touched = [i for i in intents if set(i.get("nets", [])) & nets]
        if not touched:
            return True, None
        try:
            before = self.si_fields(self.base["board"], rules)
            after = self.si_fields(board, rules)
            b0, b1 = before.get("si_layout_failures"), after.get("si_layout_failures")
            if b1 is None or (b0 is not None and b1 > b0):
                return False, "si_layout_failures"
            return True, None
        except Exception as error:
            return False, "si_error:%r" % (error,)

    def si_fields(self, board, rules):
        """PNR_SI post-route side fields of a board, cached per board file."""
        from pnr.si.report import candidate_side_fields

        if not hasattr(self, "_si"):
            self._si = {}
        key = str(board)
        if key not in self._si:
            folder = self.work / "si" / ("%02d" % len(self._si))
            folder.mkdir(parents=True, exist_ok=True)
            self._si[key] = candidate_side_fields(Path(board), rules, out_dir=folder)
            save(folder / "side-fields.json", dict(board=key, fields=self._si[key]))
        return self._si[key]

    def end_gate(self):
        """G_end(B_0, B_n); on failure bisect the checkpoints, drop the first failing transaction,
        replay the later ones (each re-gated), and re-check; after 3 drop rounds revert to B_0."""
        from pnr.profile import span

        t0 = time.monotonic()
        report = dict(passed=True, probes=[], dropped=[], reverted=False, rounds=0)
        with span("gloss_end_gate"):
            while self.history:
                specs = []
                for h in self.history:
                    specs += h["specs"]
                    h["specs_so_far"] = list(specs)
                ok, reasons = self.end_check(self.history[-1])
                report["probes"].append(dict(k=len(self.history), ok=ok, reasons=reasons))
                if ok:
                    report["passed"] = True
                    break
                report["passed"] = False
                if report["rounds"] >= 3:
                    report["reverted"] = True
                    self.history = []
                    self.current, self.current_drc, self.current_facts = (
                        self.base["board"],
                        self.base["drc"],
                        self.base["facts"],
                    )
                    break
                lo, hi = 1, len(self.history)  # smallest failing k (1-based); k = n fails
                while lo < hi:
                    mid = (lo + hi) // 2
                    ok_mid, why = self.end_check(self.history[mid - 1])
                    report["probes"].append(dict(k=mid, ok=ok_mid, reasons=why))
                    if ok_mid:
                        lo = mid + 1
                    else:
                        hi = mid
                bad = self.history[lo - 1]
                report["dropped"].append(
                    dict(transaction=bad["transaction"], k=lo, reasons=reasons)
                )
                report["rounds"] += 1
                for s in bad["specs"]:
                    self.blacklist.add(s["id"])
                later = self.history[lo:]
                self.history = self.history[: lo - 1]
                h = self.history[-1] if self.history else self.base
                self.current, self.current_drc, self.current_facts = (
                    h["board"],
                    h["drc"],
                    h["facts"],
                )
                for h in later:  # replay k+1..n onto B_{k-1}, each re-gated
                    ok_r, record, _ = self.transaction("replay", list(h["specs"]))
                    record["replay_of"] = h["transaction"]
                    self.transactions.append(record)
        if report["rounds"] and not self.history:
            report["reverted"] = True  # every transaction dropped: output is B_0
        self.seconds["end_gate"] += time.monotonic() - t0
        save(self.work / "end-gate.json", report)
        return report

    def finish(self, source, steps_report, end, status):
        a = self.a
        out = Path(a.out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        final = self.history[-1]["board"] if self.history else None
        if final is None:
            shutil.copyfile(source, out)  # byte-identical B_0
            for ext in (".kicad_pro", ".kicad_dru"):
                if source.with_suffix(ext).exists() and source.with_suffix(ext) != out.with_suffix(
                    ext
                ):
                    shutil.copyfile(source.with_suffix(ext), out.with_suffix(ext))
        else:
            copy_board(final, out)
            if final.with_suffix(".kicad_dru").exists():
                shutil.copyfile(final.with_suffix(".kicad_dru"), out.with_suffix(".kicad_dru"))
        table = source.parent / "fp-lib-table"
        if (
            table.exists()
            and out.parent != source.parent
            and not (out.parent / "fp-lib-table").exists()
        ):
            (out.parent / "fp-lib-table").write_text(
                table.read_text().replace("${KIPRJMOD}", str(source.parent))
            )
        kept = self.history
        edits_by_step = Counter()
        created = []
        inverse = []
        for h in kept:
            for s in h["specs"]:
                edits_by_step[s["step"]] += 1
                ap = h["applied"].get(s["id"], {})
                created += ap.get("created", [])
                inverse.append(
                    dict(
                        id=s["id"],
                        step=s["step"],
                        net=s.get("net"),
                        layer=s["layer"],
                        created=ap.get("created", []),
                        old_segments_nm=s.get("old_segments_nm")
                        or [m["old_segments_nm"] for m in s.get("members") or []],
                    )
                )
        rejections = Counter()
        cap_drops = Counter()
        for t in self.transactions:
            if not t["accepted"]:
                for r in t.get("reasons") or [t.get("reason", "unknown")]:
                    rejections[r] += 1
            for why in (t.get("dropped") or {}).values():
                if why.startswith("cap:"):
                    cap_drops[why[4:]] += 1
                    why = "cap"
                rejections["dropped:" + why] += 1
        summary = dict(
            label=a.label,
            status=status,
            source_sha256=sha256(source),
            output_sha256=sha256(out),
            steps=list(self.conf.steps),
            proposed_transactions=len(self.transactions),
            accepted_transactions=len(kept),
            rejected_transactions=sum(not t["accepted"] for t in self.transactions),
            split_transactions=sum(
                1 for t in self.transactions if not t["accepted"] and len(t["edits"]) > 1
            ),
            edits_by_step=dict(edits_by_step),
            rejections_by_check=dict(rejections),
            end_gate=end,
            stop=self.stop,
            seconds={k: round(v, 2) for k, v in self.seconds.items()},
            wall_seconds=round(time.monotonic() - self.started, 2),
            budget=dict(
                seconds=round(self.budget_end - self.started, 1),
                max_transactions=self.conf.max_transactions,
                batch=self.conf.batch,
            ),
            objective_before=objective(self.base["drc"], self.base["facts"]),
            objective_after=(
                objective(kept[-1]["drc"], kept[-1]["facts"])
                if kept
                else objective(self.base["drc"], self.base["facts"])
            ),
            planning=dict(
                inventories=len(self.planning),
                deadline_hits=sum(p["deadline_hit"] for p in self.planning),
                max_seconds=max([p["seconds"] or 0 for p in self.planning] or [0]),
                min_margin_seconds=(
                    min(
                        [
                            p["margin_seconds"]
                            for p in self.planning
                            if p["margin_seconds"] is not None
                        ]
                        or [None]
                    )
                    if self.planning
                    else None
                ),
                search_budget_refusals=sum(p["budget_refusals"] or 0 for p in self.planning),
                rows=self.planning,
            ),
        )
        if getattr(self.conf, "classes", None) or getattr(self.conf, "classes_from", ()):
            summary["cross_group"] = dict(
                groups=str(self.conf.classes) if self.conf.classes else None,
                derived_from=list(getattr(self.conf, "classes_from", ())),
                cap_mm=self.conf.cross_group_mm,
                trial_drops=dict(sorted(cap_drops.items())),
            )
        summary.update(
            metrics_before=self.metrics_before,
            metrics_after=(
                self.measure_board(out, "metrics-after.json") if kept else self.metrics_before
            ),
        )
        by_step = {}
        for (
            step,
            rep_,
        ) in steps_report.items():  # accepted-edit deltas per step (gloss regressions visible)
            total = Counter()
            for row in rep_.get("rounds", []):
                for key, value in (row.get("delta") or {}).items():
                    total[key] += value
            by_step[step] = {k: round(v, 4) for k, v in total.items()}
        summary["delta_by_step"] = by_step
        result = dict(
            summary,
            steps_report=steps_report,
            transactions=self.transactions,
            created_uuids=created,
            inverse_specs=inverse,
        )
        save(a.report, result)
        return result


def measure(a):
    """--measure BOARD...: cold DRC, metrics/audit/pad-entry worker per board, sequentially."""
    rows = {}
    kicad_python = a.kicad_python or os.environ.get("PNR_KICAD_PYTHON") or sys.executable
    for board in a.measure:
        board = Path(board).resolve()
        from pnr.native_drc import run_drc

        t0 = time.monotonic()
        drc = run_drc(a.kicad_cli, board, board.with_suffix(".measure.drc.json"), final=True)
        report = board.with_suffix(".metrics.json")
        cmd = [
            kicad_python,
            "-m",
            "pnr.gloss",
            str(board),
            "--worker",
            "metrics",
            "--full",
            "--rules",
            str(Path(a.rules).resolve()),
            "--report",
            str(report),
        ]
        if getattr(a, "classes", None):  # report the functional-group runs too
            cmd += ["--classes", str(Path(a.classes).resolve())]
        if getattr(a, "classes_from", None):
            cmd += ["--classes-from", a.classes_from]
        if (getattr(a, "classes", None) or getattr(a, "classes_from", None)) and getattr(
            a, "cross_group_mm", None
        ) is not None:
            cmd += ["--cross-group-mm", repr(float(a.cross_group_mm))]
        for s in a.annotation_source:
            cmd += ["--annotation-source", str(Path(s).resolve())]
        run_kicad(cmd, report.with_suffix(".log"))
        m = read(report)
        audit = m["audit"]
        si = None
        if a.si and os.environ.get("PNR_SI") == "1":
            from pnr.si.report import candidate_side_fields

            si = candidate_side_fields(
                board, read(a.rules), out_dir=board.parent / (board.stem + ".si")
            )
        rows[str(board)] = dict(
            si=si,
            drc=dict(
                violations=len(drc["violations"]),
                unconnected=len(drc["unconnected_items"]),
                types=dict(Counter(v["type"] for v in drc["violations"])),
            ),
            objective=[
                len(drc["violations"]),
                m["pad_entry"]["blocked"],
                audit["reference_failures"],
                audit["subwidth"],
                audit["unqualified_pairs"],
                len(drc["unconnected_items"]),
            ],
            audit=audit,
            pad_entry=m["pad_entry"],
            metrics=m["metrics"],
            seconds=round(time.monotonic() - t0, 2),
            sha256=sha256(board),
        )
    save(a.out, rows)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path, nargs="?")
    ap.add_argument("--rules", required=True, type=Path)
    ap.add_argument("--annotation-source", action="append", default=[])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--work-dir", type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--kicad-cli", default=os.environ.get("PNR_KICAD_CLI", "kicad-cli"))
    ap.add_argument("--kicad-python", default=None)
    ap.add_argument("--label", default="gloss")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument(
        "--measure", nargs="+", type=Path, help="measure these boards (with --out JSON)"
    )
    ap.add_argument("--worker", choices=["inventory", "trial", "facts", "metrics"])
    ap.add_argument("--step", choices=STEPS)
    ap.add_argument("--spec", type=Path)
    ap.add_argument("--drc", type=Path)
    ap.add_argument("--skip", type=Path)
    ap.add_argument("--deadline", type=float, default=INVENTORY_SAFETY_SECONDS)
    ap.add_argument("--gloss-si", action="store_true")
    ap.add_argument(
        "--guard-open-nets",
        action="store_true",
        help="G2 also guards every track/via of open nets (the 06g pass, before refinement)",
    )
    ap.add_argument(
        "--no-hug", action="store_true", help="worker: no adjacency tie-breaks (PNR_GLOSS_HUG=0)"
    )
    ap.add_argument(
        "--classes",
        default=None,
        help="worker / --measure: PNR_GLOSS_CLASSES functional groups file",
    )
    ap.add_argument(
        "--classes-from",
        default=None,
        help="worker / --measure: PNR_GLOSS_CLASSES_FROM (netclasses,pairs,si,length_match)",
    )
    ap.add_argument(
        "--cross-group-mm",
        type=float,
        default=None,
        help="worker / --measure: PNR_GLOSS_CROSS_GROUP_MM (default 10 with --classes)",
    )
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--metrics", action="store_true", help="controller: metrics before/after the pass"
    )
    ap.add_argument(
        "--si", action="store_true", help="--measure: PNR_SI side fields too (PNR_SI=1)"
    )
    a = ap.parse_args(argv)
    if a.worker:
        if a.drc is not None and not a.drc.exists():
            a.drc = None
        return worker(a)
    if a.measure:
        if not a.out:
            ap.error("--measure requires --out")
        return measure(a)
    if not all([a.board, a.out, a.work_dir, a.report]):
        ap.error("board, --out, --work-dir and --report are required")
    try:
        conf = settings()
        check_groups(conf, read(a.rules))
    except ValueError as error:
        ap.error(str(error))
    result = GlossPass(a, conf).run()
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "label",
                    "status",
                    "accepted_transactions",
                    "proposed_transactions",
                    "objective_before",
                    "objective_after",
                    "wall_seconds",
                )
            }
        )
    )
    return result


if __name__ == "__main__":
    from pnr.profile import run

    # PNR_PROFILE_DIR: the gloss stage under pnr.profile; without it a plain main().
    run("gloss", main)
