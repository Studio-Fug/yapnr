"""atopile source resolver for the subset of the language the viewer needs.

Parses every .ato file (defs, docstrings, trailing/paragraph comments, imports,
@pnr annotations), elaborates the instance tree from the entry module, unions
connected nodes (signals, ElectricPower members, package pins) with provenance
per edge, then joins the result to graph.json: components by address, nets by
their exact pad set. Net semantics are mechanical and deterministic; nothing
here guesses from reference designators or net names.

The entry module is given (the configured ato.yaml build target), else it is
chosen among every build entry of the nearest ato.yaml and every module that
instantiates ``board``: the candidate whose instance tree covers the most
netlist addresses wins.
"""

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

STDLIB = {"ElectricPower": ("hv", "lv")}
PASSIVE = {"R", "C", "L", "FB"}
GENERIC = {
    "p1",
    "p2",
    "hv",
    "lv",
    "pos",
    "neg",
    "GND",
    "VDD",
    "VCC",
    "VIN",
    "IN",
    "OUT",
    "EN",
    "shell",
    "VSS",
    "PAD",
    "EP",
}
HEAD = re.compile(r"^(module|component|interface)\s+(\w+)(?:\s+from\s+(\w+))?\s*:$")
NEW = re.compile(r"^(\w+)\s*=\s*new\s+(\w+)$")
SIG = re.compile(r"^signal\s+(\w+)(?:\s*~\s*(.+))?$")
PIN = re.compile(r"^pin\s+(\w+)$")
ASSIGN = re.compile(r"^([A-Za-z_][\w.]*)\.(\w+)\s*=\s*(.+)$")
CONN = re.compile(r"^(.+?)\s*~\s*(.+)$")
REF = re.compile(r"^(?:pin\s+(\w+)|([A-Za-z_]\w*(?:\.\w+)*))$")
ANN = re.compile(r"^\s*#\s*@pnr-([\w-]+)\s+(\{.*\})\s*$")
FROM = re.compile(r'^from\s+"([^"]+)"\s+import\s+(.+)$')
IMP = re.compile(r"^import\s+(.+)$")
KV = re.compile(r'(\w+)="([^"]*)"')
SUPPLY = re.compile(r"^(?:V(?:DD|CC|IN|PWR|BUS)\w*|VS(?:pos)?|VOUT|OUT|LDO_\w+|P\d+V\d+\w*)$")
SOURCE = re.compile(r"^(?:V?OUT\d*|SW\d*|LDO_\w+|PG|B)$")
AUTO = re.compile(r"^(?:\d+|[A-Za-z]|hv|lv|board-.*|.*-\d+(?:-\d+)*)$")
NEG = re.compile(
    r"\b(?:never|not|nor|without|isn't|aren't|don't)\b[^.;:()!?]*", re.I
)  # a negated clause: 'never ground or VBUS'


def sha(b):
    return hashlib.sha256(b).hexdigest()


def split_comment(s):
    q = None
    for i, ch in enumerate(s):
        if q:
            q = None if ch == q else q
        elif ch in "\"'":
            q = ch
        elif ch == "#":
            return s[:i].rstrip(), s[i + 1 :].strip()
    return s.rstrip(), ""


def clean(c):
    return re.sub(r"^[-=\s#]+|[-=\s]+$", "", c or "").strip()


def pretty(v):
    return (v or "").replace("+/- ", "±").replace("+/-", "±")


def clip(s, n=90):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def ref(s):
    m = REF.match(s.strip())
    return None if not m else ("pin", m[1]) if m[1] else ("ref", m[2])


def parse_file(path, rel):
    """One .ato file -> {path, kind, lines, sha, imports, defs, annotations, comments, pragmas,
    unparsed}."""
    raw = path.read_bytes()
    lines = raw.decode("utf-8", "replace").splitlines()
    f = dict(
        path=rel,
        kind="part" if rel.startswith("parts/") else "design",
        lines=len(lines),
        sha=sha(raw),
        imports={},
        stdlib=[],
        defs={},
        annotations=[],
        comments=[],
        pragmas=[],
        unparsed=[],
    )
    cur, lead, para, section, i = None, [], [], "", 0
    while i < len(lines):
        rawl, ln = lines[i], i + 1
        s = rawl.strip()
        i += 1
        if not s:
            lead, para = [], []
            continue
        m = ANN.match(rawl)
        if m:
            try:
                data = json.loads(m[2])
            except ValueError as e:
                data, err = None, str(e)
            a = dict(
                kind=m[1],
                file=rel,
                line=ln,
                text=s,
                scope_def=cur and cur["name"],
                note=" ".join(lead),
            )
            a.update(data=data) if data is not None else a.update(error=err)
            f["annotations"].append(a)
            lead, para = [], []
            continue
        if s.startswith("#"):
            if s.startswith("#pragma"):
                f["pragmas"].append(dict(line=ln, text=s))
                continue
            c = clean(s[1:])
            f["comments"].append(dict(line=ln, text=c, def_=cur and cur["name"]))
            if s[1:].strip().startswith(("--", "==")):
                section, lead, para = c, [], []
            elif c:
                lead = lead + [c]
                para = lead
            continue
        if rawl[0] not in " \t":
            cur, lead, para, section = None, [], [], ""
            code, _ = split_comment(s)
            m = HEAD.match(code)
            if m:
                cur = dict(
                    name=m[2], kind=m[1], base=m[3], file=rel, line=ln, end=ln, doc="", body=[]
                )
                f["defs"][m[2]] = cur
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and lines[j].strip().startswith('"""'):
                    t, k = lines[j].strip()[3:], j
                    while '"""' not in t and k + 1 < len(lines):
                        k += 1
                        t += "\n" + lines[k].strip()
                    cur["doc"] = t.split('"""')[0].strip()
                    cur["end"] = k + 1
                    i = k + 1
            elif FROM.match(code):
                m = FROM.match(code)
                for n in m[2].split(","):
                    f["imports"][n.strip()] = m[1]
            elif IMP.match(code):
                f["stdlib"] += [n.strip() for n in IMP.match(code)[1].split(",")]
            else:
                f["unparsed"].append(dict(line=ln, text=s))
            continue
        if cur is None:
            f["unparsed"].append(dict(line=ln, text=s))
            continue
        code, comment = split_comment(s)
        st = dict(file=rel, line=ln, text=code, lead=lead, para=para, section=section, op="unknown")
        if comment:
            st["comment"] = comment
        lead = []
        if m := NEW.match(code):
            st.update(op="new", name=m[1], type=m[2])
        elif m := SIG.match(code):
            st.update(op="signal", name=m[1], rhs=ref(m[2]) if m[2] else None)
        elif m := PIN.match(code):
            st.update(op="pin", pin=m[1])
        elif code.startswith("trait "):
            st.update(op="trait", kv=dict(KV.findall(code)), trait=code.split()[1].split("<")[0])
        elif code == "pass":
            st.update(op="pass")
        elif (m := ASSIGN.match(code)) and "~" not in code:
            st.update(op="assign", target=m[1], attr=m[2], value=m[3].strip())
        elif (m := CONN.match(code)) and ref(m[1]) and ref(m[2]):
            st.update(op="conn", a=ref(m[1]), b=ref(m[2]))
        cur["body"].append(st)
        cur["end"] = ln
        if st["op"] == "unknown":
            f["unparsed"].append(dict(line=ln, text=s))
    return f


def ato_files(src):
    """(rel, path) for every regular .ato file under src; symlinks and escapes are skipped."""
    src = Path(src).resolve()
    out = []
    for d, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "build")
        for n in sorted(files):
            p = Path(d) / n
            if (
                n.endswith(".ato")
                and not p.is_symlink()
                and p.is_file()
                and p.resolve().is_relative_to(src)
            ):
                out.append((p.relative_to(src).as_posix(), p))
    return out


def find_entries(src):
    """Build entries from the nearest ato.yaml: [(file relative to src, module)]."""
    src = Path(src).resolve()
    for d in [src, *src.parents][:5]:
        y = d / "ato.yaml"
        if y.is_file():
            out = []
            for m in re.finditer(r'^\s*entry:\s*["\']?([^:"\'\s]+):(\w+)', y.read_text(), re.M):
                p = (d / m[1]).resolve()
                if p.is_relative_to(src):
                    out.append((p.relative_to(src).as_posix(), m[2]))
            return out
    return []


class Design:
    def __init__(self, src):
        self.src = Path(src).resolve()
        self.files = {rel: parse_file(p, rel) for rel, p in ato_files(self.src)}
        self.by_name = defaultdict(list)
        for f in self.files.values():
            for rel_imp in list(f["imports"]):
                t = f["imports"][rel_imp]
                cands = [(Path(f["path"]).parent / t).as_posix(), t]
                f["imports"][rel_imp] = next(
                    (c for c in (os.path.normpath(c) for c in cands) if c in self.files), t
                )
            for d in f["defs"].values():
                self.by_name[d["name"]].append(d)
        for f in self.files.values():
            for d in f["defs"].values():
                news = [s for s in d["body"] if s["op"] == "new"]
                sigs = [s for s in d["body"] if s["op"] == "signal"]
                cd = self.lookup(news[0]["type"], f["path"]) if len(news) == 1 else None
                d["wrapper"] = (
                    d["kind"] == "module"
                    and bool(cd)
                    and cd["kind"] == "component"
                    and bool(sigs)
                    and all(
                        s["rhs"]
                        and s["rhs"][0] == "ref"
                        and s["rhs"][1].split(".")[0] == news[0]["name"]
                        for s in sigs
                    )
                    and not [s for s in d["body"] if s["op"] in ("conn", "assign")]
                )
                traits = {}
                for s in d["body"]:
                    if s["op"] == "trait":
                        traits.update({s["trait"] + "." + k: v for k, v in s["kv"].items()})
                d["mpn"], d["manufacturer"] = traits.get("is_atomic_part.partnumber"), traits.get(
                    "is_atomic_part.manufacturer"
                )
                d["prefix"] = traits.get("has_designator_prefix.prefix")
                d["part_links"] = {}
                for key, value in traits.items():
                    field = key.rsplit(".", 1)[-1].lower()
                    if field in ("datasheet", "datasheet_url", "octopart_url", "easyeda_url"):
                        if isinstance(value, str) and value.startswith(("https://", "http://")):
                            d["part_links"][
                                "datasheet_url" if field == "datasheet" else field
                            ] = value

    def lookup(self, name, frm):
        f = self.files.get(frm)
        if f:
            if name in f["defs"]:
                return f["defs"][name]
            t = f["imports"].get(name)
            if t in self.files and name in self.files[t]["defs"]:
                return self.files[t]["defs"][name]
        if name in STDLIB:
            return dict(
                name=name,
                kind="stdlib",
                members=STDLIB[name],
                file=None,
                base=None,
                body=[],
                wrapper=False,
                doc="",
            )
        hits = self.by_name.get(name, [])
        return hits[0] if len(hits) == 1 else None

    def body(self, d, seen=()):
        if d.get("base") and d["name"] not in seen:
            b = self.lookup(d["base"], d["file"])
            if b:
                yield from self.body(b, seen + (d["name"],))
        yield from d["body"]

    def covers(self, d, addresses):
        """Cheap count of graph addresses whose name path resolves under entry def d."""
        n = 0
        for a in addresses:
            t = d
            for seg in a.split("."):
                st = (
                    next((s for s in self.body(t) if s["op"] == "new" and s["name"] == seg), None)
                    if t
                    else None
                )
                t = st and self.lookup(st["type"], st["file"])
                if not t:
                    break
            n += bool(t)
        return n

    # ------------------------------------------------------------ elaboration
    def elaborate(self, entry_file, entry_name):
        root = self.lookup(entry_name, entry_file)
        if not root:
            raise ValueError(f"entry {entry_file}:{entry_name} not found")
        self.entry = f"{entry_file}:{entry_name}"
        self.inst, self.nodes, self.up, self.edges, self.notes = {}, {}, {}, [], []
        self.assign, self.refs, self.pin_sigs = (
            defaultdict(dict),
            defaultdict(list),
            defaultdict(list),
        )
        self._build("", root, None, None)
        for addr, it in list(self.inst.items()):
            if it["kind"] in ("module", "component"):
                self._connect(addr, it)
        kids = [c for c in self.inst[""].get("children", []) if self.inst[c]["kind"] == "module"]
        self.base = kids[0] if len(kids) == 1 else ""
        return self

    def _node(self, path, kind, owner, st, **kw):
        self.nodes.setdefault(path, dict(path=path, kind=kind, owner=owner, st=st, **kw))
        self.up.setdefault(path, path)

    def _build(self, addr, d, st, parent):
        self.inst[addr] = dict(
            addr=addr,
            name=addr.rsplit(".", 1)[-1],
            type=d["name"],
            kind=d["kind"],
            d=d,
            st=st,
            parent=parent,
            children=[],
            wrapper=d.get("wrapper", False),
        )
        if parent is not None:
            self.inst[parent]["children"].append(addr)

        def j(n):
            return f"{addr}.{n}" if addr else n

        if d["kind"] == "stdlib":
            for m in d["members"]:
                self._node(j(m), "member", addr, st, iface=d["name"], member=m)
            return
        for s in self.body(d):
            if s["op"] == "new":
                cd = self.lookup(s["type"], s["file"])
                if cd is None:
                    self.notes.append(f"unresolved type {s['type']} at {s['file']}:{s['line']}")
                else:
                    self._build(j(s["name"]), cd, s, addr)
            elif s["op"] == "signal":
                self._node(j(s["name"]), "signal", addr, s, part=d["kind"] == "component")
            elif s["op"] == "pin":
                self._node(j("pin" + s["pin"]), "pin", addr, s, pad=s["pin"])

    def find(self, x):
        while self.up[x] != x:
            self.up[x] = self.up[self.up[x]]
            x = self.up[x]
        return x

    def _union(self, a, b, prov):
        self.edges.append((a, b, prov))
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.up[max(ra, rb)] = min(ra, rb)

    def _resolve(self, addr, r, prov):
        kind, v = r

        def j(n):
            return f"{addr}.{n}" if addr else n

        if kind == "pin":
            if self.inst[addr]["kind"] != "component":
                self.notes.append(f"pin outside component at {prov['file']}:{prov['line']}")
                return None
            self._node(j("pin" + v), "pin", addr, None, pad=v)
            return j("pin" + v)
        p = j(v)
        if p in self.nodes or p in self.inst:
            return p
        self.notes.append(
            f"unresolved reference {v} in {addr or '<root>'} at {prov['file']}:{prov['line']}"
        )
        return None

    def _touch(self, prov, paths):
        if prov["level"] != "design":
            return
        for p in paths:
            segs = p.split(".")
            for k in range(1, len(segs) + 1):
                pre = ".".join(segs[:k])
                it = self.inst.get(pre)
                if it and (it["kind"] == "component" or it["wrapper"]):
                    self.refs[pre].append(prov)

    def _connect(self, addr, it):
        d = it["d"]
        level = "part" if d["kind"] == "component" else "wrapper" if it["wrapper"] else "design"
        for s in self.body(d):
            prov = dict(file=s["file"], line=s["line"], text=s["text"], scope=addr, level=level)
            if s.get("comment"):
                prov["comment"] = s["comment"]
            for k in ("lead", "para"):
                if s[k]:
                    prov[k] = " ".join(s[k])
            if s["op"] == "signal" and s["rhs"]:
                self._join(addr, ("ref", s["name"]), s["rhs"], prov)
            elif s["op"] == "conn":
                self._join(addr, s["a"], s["b"], prov)
            elif s["op"] == "assign":
                p = self._resolve(addr, ("ref", s["target"]), prov)
                if p:
                    self.assign[p][s["attr"]] = dict(value=s["value"], **prov)
                    self._touch(prov, [p])

    def _join(self, addr, ra, rb, prov):
        a, b = self._resolve(addr, ra, prov), self._resolve(addr, rb, prov)
        if not a or not b:
            return
        self._touch(prov, [a, b])
        if a in self.nodes and b in self.nodes:
            self._union(a, b, prov)
            na, nb = self.nodes[a], self.nodes[b]
            for p, q in ((na, nb), (nb, na)):
                if (
                    p["kind"] == "pin"
                    and q["kind"] == "signal"
                    and p["owner"] == q["owner"] == addr
                ):
                    self.pin_sigs[p["path"]].append(q["path"].rsplit(".", 1)[-1])
        elif (
            a in self.inst
            and b in self.inst
            and self.inst[a]["kind"] == self.inst[b]["kind"] == "stdlib"
            and self.inst[a]["type"] == self.inst[b]["type"]
        ):
            for m in self.inst[a]["d"]["members"]:
                self._union(f"{a}.{m}", f"{b}.{m}", dict(prov, member=m))
        else:
            self.notes.append(f"cannot connect {a} ~ {b} at {prov['file']}:{prov['line']}")

    def rel(self, p):
        return (
            p[len(self.base) + 1 :]
            if self.base and p.startswith(self.base + ".")
            else "" if p == self.base else p
        )


def _load(x):
    if x is None or isinstance(x, dict):
        return x
    return json.loads(Path(x).read_text())


def decl(st):
    return dict(file=st["file"], line=st["line"], text=st["text"]) if st else {}


def build_index(src, graph, rules=None, entry=None):
    """Mechanical source index per the viewer API contract (version 1)."""
    g, rules = _load(graph), _load(rules) or {}
    D = Design(src)
    addrs = [c["address"] for c in g.get("components", []) if c.get("address")]
    cands = [entry] if entry else find_entries(D.src)
    if not cands or not entry:
        cands += [
            (d["file"], d["name"])
            for f in D.files.values()
            if f["kind"] == "design"
            for d in f["defs"].values()
            if d["kind"] == "module"
            and any(s["op"] == "new" and s["name"] == "board" for s in d["body"])
        ]
    cands = [c for c in dict.fromkeys(tuple(c) for c in cands) if D.lookup(c[1], c[0])]
    if not cands:
        raise ValueError("no entry module found")
    gname = str(g.get("name") or "")
    ef, en = (
        max(cands, key=lambda c: (D.covers(D.lookup(c[1], c[0]), addrs), Path(c[0]).stem in gname))
        if len(cands) > 1
        else cands[0]
    )
    D.elaborate(ef, en)
    rel = D.rel
    ref_of = {c["address"]: c["ref"] for c in g.get("components", [])}
    comp_of = {c["ref"]: c for c in g.get("components", [])}

    def pad_node(r, pad):
        return f"{comp_of[r]['address']}.pin{pad}"

    notes = list(dict.fromkeys(D.notes))
    for f in D.files.values():
        if f["kind"] == "design" and f["unparsed"]:
            notes += [f"unparsed {f['path']}:{u['line']}: {u['text']}" for u in f["unparsed"][:5]]

    # classes: root -> members, pads, edges
    members = defaultdict(list)
    for p in D.nodes:
        members[D.find(p)].append(p)
    cls_pads = defaultdict(set)
    for p, n in D.nodes.items():
        if n["kind"] == "pin" and n["owner"] in ref_of:
            cls_pads[D.find(p)].add((ref_of[n["owner"]], n["pad"]))
    cls_edges = defaultdict(list)
    for a, b, prov in D.edges:
        cls_edges[D.find(a)].append(prov)

    # validation: every graph net is exactly one class
    mism, net_cls, comps_missing = [], {}, []
    for c in g.get("components", []):
        it = D.inst.get(c["address"])
        if not it or it["kind"] != "component":
            comps_missing.append(c["ref"])
            mism.append(
                dict(
                    kind="component",
                    ref=c["ref"],
                    address=c["address"],
                    reason="address does not resolve to a component instance",
                )
            )
    for n in g.get("nets", []):
        pads = {(r, str(p)) for r, p in n["pins"]}
        roots = {
            D.find(pad_node(r, p)) if pad_node(r, p) in D.nodes else None
            for r, p in pads
            if r in comp_of
        }
        if None in roots or len(roots) != 1:
            mism.append(
                dict(
                    kind="net",
                    net=n["name"],
                    reason="pads span %d source classes" % len(roots - {None}),
                    missing=sorted(
                        f"{r}.{p}"
                        for r, p in pads
                        if r not in comp_of or pad_node(r, p) not in D.nodes
                    ),
                )
            )
            continue
        root = roots.pop()
        if cls_pads[root] != pads:
            mism.append(
                dict(
                    kind="net",
                    net=n["name"],
                    reason="pad set differs",
                    extra=sorted(f"{r}.{p}" for r, p in cls_pads[root] - pads),
                    missing=sorted(f"{r}.{p}" for r, p in pads - cls_pads[root]),
                )
            )
            continue
        net_cls[n["name"]] = root
    for c in g.get("components", []):
        for p in c["pads"]:
            nd = pad_node(c["ref"], p["name"])
            if p["name"] and not p["net"] and nd in D.nodes and len(cls_pads[D.find(nd)]) > 1:
                mism.append(
                    dict(
                        kind="pad",
                        ref=c["ref"],
                        pad=p["name"],
                        reason="unconnected in graph but connected in source",
                    )
                )
    fp_pads = {(c["address"], p["name"]) for c in g.get("components", []) for p in c["pads"]}
    notes += [
        f"source pin {n['owner']}:{n['pad']} has no footprint pad"
        for n in D.nodes.values()
        if n["kind"] == "pin" and n["owner"] in ref_of and (n["owner"], n["pad"]) not in fp_pads
    ][:20]
    net_of_pad = {
        (c["ref"], p["name"]): p["net"]
        for c in g.get("components", [])
        for p in c["pads"]
        if p["name"]
    }

    # component metadata
    def comp_inst(c):
        a = c["address"]
        it = D.inst.get(a)
        par = D.inst.get(it["parent"]) if it else None
        return par["addr"] if par and par["wrapper"] else a

    def part_label(ref):
        c = comp_of[ref]
        it = D.inst.get(c["address"])
        if not it:
            return c["footprint"].split(":")[0]
        return it["d"].get("mpn") or it["type"].removesuffix("_package")

    def is_passive(ref):
        it = D.inst.get(comp_of[ref]["address"])
        if not it:
            return True
        par = D.inst.get(it["parent"])
        return bool(par and par["wrapper"]) or it["d"].get("prefix") in PASSIVE

    def sigs_of(ref, pad):
        return D.pin_sigs.get(pad_node(ref, pad)) or []

    # annotations -> resolved (ref, pads, net)
    def resolve_target(t, scope_def):
        t = t.strip(".")
        insts = {comp_inst(c): c["ref"] for c in g.get("components", []) if c["address"] in D.inst}
        scopes = (
            [a for a, it in D.inst.items() if it["type"] == scope_def and it["kind"] == "module"]
            if scope_def
            else []
        )
        for cand in [f"{s}.{t}" if s else t for s in scopes] + [
            f"{D.base}.{t}" if D.base else t,
            t,
        ]:
            if cand in insts:
                return insts[cand]
        hits = [r for a, r in insts.items() if a.endswith("." + t)]
        return hits[0] if len(hits) == 1 else None

    anns, cur_by_net, cur_by_ref, pairs_by_net = (
        [],
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
    )
    for f in D.files.values():
        for a in f["annotations"]:
            data = a.get("data") or {}
            rec = dict(kind=a["kind"], file=a["file"], line=a["line"], text=a["text"], data=data)
            if a.get("note"):
                rec["note"] = clip(a["note"], 600)
            if a.get("error"):
                rec["error"] = a["error"]
            if a["kind"] in ("current", "plane-access") and data.get("target"):
                r = resolve_target(data["target"], a["scope_def"])
                pads = [str(p) for p in data.get("pads", [])]
                nets = {net_of_pad.get((r, p)) for p in pads} if r else set()
                rec.update(ref=r, pads=pads, net=nets.pop() if len(nets) == 1 else None)
                if r and rec["net"]:
                    cur = dict(
                        file=a["file"],
                        line=a["line"],
                        target=data["target"],
                        ref=r,
                        pads=pads,
                        rms_current_a=data.get("rms_current_a"),
                        peak_current_a=data.get("peak_current_a"),
                        scope=(
                            data.get("scope", "net") if a["kind"] == "current" else "plane_access"
                        ),
                        annotation="pnr-" + a["kind"],
                    )
                    for k in (
                        "neck_max_length_mm",
                        "kind",
                        "plane",
                        "surface",
                        "max_array_span_mm",
                    ):
                        if k in data:
                            cur[k] = data[k]
                    if rec.get("note"):
                        cur["note"] = rec["note"]
                    cur_by_net[rec["net"]].append(cur)
                    cur_by_ref[r].append(cur)
                else:
                    notes.append(
                        f"@pnr-{a['kind']} at {a['file']}:{a['line']} does not resolve to one net"
                    )
            elif a["kind"] == "pair":
                for pol in ("p", "n"):
                    for term in data.get("terminal_chain", []):
                        t, _, pad = str(term.get(pol, "")).partition(":")
                        r = resolve_target(t, a["scope_def"]) if t else None
                        n = r and net_of_pad.get((r, pad))
                        if n and not any(
                            x["line"] == a["line"] and x["file"] == a["file"]
                            for x in pairs_by_net[n]
                        ):
                            pairs_by_net[n].append(
                                dict(
                                    name=data.get("name"),
                                    polarity=pol,
                                    file=a["file"],
                                    line=a["line"],
                                )
                            )
            anns.append(rec)

    # comment paragraphs (consecutive comment lines) per def, for mention lookup: never a
    # half-sentence line
    def_comments = defaultdict(list)
    for f in D.files.values():
        last = None
        for c in f["comments"]:
            if not (c["def_"] and c["text"]):
                last = None
                continue
            if last and last["def_"] == c["def_"] and last["end"] + 1 == c["line"]:
                last.update(text=last["text"] + " " + c["text"], end=c["line"])
            else:
                last = dict(c, end=c["line"])
                def_comments[(f["path"], c["def_"])].append(last)

    # net classes from rules
    net_class = {}
    for nc in rules.get("net_classes", []) or []:
        for n in nc.get("nets", []):
            net_class.setdefault(n, nc.get("name"))

    # ambiguity of leaf names across classes for low_info
    leaf_cls = defaultdict(set)
    for p, n in D.nodes.items():
        if n["kind"] != "pin":
            leaf_cls[p.rsplit(".", 1)[-1]].add(D.find(p))

    def alias_ok(n):
        if n["kind"] == "member":
            return True
        return n["kind"] == "signal" and not n.get("part") and not D.inst[n["owner"]]["wrapper"]

    def voltage_of(iface):
        v = D.assign.get(iface, {}).get("voltage")
        return v and v["value"]

    def plabel(r):
        it = D.inst.get(comp_of[r]["address"])
        if not it:
            return part_label(r)
        par = D.inst.get(it["parent"])
        return (
            par["type"]
            if par and par["wrapper"]
            else clip(re.sub(r"\s*\(\w+\)", "", it["d"].get("doc") or ""), 30) or part_label(r)
        )

    def words(t):
        return {w.lower() for w in re.findall(r"[A-Za-z0-9_]+", t or "")}

    def said(
        t,
    ):  # the comment minus its negated clauses: 'never ground or VBUS' does not describe GND or VBUS
        return NEG.sub(" ", t or "")

    def mention(text, pin, others):
        """Sentences of a comment paragraph for pin: drops sentences about other pins of the part;
        None if pin is not named."""

        def hit(x, n):
            return re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", said(x))

        keep = [
            x
            for x in re.split(r"(?<=[.!?])\s+", text)
            if hit(x, pin) or not any(hit(x, o) for o in others)
        ]
        return " ".join(keep) if any(hit(x, pin) for x in keep) else None

    def toks(names):
        out = set()
        for n in names:
            for x in n.split("."):
                if len(x) >= 2 and not re.fullmatch(r"p\d+|_p", x):
                    out |= {x.lower(), x.lower()[1:] if re.fullmatch(r"p\d\w*", x) else x.lower()}
        return out

    def order(e):
        return (e["scope"].count("."), e["file"], e["line"])

    nets = {}
    for gn in g.get("nets", []):
        name = gn["name"]
        root = net_cls.get(name)
        pins = []
        for r, p in gn["pins"]:
            c = comp_of.get(r)
            pins.append(
                dict(
                    ref=r,
                    pad=str(p),
                    pin="/".join(sigs_of(r, p)) if c else None,
                    address=c and c["address"],
                    instance=c and rel(comp_inst(c)),
                    part=c and part_label(r),
                )
            )
            if c and is_passive(r):
                pins[-1]["label"] = plabel(r)
        net = dict(name=name, pins=pins, matched=root is not None)
        if net_class.get(name):
            net["net_class"] = net_class[name]
        if root is None:
            net.update(
                title=name,
                kind="unconnected" if len(pins) == 1 else "signal",
                low_info=bool(AUTO.match(name)),
                aliases=[],
                summary="Net not matched to source.",
                comments=[],
                statements=[],
                currents=cur_by_net.get(name, []),
            )
            nets[name] = net
            continue
        al = []
        for n in (D.nodes[p] for p in members[root]):
            if alias_ok(n):
                st = n["st"]
                a = dict(
                    path=n["path"], rel=rel(n["path"]), depth=n["path"].count(".") + 1, **decl(st)
                )
                if n["kind"] == "member":
                    a.update(iface=n["iface"], member=n["member"], interface=rel(n["owner"]))
                    if voltage_of(n["owner"]):
                        a["voltage"] = voltage_of(n["owner"])
                for k in ("comment", "lead", "para", "section"):
                    v = st and st.get(k)
                    if v:
                        a[k] = v if isinstance(v, str) else " ".join(v)
                al.append(a)
        al.sort(key=lambda a: (a["depth"], a["path"]))
        hv = [a for a in al if a.get("member") == "hv"]
        lv = [a for a in al if a.get("member") == "lv"]
        voltage = next((a["voltage"] for a in hv if a.get("voltage")), None)
        currents = sorted(cur_by_net.get(name, []), key=lambda c: (c["file"], c["line"]))
        big = any(c["scope"] == "net" and (c.get("rms_current_a") or 0) >= 1 for c in currents)
        supply = any(SUPPLY.match(s) for p in pins for s in (p["pin"] or "").split("/"))
        kind = (
            "unconnected"
            if len(pins) == 1
            else (
                "ground"
                if lv and not hv
                else (
                    "power"
                    if hv or voltage or net_class.get(name) == "power" or big or supply
                    else "signal"
                )
            )
        )
        # statements: design level, else wrapper/part level (nets formed inside one package)
        edges = cls_edges[root]
        dsn = sorted([e for e in edges if e["level"] == "design"], key=order)
        seen, stmts = set(), []
        for e in dsn or sorted(edges, key=order):
            if (e["file"], e["line"]) not in seen:
                seen.add((e["file"], e["line"]))
                stmts.append(
                    {k: e[k] for k in ("file", "line", "text", "comment") if k in e}
                    | dict(scope=rel(e["scope"]))
                )
        by_ref = defaultdict(list)
        for p in pins:
            by_ref[p["ref"]] += [
                s for s in (p["pin"] or "").split("/") if s and s not in by_ref[p["ref"]]
            ]
        actives = sorted(
            [r for r in by_ref if not is_passive(r)],
            key=lambda r: (
                not any(SOURCE.match(s) for s in by_ref[r]),
                -len(comp_of[r]["pads"]),
                comp_of[r]["address"],
            ),
        )
        passives = [r for r in by_ref if is_passive(r)]
        best = (
            min(
                hv,
                key=lambda a: (not a.get("voltage"), a["depth"], not a.get("comment"), a["path"]),
            )
            if kind == "power" and hv
            else (
                min(
                    al,
                    key=lambda a: (a["depth"], not (a.get("comment") or a.get("lead")), a["path"]),
                )
                if al and kind != "ground"
                else None
            )
        )
        # comments: declarations and statement comments first; paragraph/instance/module comments
        # only when they name this net
        tk = toks(
            [s for r in by_ref for s in by_ref[r]]
            + [a["rel"] for a in al]
            + [a.get("interface", "") for a in al]
            + [rel(comp_inst(comp_of[r])).rsplit(".", 1)[-1] for r in by_ref]
            + ([name] if not AUTO.match(name) else [])
        )

        def ok(t):
            return bool(t) and bool(words(said(t)) & tk)

        # a comment that names this net only inside a negated clause ('never ground or VBUS') is
        # about something else
        tks, own = tk - {x.lower() for x in GENERIC}, tk | (
            {"ground", "gnd"} if kind == "ground" else set()
        )
        denies = (
            lambda t: bool(words(" ".join(m[0] for m in NEG.finditer(t))) & own)
            and not words(said(t)) & tks
        )
        cm = [best.get("comment"), best.get("lead")] if best else []
        cm += [x for a in al if a.get("member") != "lv" for x in (a.get("comment"), a.get("lead"))]
        cm += (
            [e.get("lead") for e in dsn if e.get("member")]
            + [e.get("comment") for e in dsn]
            + [e.get("lead") for e in dsn]
        )
        if kind != "ground":
            cm += [a["para"] for a in al if ok(a.get("para"))] + [
                e["para"] for e in dsn if ok(e.get("para"))
            ]
        if kind in ("signal", "unconnected"):
            for r in by_ref:
                st = (D.inst.get(comp_inst(comp_of[r])) or {}).get("st") or {}
                cm += [
                    x
                    for x in (
                        st.get("comment"),
                        " ".join(st.get("lead") or ()),
                        " ".join(st.get("para") or ()),
                    )
                    if ok(x)
                ]
        if kind != "ground":
            for r in actives:
                par = D.inst.get(D.inst[comp_of[r]["address"]]["parent"])
                if par and par["kind"] == "module":
                    others = {
                        x
                        for p in comp_of[r]["pads"]
                        for x in sigs_of(r, p["name"])
                        if len(x) >= 3 and x not in GENERIC
                    } - set(by_ref[r])
                    for s in by_ref[r]:
                        if len(s) >= 3 and s not in GENERIC:
                            cm += [
                                x
                                for c in def_comments[(par["d"]["file"], par["d"]["name"])]
                                if (x := mention(c["text"], s, others))
                            ]
        comments = [
            clip(c, 240)
            for c in dict.fromkeys(
                c for c in cm if c and not c.startswith("@pnr") and not denies(c)
            )
        ][:8]
        # title
        qual = (
            best
            and (best.get("comment") or best.get("lead"))
            or next((e["comment"] for e in dsn if e.get("comment")), None)
        )
        anchors = actives[:2] or passives[:2]

        def lab(r):
            named = "/".join(by_ref[r]) or "pad " + next(p["pad"] for p in pins if p["ref"] == r)
            return f"{rel(comp_inst(comp_of[r]))}.{named}"

        if kind == "ground":
            title = (
                "GND (common return)"
                if len({a["interface"] for a in lv}) > 1
                else f"GND ({lv[0]['interface']})"
            )
        elif best and best.get("member") == "hv":
            title = f"{best['interface']} {pretty(voltage) + ' ' if voltage else ''}rail"
        elif best:
            title = best["rel"]
        elif len(anchors) == 2:
            a, b = lab(anchors[0]), lab(anchors[1])
            pre = os.path.commonprefix([a.split("."), b.split(".")])
            title = f"{a} ↔ {'.'.join(b.split('.')[len(pre):]) if len(pre) < len(b.split('.')) - 1 else b}"
        elif anchors:
            unconnected = ", unconnected" if kind == "unconnected" else ""
            label = lab(anchors[0]).replace(".pad ", " pad ")
            title = f"{label} ({part_label(anchors[0])}{unconnected})"
        else:
            title = name
        if kind == "unconnected" and not title.endswith("unconnected)"):
            title += " (unconnected)"
        elif qual and kind != "ground":
            title += " — " + clip(qual, 60)
        # summary
        refs = list(dict.fromkeys(p["ref"] for p in pins))
        others = list(
            dict.fromkeys(
                a.get("interface") or a["rel"]
                for a in al
                if a is not best and a.get("member") != "lv"
            )
        )[:4]
        scope = min((e["scope"] for e in dsn or edges), key=lambda s: s.count("."), default="")
        sd = D.inst.get(scope, {}).get("d") or {}
        if kind == "ground":
            ifs = list(dict.fromkeys(a["interface"] for a in lv))
            more = ", …" if len(ifs) > 6 else ""
            s1 = (
                f"Common return joining {len(ifs)} ElectricPower grounds"
                f" ({', '.join(ifs[:6])}{more})."
            )
        elif kind == "unconnected":
            p = pins[0]
            s1 = (
                f"Single pad, connected to nothing else in source: {p['part']} {p['pin'] or 'pad ' + p['pad']} "
                f"({p['instance']}, {p['ref']}.{p['pad']})."
            )
        elif best:
            s1 = (
                ("Power rail " if kind == "power" else "Signal ")
                + str(best.get("interface") if best.get("member") == "hv" else best["rel"])
                + (f" ({pretty(voltage)})" if voltage else "")
                + (f", merged with {', '.join(others)}" if others else "")
                + "."
            )
        elif D.inst.get(scope, {}).get("kind") == "component":
            s1 = f"Pins joined inside the {sd.get('mpn') or sd.get('name')} package definition ({rel(scope)})."
        else:
            s1 = (
                (
                    f"Unnamed {'power ' if kind == 'power' else ''}net inside {rel(scope) or 'the board'} "
                    f"({sd.get('name', '?')}"
                )
                + (f": {clip(sd['doc'].split(chr(10))[0], 80)}" if sd.get("doc") else "")
                + ")"
                + "."
            )
        act = [
            f"{part_label(r)} {'/'.join(by_ref[r]) or '?'} ({rel(comp_inst(comp_of[r]))})"
            for r in actives
        ]
        pas = Counter(plabel(r) for r in passives)
        s2 = (
            ""
            if kind == "unconnected"
            else f"{len(pins)} pads on {len(refs)} parts: "
            + "; ".join(act[:5])
            + (f"; +{len(act) - 5} more parts" if len(act) > 5 else "")
            + (
                (
                    ("; " if act else "")
                    + "passives "
                    + ", ".join(f"{v}× {k}" for k, v in pas.most_common(4))
                    + (", …" if len(pas) > 4 else "")
                )
                if pas
                else ""
            )
            + "."
        )
        netc = [c for c in currents if c["scope"] == "net"]
        s3 = (
            (
                f" Net current envelope {max(c['rms_current_a'] for c in netc)} A rms"
                f" / {max(c['peak_current_a'] for c in netc)} A peak."
            )
            if netc
            else ""
        ) + (f" Note: {clip(comments[0], 160)}" if comments and comments[0] != qual else "")
        pads = {p["pad"] for p in pins}
        net.update(
            title=title,
            kind=kind,
            voltage=voltage,
            scope=rel(scope),
            low_info=bool(AUTO.match(name))
            or name in pads
            or ("-" not in name and len(leaf_cls.get(name, ())) > 1),
            aliases=al,
            summary=" ".join((s1 + " " + s2 + s3).split()),
            comments=comments,
            statements=stmts,
            currents=currents,
        )
        if voltage is None:
            net.pop("voltage")
        if pairs_by_net.get(name):
            net["pairs"] = pairs_by_net[name]
        nets[name] = net

    # components
    comps = {}
    for c in g.get("components", []):
        it = D.inst.get(c["address"])
        if not it:
            comps[c["ref"]] = dict(
                address=c["address"],
                instance=c["address"].removesuffix("._p"),
                matched=False,
                pins={p["name"]: dict(pin=None, net=p["net"]) for p in c["pads"] if p["name"]},
            )
            continue
        ia = comp_inst(c)
        ii = D.inst[ia]
        chain, a = [], c["address"]
        while a and a in D.inst:
            x = D.inst[a]
            chain.append(dict(address=a, module=x["type"], **decl(x["st"])))
            a = x["parent"]
        chain.reverse()
        st = ii["st"] or {}
        doc = ii["d"].get("doc") or it["d"].get("doc") or ""
        seen, stmts = set(), []
        for pv in sorted(
            D.refs.get(ia, []) + (D.refs.get(c["address"], []) if ia != c["address"] else []),
            key=lambda e: (e["file"], e["line"]),
        ):
            if (pv["file"], pv["line"]) not in seen:
                seen.add((pv["file"], pv["line"]))
                stmts.append(
                    {k: pv[k] for k in ("file", "line", "text", "comment") if k in pv}
                    | dict(scope=rel(pv["scope"]))
                )
        tk = toks([ii["name"]] + [x for p in c["pads"] for x in sigs_of(c["ref"], p["name"])])
        cm = [x for x in (st.get("comment"), " ".join(st.get("lead") or ())) if x] + [
            x for x in [" ".join(st.get("para") or ())] if x and words(x) & tk
        ]
        comps[c["ref"]] = dict(
            address=c["address"],
            instance=ia,
            rel=rel(ia),
            type=ii["type"],
            part=it["type"],
            mpn=it["d"].get("mpn"),
            manufacturer=it["d"].get("manufacturer"),
            part_links={
                **it["d"].get("part_links", {}),
                **{
                    key: c[key]
                    for key in ("datasheet_url", "octopart_url", "easyeda_url")
                    if isinstance(c.get(key), str) and c[key].startswith(("https://", "http://"))
                },
            },
            passive=is_passive(c["ref"]),
            doc=doc,
            comments=list(dict.fromkeys(cm)),
            file=st.get("file"),
            line=st.get("line"),
            text=st.get("text"),
            chain=chain,
            statements=stmts,
            pins={
                p["name"]: dict(
                    pin="/".join(sigs_of(c["ref"], p["name"])) or None, net=p["net"] or None
                )
                for p in c["pads"]
                if p["name"]
            },
            currents=sorted(cur_by_ref.get(c["ref"], []), key=lambda x: (x["file"], x["line"])),
            matched=True,
        )
        if not doc:
            comps[c["ref"]].pop("doc")

    # (file, line) -> refs/nets, for source-to-board navigation
    lines = defaultdict(lambda: defaultdict(lambda: dict(refs=[], nets=[])))

    def add(f, line, k, v):
        if f and line and v not in lines[f][str(line)][k]:
            lines[f][str(line)][k].append(v)

    for r, c in comps.items():
        for x in c.get("chain", []) + c.get("statements", []) + c.get("currents", []):
            add(x.get("file"), x.get("line"), "refs", r)
    for n, v in nets.items():
        for x in v["statements"] + v["aliases"] + v["currents"]:
            add(x.get("file"), x.get("line"), "nets", n)

    modules = {}
    for f in D.files.values():
        for d in f["defs"].values():
            modules.setdefault(
                d["name"],
                dict(
                    file=d["file"],
                    line=d["line"],
                    end=d["end"],
                    doc=d["doc"],
                    base=d["base"],
                    kind=d["kind"],
                    **({"wrapper": True} if d["wrapper"] else {}),
                    **({"mpn": d["mpn"]} if d.get("mpn") else {}),
                ),
            )
    matched = sum(1 for n in g.get("nets", []) if n["name"] in net_cls)
    return dict(
        version=1,
        src_root=str(D.src),
        entry=D.entry,
        base=D.base,
        files=[
            dict(path=f["path"], kind=f["kind"], lines=f["lines"], sha=f["sha"])
            for f in D.files.values()
        ],
        modules=modules,
        components=comps,
        nets=nets,
        annotations=anns,
        lines={f: dict(v) for f, v in lines.items()},
        validation=dict(
            nets_total=len(g.get("nets", [])),
            nets_matched=matched,
            components_total=len(comps),
            components_matched=len(comps) - len(comps_missing),
            mismatches=mism,
            notes=notes[:200],
            source_classes=len(members),
            entry_candidates=[f"{a}:{b}" for a, b in cands],
        ),
    )


def dossier(index):
    """(sha, compact per-net dossiers) of the mechanical facts an LLM may summarise."""
    out = []
    for n in sorted(index["nets"].values(), key=lambda n: n["name"]):
        out.append(
            dict(
                name=n["name"],
                title=n["title"],
                kind=n["kind"],
                voltage=n.get("voltage"),
                net_class=n.get("net_class"),
                low_info=n.get("low_info"),
                scope=n.get("scope"),
                summary=n.get("summary"),
                aliases=[a["rel"] for a in n["aliases"]][:8],
                pins=[
                    f"{p['ref']}.{p['pad']} {p['part']} {p['pin'] or '-'} ({p['instance']})"
                    for p in n["pins"]
                ][:14],
                more_pins=max(0, len(n["pins"]) - 14),
                comments=n["comments"][:5],
                currents=[
                    f"{c['scope']} {c['rms_current_a']}A rms/{c['peak_current_a']}A pk"
                    f" at {c['target']}:{','.join(c['pads'])}"
                    for c in n["currents"]
                ][:5],
                current_notes=list(
                    dict.fromkeys(clip(c["note"], 300) for c in n["currents"] if c.get("note"))
                )[:3],
                statements=[f"{s['file']}:{s['line']} {s['text']}" for s in n["statements"]][:8],
            )
        )
    return sha(json.dumps(out, sort_keys=True, separators=(",", ":")).encode()), out


if __name__ == "__main__":
    import sys
    import time

    t = time.time()
    ix = build_index(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    v = ix["validation"]
    print(
        json.dumps(
            {
                k: v[k]
                for k in (
                    "nets_total",
                    "nets_matched",
                    "components_total",
                    "components_matched",
                    "source_classes",
                )
            }
        ),
        f"{time.time() - t:.2f}s",
    )
    for m in v["mismatches"][:20]:
        print("MISMATCH", m)
    for n in v["notes"][:30]:
        print("NOTE", n)
