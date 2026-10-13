"""Deterministic Euclidean minimum spanning trees of displayed placement pads.

This is geometric topology, not routed connectivity or the smooth solver objective.
Repeated physical pads with the same (reference, pin) are one electrical terminal.
"""

import math


def spans(graph, decision=None):
    pads = {}
    moves = {m["ref"]: m for m in (decision or {}).get("moves", [])}
    for c in graph.get("components", []):
        pos, rot = c["pos"], c.get("rot", 0)
        if c["ref"] in moves:
            pos, rot = moves[c["ref"]]["after"], moves[c["ref"]]["rotation"]
        if decision and c["ref"] == decision["ref"]:
            pos, rot = decision["position"], decision["rotation"]
        angle = math.radians(rot)
        ct, st = math.cos(angle), math.sin(angle)
        for p in c.get("pads", []):
            x, y = p["offset"]
            point = [pos[0] + x * ct - y * st, pos[1] + x * st + y * ct]
            pads.setdefault((c["ref"], str(p["name"])), []).append(point)
    nets = []
    for net in sorted(graph.get("nets", []), key=lambda n: n["name"]):
        pins = sorted({(ref, str(pin)) for ref, pin in net["pins"]})
        missing = [list(pin) for pin in pins if pin not in pads]
        pins = [pin for pin in pins if pin in pads]
        edges = []
        if pins:
            # Prim with an O(n) frontier; tie breaks are stable by terminal and pad.
            remaining, best, current = set(range(1, len(pins))), {}, 0
            while remaining:
                for j in sorted(remaining):
                    distance, a, b = min(
                        (math.dist(a, b), a, b) for a in pads[pins[current]] for b in pads[pins[j]]
                    )
                    candidate = (distance, current, j, a, b)
                    if j not in best or candidate < best[j]:
                        best[j] = candidate
                distance, i, j, a, b = min(best[k] for k in remaining)
                edges.append(
                    dict(
                        start=a,
                        end=b,
                        source=list(pins[i]),
                        target=list(pins[j]),
                        length_mm=distance,
                    )
                )
                remaining.remove(j)
                current = j
        nets.append(
            dict(
                name=net["name"],
                edges=edges,
                length_mm=sum(e["length_mm"] for e in edges),
                missing_pins=missing,
            )
        )
    return dict(
        nets=nets,
        length_mm=sum(n["length_mm"] for n in nets),
        scope="Euclidean minimum spanning trees at displayed pad rotations; no obstacle or copper model",
    )
