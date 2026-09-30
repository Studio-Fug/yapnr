"""Build the schematic model (schema ``pnr-schematic-v1``) for one scope.

usage: python -m yapnr.viewer.services.schematic_build <request.json> <out.json>

Runs as a subprocess with the engine runtime first on PYTHONPATH (like cost_compute), so a
frozen engine can be used. Every stage degrades rather than fails: without the runtime
(``pnr.power_topology``, ``pnr.hier``) there are no power roles or hard groups; a part folder
without a parsable symbol is drawn as a generic box with its pad names.

Request keys: graph, rules, constraints, parts, ato_src, symbol_cache, refs
(list or null for the whole board). Everything here is derived mechanically
from the netlist, the part libraries, the atopile source and the constraints.
"""

import hashlib
import json
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

from yapnr.viewer.services.schematic_sym import cached_part_symbol, generic_symbol

SCHEMA = "pnr-schematic-v1"
LABEL_MIN_PARTS = 7  # signal net with at least this many parts in scope -> named labels
RAIL_MIN_PARTS = 5  # power net with at least this many parts in scope -> rail labels
WARN = []


def warn(msg):
    WARN.append(str(msg))


try:  # frozen pnr runtime (optional)
    import yaml
    from pnr.constraints import compile_constraints
    from pnr.graph import BoardGraph
    from pnr.hier.blocks import aspect_sizes, extract_blocks, sub_board
    from pnr.power_topology import derive

    HAVE_PNR = True
except Exception as _ex:  # pragma: no cover - exercised only without the runtime
    HAVE_PNR = False
    warn("pnr runtime unavailable (%s): no power roles, blocks or hard groups" % _ex)


def natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def instance(address):
    """board.converter.output_cap3._p -> board.converter.output_cap3 (passive wrapper)."""
    return re.sub(r"\._p$", "", address or "")


def local_name(address, prefix):
    a = instance(address)
    return a[len(prefix) + 1 :] if prefix and a.startswith(prefix + ".") else a.split(".")[-1]


def module_of(address):
    a = instance(address).split(".")
    return ".".join(a[:2]) if len(a) > 2 else None


# ------------------------------------------------------------------ atopile source


def parse_ato(src_dir):
    """{module: {base, doc, inst: {name: type}}} from the design's *.ato files."""
    mods = {}
    for f in sorted(Path(src_dir).glob("*.ato")) if src_dir and Path(src_dir).is_dir() else []:
        cur, want_doc = None, False
        for line in f.read_text(errors="replace").splitlines():
            m = re.match(r"^(module|component|interface)\s+(\w+)(?:\s+from\s+(\w+))?\s*:", line)
            if m:
                cur = mods.setdefault(
                    m.group(2), dict(base=m.group(3), doc="", inst={}, file=f.name)
                )
                want_doc = True
                continue
            if cur is None:
                continue
            if line.strip() and not line[0].isspace():
                cur = None
                continue
            s = line.strip()
            if want_doc and s:
                want_doc = False
                if s.startswith('"""'):
                    cur["doc"] = s[3:].split('"""')[0].strip()
            m = re.match(r"^(\w+)\s*=\s*new\s+(\w+)", s)
            if m:
                cur["inst"][m.group(1)] = m.group(2)
    return mods


def ato_types(mods, addresses):
    """address -> [type per path segment]; the root is the module instantiating 'board'."""

    def child(t, name):
        seen = set()
        while t in mods and t not in seen:
            seen.add(t)
            if name in mods[t]["inst"]:
                return mods[t]["inst"][name]
            t = mods[t]["base"]
        return None

    def resolve(root, address):
        segs = address.split(".")
        out, t = [], root
        for s in segs[1:]:
            t = child(t, s)
            if t is None:
                break
            out.append(t)
        return [root] + out

    first = {a.split(".")[0] for a in addresses if a}
    best, best_n = None, -1
    for m in mods.values():
        for f in first:
            if f in m["inst"]:
                root = m["inst"][f]
                n = sum(len(resolve(root, a)) for a in addresses if a)
                if n > best_n:
                    best, best_n = root, n
    return {a: resolve(best, a) for a in addresses if a} if best else {}


def clean_doc(doc):
    doc = re.sub(r"\s*\((?:C\d+|\w*\d\w*)\)", "", doc or "").strip()
    return doc.rstrip(".").strip()


def part_doc(parts_dir, lib, cache={}):
    """Docstring of the atomic part component ('330pF (331) ±5% 50V')."""
    if (parts_dir, lib) not in cache:
        doc = ""
        d = Path(parts_dir or "") / lib
        if lib and "/" not in lib and d.is_dir():
            for f in sorted(d.glob("*.ato")):
                m = re.search(
                    r'^component\s+\w+\s*:\s*\n\s*"""(.*?)"""',
                    f.read_text(errors="replace"),
                    re.M | re.S,
                )
                if m:
                    doc = " ".join(m.group(1).split())
                    break
        cache[(parts_dir, lib)] = doc
    return cache[(parts_dir, lib)]


def value_text(address, types, mods, sym_value, pkg_doc):
    """Human value: the passive wrapper's docstring ('100 k 0402'), the part's own
    short docstring ('330pF ±5% 50V'), else the MPN from the symbol."""
    segs = (address or "").split(".")
    if len(types) == len(segs) and address.endswith("._p") and len(types) >= 2:
        wrapper = types[-2]
        doc = clean_doc((mods.get(wrapper) or {}).get("doc"))
        return (doc if doc and len(doc) <= 40 else wrapper), wrapper
    doc = clean_doc(pkg_doc)
    if doc and len(doc) <= 28:
        return doc, None
    return sym_value, None


# ------------------------------------------------------------------ grouping (tree)


def stem_tokens(name):
    toks = [re.sub(r"\d+$", "", t) for t in name.split("_") if t]
    return [t for t in toks if t]


def trie_groups(members, prefix_id):
    """Name-trie families (output_cap0..7 -> output_cap) with >= 2 members."""
    root = dict(children={}, refs=[])
    for ref, name in members.items():
        node = root
        node["refs"].append(ref)
        for t in stem_tokens(name):
            node = node["children"].setdefault(t, dict(children={}, refs=[]))
            node["refs"].append(ref)

    def walk(node, path):
        fams = []
        for tok, ch in sorted(node["children"].items()):
            p = path + [tok]
            sub = walk(ch, p)
            if len(ch["refs"]) >= 2:
                if len(sub) == 1 and set(sub[0]["refs"]) == set(ch["refs"]):
                    fams.append(sub[0])
                else:
                    key = "_".join(p)
                    fams.append(
                        dict(
                            kind="stem",
                            id="stem:%s/%s" % (prefix_id, key),
                            key=key,
                            label=key,
                            refs=sorted(ch["refs"], key=natural),
                            children=sub,
                        )
                    )
            else:
                fams.extend(sub)
        return fams

    fams = walk(root, [])
    while len(fams) == 1 and set(fams[0]["refs"]) == set(members):
        fams = fams[0]["children"]
    return fams


def hard_groups(cc, refs):
    out = []
    inside = set(refs)
    for con in cc.constraints if cc is not None else []:
        if con.kind != "group":
            continue
        anchor = con.params.get("anchor")
        members = [r for r in con.refs if r in inside]
        if anchor in inside and anchor not in members:
            members.append(anchor)
        if len(members) < 2:
            continue
        out.append(
            dict(
                name=con.name,
                anchor=anchor,
                refs=sorted(members),
                radius_mm=con.params.get("radius_mm"),
                hard=getattr(con.enforcement, "name", str(con.enforcement)) == "HARD",
            )
        )
    return out


def tree_for(by, refs, prefix, hard):
    """Hard groups nest by containment; a shared anchor stays at the parent level."""
    scope = set(refs)
    groups = [dict(h) for h in hard if set(h["refs"]) < scope]
    anchors = defaultdict(int)
    for h in groups:
        anchors[h["anchor"]] += 1
    for h in groups:
        if anchors[h["anchor"]] > 1:
            h["refs"] = [r for r in h["refs"] if r != h["anchor"]]
            h["satellite_of"] = h["anchor"]
    groups = [h for h in groups if len(h["refs"]) >= 2]
    groups.sort(key=lambda h: (-len(h["refs"]), h["name"] or ""))
    assigned = {}
    for i, h in enumerate(groups):
        for r in h["refs"]:
            prev = assigned.get(r)
            if prev is None or set(h["refs"]) <= set(groups[prev]["refs"]):
                assigned[r] = i
    parent = {}
    for i, h in enumerate(groups):
        owners = [j for j in range(i) if set(h["refs"]) <= set(groups[j]["refs"])]
        parent[i] = owners[-1] if owners else None

    def node_for(i):
        h = groups[i]
        anchor_ok = h["anchor"] in by
        base = instance(by[h["anchor"]].get("address")) if anchor_ok else (h["name"] or str(i))
        gid = "group:" + base + ("/" + (h["name"] or str(i)) if h.get("satellite_of") else "")
        kids = [node_for(j) for j in range(len(groups)) if parent.get(j) == i]
        loose = [r for r in h["refs"] if assigned.get(r) == i]
        fams = trie_groups({r: local_name(by[r].get("address"), prefix) for r in loose}, gid)
        anchor_name = (
            local_name(by[h["anchor"]].get("address"), prefix)
            if anchor_ok
            else (h["name"] or "group")
        )
        label = anchor_name + " group"
        if h.get("satellite_of") and h.get("radius_mm"):
            label += (
                " · r%g mm" % h["radius_mm"]
            )  # constraint groups are unnamed; the radius tells them apart
        return dict(
            kind="group",
            id=gid,
            label=label,
            anchor=h["anchor"],
            satellite_of=h.get("satellite_of"),
            radius_mm=h["radius_mm"],
            hard=h["hard"],
            refs=sorted(h["refs"], key=natural),
            children=kids + fams,
        )

    top = [node_for(i) for i in range(len(groups)) if parent[i] is None]
    loose = [r for r in refs if r not in assigned]
    return top + trie_groups(
        {r: local_name(by[r].get("address"), prefix) for r in loose}, prefix or "board"
    )


# ------------------------------------------------------------------ build


def sha_file(p):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def pin_label(name):
    return re.sub(r"~\{([^}]*)\}", r"/\1", name or "")


def auto_named(net):
    return bool(re.fullmatch(r"(board[\w.]*-)?[\d-]+", net))


def build(req):
    t0 = time.time()
    raw = json.loads(Path(req["graph"]).read_text())
    rules = (
        json.loads(Path(req["rules"]).read_text())
        if req.get("rules") and Path(req["rules"]).is_file()
        else {}
    )
    comps_raw = {c["ref"]: c for c in raw["components"]}
    board_refs = sorted(comps_raw, key=natural)
    g = cc = None
    blocks = []
    if HAVE_PNR:
        try:
            g = BoardGraph.from_json(Path(req["graph"]).read_text())
            doc = (
                yaml.safe_load(Path(req["constraints"]).read_text())
                if req.get("constraints")
                else {}
            )
            holes = {
                v["name"]
                for v in (doc or {}).get("mounting_hole", [])
                if isinstance(v, dict) and "name" in v
            }
            if holes:
                g.components = [c for c in g.components if c.ref not in holes]
                for net in g.nets:
                    net.pins = [p for p in net.pins if p[0] not in holes]
            cc = compile_constraints(
                doc or {},
                g.refs,
                {c.address: c.ref for c in g.components if c.address},
                {f"{c.address}:{p.name}": p.net for c in g.components for p in c.pads if c.address},
            )
            blocks = extract_blocks(g, cc)
        except Exception as ex:
            warn("constraints/blocks unavailable: %s" % ex)
            g = cc = None
            blocks = []

    # ---- scope -------------------------------------------------------------------
    want = req.get("refs")
    scope = dict(
        kind="board",
        name=raw.get("name") or "board",
        refs=board_refs,
        prefix="",
        ports=[],
        match="board",
    )
    match = None
    if want is not None:
        want = [r for r in want if isinstance(r, str)]
        extra = sorted(set(want) - set(comps_raw), key=natural)
        inside = sorted(set(want) & set(comps_raw), key=natural)
        if extra:
            warn("lane parts not in the netlist: " + " ".join(extra))
        if inside and len(inside) < 0.95 * len(board_refs):
            ws = set(inside)
            best = max(
                blocks, key=lambda b: len(ws & set(b.refs)) / len(ws | set(b.refs)), default=None
            )
            jac = len(ws & set(best.refs)) / len(ws | set(best.refs)) if best else 0
            if best and jac >= 0.5:
                match = best
                scope = dict(
                    kind="block",
                    name=best.name,
                    refs=inside,
                    prefix=best.prefix,
                    source=best.source,
                    ports=list(best.external_nets),
                    internal_nets=list(best.internal_nets),
                    match="exact" if jac == 1 else "approximate",
                    missing_refs=sorted(set(best.refs) - ws, key=natural),
                    extra_refs=sorted(ws - set(best.refs), key=natural),
                    template_key=hashlib.sha256(best.template.encode()).hexdigest()[:12],
                )
                if jac < 1:
                    warn(
                        "lane parts differ from block %s (missing %s, extra %s)"
                        % (best.name, scope["missing_refs"], scope["extra_refs"])
                    )
            else:
                mods = defaultdict(int)
                for r in inside:
                    mods[module_of(comps_raw[r].get("address")) or "board"] += 1
                name = max(mods, key=mods.get) if mods else "parts"
                scope = dict(
                    kind="block",
                    name=name,
                    refs=inside,
                    prefix=name if name != "board" else "",
                    source="subset",
                    ports=[],
                    match="none",
                )
                warn("lane parts match no source block; showing them as a subset")
    inside = set(scope["refs"])
    by = {r: comps_raw[r] for r in inside}

    # ---- power roles ------------------------------------------------------------
    roles = None
    if HAVE_PNR and g is not None:
        try:
            if scope["kind"] == "block" and match is not None:
                w, h = aspect_sizes(g, match)[0][:2]
                sg, sc, sr = sub_board(g, cc, rules, match, w, h)
                roles = derive(sg, sc, sr)
            elif scope["kind"] == "board":
                roles = derive(g, cc, rules, block_of={r: b.name for b in blocks for r in b.refs})
        except Exception as ex:
            warn("power topology unavailable: %s" % ex)
            roles = None
    tier = (roles or {}).get("tier", {})
    ports = set(scope.get("ports") or [])

    # ---- modules and atopile types ------------------------------------------------
    mods = parse_ato(req.get("ato_src"))
    types = ato_types(mods, [c.get("address") or "" for c in raw["components"]])
    modules = {}
    for r in scope["refs"]:
        m = module_of(by[r].get("address"))
        if m:
            modules.setdefault(m, []).append(r)
    module_list = []
    for m, rs in sorted(modules.items()):
        t = next(
            (
                types.get(by[r].get("address"), [None, None])[1]
                for r in rs
                if len(types.get(by[r].get("address"), [])) > 1
            ),
            None,
        )
        module_list.append(
            dict(
                id=m,
                label=m.split(".", 1)[1],
                type=t,
                doc=(mods.get(t) or {}).get("doc", "") if t else "",
                refs=sorted(rs, key=natural),
            )
        )
    by_type = defaultdict(list)
    for m in module_list:
        if m["type"]:
            by_type[m["type"]].append(m["id"])
    for m in module_list:
        m["twins"] = [x for x in by_type.get(m["type"], []) if x != m["id"]]

    # ---- subcircuit tree ------------------------------------------------------------
    hard = hard_groups(cc, scope["refs"]) if cc is not None else []
    if scope["kind"] == "block":
        tree = tree_for(by, scope["refs"], scope.get("prefix") or "", hard)
        units = [
            dict(id="unit:" + scope["name"], label=scope["name"], kind="block", refs=scope["refs"])
        ]
    else:
        tree = []
        top = [r for r in scope["refs"] if not module_of(by[r].get("address"))]
        for m in module_list:
            tree.append(
                dict(
                    kind="module",
                    id=m["id"],
                    label=m["label"] + (" · " + m["type"] if m["type"] else ""),
                    type=m["type"],
                    refs=m["refs"],
                    children=tree_for(by, m["refs"], m["id"], hard),
                )
            )
        tree.extend(tree_for(by, top, "board", hard))
        units = []
        placed = set()
        for nd in tree:
            if nd["kind"] in ("module", "group"):
                units.append(
                    dict(
                        id="unit:" + nd["id"],
                        label=nd["label"],
                        kind=nd["kind"],
                        node=nd["id"],
                        refs=nd["refs"],
                    )
                )
                placed.update(nd["refs"])
        rest = [r for r in scope["refs"] if r not in placed]
        if rest:
            units.insert(0, dict(id="unit:board", label="top level", kind="top", refs=rest))

    # ---- components, symbols, pad -> pin ------------------------------------------------
    symbols, comps, issues = {}, [], []
    cache_dir = req.get("symbol_cache")
    sigs = {}
    for r in scope["refs"]:
        c = by[r]
        lib = (c.get("footprint") or "").split(":")[0]
        pads = [p["name"] for p in c.get("pads", [])]
        key = lib
        if key not in symbols:
            sym, signals, reason = (
                cached_part_symbol(req["parts"], lib, cache_dir)
                if req.get("parts")
                else (None, {}, "no parts dir")
            )
            if sym is None:
                warn("%s: generic symbol (%s)" % (lib or r, reason))
            symbols[key] = sym
            sigs[key] = signals
        sym = symbols[key]
        if sym is None or not (sym["units"][0]["pins"]):
            key = "generic:" + (lib or r) + ":" + str(len(set(pads)))
            if key not in symbols:
                symbols[key] = generic_symbol(
                    lib or r, pads, sigs.get(lib), reason="missing or unparsable symbol"
                )
            sym = symbols[key]
        pinnums = {p["number"] for u in sym["units"] for p in u["pins"]}
        if not pinnums & {p for p in pads if p} and pads:
            key = "generic:" + (lib or r) + ":" + str(len(set(pads)))
            if key not in symbols:
                symbols[key] = generic_symbol(
                    lib or r, pads, sigs.get(lib), reason="no symbol pin matches a pad"
                )
            warn("%s: symbol pins do not match pads; generic symbol" % r)
            sym = symbols[key]
            pinnums = {p["number"] for u in sym["units"] for p in u["pins"]}
        pins, unmapped = {}, []
        for p in c.get("pads", []):
            if p["name"] in pinnums:
                cur = pins.setdefault(p["name"], dict(number=p["name"], net=p["net"], pads=0))
                cur["pads"] += 1
                if cur["net"] != p["net"]:
                    issues.append(
                        dict(
                            ref=r,
                            pin=p["name"],
                            kind="pad_net_conflict",
                            nets=[cur["net"], p["net"]],
                        )
                    )
            else:
                unmapped.append(dict(pad=p["name"], net=p["net"]))
        nc = sorted(pinnums - set(pins), key=natural)
        t = types.get(c.get("address") or "", [])
        pdoc = part_doc(req.get("parts"), lib)
        val, wrapper = value_text(c.get("address") or "", t, mods, sym.get("value") or "", pdoc)
        comps.append(
            dict(
                ref=r,
                address=c.get("address"),
                instance=instance(c.get("address")),
                local=local_name(c.get("address"), scope.get("prefix") or ""),
                module=module_of(c.get("address")),
                lib=key,
                unit=1,
                value=val,
                type=wrapper,
                mpn=sym.get("value"),
                doc=pdoc,
                ref_prefix=sym.get("ref_prefix"),
                footprint=(c.get("footprint") or ":").split(":", 1)[1],
                pins=sorted(pins.values(), key=lambda x: natural(x["number"])),
                unmapped_pads=unmapped,
                pins_without_pad=nc,
                power=dict(
                    tier=tier.get(r),
                    mixed=r in (roles or {}).get("mixed", []),
                    controller=r in (roles or {}).get("controllers", []),
                    carrying=(roles or {}).get("carrying", {}).get(r, {}),
                ),
            )
        )
        if unmapped and any(u["net"] for u in unmapped):
            issues.append(dict(ref=r, kind="pad_without_pin", pads=unmapped))
    pin_name = {}
    for cp in comps:
        s = symbols[cp["lib"]]
        names = {p["number"]: p["name"] for u in s["units"] for p in u["pins"]}
        for p in cp["pins"]:
            pin_name[(cp["ref"], p["number"])] = (
                pin_label(names.get(p["number"], "")) or p["number"]
            )

    # ---- nets ------------------------------------------------------------------------
    P = set((roles or {}).get("power_nets", []))
    R = set((roles or {}).get("return_nets", []))
    if roles is None:  # no runtime: classify by name
        for n in raw["nets"]:
            if n["name"] in ("lv", "gnd", "GND") or n["name"].endswith("-lv"):
                R.add(n["name"])
            elif (
                n["name"].endswith("hv") or n["name"].startswith("p") and n["name"].endswith("-hv")
            ):
                P.add(n["name"])
    loop_nets = {n for loop in (roles or {}).get("loops", []) for n in loop["nets"]}
    unit_of = {}
    if scope["kind"] == "board":
        for u in units:
            for r in u["refs"]:
                unit_of[r] = u["id"]
    npins = {cp["ref"]: len(cp["pins"]) for cp in comps}
    nets = []
    for n in raw["nets"]:
        pins = sorted(
            {(r, p) for r, p in n["pins"] if r in inside},
            key=lambda x: (natural(x[0]), natural(x[1])),
        )
        if not pins:
            continue
        total = len({(r, p) for r, p in n["pins"]})
        kind = "ground" if n["name"] in R else "power" if n["name"] in P else "signal"
        k = len({r for r, _ in pins})
        span = len({unit_of.get(r, "unit:board") for r, _ in pins}) if unit_of else 1
        port = n["name"] in ports
        if kind == "ground":
            draw = "rail"
        elif len(pins) == 1 and not port:
            draw = "nc"
        elif kind == "power" and port:
            draw = "rail"
        elif kind == "power" and n["name"] in loop_nets and span == 1:
            draw = "wire"
        elif kind == "power" and (k >= RAIL_MIN_PARTS or span > 1):
            draw = "rail"
        elif kind == "signal" and (k >= LABEL_MIN_PARTS or span > 1 or (port and k == 1)):
            draw = "label"
        elif k == 1:
            draw = "label"  # several pins of one part on one net: a label pair, not a self-loop
        else:
            draw = "wire"
        alias = None
        if auto_named(n["name"]) and kind != "ground":
            hub = max(pins, key=lambda x: (npins.get(x[0], 0), natural(x[0])))
            alias = "%s.%s" % (hub[0], pin_name.get(hub, hub[1]))
        nets.append(
            dict(
                name=n["name"],
                label="GND" if kind == "ground" else (alias or n["name"]),
                alias=alias,
                kind=kind,
                draw=draw,
                pins=[list(p) for p in pins],
                pins_total=total,
                port=port,
                span=span,
                loop=n["name"] in loop_nets,
            )
        )

    # ---- twins: parallel identical parts (same family, footprint, pin->net map) -------------
    arrays = []
    comp_by = {c["ref"]: c for c in comps}

    def walk(nodes):
        for nd in nodes:
            if nd["kind"] == "stem":
                sig = defaultdict(list)
                for r in nd["refs"]:
                    cp = comp_by[r]
                    sig[(cp["lib"], tuple((p["number"], p["net"]) for p in cp["pins"]))].append(r)
                for key, rs in sig.items():
                    if len(rs) >= 2:
                        arrays.append(
                            dict(
                                id="array:%s:%s" % (nd["id"], rs[0]),
                                refs=sorted(rs, key=natural),
                                lib=key[0],
                                stem=nd["key"],
                            )
                        )
            walk(nd.get("children", []))

    walk(tree)

    # ---- highlights ----------------------------------------------------------------------
    highlights = []
    if scope["kind"] == "block":
        highlights.append(
            dict(
                id="block:" + scope["name"],
                label="Block " + scope["name"],
                style="block",
                refs=scope["refs"],
                nets=sorted(ports),
            )
        )
    else:
        tkey = defaultdict(list)
        for b in blocks:
            tkey[b.template].append(b.name)
        for b in blocks:
            highlights.append(
                dict(
                    id="block:" + b.name,
                    label="Block " + b.name,
                    style="block",
                    source=b.source,
                    refs=sorted(b.refs, key=natural),
                    nets=list(b.external_nets),
                    template_key=hashlib.sha256(b.template.encode()).hexdigest()[:12],
                    twins=[x for x in tkey[b.template] if x != b.name],
                )
            )
    classes = (roles or {}).get("classes", [])
    for i, l in enumerate((roles or {}).get("loops", [])):
        members = [classes[k]["members"] for k in l["classes"]]
        highlights.append(
            dict(
                id="loop:%d" % i,
                label=("Hot loop " if l["hot"] else "Conduction path ")
                + " › ".join(short_class(classes[k]["members"]) for k in l["classes"]),
                style="hot-loop" if l["hot"] else "power-path",
                refs=sorted({r for m in members for r in m if r in inside}, key=natural),
                nets=l["nets"],
                peak_a=l.get("peak_a"),
                weight=round(l.get("weight", 0), 4),
                labels=l.get("labels"),
                classes=[sorted(m, key=natural) for m in members],
                links=[
                    dict(
                        net=x["net"],
                        a=sorted(classes[x["a"]]["members"], key=natural),
                        b=sorted(classes[x["b"]]["members"], key=natural),
                    )
                    for x in l["links"]
                ],
            )
        )
    tier_names = {
        1: "Power stage (tier 1)",
        2: "Controller (tier 2)",
        3: "Support passives (tier 3)",
    }
    for t in (1, 2, 3):
        rs = sorted((r for r, v in tier.items() if v == t and r in inside), key=natural)
        if rs:
            highlights.append(
                dict(id="tier:%d" % t, label=tier_names[t], style="tier", tier=t, refs=rs, nets=[])
            )

    symbols = {
        k: s for k, s in symbols.items() if s is not None
    }  # libraries replaced by generic boxes
    payload = dict(
        schema=SCHEMA,
        built_at=time.time(),
        source=dict(
            graph=req["graph"],
            graph_sha256=sha_file(req["graph"]),
            rules=req.get("rules"),
            constraints=req.get("constraints"),
            parts=req.get("parts"),
            ato_src=req.get("ato_src"),
            runtime=os.environ.get("PYTHONPATH", ""),
            power_topology=(roles or {}).get("schema"),
        ),
        scope=scope,
        modules=module_list,
        units=units,
        symbols=symbols,
        components=comps,
        nets=nets,
        tree=tree,
        arrays=arrays,
        power=(
            dict(
                power_nets=sorted(P),
                return_nets=sorted(R),
                envelope=(roles or {}).get("envelope", {}),
                classes=[
                    dict(id="class:%d" % i, members=k["members"], nets=k["nets"], shunt=k["shunt"])
                    for i, k in enumerate(classes)
                ],
                series=(roles or {}).get("series"),
                controllers=(roles or {}).get("controllers", []),
                mixed=(roles or {}).get("mixed", []),
            )
            if roles
            else None
        ),
        highlights=highlights,
        issues=issues,
        warnings=WARN,
        layout_hints=dict(
            label_min_parts=LABEL_MIN_PARTS,
            rail_min_parts=RAIL_MIN_PARTS,
            input_ports=((roles or {}).get("series") or {}).get("ports", [None])[:1],
            output_ports=((roles or {}).get("series") or {}).get("ports", [None, None])[1:],
        ),
        seconds=round(time.time() - t0, 3),
    )
    return payload


def short_class(members):
    ms = sorted(members, key=natural)
    return (
        ms[0]
        if len(ms) == 1
        else "%s…%s (%d)" % (ms[0], ms[-1], len(ms)) if len(ms) > 3 else "|".join(ms)
    )


def main(argv):
    req = json.loads(Path(argv[1]).read_text())
    out = Path(argv[2])
    try:
        payload = build(req)
    except Exception as ex:
        payload = dict(
            schema=SCHEMA,
            error="%s: %s" % (type(ex).__name__, ex),
            trace=traceback.format_exc(),
            warnings=WARN,
        )
    tmp = out.with_name(out.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.replace(out)
    return 0 if "error" not in payload else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
