"""Mechanical power-stage topology for power-first placement (``PNR_POWER_FIRST=1``).

Derives from source data only which parts form the power stage, the order in
which the placement objective organises them, and the hot current loops that
must stay compact. Nothing here reads reference designators, values or
footprints; renaming refs changes labels only. Pure Python (no numpy/torch) so
the core ``pnr`` library, :mod:`pnr.hier.synth_native` and
:mod:`pnr.hier.native_block` can import it.

Nets and currents
  R  return nets: net classes with a ``plane_layer`` plus ``plane_access_intents`` nets
  P  power nets: other nets whose :func:`pnr.electrical.net_policy` mode is ``power``
  E  envelope: ``rules['electrical_nets']`` rms/peak (@pnr-current, net scope). A return
     net uses its largest plane-access budget, else the largest envelope on the board
     (as does a power net that only has a class width).
  I(ref, pad): the terminal budget covering the pad (a group budget applies to every
     pad of the group), else E(net).

Carrying pad: net in P or R, I >= CARRY_FRAC * E(net), and
  * series bound: a part with exactly two connected nets passes at most the smaller
    end current (a return end is unbounded, a signal end carries nothing);
  * pad share: a part with three or more connected nets and no terminal budget on
    the net carries it only if >= PAD_SHARE of its connected pads are on P or R.
A connected net has two or more pins or is a block port.

Tiers: 1 = carrying pads on >= 2 distinct P/R nets; controller = >= 3 distinct
signal nets (not P/R, connected); mixed = tier 1 and controller; 2 = controllers
not in tier 1; 3 = everything else.

Weights: w_n = outer_width_mm(n) / fab.track_width_mm on P nets, 1 on R nets,
W = max w_n. Loops: minimum cycle basis (Horton, GF(2) elimination) of the
bipartite graph of tier-1 classes (parts with identical carrying-net sets, and at
top level the same block) and
P/R nets. A cycle through a shunt class (two-net parts across one P and one R
net) is a hot loop, weight W * Ipk(loop) / max hot Ipk; any other cycle is a
conduction path and gets no term.
"""

from __future__ import annotations

import fnmatch
import itertools
import math
import os
from collections import deque

FLAG = "PNR_POWER_FIRST"
SCHEMA = "pnr-power-topology-v1"
CARRY_FRAC = 0.25
PAD_SHARE = 0.5
CONTROLLER_MIN_SIGNAL_NETS = 3
MAX_LOOP_COMBOS = 64


def enabled() -> bool:
    """True when the power-first placement path is requested."""
    return os.environ.get(FLAG) == "1"


class PowerTopologyUnavailable(ValueError):
    """The rules carry no current data, so no power-first roles can be derived."""


def _plane_patterns(constraints, rules):
    if constraints is not None:
        return [p for nc in constraints.net_classes if nc.plane_layer for p in nc.nets]
    return [
        p
        for nc in rules.get("net_classes") or []
        if nc.get("plane_layer")
        for p in nc.get("nets", [])
    ]


def _unique(seq):
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def minimum_cycle_basis(adj, key):
    """Horton minimum cycle basis of an undirected simple graph.

    ``adj`` maps node -> iterable of neighbours; ``key`` orders nodes (ties and
    candidate order are resolved by it, so the result is deterministic). Returns
    cycles as vertex lists (closing edge implicit), shortest first.
    """
    nodes = sorted(adj, key=key)
    nbrs = {v: sorted(set(adj[v]), key=key) for v in nodes}
    edges = sorted(
        {tuple(sorted((u, v), key=key)) for u in nodes for v in nbrs[u]},
        key=lambda e: (key(e[0]), key(e[1])),
    )
    bit = {e: i for i, e in enumerate(edges)}
    seen, ncomp = set(), 0
    for v in nodes:
        if v in seen:
            continue
        ncomp += 1
        todo = [v]
        seen.add(v)
        while todo:
            u = todo.pop()
            for w in nbrs[u]:
                if w not in seen:
                    seen.add(w)
                    todo.append(w)
    dim = len(edges) - len(nodes) + ncomp
    if dim <= 0:
        return []

    def vector(cyc):
        v = 0
        for a, b in zip(cyc, cyc[1:] + cyc[:1]):
            v |= 1 << bit[tuple(sorted((a, b), key=key))]
        return v

    cands = {}
    for root in nodes:
        prev = {root: None}
        queue = deque([root])
        while queue:
            u = queue.popleft()
            for w in nbrs[u]:
                if w not in prev:
                    prev[w] = u
                    queue.append(w)

        def path(x):
            out = []
            while x is not None:
                out.append(x)
                x = prev[x]
            return out[::-1]

        for x, y in edges:
            if x not in prev or y not in prev or prev[x] == y or prev[y] == x:
                continue
            px, py = path(x), path(y)
            if set(px) & set(py) != {root}:
                continue
            cyc = canonical_cycle(px + py[::-1][:-1], key)
            vec = vector(cyc)
            if vec not in cands:
                cands[vec] = cyc
    rows, basis = [], []
    for vec, cyc in sorted(cands.items(), key=lambda kv: (len(kv[1]), [key(v) for v in kv[1]])):
        r = vec
        for b in rows:
            r = min(r, r ^ b)
        if r:
            rows.append(r)
            rows.sort(reverse=True)
            basis.append(cyc)
            if len(basis) == dim:
                break
    return basis


def canonical_cycle(cyc, key):
    """Rotate to the smallest class vertex, oriented toward its smaller neighbour."""
    classes = [i for i, v in enumerate(cyc) if v[0] == "K"] or list(range(len(cyc)))
    k = min(classes, key=lambda i: key(cyc[i]))
    seq = cyc[k:] + cyc[:k]
    if len(seq) > 2 and key(seq[-1]) < key(seq[1]):
        seq = [seq[0]] + seq[1:][::-1]
    return seq


def derive(graph, constraints, rules, *, block_of=None):
    """Power-first roles of ``graph`` (see module doc). JSON-serialisable dict.

    ``constraints`` supplies plane classes and hard groups (``None``: plane classes
    from ``rules['net_classes']``, no hard-group diagnostic). ``block_of`` maps
    ref -> block name at top level; a cycle whose parts span more than one block
    is a conduction path, not a hot loop.
    """
    from pnr.electrical import net_policy

    en = rules.get("electrical_nets") or {}
    if not any(q.get("rms_current_a") for q in en.values()):
        raise PowerTopologyUnavailable("rules carry no electrical_nets current envelopes")
    track = float((rules.get("fab") or {}).get("track_width_mm", 0.2))
    comps = list(graph.components)
    cidx = {c.ref: i for i, c in enumerate(comps)}
    norder = {n.name: i for i, n in enumerate(graph.nets)}
    npins = {n.name: len(n.pins) for n in graph.nets}
    present = set(norder)
    ports = set(rules.get("block_ports") or [])
    connected = {n for n in present if npins[n] >= 2 or n in ports}
    pats = _plane_patterns(constraints, rules)
    access = [a for a in rules.get("plane_access_intents") or [] if a.get("net") in present]
    R = {n for n in present if any(fnmatch.fnmatch(n, p) for p in pats)} | {
        a["net"] for a in access
    }
    P = {n for n in present - R if net_policy(n, rules)["mode"] == "power"}
    loop_nets = P | R
    signal = connected - loop_nets
    by_net = lambda names: sorted(names, key=norder.get)

    board_rms = max((float(q.get("rms_current_a") or 0) for q in en.values()), default=0.0)
    board_pk = max(
        (float(q.get("peak_current_a") or q.get("rms_current_a") or 0) for q in en.values()),
        default=0.0,
    )
    env, env_source = {}, {}
    for n in by_net(loop_nets):
        q = en.get(n) or {}
        if n in R:
            pa = [a for a in access if a["net"] == n]
            if pa:
                env[n] = (
                    max(float(a["rms_current_a"]) for a in pa),
                    max(float(a.get("peak_current_a", a["rms_current_a"])) for a in pa),
                )
                env_source[n] = "plane_access"
            else:
                env[n] = (board_rms, board_pk)
                env_source[n] = "board_max"
        elif q.get("rms_current_a"):
            env[n] = (float(q["rms_current_a"]), float(q.get("peak_current_a", q["rms_current_a"])))
            env_source[n] = "electrical_nets"
        else:
            env[n] = (board_rms, board_pk)
            env_source[n] = "board_max"

    term = {}
    for a in rules.get("current_intents") or []:
        if a.get("scope") == "terminal" and a.get("ref") in cidx:
            term.setdefault((a["ref"], a["net"]), []).append(a)

    def pad_current(ref, pad):
        recs = [a for a in term.get((ref, pad.net), ()) if pad.name in a["pads"]]
        if recs:
            return sum(float(a["rms_current_a"]) for a in recs), True
        return env[pad.net][0], False

    carrying, pad_amps = {}, {}
    for c in comps:
        conn = [p for p in c.pads if p.net in connected]
        conn_nets = _unique(p.net for p in conn)
        loop_pads = [p for p in conn if p.net in loop_nets]
        share_ok = bool(conn) and len(loop_pads) >= PAD_SHARE * len(conn)
        through = None
        if len(conn_nets) == 2:
            ends = []
            for n in conn_nets:
                if n in R:
                    ends.append(math.inf)
                elif n in P:
                    ends.append(max(pad_current(c.ref, p)[0] for p in conn if p.net == n))
                else:
                    ends.append(0.0)
            through = min(ends)
        for p in loop_pads:
            amps, _ = pad_current(c.ref, p)
            if through is not None:
                amps = min(amps, through)
            elif len(conn_nets) >= 3 and (c.ref, p.net) not in term and not share_ok:
                amps = 0.0
            pad_amps[(c.ref, p.name)] = amps
            if amps >= CARRY_FRAC * env[p.net][0] - 1e-12 and amps > 0:
                pads = carrying.setdefault(c.ref, {}).setdefault(p.net, [])
                if p.name not in pads:
                    pads.append(p.name)
    tier1 = {r for r, nets in carrying.items() if len(nets) >= 2}
    sig_nets = {c.ref: _unique(p.net for p in c.pads if p.net in signal) for c in comps}
    controllers = {r for r, s in sig_nets.items() if len(s) >= CONTROLLER_MIN_SIGNAL_NETS}
    tier = {c.ref: 1 if c.ref in tier1 else 2 if c.ref in controllers else 3 for c in comps}
    mixed = tier1 & controllers

    weight = {n: float(net_policy(n, rules)["outer_width_mm"]) / track for n in by_net(P)}
    W = max(weight.values(), default=1.0)
    w_ret = 1.0

    def pins_on(ref, net, only_carrying):
        if only_carrying:
            return list(carrying.get(ref, {}).get(net, []))
        return _unique(p.name for p in comps[cidx[ref]].pads if p.net == net)

    elements = []
    trunk = {}
    for n in by_net(loop_nets):
        pins = [[c.ref, name] for c in comps if c.ref in tier1 for name in pins_on(c.ref, n, True)]
        trunk[n] = pins
        if len({r for r, _ in pins}) >= 2:
            elements.append(
                dict(
                    kind="bbox",
                    role="trunk",
                    name="trunk:" + n,
                    net=n,
                    stage=1,
                    weight=weight[n] if n in P else w_ret,
                    pins=pins,
                )
            )

    # --- classes and loops -------------------------------------------------------
    class_of, classes = {}, []
    for c in comps:
        if c.ref not in tier1:
            continue
        # Parallel parts share a class; at top level only within one block.
        key = (frozenset(carrying[c.ref]), (block_of or {}).get(c.ref))
        if key not in class_of:
            class_of[key] = len(classes)
            classes.append(dict(members=[], nets=by_net(key[0])))
        classes[class_of[key]]["members"].append(c.ref)
    conn_nets_of = {c.ref: _unique(p.net for p in c.pads if p.net in connected) for c in comps}
    for k in classes:
        nets = k["nets"]
        k["label"] = "|".join(k["members"])
        k["shunt"] = (
            len(nets) == 2
            and len(set(nets) & P) == 1
            and len(set(nets) & R) == 1
            and all(len(conn_nets_of[r]) == 2 for r in k["members"])
        )

    def bipartite(indices):
        adj = {}
        for i in indices:
            for n in classes[i]["nets"]:
                adj.setdefault(("K", i), set()).add(("N", n))
                adj.setdefault(("N", n), set()).add(("K", i))
        return adj

    adj = bipartite(range(len(classes)))
    key = lambda v: (0, v[1]) if v[0] == "K" else (1, norder[v[1]])
    # Only a loop inside one block can be hot, so at top level the cycle basis is
    # taken per block; a cycle through several blocks would be a conduction path.
    groups = {}
    for i, k in enumerate(classes):
        groups.setdefault((block_of or {}).get(k["members"][0]), []).append(i)
    cycles = [
        cyc for indices in groups.values() for cyc in minimum_cycle_basis(bipartite(indices), key)
    ]
    loops = []
    for cyc in cycles:
        ks = [v[1] for v in cyc[0::2]]
        ns = [v[1] for v in cyc[1::2]]
        members = [classes[i]["members"] for i in ks]
        blocks = {block_of.get(r) for m in members for r in m} - {None} if block_of else set()
        hot = any(classes[i]["shunt"] for i in ks) and len(blocks) <= 1
        peak = max((env[n][1] for n in ns if n in P), default=0.0)
        loops.append(
            dict(
                classes=ks,
                labels=[classes[i]["label"] for i in ks],
                nets=ns,
                hot=hot,
                peak_a=peak,
                links=[dict(net=ns[j], a=ks[j], b=ks[(j + 1) % len(ks)]) for j in range(len(ks))],
            )
        )
    top = max((l["peak_a"] for l in loops if l["hot"]), default=0.0)
    for li, l in enumerate(loops):
        l["weight"] = W * l["peak_a"] / top if l["hot"] and top > 0 else 0.0
        if l["weight"] <= 0:
            continue
        members = [classes[i]["members"] for i in l["classes"]]
        combos = math.prod(len(m) for m in members)
        elements.append(
            dict(
                kind="loop",
                role="loop",
                name="loop:" + ">".join(l["labels"]),
                loop=li,
                stage=1,
                weight=l["weight"],
                nets=l["nets"],
                members=members,
                pins={
                    r: {n: pins_on(r, n, True) for n in l["nets"] if n in carrying.get(r, {})}
                    for m in members
                    for r in m
                },
                capped=combos > MAX_LOOP_COMBOS,
            )
        )

    # --- taps, signal nets ---------------------------------------------------------
    early = lambda ref: tier[ref] in (1, 2)
    for n in by_net(P):
        pins_all = _unique((r, p) for r, p in next(x.pins for x in graph.nets if x.name == n))
        tr = {tuple(t) for t in trunk[n]}
        if not tr:
            if len(pins_all) >= 2:
                elements.append(
                    dict(
                        kind="bbox",
                        role="signal",
                        name="net:" + n,
                        net=n,
                        stage=2 if all(early(r) for r, _ in pins_all) else 3,
                        weight=1.0,
                        pins=[list(p) for p in pins_all],
                    )
                )
            continue
        for r, p in pins_all:
            if (r, p) in tr:
                continue
            targets = [t for t in trunk[n] if t[0] != r] or trunk[n]
            elements.append(
                dict(
                    kind="tap",
                    role="tap",
                    name="tap:%s.%s" % (r, p),
                    net=n,
                    stage=2 if early(r) else 3,
                    weight=1.0,
                    pin=[r, p],
                    targets=targets,
                )
            )
    for net in graph.nets:
        if net.name in loop_nets or len(net.pins) < 2:
            continue
        pins = [list(p) for p in _unique(tuple(p) for p in net.pins)]
        elements.append(
            dict(
                kind="bbox",
                role="signal",
                name="net:" + net.name,
                net=net.name,
                stage=2 if all(early(r) for r, _ in pins) else 3,
                weight=1.0,
                pins=pins,
            )
        )

    roles = dict(
        schema=SCHEMA,
        carry_frac=CARRY_FRAC,
        pad_share=PAD_SHARE,
        power_nets=by_net(P),
        return_nets=by_net(R),
        signal_nets=by_net(signal),
        envelope={n: dict(rms_a=env[n][0], peak_a=env[n][1], source=env_source[n]) for n in env},
        weight=weight,
        w_ret=w_ret,
        W=W,
        carrying={r: carrying[r] for r in (c.ref for c in comps) if r in carrying},
        tier=tier,
        tier1=[c.ref for c in comps if c.ref in tier1],
        controllers=[c.ref for c in comps if c.ref in controllers],
        mixed=[c.ref for c in comps if c.ref in mixed],
        classes=classes,
        loops=loops,
        elements=elements,
    )
    roles["series"] = _series_path(roles, adj, key, ports & P, classes)
    roles["hard_group_diagnostic"] = _hard_group_diagnostic(
        roles, constraints, pad_amps, comps, cidx
    )
    return roles


def _series_path(roles, adj, key, port_nets, classes):
    """Longest shortest port-to-port path over power nets (report only)."""
    R = set(roles["return_nets"])
    best = None
    ports = sorted((("N", n) for n in port_nets if ("N", n) in adj), key=key)
    for a, b in itertools.combinations(ports, 2):
        prev, queue = {a: None}, deque([a])
        while queue:
            u = queue.popleft()
            for w in sorted(adj[u], key=key):
                if w[0] == "N" and w[1] in R:
                    continue
                if w not in prev:
                    prev[w] = u
                    queue.append(w)
        if b not in prev:
            continue
        path, x = [], b
        while x is not None:
            path.append(x)
            x = prev[x]
        path.reverse()
        if best is None or len(path) > len(best):
            best = path
    if not best:
        return None
    return dict(
        ports=[best[0][1], best[-1][1]],
        nets=[v[1] for v in best[0::2]],
        classes=[v[1] for v in best[1::2]],
        labels=[classes[v[1]]["label"] for v in best[1::2]],
    )


def _hard_group_diagnostic(roles, constraints, pad_amps, comps, cidx):
    """Hard group edges that tie a hot-loop part to an anchor it shares no carrying power net with.

    Report only: power-first never edits authored constraints. The authored radius
    caps how compact the power stage can become; the decision stays with the author.
    """
    if constraints is None:
        return []
    from pnr.constraints import Enforcement

    carrying = roles["carrying"]
    P = set(roles["power_nets"])
    in_hot = {
        r
        for l in roles["loops"]
        if l["hot"]
        for i in l["classes"]
        for r in roles["classes"][i]["members"]
    }
    out, seen = [], set()
    for con in constraints.constraints:
        if con.kind != "group" or con.enforcement is not Enforcement.HARD:
            continue
        anchor = con.params.get("anchor")
        if anchor not in cidx:
            continue
        for member in sorted((r for r in con.refs if r in cidx), key=cidx.get):
            if member == anchor or member not in in_hot:
                continue
            nets = [n for n in carrying.get(member, {}) if n in P]
            if not nets or any(n in carrying.get(anchor, {}) for n in nets):
                continue
            radius = float(con.params.get("radius_mm") or 5.0)
            k = (anchor, member, radius)
            if k in seen:
                continue
            seen.add(k)
            anchor_amps = {}
            for n in nets:
                amps = [
                    pad_amps.get((anchor, p.name)) for p in comps[cidx[anchor]].pads if p.net == n
                ]
                amps = [a for a in amps if a is not None]
                anchor_amps[n] = max(amps) if amps else None
            out.append(
                dict(
                    anchor=anchor,
                    member=member,
                    radius_mm=radius,
                    member_power_nets=nets,
                    anchor_current_a=anchor_amps,
                    constraint=con.name,
                    finding="hard group ties a hot-loop part to an anchor that carries none of "
                    "its power current; the radius caps how compact the power stage can be",
                )
            )
    return out


def summary(roles):
    """Compact, JSON-friendly digest for logs, trials and the library."""
    tiers = {k: [r for r, t in roles["tier"].items() if t == k] for k in (1, 2, 3)}
    return dict(
        schema=roles["schema"],
        power_nets=roles["power_nets"],
        return_nets=roles["return_nets"],
        tier1=tiers[1],
        tier2=tiers[2],
        tier3=tiers[3],
        mixed=roles["mixed"],
        controllers=roles["controllers"],
        W=roles["W"],
        loops=[
            dict(
                labels=l["labels"],
                nets=l["nets"],
                hot=l["hot"],
                peak_a=l["peak_a"],
                weight=round(l["weight"], 4),
            )
            for l in roles["loops"]
        ],
        series=roles["series"],
        hard_group_diagnostic=roles["hard_group_diagnostic"],
    )
