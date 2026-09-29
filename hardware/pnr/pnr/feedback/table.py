"""Pooled connection failure statistics of one scope (template or halving run).

Built from scratch every generation from the records' feedback (never updated
in place), so a resume rebuilds the same table. A node is
``dict(id=..., parent=<id or None>, fb=<signals record>)``; nodes whose
feedback is missing, or (with ``router``) was produced by another router, do not
count as evaluations: failure statistics depend on the router (flags agree
Jaccard 0.93-0.96 between repeats of one router, 0.27-0.31 across shove on/off).
"""
from __future__ import annotations

import hashlib
import json

FLOOR_RATE = 0.9        # a connection failing in >= 90% of evaluations ...
FLOOR_MIN_EVALS = 6     # ... of at least this many is the structural floor: reported, never a move target
PAIR_MIN_RATE = 0.25    # population attraction only for connections failing this often


class Table:
    def __init__(self, nodes, scope='', router=None):
        self.scope, self.router = scope, router
        self.nodes = {}
        for n in nodes:
            if n.get('id') is not None:
                self.nodes[n['id']] = n
        evals = [n for n in self.nodes.values() if n.get('fb') and not n['fb'].get('missing')
                 and (router is None or n['fb'].get('router') == router)]
        self.n_evals = len(evals)
        conns = {}
        self._failed = {}
        for n in sorted(evals, key=lambda n: str(n['id'])):
            ids = set()
            for c in n['fb'].get('conns') or []:
                ids.add(c['id'])
                e = conns.setdefault(c['id'], dict(id=c['id'], net=c.get('net'), mode=c.get('mode'),
                                                   a=list(c['a']), b=list(c['b']), fails=0, no_room=0))
                e['fails'] += 1
                e['no_room'] += bool(c.get('no_room'))
            self._failed[n['id']] = ids
        for e in conns.values():
            e['f'] = e['fails'] / self.n_evals if self.n_evals else 0.0
        self.conns = {k: conns[k] for k in sorted(conns)}
        self.floor = sorted(k for k, e in self.conns.items()
                            if self.n_evals >= FLOOR_MIN_EVALS and e['f'] >= FLOOR_RATE)

    def rate(self, cid):
        e = self.conns.get(cid)
        return e['f'] if e else 0.0

    def is_floor(self, cid):
        return cid in self.floor

    def lineage(self, node_id):
        """[node_id, parent, grandparent, ...] (cycle-safe)."""
        out, seen = [], set()
        while node_id is not None and node_id not in seen and node_id in self.nodes:
            seen.add(node_id)
            out.append(node_id)
            node_id = self.nodes[node_id].get('parent')
        return out

    def lineage_fails(self, cid, node_id):
        """Evaluations along ``node_id``'s lineage (itself included) where ``cid`` failed."""
        return sum(cid in self._failed.get(n, ()) for n in self.lineage(node_id))

    def pair_weights(self, weight=10.0, cap=20.0, min_rate=PAIR_MIN_RATE):
        """{(key_a, pad_a, key_b, pad_b): min(cap, weight * f)} for frequent, non-floor failures."""
        out = {}
        for cid, e in self.conns.items():
            if e['f'] >= min_rate and cid not in self.floor:
                out[(e['a'][0], e['a'][1], e['b'][0], e['b'][1])] = min(cap, weight * e['f'])
        return out

    def to_json(self):
        return dict(scope=self.scope, router=self.router, n_evals=self.n_evals, floor=self.floor,
                    conns=[dict(e, f=round(e['f'], 4)) for e in self.conns.values()])

    def sha(self):
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()[:12]


def build(nodes, scope='', router=None):
    return Table(nodes, scope, router)
