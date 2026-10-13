"""Authored nested placement banks, independent of electrical net names.

Banks are compactness groups in global placement, bounded-diameter groups in
legalization, and collective translation units during external shove. They do not
pre-route copper or lock a board coordinate.
"""

import math
import os
from dataclasses import replace


def enabled():
    return os.environ.get("PNR_PLACEMENT_BANKS") == "1"


def activate(constraints):
    """Promote an enabled bank's diameter to a checked placement invariant.

    A soft pull alone can be undone by sequential packing. Pairwise hard discs
    retain the declared subgroup through packing, shove and independent checks.
    The original compiled document remains unchanged for flag-off A/B replay.
    """
    from pnr.constraints import Enforcement

    if not enabled() or not any(
        c.params.get("placement_bank") and c.enforcement is not Enforcement.HARD
        for c in constraints.constraints
    ):
        return constraints
    return replace(
        constraints,
        constraints=[
            replace(c, enforcement=Enforcement.HARD) if c.params.get("placement_bank") else c
            for c in constraints.constraints
        ],
    )


def compile_banks(entries, known_refs):
    from pnr.constraints import Constraint, ConstraintError, Enforcement

    out, taken, names = [], set(), set()
    for entry in entries or []:
        name = entry.get("name")
        refs = entry.get("members", [])
        span, weight = entry.get("max_span_mm"), entry.get("weight", 2.0)
        if not isinstance(name, str) or not name or name in names:
            raise ConstraintError("placement_bank requires a unique name")
        if (
            not isinstance(refs, list)
            or len(refs) < 2
            or any(not isinstance(r, str) or r not in known_refs for r in refs)
            or len(set(refs)) != len(refs)
            or taken.intersection(refs)
        ):
            raise ConstraintError("placement_bank requires disjoint known members")
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (span, weight)
        ):
            raise ConstraintError("placement_bank needs positive max_span_mm and weight")
        refs = sorted(refs)
        taken.update(refs)
        names.add(name)
        # Every pair shares the cost: no arbitrarily privileged anchor capacitor.
        for i, anchor in enumerate(refs):
            for member in refs[i + 1 :]:
                out.append(
                    Constraint(
                        kind="group",
                        enforcement=Enforcement.SOFT,
                        refs=(member,),
                        params=dict(
                            anchor=anchor,
                            radius_mm=float(span),
                            placement_bank=name,
                            bank_members=refs,
                            parent=entry.get("parent"),
                        ),
                        weight=float(weight) / (len(refs) - 1),
                        name=name,
                    )
                )
    return out


def members(graph, constraints):
    """Validate explicit banks electrically before they become translation units."""
    if not enabled():
        return []
    by_ref = {c.ref: c for c in graph.components}
    banks = {}
    for con in constraints.constraints:
        name = con.params.get("placement_bank")
        if name:
            refs = tuple(con.params["bank_members"])
            if name in banks and banks[name] != refs:
                raise ValueError("Inconsistent placement bank membership")
            banks[name] = refs
    seen = set()
    for refs in banks.values():
        if seen.intersection(refs) or any(r not in by_ref for r in refs):
            raise ValueError("Placement banks require disjoint existing members")
        seen.update(refs)
        nets = [frozenset(p.net for p in by_ref[r].pads if p.net) for r in refs]
        if any(len(n) != 2 or n != nets[0] for n in nets):
            raise ValueError("Bulk capacitor bank members must share both electrical nets")
    return list(banks.values())
