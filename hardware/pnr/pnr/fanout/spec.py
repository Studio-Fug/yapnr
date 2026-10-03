"""The ``fanout:`` constraint section: parsing, validation and its compiled form.

``parse`` turns one YAML entry into a plain, JSON-serializable dict (the form
``rules.json`` carries under ``fanouts``), ``expand_nets`` resolves its net globs
against the netlist, and ``check`` validates what needs the routing rules (the fab
profile and the copper stack). Every problem is a :class:`FanoutError` naming the
key, so the constraint compiler can report it as a ``ConstraintError``.
"""

from __future__ import annotations

import fnmatch
import math
from typing import Dict, List, Optional, Sequence

EDGES = ("north", "south", "east", "west")
SITE_KINDS = ("interstitial", "vacant", "outside", "in_pad")
ZONES = ("interior", "shadow")
KEYS = {
    "name",
    "ref",
    "pads",
    "skip_pads",
    "via_classes",
    "surface_rings",
    "forbidden_exits",
    "reserved",
    "escape_layers",
    "ring_layers",
    "neck_mm",
    "lock",
    "bottom_sites",
    "variant",
}


class FanoutError(ValueError):
    """A fanout input that cannot be honoured (names the offending key)."""


def _number(value, where, minimum=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FanoutError("%s must be a finite number" % where)
    if positive and value <= 0:
        raise FanoutError("%s must be positive" % where)
    if minimum is not None and value < minimum:
        raise FanoutError("%s must be at least %s" % (where, minimum))
    return float(value)


def _strings(value, where):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise FanoutError("%s must be a string or a list of strings" % where)
    return list(value)


def _polygon(entry, where):
    if "rect" in entry and "polygon" in entry:
        raise FanoutError(where + ": give rect or polygon, not both")
    if "rect" in entry:
        rect = entry["rect"]
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise FanoutError(where + ".rect needs four numbers")
        x0, y0, x1, y1 = (_number(v, where + ".rect") for v in rect)
        if x0 >= x1 or y0 >= y1:
            raise FanoutError(where + ".rect must have positive area")
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    poly = entry.get("polygon")
    if not isinstance(poly, (list, tuple)) or len(poly) < 3:
        raise FanoutError(where + " needs a rect or a polygon of at least three points")
    out = []
    for p in poly:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise FanoutError(where + ".polygon points are [x, y]")
        out.append([_number(p[0], where + ".polygon"), _number(p[1], where + ".polygon")])
    return out


def parse(entry: Dict, known_refs: Sequence[str], index: int = 0) -> Dict:
    """One ``fanout:`` list entry, validated and normalized."""
    if not isinstance(entry, dict):
        raise FanoutError("fanout[%d] must be a mapping" % index)
    where = "fanout[%d]" % index
    unknown = sorted(set(entry) - KEYS)
    if unknown:
        raise FanoutError("%s: unknown key(s) %s" % (where, ", ".join(unknown)))
    ref = entry.get("ref")
    if not isinstance(ref, str) or ref not in known_refs:
        raise FanoutError("%s.ref: unknown component ref %r" % (where, ref))
    name = str(entry.get("name") or ref)
    where = "fanout %s" % name
    classes = entry.get("via_classes") or {}
    if not isinstance(classes, dict):
        raise FanoutError(where + ".via_classes must be a mapping of class name to class")
    compiled = []
    for cname in sorted(classes, key=lambda c: (c == "default", c)):
        spec = classes[cname] or {}
        w = "%s.via_classes.%s" % (where, cname)
        if not isinstance(spec, dict):
            raise FanoutError(w + " must be a mapping")
        bad = sorted(set(spec) - {"diameter_mm", "drill_mm", "nets", "sites"})
        if bad:
            raise FanoutError("%s: unknown key(s) %s" % (w, ", ".join(bad)))
        diameter = _number(spec.get("diameter_mm"), w + ".diameter_mm", positive=True)
        drill = _number(spec.get("drill_mm"), w + ".drill_mm", positive=True)
        if drill >= diameter:
            raise FanoutError(w + ": the drill must be smaller than the diameter")
        sites = _strings(spec.get("sites"), w + ".sites") or list(SITE_KINDS[:3])
        for s in sites:
            if s not in SITE_KINDS:
                raise FanoutError("%s.sites: %r is not one of %s" % (w, s, ", ".join(SITE_KINDS)))
        nets = _strings(spec.get("nets"), w + ".nets")
        if cname == "default" and nets:
            raise FanoutError(w + ": the default class takes every other net (no nets)")
        if cname != "default" and not nets:
            raise FanoutError(w + ": a class names its nets (or is called default)")
        compiled.append(
            dict(name=cname, diameter_mm=diameter, drill_mm=drill, nets=nets, sites=sites)
        )
    rings = entry.get("surface_rings", 2)
    if isinstance(rings, bool) or not isinstance(rings, int) or rings < 0:
        raise FanoutError(where + ".surface_rings must be a whole number >= 0")
    exits = entry.get("forbidden_exits") or []
    forbidden: Dict[str, List[str]] = {}
    if isinstance(exits, dict):
        for layer, edges in sorted(exits.items()):
            forbidden[str(layer)] = _strings(edges, where + ".forbidden_exits." + str(layer))
    else:
        forbidden["*"] = _strings(exits, where + ".forbidden_exits")
    for layer, edges in forbidden.items():
        for e in edges:
            if e not in EDGES:
                raise FanoutError(
                    "%s.forbidden_exits: %r is not one of %s" % (where, e, ", ".join(EDGES))
                )
        forbidden[layer] = sorted(set(edges))
    reserved = []
    for k, r in enumerate(entry.get("reserved") or []):
        w = "%s.reserved[%d]" % (where, k)
        if not isinstance(r, dict):
            raise FanoutError(w + " must be a mapping")
        bad = sorted(set(r) - {"rect", "polygon", "layers", "frame", "name"})
        if bad:
            raise FanoutError("%s: unknown key(s) %s" % (w, ", ".join(bad)))
        frame = r.get("frame", "part")
        if frame not in ("part", "board"):
            raise FanoutError(w + ".frame must be part or board")
        reserved.append(
            dict(
                name=str(r.get("name") or "reserved%d" % k),
                polygon=_polygon(r, w),
                frame=frame,
                layers=_strings(r.get("layers"), w + ".layers") or ["*"],
            )
        )
    escape = entry.get("escape_layers")
    escape = None if escape is None else _strings(escape, where + ".escape_layers")
    ring_layers = {}
    for key, layers in sorted((entry.get("ring_layers") or {}).items(), key=lambda kv: str(kv[0])):
        try:
            ring = int(key)
        except (TypeError, ValueError):
            raise FanoutError(where + ".ring_layers keys are ring numbers") from None
        ring_layers[str(ring)] = _strings(layers, "%s.ring_layers.%s" % (where, key))
    neck = entry.get("neck_mm")
    neck = None if neck is None else _number(neck, where + ".neck_mm", positive=True)
    lock = entry.get("lock", True)
    if not isinstance(lock, bool):
        raise FanoutError(where + ".lock must be a boolean")
    variant = entry.get("variant", 0)
    if isinstance(variant, bool) or not isinstance(variant, int) or variant < 0:
        raise FanoutError(where + ".variant must be a whole number >= 0")
    bottom = entry.get("bottom_sites")
    sites = None
    if bottom is not None:
        w = where + ".bottom_sites"
        if not isinstance(bottom, dict):
            raise FanoutError(w + " must be a mapping")
        bad = sorted(set(bottom) - {"parts", "max_stub_mm", "zone", "rotations"})
        if bad:
            raise FanoutError("%s: unknown key(s) %s" % (w, ", ".join(bad)))
        parts = _strings(bottom.get("parts"), w + ".parts")
        if not parts:
            raise FanoutError(w + ".parts names at least one part")
        missing = [p for p in parts if p not in known_refs]
        if missing:
            raise FanoutError("%s.parts: unknown component ref(s) %s" % (w, ", ".join(missing)))
        if len(set(parts)) != len(parts) or ref in parts:
            raise FanoutError(w + ".parts must be distinct and not the fanned-out part")
        zone = bottom.get("zone", "interior")
        if zone not in ZONES:
            raise FanoutError("%s.zone must be one of %s" % (w, ", ".join(ZONES)))
        rotations = bottom.get("rotations", [0, 90])
        if not isinstance(rotations, (list, tuple)) or not rotations:
            raise FanoutError(w + ".rotations is a list of quarter turns")
        for r in rotations:
            if isinstance(r, bool) or not isinstance(r, (int, float)) or r % 90:
                raise FanoutError(w + ".rotations is a list of quarter turns")
        sites = dict(
            parts=parts,
            max_stub_mm=_number(bottom.get("max_stub_mm", 0.5), w + ".max_stub_mm", minimum=0),
            zone=zone,
            rotations=[float(r) % 360 for r in rotations],
        )
    return dict(
        name=name,
        ref=ref,
        pads=_strings(entry.get("pads", "*"), where + ".pads"),
        skip_pads=_strings(entry.get("skip_pads"), where + ".skip_pads"),
        via_classes=compiled,
        surface_rings=rings,
        forbidden_exits=forbidden,
        reserved=reserved,
        escape_layers=escape,
        ring_layers=ring_layers,
        neck_mm=neck,
        lock=lock,
        bottom_sites=sites,
        variant=variant,
    )


def parse_all(raw, known_refs: Sequence[str]) -> List[Dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FanoutError("fanout must be a list of entries")
    out = [parse(entry, known_refs, k) for k, entry in enumerate(raw)]
    names = [f["name"] for f in out]
    if len(set(names)) != len(names):
        raise FanoutError("fanout names must be distinct")
    refs = [f["ref"] for f in out]
    if len(set(refs)) != len(refs):
        raise FanoutError("a part has at most one fanout")
    return out


def expand_nets(spec: Dict, net_names: Sequence[str]) -> Dict:
    """``spec`` with each via class's net globs expanded against ``net_names``
    (sorted literal names; an unknown literal name is dropped, as net classes do)."""
    out = dict(spec)
    classes = []
    for c in spec["via_classes"]:
        nets = sorted({n for n in net_names for p in c["nets"] if fnmatch.fnmatchcase(n, p)})
        classes.append(dict(c, nets=nets))
    out["via_classes"] = classes
    return out


def class_for(spec: Dict, net: str) -> Optional[Dict]:
    """The via class of ``net``: the first class naming it, else ``default``, else None."""
    default = None
    for c in spec["via_classes"]:
        if c["name"] == "default":
            default = c
        elif net in c["nets"]:
            return c
    return default


def check(spec: Dict, rules: Dict, layers: Sequence[str]) -> List[str]:
    """Validate ``spec`` against the routing rules and the plan's copper ``layers``
    (the grid layers, in stack order). Returns warnings; raises :class:`FanoutError`."""
    from pnr.fab_profile import geometry

    where = "fanout %s" % spec["name"]
    warnings = []
    fab = dict(rules.get("fab") or {})
    g = geometry(rules)
    if not spec["via_classes"]:
        fab_d, fab_h = float(fab.get("via_diameter_mm", 0.45)), float(fab.get("via_drill_mm", 0.25))
        spec["via_classes"] = [
            dict(
                name="default",
                diameter_mm=fab_d,
                drill_mm=fab_h,
                nets=[],
                sites=["vacant", "outside"],
            )
        ]
    min_drill = float(fab.get("min_through_drill_mm", 0.0) or 0.0)
    annular = float(fab.get("via_annular_mm", 0.0) or 0.0)
    min_dia = fab.get("min_via_diameter_mm")
    for c in spec["via_classes"]:
        w = "%s.via_classes.%s" % (where, c["name"])
        if c["drill_mm"] < min_drill - 1e-9:
            raise FanoutError(
                "%s: drill %.3f is under the fab's min_through_drill %.3f"
                % (w, c["drill_mm"], min_drill)
            )
        if (c["diameter_mm"] - c["drill_mm"]) / 2 < annular - 1e-9:
            raise FanoutError(
                "%s: annular ring %.4f is under the fab's via_annular %.4f"
                % (w, (c["diameter_mm"] - c["drill_mm"]) / 2, annular)
            )
        if min_dia is not None and c["diameter_mm"] < float(min_dia) - 1e-9:
            raise FanoutError(
                "%s: diameter %.3f is under the fab's min_via_diameter %.3f"
                % (w, c["diameter_mm"], float(min_dia))
            )
        if "in_pad" in c["sites"] and g.in_pad is None:
            raise FanoutError(w + ": in_pad sites need the fab profile's filled via-in-pad class")
    if spec["escape_layers"] is not None:
        for layer in spec["escape_layers"]:
            if layer not in layers:
                raise FanoutError(
                    "%s.escape_layers: %s is not a routing layer of the stack (%s)"
                    % (where, layer, ", ".join(layers))
                )
    for ring, names in spec["ring_layers"].items():
        for layer in names:
            if layer not in layers:
                raise FanoutError(
                    "%s.ring_layers.%s: %s is not a routing layer" % (where, ring, layer)
                )
    for layer in spec["forbidden_exits"]:
        if layer != "*" and layer not in layers:
            raise FanoutError("%s.forbidden_exits: %s is not a routing layer" % (where, layer))
    for r in spec["reserved"]:
        for layer in r["layers"]:
            if layer != "*" and layer not in layers:
                warnings.append(
                    "%s.reserved %s: layer %s is not a routing layer (ignored)"
                    % (where, r["name"], layer)
                )
    return warnings
