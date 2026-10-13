"""Pre-synthesis validation of relations a rigid hierarchy cannot straddle.

This oracle consumes the original compiled constraints, before sub-board
filtering can remove an external anchor. Repair is conservative: connected
movable blocks merge; any such component touching an interface stays flat.
"""

from pnr.constraints import Enforcement


def relation_refs(con):
    """Relations whose original member coordinates must survive collapse."""
    if con.kind == "group" and con.enforcement == Enforcement.HARD:
        refs = list(con.refs) + [con.params.get("anchor")]
    elif con.kind == "orientation" and len(con.refs) > 1:
        refs = list(con.refs)
    elif con.kind == "keepout" and con.params.get("extent") and not con.params.get("polygon"):
        refs = list(con.refs)
    else:
        refs = []
    return tuple(sorted({r for r in refs if r}))


def partition_violations(blocks, constraints):
    owner, findings = {}, []
    for block in blocks:
        for ref in block.refs:
            if ref in owner:
                findings.append(
                    dict(kind="duplicate_member", ref=ref, blocks=[owner[ref], block.name])
                )
            owner[ref] = block.name
    for con in constraints.constraints:
        refs = relation_refs(con)
        for name in sorted({owner[r] for r in refs if r in owner}):
            inside = [r for r in refs if owner.get(r) == name]
            outside = [r for r in refs if owner.get(r) != name]
            if outside:
                findings.append(
                    dict(
                        kind=con.kind,
                        constraint=con.name,
                        block=name,
                        inside=inside,
                        outside=outside,
                    )
                )
    return findings


def validate_partition(blocks, constraints):
    findings = partition_violations(blocks, constraints)
    if findings:
        raise ValueError(
            "hierarchy partition loses source relations before synthesis: " + repr(findings)
        )


def close_partition(blocks, constraints, interface):
    """Close entire candidate blocks over hard relations; never move interfaces."""
    from pnr.hier.blocks import Block

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def join(refs):
        if refs:
            root = find(refs[0])
            for ref in refs[1:]:
                parent[find(ref)] = root

    for block in blocks:
        join(block.refs)
    for con in constraints.constraints:
        join(relation_refs(con))
    groups = {}
    for block in blocks:
        groups.setdefault(find(block.refs[0]), []).append(block)
    pinned = {find(ref) for ref in interface}
    out = []
    for root, members in groups.items():
        if root in pinned:
            continue
        refs = sorted(r for r in parent if find(r) == root)
        if len(members) == 1 and refs == sorted(members[0].refs):
            out.append(members[0])
        else:
            out.append(
                Block(
                    name="joined:" + "+".join(sorted(b.name for b in members)),
                    refs=refs,
                    source="group",
                )
            )
    out.sort(key=lambda b: b.name)
    validate_partition(out, constraints)
    return out
