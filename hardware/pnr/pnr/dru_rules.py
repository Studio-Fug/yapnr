"""A board's KiCad custom rules (``.kicad_dru``) where they constrain routing
(``board.dru_routing: true``).

KiCad's DRC judges the routed board by the board's custom rules too, but the router
reads only the fab profile and the net classes. This module parses the rules file
(stdlib only) and maps the subset whose effect on routing it can state exactly onto
the router's existing mechanisms:

* ``(constraint disallow via)`` true for a net (``A.hasNetclass('XTAL')``): the net
  routes on its pads' one copper layer (``no_via``; route_board sets its
  ``layer_mask``), so it never changes layer;
* ``(constraint disallow track)`` true for a net on some copper layers
  (``A.Layer != 'F.Cu' && A.Layer != 'B.Cu'``): those layers leave its
  ``layer_mask`` (``track_layers``);
* ``(constraint clearance (min d))`` between two kinds of nets
  (``A.hasNetclass('SW') && B.hasNetclass('XTAL')``): a pair clearance
  (``pair_clearances``). route_board raises one side's clearance to ``d`` when ``d``
  is at most :data:`PAIR_RAISE_MAX_MM` (the side with fewer nets: every pair of the
  two then keeps ``d``), and bars each side's tracks and vias within ``d`` of the
  other side's pads, escapes and fixed copper when it is larger; the copper audit
  judges the pairs;
* ``(constraint length (max L))`` for a net: reported against its routed length
  (``length_max``);
* ``(constraint physical_hole_clearance (min x))`` against ``Edge.Cuts`` for a via:
  the hole-to-edge limit ``board.edge: exact`` keeps (``hole_to_edge_mm``);
  ``edge_clearance`` likewise (``edge_clearance_mm``).

A condition is evaluated in three values for a track or a via of each net (and,
for two-item rules, a copper item of each other net): ``A/B.Type``,
``A/B.NetClass``, ``A/B.NetName``, ``A/B.Layer``, ``A/B.hasNetclass('X')``,
``A/B.Hole`` of a via, literals, ``== != < <= > >=``, ``&& || !`` and parentheses.
Anything else (``intersectsArea``, ``intersectsCourtyard``, pad properties, ...)
is unknown; a rule whose verdict stays unknown, and every constraint that is not one
of the above, is listed in ``unmodelled`` with its name and a reason, never dropped.
Area rules are the copper keepouts' and rule areas' (fixed blocks); sizes, pairs and
skews are the fab profile's, the net classes' and the pair router's.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, Optional

# A pair clearance up to this (mm) raises one side's class clearance for routing;
# a larger one keeps the sides apart by keep-outs around each other's copper.
PAIR_RAISE_MAX_MM = 1.0


# ------------------------------------------------------------------ s-expressions


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "()":
            yield c
            i += 1
        elif c == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                out.append(text[j])
                j += 1
            yield ("str", "".join(out))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield ("sym", text[i:j])
            i = j


def _parse_sexpr(text: str) -> list:
    stack = [[]]
    for tok in _tokens(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) < 2:
                raise ValueError("unbalanced ')' in custom rules")
            item = stack.pop()
            stack[-1].append(item)
        else:
            stack[-1].append(tok[1])
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in custom rules")
    return stack[0]


def _mm(value: str) -> Optional[float]:
    m = re.fullmatch(r"\s*(-?[\d.]+)\s*(mm|mil|in)?\s*", str(value))
    if not m:
        return None
    v = float(m.group(1))
    return v * {"mm": 1.0, None: 1.0, "mil": 0.0254, "in": 25.4}[m.group(2)]


def parse(text: str) -> List[Dict]:
    """The rules of a ``.kicad_dru`` text: ``{name, layer, condition, constraints}``,
    a constraint ``{type, min, opt, max, items}`` (``items``: a disallow's kinds)."""
    rules = []
    for node in _parse_sexpr(text):
        if not isinstance(node, list) or not node or node[0] != "rule":
            continue
        rule = {"name": node[1] if len(node) > 1 else "", "layer": None, "condition": None}
        constraints = []
        for part in node[2:]:
            if not isinstance(part, list) or not part:
                continue
            if part[0] == "layer" and len(part) > 1:
                rule["layer"] = part[1]
            elif part[0] == "condition" and len(part) > 1:
                rule["condition"] = part[1]
            elif part[0] == "constraint" and len(part) > 1:
                c = {"type": part[1], "items": []}
                for arg in part[2:]:
                    if isinstance(arg, list) and len(arg) > 1 and arg[0] in ("min", "opt", "max"):
                        c[arg[0]] = _mm(arg[1])
                    elif isinstance(arg, str):
                        c["items"].append(arg)
                constraints.append(c)
        rule["constraints"] = constraints
        rules.append(rule)
    return rules


# ------------------------------------------------------------------ conditions

_COND_TOKEN = re.compile(
    r"\s*(?:(\|\||&&|==|!=|<=|>=|<|>|!|\(|\)|,)|'([^']*)'|\"([^\"]*)\"|"
    r"([AB])\.([A-Za-z_][A-Za-z_0-9]*)|(-?[\d.]+(?:mm|mil|in)?)|([A-Za-z_][A-Za-z_0-9]*))"
)


@lru_cache(maxsize=None)
def _cond_tokens(text: str):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _COND_TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError("cannot read condition at %r" % text[pos : pos + 20])
        pos = m.end()
        op, s1, s2, item, prop, num, word = m.groups()
        if op:
            out.append(("op", op))
        elif s1 is not None or s2 is not None:
            out.append(("lit", s1 if s1 is not None else s2))
        elif item:
            out.append(("prop", item, prop))
        elif num:
            out.append(("num", _mm(num)))
        else:
            out.append(("word", word))
    return tuple(out)


class _Unknown:
    """A term the evaluator cannot know (area, courtyard, pad property, ...)."""

    def __init__(self, why):
        self.why = why

    def __repr__(self):
        return "unknown(%s)" % self.why


def _and(a, b):
    if a is False or b is False:
        return False
    if isinstance(a, _Unknown):
        return a
    if isinstance(b, _Unknown):
        return b
    return True


def _or(a, b):
    if a is True or b is True:
        return True
    if isinstance(a, _Unknown):
        return a
    if isinstance(b, _Unknown):
        return b
    return False


def _not(a):
    return a if isinstance(a, _Unknown) else (not a)


class Item:
    """The copper item a condition is judged for: ``kind`` Track/Via/Pad (or
    ``Edge`` for an Edge.Cuts item), its net and that net's classes, its layer
    (None: a through via, any layer) and a via's drill."""

    def __init__(self, kind, net="", classes=(), layer=None, hole=None):
        self.kind = kind
        self.net = net
        self.classes = frozenset(classes)
        self.layer = layer
        self.hole = hole


def evaluate(condition: Optional[str], a: Item, b: Optional[Item] = None):
    """The condition's verdict for items ``a`` and ``b``: True, False or an
    :class:`_Unknown` naming the first unknown term."""
    if condition is None or not condition.strip():
        return True
    toks = _cond_tokens(condition)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        tok = toks[pos]
        pos += 1
        return tok

    def value(tok):
        kind = tok[0]
        if kind in ("lit", "num"):
            return tok[1]
        if kind == "prop":
            item = a if tok[1] == "A" else b
            name = tok[2]
            if peek() == ("op", "("):  # a function call
                take()
                args = []
                while peek() != ("op", ")"):
                    t = take()
                    if t == ("op", ","):
                        continue
                    args.append(t[1] if t[0] in ("lit", "num") else None)
                take()
                return call(item, name, args)
            return prop(item, name)
        return _Unknown(tok[1] if kind == "word" else str(tok))

    def call(item, name, args):
        if item is None:
            return _Unknown("B." + name)
        if name == "hasNetclass" and args:
            return args[0] in item.classes
        return _Unknown(name)

    def prop(item, name):
        if item is None:
            return _Unknown("B." + name)
        if name == "Type":
            return item.kind
        if name in ("NetClass", "Net_Class"):
            return ("classes", item.classes)
        if name in ("NetName", "Net_Name"):
            return item.net
        if name == "Layer":
            if item.kind == "Edge":
                return "Edge.Cuts"
            return item.layer if item.layer is not None else _Unknown("Layer of a through via")
        if name in ("Hole", "Hole_Size_X", "Hole_Size_Y") and item.kind == "Via":
            return item.hole if item.hole is not None else _Unknown(name)
        return _Unknown(name)

    def compare(x, op, y):
        if isinstance(x, _Unknown):
            return x
        if isinstance(y, _Unknown):
            return y
        if isinstance(x, tuple) or isinstance(y, tuple):  # a net class: membership
            classes, other = (x[1], y) if isinstance(x, tuple) else (y[1], x)
            if op in ("==", "!="):
                hit = other in classes
                return hit if op == "==" else not hit
            return _Unknown("NetClass " + op)
        if op == "==":
            return x == y
        if op == "!=":
            return x != y
        try:
            return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]
        except TypeError:
            return _Unknown("compare " + op)

    def primary():
        tok = take()
        if tok == ("op", "!"):
            return _not(primary())
        if tok == ("op", "("):
            v = disjunction()
            if take() != ("op", ")"):
                raise ValueError("missing ')' in condition")
            return v
        left = value(tok)
        nxt = peek()
        if nxt and nxt[0] == "op" and nxt[1] in ("==", "!=", "<", "<=", ">", ">="):
            op = take()[1]
            right = value(take())
            return compare(left, op, right)
        if isinstance(left, bool) or isinstance(left, _Unknown):
            return left
        return _Unknown("bare value")

    def conjunction():
        v = primary()
        while peek() == ("op", "&&"):
            take()
            v = _and(v, primary())
        return v

    def disjunction():
        v = conjunction()
        while peek() == ("op", "||"):
            take()
            v = _or(v, conjunction())
        return v

    result = disjunction()
    if pos != len(toks):
        raise ValueError("unexpected %r in condition" % (toks[pos],))
    return result


# ------------------------------------------------------------------ mapping


def net_classes_of(rules: Dict) -> Dict[str, frozenset]:
    """net -> the KiCad net classes writeback gives it: the rules' net classes and a
    ``dp_<name>`` class per differential pair (pnr.writeback). A net in none is in
    KiCad's ``Default`` (:data:`DEFAULT_CLASS`; the callers fill it in)."""
    out: Dict[str, set] = {}
    for nc in (rules or {}).get("net_classes", []):
        for n in nc.get("nets", []):
            out.setdefault(n, set()).add(nc["name"])
    for dp in (rules or {}).get("diff_pairs", []):
        for n in (dp.get("p"), dp.get("n")):
            if n:
                out.setdefault(n, set()).add("dp_" + dp["name"])
    return {n: frozenset(c) for n, c in out.items()}


DEFAULT_CLASS = frozenset({"Default"})  # KiCad's class of a net without one


def copper_layer_names(count: int) -> List[str]:
    count = max(2, int(count))
    return ["F.Cu"] + ["In%d.Cu" % i for i in range(1, count - 1)] + ["B.Cu"]


def _verdict_text(v) -> str:
    return "unknown term %s" % v.why if isinstance(v, _Unknown) else str(v)


def routing_rules(
    text: str, rules: Dict, nets: List[str], via_drill: Optional[float] = None
) -> Dict:
    """The routing effects of the custom rules ``text`` for ``nets`` (module doc):
    ``{no_via, track_layers, pair_clearances, length_max, hole_to_edge_mm,
    edge_clearance_mm, applied, unmodelled}``."""
    fab = dict((rules or {}).get("fab") or {})
    drill = via_drill if via_drill is not None else fab.get("via_drill_mm")
    layers = copper_layer_names((rules or {}).get("layers", 2))
    classes = net_classes_of(rules)
    out = {
        "no_via": [],
        "track_layers": {},
        "pair_clearances": [],
        "length_max": {},
        "applied": [],
        "unmodelled": [],
    }
    no_via, banned, length = set(), {}, {}
    # Nets judged alike: one representative per (classes) signature, per net when a
    # rule names nets.
    names_nets = any(
        r["condition"] and re.search(r"\bNet_?Name\b", r["condition"]) for r in parse(text)
    )
    groups: Dict[tuple, List[str]] = {}
    for n in sorted(nets):
        key = (classes.get(n) or DEFAULT_CLASS, n if names_nets else None)
        groups.setdefault(key, []).append(n)

    def unmodelled(rule, constraint, reason):
        out["unmodelled"].append(
            {"rule": rule["name"], "constraint": constraint["type"], "reason": reason}
        )

    for rule in parse(text):
        cond = rule["condition"]
        for c in rule["constraints"]:
            kind = c["type"]
            try:
                if kind == "disallow":
                    _disallow(rule, c, groups, layers, drill, no_via, banned, out, unmodelled)
                elif kind == "clearance":
                    _pair(rule, c, groups, drill, fab, out, unmodelled)
                elif kind == "length":
                    if c.get("max") is None:
                        unmodelled(rule, c, "length without a max")
                        continue
                    hit = []
                    for (cls, _), members in groups.items():
                        v = evaluate(cond, Item("Track", members[0], cls, layers[0]))
                        if isinstance(v, _Unknown):
                            unmodelled(rule, c, _verdict_text(v))
                            hit = None
                            break
                        if v:
                            hit += members
                    if hit:
                        for n in hit:
                            length[n] = min(length.get(n, c["max"]), c["max"])
                        out["applied"].append(
                            {
                                "rule": rule["name"],
                                "effect": "length max %g mm" % c["max"],
                                "nets": len(hit),
                            }
                        )
                elif kind in ("physical_hole_clearance", "edge_clearance"):
                    v = evaluate(cond, Item("Via", "", (), None, drill), Item("Edge"))
                    if v is True and c.get("min") is not None:
                        key = (
                            "hole_to_edge_mm"
                            if kind == "physical_hole_clearance"
                            else "edge_clearance_mm"
                        )
                        out[key] = max(out.get(key, 0.0), c["min"])
                        out["applied"].append(
                            {"rule": rule["name"], "effect": "%s %g mm" % (key, c["min"])}
                        )
                    else:
                        unmodelled(rule, c, "not a via-to-Edge.Cuts rule (%s)" % _verdict_text(v))
                else:
                    unmodelled(
                        rule, c, _OWNERS.get(kind, "not a routing constraint the router maps")
                    )
            except ValueError as error:
                unmodelled(rule, c, "unreadable condition: %s" % error)
    out["no_via"] = sorted(no_via)
    out["track_layers"] = {
        n: [la for la in layers if la not in bad] for n, bad in sorted(banned.items())
    }
    out["length_max"] = dict(sorted(length.items()))
    return out


# Constraint types other stages own, and where (the reason a rule stays unmodelled).
_OWNERS = {
    "track_width": "track widths are the net classes' (net_class width_mm)",
    "via_diameter": "via sizes are the fab profile's and the fanout's via classes",
    "hole_size": "via drills are the fab profile's and the fanout's via classes",
    "annular_width": "via sizes are the fab profile's",
    "hole_clearance": "hole clearances are the fab profile's",
    "hole_to_hole": "hole spacing is the fab profile's (hole_to_hole_mm)",
    "diff_pair_gap": "pair geometry is the pair router's (diff_pair)",
    "diff_pair_uncoupled": "pair geometry is the pair router's (diff_pair)",
    "skew": "skew is the length tuner's (diff_pair / length_match)",
    "physical_clearance": "via-to-pad copper is the fab profile's (via_to_smd_pad_mm)",
    "bridged_mask": "mask apertures are the footprints'",
    "courtyard_clearance": "courtyards are the placer's",
    "silk_clearance": "silkscreen is not routed",
    "text_height": "text is not routed",
    "text_thickness": "text is not routed",
    "thermal_relief_gap": "zone fills are the plane stage's",
    "thermal_spoke_width": "zone fills are the plane stage's",
    "zone_connection": "zone fills are the plane stage's",
}


def _disallow(rule, c, groups, layers, drill, no_via, banned, out, unmodelled):
    items = set(c["items"])
    cond = rule["condition"]
    routed = items & {"via", "track", "through_via", "blind_via", "micro_via", "buried_via"}
    if not routed:
        unmodelled(rule, c, "disallows %s: not routed copper" % " ".join(sorted(items)))
        return
    if items & {"via", "through_via"}:
        hit = []
        for (cls, _), members in groups.items():
            v = evaluate(cond, Item("Via", members[0], cls, None, drill))
            if isinstance(v, _Unknown):
                unmodelled(rule, c, "via verdict: %s" % _verdict_text(v))
                hit = None
                break
            if v:
                hit += members
        if hit:
            no_via.update(hit)
            out["applied"].append({"rule": rule["name"], "effect": "no vias", "nets": sorted(hit)})
    if "track" in items:
        effects = {}
        for (cls, _), members in groups.items():
            for la in layers:
                if rule["layer"] and rule["layer"] != la:
                    continue
                v = evaluate(cond, Item("Track", members[0], cls, la))
                if isinstance(v, _Unknown):
                    unmodelled(rule, c, "track verdict: %s" % _verdict_text(v))
                    return
                if v:
                    for n in members:
                        banned.setdefault(n, set()).add(la)
                        effects.setdefault(la, set()).add(n)
        if effects:
            out["applied"].append(
                {
                    "rule": rule["name"],
                    "effect": "no tracks on %s" % ", ".join(sorted(effects)),
                    "nets": len(set().union(*effects.values())),
                }
            )


def _pair(rule, c, groups, drill, fab, out, unmodelled):
    d = c.get("min")
    if d is None:
        unmodelled(rule, c, "clearance without a min")
        return
    cond = rule["condition"]
    keys = sorted(groups)
    pairs = set()
    for ka in keys:
        for kb in keys:
            hit = False
            for kind_a in ("Track", "Via"):
                for kind_b in ("Track", "Via", "Pad"):
                    a = Item(
                        kind_a, groups[ka][0], ka[0], "F.Cu" if kind_a == "Track" else None, drill
                    )
                    b = Item(
                        kind_b, groups[kb][0], kb[0], "F.Cu" if kind_b != "Via" else None, drill
                    )
                    v = evaluate(cond, a, b)
                    if isinstance(v, _Unknown):
                        # A rule that only concerns other item kinds (pads) decides
                        # False for routed copper elsewhere; an unknown left here is
                        # an area or a property the router cannot judge.
                        unmodelled(rule, c, "clearance verdict: %s" % _verdict_text(v))
                        return
                    hit = hit or v
            if hit:
                pairs.add((ka, kb))
    if not pairs:
        unmodelled(rule, c, "applies to no routed copper (track or via)")
        return
    if d <= float(fab.get("clearance_mm", 0.0)) + 1e-9:
        unmodelled(rule, c, "%g mm is not above the fab clearance: nothing to add" % d)
        return
    # Unordered pairs of net groups, merged into sides: the B sides of one A side,
    # then the A sides of one B side (a full product, as class rules give, is one
    # entry).
    seen = set()
    by_a: Dict[tuple, set] = {}
    for ka, kb in sorted(pairs):
        key = tuple(sorted((ka, kb)))
        if key in seen:
            continue
        seen.add(key)
        by_a.setdefault(tuple(sorted(groups[key[0]])), set()).update(groups[key[1]])
    by_b: Dict[tuple, set] = {}
    for side_a, side_b in by_a.items():
        by_b.setdefault(tuple(sorted(side_b)), set()).update(side_a)
    for side_b, side_a in sorted(by_b.items()):
        out["pair_clearances"].append(
            {"rule": rule["name"], "clearance_mm": d, "a": sorted(side_a), "b": list(side_b)}
        )
    out["applied"].append(
        {"rule": rule["name"], "effect": "pair clearance %g mm" % d, "pairs": len(seen)}
    )


def attach_dru(rules: Dict, dru_text: Optional[str], nets: List[str]) -> None:
    """``rules["dru"]`` (:func:`routing_rules`) when the rules ask for it
    (``dru_routing: true``) and the board has a custom rules file; a board
    without one gets an empty record that says so."""
    if not (rules or {}).get("dru_routing"):
        return
    if not dru_text:
        rules["dru"] = {"unmodelled": [], "applied": [], "source": "none"}
        return
    rules["dru"] = dict(routing_rules(dru_text, rules, nets), source="kicad_dru")
