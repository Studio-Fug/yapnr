#!/usr/bin/env python3
"""Compare two pad-level JSON netlists (from netlist_json.py) net by net.

    python3 netlist_diff.py ATOPILE.json ROUTED.json > diff.txt

Nets are compared as sets of (ref, pad) members, so a renamed net with the
same membership is reported as a rename, not a break. Pure stdlib; any Python 3.
"""
import json
import sys
from collections import defaultdict


def load(path):
    pads = json.load(open(path))["pads"]
    by_pad = {(p["ref"], p["pad"]): p["net"] for p in pads}
    nets = defaultdict(set)
    for key, net in by_pad.items():
        if net:
            nets[net].add(key)
    return by_pad, nets


def fmt(keys):
    return " ".join(f"{r}.{p}" for r, p in sorted(keys))


def main(a_path, b_path):
    a_pad, a_net = load(a_path)
    b_pad, b_net = load(b_path)
    out = [f"A = {a_path.split('/')[-1]}  B = {b_path.split('/')[-1]}"]
    only_a = sorted(set(a_pad) - set(b_pad))
    only_b = sorted(set(b_pad) - set(a_pad))
    out.append(f"pads: A={len(a_pad)} B={len(b_pad)} only-A={len(only_a)} only-B={len(only_b)}")
    if only_a:
        out.append("  pads only in A: " + fmt(only_a))
    if only_b:
        out.append("  pads only in B: " + fmt(only_b))
    common = set(a_pad) & set(b_pad)
    b_by_members = {frozenset(m & common): n for n, m in b_net.items()}
    same = renamed = 0
    diffs = []
    for net in sorted(a_net):
        members = a_net[net] & common
        if not members:
            continue
        if b_by_members.get(frozenset(members)) == net:
            same += 1
        elif frozenset(members) in b_by_members:
            renamed += 1
            diffs.append(f"RENAMED  {net} -> {b_by_members[frozenset(members)]}")
        else:
            bnets = defaultdict(set)
            for k in members:
                bnets[b_pad[k] or "<no net>"].add(k)
            parts = "; ".join(f"{n}: {fmt(ks)}" for n, ks in sorted(bnets.items()))
            diffs.append(f"DIFFERS  {net} ({len(members)} pads) -> {parts}")
    out.append(
        f"nets in A: {len(a_net)}  identical: {same}  renamed: {renamed}  "
        f"differing: {len(diffs) - renamed}"
    )
    out.extend(diffs)
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
