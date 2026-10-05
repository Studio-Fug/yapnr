"""The ``plane_partition:`` and ``ir_drop:`` constraint sections (stdlib only).

``plane_partition`` divides one dedicated plane layer among several supply rails
(:mod:`pnr.plane_partition`); ``ir_drop`` asks for the DC drop report of a rail
(:mod:`pnr.ir_drop`, :mod:`pnr.ir_extract`). Both compile to plain JSON lists the
routing rules carry under the same names, only when declared.

    plane_partition:
      - layer: In3.Cu
        nets: [1V0_RF1, 1V0_RF2, 1V2]   # names or globs, each a plane net of the layer
        order: current                  # current (default) or listed
        split_gap_mm: 0.3               # copper gap between two rails
        min_width_mm: 1.0               # a rail's narrowest trunk
        fill: GND                       # what is left (a net), or none
        core_no_vias: true              # other nets' vias stay out of each trunk core
        terminal_reach_mm: 0.8          # a pad's drop lands within this of it
        neck_mm: 0                      # a trunk may narrow this close to its terminals
        protect_fanouts: true           # declared fanouts' access cells stay open
        fixed_lands: true               # own-net fixed pads/zones on the layer join
        region: {refs: [U2, L1], margin_mm: 0.5}  # an outer pour inside a region
        terminals: pad                  # (with region) whole lands, or reach discs
        connect: solid                  # (with region) the zones' pad connection
        stitch_vias: 4                  # (with region) vias per plane net's pour
        pieces: [5V_SYS, GND]           # (with region) plane nets poured as pieces,
                                        # each stitched to its plane on its own
        currents: {1V2: 1.0}            # A, else the net's @pnr-current peak or class
        budgets_mohm: {1V2: 12}         # widens a trunk for its IR budget
        sources: {1V2: {"@pmic.fb_1v2": "2"}}  # the trunk's root (else a central pad)

    ir_drop:
      - net: 1V0_RF1
        sources: {"@pmic.fb_rf1": ["2"]}     # {part: [pads]} or ["REF:PAD", ...]
        sinks: {"@radio.u1": [G5, H5, J5]}   # the same forms, or all (the default:
                                             # every pad but the sources and the
                                             # capacitors', no DC load)
        exclude: ["R54:1"]                   # more pads without a DC load (with all)
        current_a: 2.5     # default: the net's @pnr-current peak, else its class current
        split: equal       # or area
        budget_mohm: 4.0   # or budget_mv
        temperature_c: 60
        h_mm: 0.1          # the plane raster
        two_point: true    # also each sink's resistance alone (one solve per sink)
        hard: false        # a failure fails the run only when true

A part is a ref or an ``@address`` (the constraint compiler resolves an address key
to its ref, as everywhere in the constraints).
"""

from __future__ import annotations

import math
from typing import Dict, List

PARTITION_KEYS = {
    "layer",
    "nets",
    "order",
    "split_gap_mm",
    "min_width_mm",
    "fill",
    "core_no_vias",
    "terminal_reach_mm",
    "currents",
    "budgets_mohm",
    "sources",
    "h_mm",
    "neck_mm",
    "protect_fanouts",
    "region",
    "terminals",
    "connect",
    "stitch_vias",
    "fixed_lands",
    "pieces",
}
IR_KEYS = {
    "net",
    "sources",
    "sinks",
    "current_a",
    "split",
    "budget_mohm",
    "budget_mv",
    "temperature_c",
    "hard",
    "h_mm",
    "two_point",
    "exclude",
}


class PowerSpecError(ValueError):
    """A plane_partition or ir_drop input that cannot be honoured (names the key)."""


def _num(value, where, positive=False, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PowerSpecError("%s must be a finite number" % where)
    if positive and value <= 0:
        raise PowerSpecError("%s must be positive" % where)
    if minimum is not None and value < minimum:
        raise PowerSpecError("%s must be at least %s" % (where, minimum))
    return float(value)


def _names(value, where) -> List[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise PowerSpecError("%s must be a name or a list of names" % where)
    return list(value)


def _table(value, where, positive=True) -> Dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PowerSpecError("%s must be a mapping of net to number" % where)
    return {str(k): _num(v, "%s.%s" % (where, k), positive=positive) for k, v in value.items()}


def parse_partition(raw) -> List[Dict]:
    """The ``plane_partition`` list, validated (net globs unexpanded)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PowerSpecError("plane_partition must be a list of entries")
    out = []
    for k, entry in enumerate(raw):
        where = "plane_partition[%d]" % k
        if not isinstance(entry, dict):
            raise PowerSpecError(where + " must be a mapping")
        bad = sorted(set(entry) - PARTITION_KEYS)
        if bad:
            raise PowerSpecError("%s: unknown key(s) %s" % (where, ", ".join(bad)))
        layer = entry.get("layer")
        if not isinstance(layer, str) or not layer:
            raise PowerSpecError(where + ".layer names the plane layer")
        nets = _names(entry.get("nets") or [], where + ".nets")
        if not nets:
            raise PowerSpecError(where + ".nets names at least one rail")
        order = entry.get("order", "current")
        if order not in ("current", "listed"):
            raise PowerSpecError(where + ".order must be current or listed")
        fill = entry.get("fill")
        if fill is not None and (not isinstance(fill, str) or not fill):
            raise PowerSpecError(where + ".fill is a net name (or absent)")
        core = entry.get("core_no_vias", True)
        if not isinstance(core, bool):
            raise PowerSpecError(where + ".core_no_vias must be a boolean")
        sources = entry.get("sources") or {}
        if not isinstance(sources, dict) or not all(
            isinstance(v, str) or (isinstance(v, dict) and len(v) == 1) for v in sources.values()
        ):
            raise PowerSpecError(where + ".sources maps a net to REF:PAD or {part: pad}")
        sources = {
            str(k): (v if isinstance(v, str) else "%s:%s" % next(iter(v.items())))
            for k, v in sources.items()
        }
        out.append(
            dict(
                layer=layer,
                nets=nets,
                order=order,
                split_gap_mm=_num(entry.get("split_gap_mm", 0.3), where + ".split_gap_mm", True),
                min_width_mm=_num(entry.get("min_width_mm", 1.0), where + ".min_width_mm", True),
                fill=fill,
                core_no_vias=core,
                terminal_reach_mm=_num(
                    entry.get("terminal_reach_mm", 0.8), where + ".terminal_reach_mm", True
                ),
                currents=_table(entry.get("currents"), where + ".currents"),
                budgets_mohm=_table(entry.get("budgets_mohm"), where + ".budgets_mohm"),
                sources={str(k): v for k, v in sorted(sources.items())},
                h_mm=_num(entry.get("h_mm", 0.1), where + ".h_mm", True),
            )
        )
        if entry.get("neck_mm") is not None:  # only when declared (rules unchanged else)
            out[-1]["neck_mm"] = _num(entry["neck_mm"], where + ".neck_mm", minimum=0.0)
        if entry.get("region") is not None:  # an outer pour (only when declared)
            out[-1].update(_outer(entry, where))
        elif any(
            entry.get(k) is not None for k in ("terminals", "connect", "stitch_vias", "pieces")
        ):
            raise PowerSpecError(
                where + ": terminals, connect, stitch_vias and pieces go with a region"
            )
        if entry.get("fixed_lands") is not None:  # only when declared
            if not isinstance(entry["fixed_lands"], bool):
                raise PowerSpecError(where + ".fixed_lands must be a boolean")
            if entry["fixed_lands"]:
                out[-1]["fixed_lands"] = True
        if entry.get("protect_fanouts") is not None:  # only when declared
            if not isinstance(entry["protect_fanouts"], bool):
                raise PowerSpecError(where + ".protect_fanouts must be a boolean")
            if entry["protect_fanouts"]:
                out[-1]["protect_fanouts"] = True
    layers = [e["layer"] for e in out]
    if len(set(layers)) != len(layers):
        raise PowerSpecError("plane_partition: one entry per layer")
    return out


def _outer(entry, where) -> Dict:
    """The keys of an outer pour (``region`` and the keys that go with it)."""
    region = entry["region"]
    if isinstance(region, dict):
        bad = sorted(set(region) - {"refs", "margin_mm"})
        if bad:
            raise PowerSpecError("%s.region: unknown key(s) %s" % (where, ", ".join(bad)))
        refs = _names(region.get("refs") or [], where + ".region.refs")
        if not refs:
            raise PowerSpecError(where + ".region.refs names at least one part")
        out_region = dict(refs=refs)
        if region.get("margin_mm") is not None:
            out_region["margin_mm"] = _num(
                region["margin_mm"], where + ".region.margin_mm", minimum=0.0
            )
    else:
        if not isinstance(region, (list, tuple)) or len(region) < 3:
            raise PowerSpecError(where + ".region is a polygon (3+ points) or {refs, margin_mm}")
        out_region = []
        for k, pt in enumerate(region):
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise PowerSpecError("%s.region[%d] is an [x, y] point" % (where, k))
            out_region.append([_num(v, "%s.region[%d]" % (where, k)) for v in pt])
    if entry.get("fill") is not None:
        raise PowerSpecError(where + ": an outer pour (region) takes no fill")
    out = dict(region=out_region)
    terminals = entry.get("terminals")
    if terminals is not None:
        if terminals not in ("pad", "reach"):
            raise PowerSpecError(where + ".terminals must be pad or reach")
        out["terminals"] = terminals
    connect = entry.get("connect")
    if connect is not None:
        if connect not in ("solid", "thermal"):
            raise PowerSpecError(where + ".connect must be solid or thermal")
        out["connect"] = connect
    if entry.get("stitch_vias") is not None:
        n = entry["stitch_vias"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise PowerSpecError(where + ".stitch_vias must be a whole number, 0 or more")
        out["stitch_vias"] = n
    if entry.get("pieces") is not None:
        # Plane nets whose pour may be several pieces, each stitched to the net's plane on
        # its own (pnr.plane_partition, pnr.route.detail.pour.stitch); only when declared.
        pieces = _names(entry["pieces"], where + ".pieces")
        if not pieces:
            raise PowerSpecError(where + ".pieces names at least one net")
        out["pieces"] = pieces
    return out


POUR_KEYS = {"layer", "net", "stitch", "connect", "clearance_mm", "min_width_mm"}


def parse_pour(raw) -> List[Dict]:
    """The ``pour`` list, validated: ``[{layer, net, stitch, connect, clearance_mm,
    min_width_mm}]`` (pnr.pour)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PowerSpecError("pour must be a list of entries")
    out = []
    seen = set()
    for k, entry in enumerate(raw):
        where = "pour[%d]" % k
        if not isinstance(entry, dict):
            raise PowerSpecError(where + " must be a mapping")
        bad = sorted(set(entry) - POUR_KEYS)
        if bad:
            raise PowerSpecError("%s: unknown key(s) %s" % (where, ", ".join(bad)))
        layer, net = entry.get("layer"), entry.get("net")
        if layer not in ("F.Cu", "B.Cu"):
            raise PowerSpecError(where + ".layer is an outer layer (F.Cu or B.Cu)")
        if not isinstance(net, str) or not net:
            raise PowerSpecError(where + ".net names the pour's net")
        if (layer, net) in seen:
            raise PowerSpecError(where + ": one entry per layer and net")
        seen.add((layer, net))
        stitch = entry.get("stitch", True)
        if not isinstance(stitch, bool):
            raise PowerSpecError(where + ".stitch must be a boolean")
        connect = entry.get("connect", "thermal")
        if connect not in ("solid", "thermal"):
            raise PowerSpecError(where + ".connect must be solid or thermal")
        row = dict(layer=layer, net=net, stitch=stitch, connect=connect)
        for key in ("clearance_mm", "min_width_mm"):
            if entry.get(key) is not None:
                row[key] = _num(entry[key], "%s.%s" % (where, key), True)
        out.append(row)
    return out


def parse_ir_drop(raw) -> List[Dict]:
    """The ``ir_drop`` list, validated."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PowerSpecError("ir_drop must be a list of entries")
    out = []
    for k, entry in enumerate(raw):
        where = "ir_drop[%d]" % k
        if not isinstance(entry, dict):
            raise PowerSpecError(where + " must be a mapping")
        bad = sorted(set(entry) - IR_KEYS)
        if bad:
            raise PowerSpecError("%s: unknown key(s) %s" % (where, ", ".join(bad)))
        net = entry.get("net")
        if not isinstance(net, str) or not net:
            raise PowerSpecError(where + ".net names the rail")
        sources = _terminals(entry.get("sources") or [], where + ".sources")
        if not sources:
            raise PowerSpecError(where + ".sources names at least one pad")
        sinks = entry.get("sinks", "all")
        if sinks != "all":
            sinks = _terminals(sinks, where + ".sinks")
        exclude = None
        if entry.get("exclude") is not None:
            if sinks != "all":
                raise PowerSpecError(where + ".exclude goes with sinks: all")
            exclude = _terminals(entry["exclude"], where + ".exclude")
        split = entry.get("split", "equal")
        if split not in ("equal", "area"):
            raise PowerSpecError(where + ".split must be equal or area")
        if entry.get("budget_mohm") is not None and entry.get("budget_mv") is not None:
            raise PowerSpecError(where + ": give budget_mohm or budget_mv, not both")
        hard = entry.get("hard", False)
        if not isinstance(hard, bool):
            raise PowerSpecError(where + ".hard must be a boolean")
        row = dict(net=net, sources=sources, sinks=sinks, split=split, hard=hard)
        if exclude:
            row["exclude"] = exclude
        for key in ("current_a", "budget_mohm", "budget_mv"):
            if entry.get(key) is not None:
                row[key] = _num(entry[key], where + "." + key, positive=True)
        row["temperature_c"] = _num(entry.get("temperature_c", 25.0), where + ".temperature_c")
        row["h_mm"] = _num(entry.get("h_mm", 0.1), where + ".h_mm", positive=True)
        if entry.get("two_point") is not None:
            if not isinstance(entry["two_point"], bool):
                raise PowerSpecError(where + ".two_point must be a boolean")
            row["two_point"] = entry["two_point"]
        out.append(row)
    return out


def _terminals(value, where) -> List[str]:
    """``["REF:PAD", ...]`` from that list or a ``{part: [pads]}`` mapping."""
    if isinstance(value, dict):
        out = []
        for part, pads in value.items():
            for pad in _names(
                [str(p) for p in pads] if isinstance(pads, list) else str(pads), where
            ):
                out.append("%s:%s" % (part, pad))
        return out
    out = _names(value, where)
    for text in out:
        if ":" not in text:
            raise PowerSpecError("%s: %r is not REF:PAD" % (where, text))
    return out


def rail_current(rules: Dict, net: str, explicit=None):
    """A rail's design current: ``explicit``, else its ``@pnr-current`` peak
    (``rules['electrical_nets']``), else its net class ``current_a``; None if unknown."""
    if explicit is not None:
        return float(explicit)
    p = (rules.get("electrical_nets") or {}).get(net) or {}
    if p.get("peak_current_a") is not None:
        return float(p["peak_current_a"])
    for c in rules.get("net_classes", []):
        if net in c.get("nets", []) and c.get("current_a"):
            return float(c["current_a"])
    return None


def resolve_terminal(text: str, parts: Dict[str, Dict]) -> List[tuple]:
    """``REF:PAD`` or ``@address:PAD`` (an address suffix, as @pnr-current targets) as
    ``[(ref, pad)]``. ``parts``: ref -> {"address": str, "pads": [names]}."""
    if ":" not in text:
        raise PowerSpecError("terminal %r is REF:PAD or @address:PAD" % text)
    target, pad = text.rsplit(":", 1)
    return [(ref, pad) for ref in resolve_part(target, parts)]


def resolve_part(target: str, parts: Dict[str, Dict]) -> List[str]:
    """The ref ``target`` names: a ref, or ``@address`` (its suffix)."""
    if not target.startswith("@"):
        if target not in parts:
            raise PowerSpecError("unknown part %r" % target)
        return [target]
    want = target[1:].strip(".")
    hits = [
        ref
        for ref, p in sorted(parts.items())
        if p.get("address")
        and (
            p["address"].removesuffix("._p") == want
            or p["address"].removesuffix("._p").endswith("." + want)
        )
    ]
    if len(hits) != 1:
        raise PowerSpecError("address %s matches %d parts" % (target, len(hits)))
    return hits
