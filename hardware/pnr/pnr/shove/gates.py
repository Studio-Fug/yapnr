"""Whole-transaction gate of a make-room candidate against the ORIGINAL board.

Run as ``python -m pnr.shove.gates ORIGINAL CANDIDATE --rules R --kicad-cli CLI
--out OUT.json [--moved MOVED.json]`` under KiCad Python (its own process, so no
board objects of the transaction are alive). Any failed check discards the
whole transaction:

* pad partition preserved and no lost connection (``connectivity_restore``);
* pad entries: none lost, none newly bad (``pad_entry.snapshot``);
* no pair reference failure (``native_electrical.reference_failures``);
* no new via-to-SMD-pad rule break (``via_in_pad.forbidden_vias`` against the
  original, not an intermediate board);
* native DRC with the generated .kicad_dru: no new violation key (silk included),
  dangling not increased, strictly fewer opens, and no moved uuid gaining
  violations (a moved item keeps its uuid, so its old key could hide a new one);
* no leftover ``PNR shove:`` / ``PNR leaf:`` rule area;
* justified sub-width power copper (:func:`unjustified_subwidth`); the unjustified
  count must not increase;
* part nudges (any footprint moved against the original): not locked, rotated,
  flipped or source-owned, at most 0.5 mm from the flow's origin pose in total, and
  no new hard placement violation under the loop constraints
  (:mod:`pnr.shove.placement`); unverifiable nudges are rejected.
"""

import argparse
import json
import math
from collections import Counter
from pathlib import Path

PREFIXES = ("PNR shove:", "PNR leaf:")


def _contract(pad, rules, cache):
    from pnr.electrical import terminal_policy

    key = pad.m_Uuid.AsString()
    if key not in cache:
        try:
            cache[key] = terminal_policy(
                pad.GetParentFootprint().GetReference(), [pad.GetNumber()], pad.GetNetname(), rules
            )
        except ValueError:
            cache[key] = None
    return cache[key]


def unjustified_subwidth(board, rules, audit):
    """UUIDs of sub-width power tracks (electrical_audit) not justified by the
    contract of a terminal they serve.

    A track entering a terminal land (an endpoint inside a same-net land with an
    explicit terminal contract, on the track's layer) must suit EVERY land it
    enters: width >= that terminal's outer width, or a source-bounded neck of it
    (``pnr.electrical.neck_budget``: at least the fab track width, length <= its
    neck_max_length_mm, loss/drop in budget), or the land is attached by its
    qualified in-pad array (then the track is a branch landing, whose entry the
    array carries). Then the track must be a valid neck of a land it enters, or at
    least as wide as the outer width of a terminal its sub-width branch serves
    (the connected sub-width copper of its net: tracks sharing an endpoint on one
    layer or a via inside their copper; a via inside a terminal land, such as an
    in-pad array under its collector, serves that land). A stretched neck, a neck
    on the wrong land or a thin track merely touching some pad is not justified."""
    from pnr.electrical import neck_budget
    from pnr.pad_entry import array_attached_pads, closest

    xy = lambda p: (p.x / 1e6, p.y / 1e6)
    tracks = {t.m_Uuid.AsString(): t for t in board.GetTracks()}
    rows = [r for r in audit["subwidth_tracks"] if r["uuid"] in tracks]
    if not rows:
        return []
    fab = rules.get("electrical_fab") or {}
    floor = rules.get("fab", {}).get("track_width_mm", 0.2)
    cache = {}
    arrays = array_attached_pads(board, rules)
    nets = {tracks[r["uuid"]].GetNetCode() for r in rows}
    pads = [
        p
        for f in board.GetFootprints()
        for p in f.Pads()
        if p.GetNetCode() in nets and _contract(p, rules, cache)
    ]
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetCode() in nets]
    sub = [tracks[r["uuid"]] for r in rows]
    ids = [t.m_Uuid.AsString() for t in sub]
    parent = list(range(len(sub) + len(vias)))  # tracks, then vias

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i, j):
        parent[find(j)] = find(i)

    ends = [(xy(t.GetStart()), xy(t.GetEnd())) for t in sub]
    meet = {}
    for i, t in enumerate(sub):
        for e in ends[i]:
            meet.setdefault(
                (t.GetNetCode(), t.GetLayer(), round(e[0] * 1e3), round(e[1] * 1e3)), []
            ).append(i)
    for members in meet.values():
        for j in members[1:]:
            join(members[0], j)
    via_lands = {}
    for n, v in enumerate(vias):
        c = xy(v.GetPosition())
        for i, t in enumerate(sub):
            if (
                t.GetNetCode() == v.GetNetCode()
                and math.dist(c, closest(c, *ends[i])) <= t.GetWidth() / 2e6 + 1e-6
            ):
                join(i, len(sub) + n)
        via_lands[n] = [
            p for p in pads if p.GetNetCode() == v.GetNetCode() and p.HitTest(v.GetPosition())
        ]
    served = {}
    for n, lands in via_lands.items():
        served.setdefault(find(len(sub) + n), set()).update(p.m_Uuid.AsString() for p in lands)
    entered = []
    for i, t in enumerate(sub):
        lands = {
            p.m_Uuid.AsString(): p
            for p in pads
            for point in (t.GetStart(), t.GetEnd())
            if p.GetNetCode() == t.GetNetCode() and p.IsOnLayer(t.GetLayer()) and p.HitTest(point)
        }
        served.setdefault(find(i), set()).update(lands)
        entered.append(lands)
    by_uid = {p.m_Uuid.AsString(): p for p in pads}
    out = []
    for i, t in enumerate(sub):
        width, length = t.GetWidth() / 1e6, t.GetLength() / 1e6
        ok, neck = True, False
        for key, p in entered[i].items():
            contract = _contract(p, rules, cache)
            if width + 1e-6 >= contract["outer_width_mm"]:
                continue
            if width + 1e-6 >= floor and fab and neck_budget(contract, width, length, fab):
                neck = True
                continue
            if key in arrays:
                continue
            ok = False
        wide = any(
            width + 1e-6 >= _contract(by_uid[key], rules, cache)["outer_width_mm"]
            for key in served.get(find(i), ())
        )
        if not ok or not (neck or wide):
            out.append(ids[i])
    return out


def facts(path, rules):
    import pcbnew as k
    from pnr.fab_profile import load_board, geometry
    from pnr.via_coalesce import partition
    from pnr.pad_entry import snapshot
    from pnr.native_electrical import reference_failures
    from pnr.electrical_audit import audit_board
    from pnr.via_in_pad import forbidden_vias

    board = load_board(path)
    k.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    audit = audit_board(board, rules, Path(path).read_text())
    g = geometry(rules)
    return dict(
        partition=partition(board),
        entries=snapshot(board, rules),
        reference=reference_failures(board, rules),
        subwidth=audit["subwidth_track_count"],
        unjustified=unjustified_subwidth(board, rules, audit),
        forbidden=sorted(forbidden_vias(board, g)) if g.via_to_smd_pad is not None else [],
        areas=sorted(
            z.GetZoneName()
            for z in board.Zones()
            if z.GetIsRuleArea() and z.GetZoneName().startswith(PREFIXES)
        ),
        board=board,
    )


def per_uuid(drc, ids):
    counts = Counter()
    for v in drc["violations"]:
        for item in v.get("items", []):
            if item.get("uuid") in ids:
                counts[item["uuid"]] += 1
    return counts


def evaluate(
    original,
    candidate,
    rules,
    cli,
    out,
    moved=(),
    constraints=None,
    placement_python=None,
    origin_board=None,
):
    from pnr.via_coalesce import preserved, acceptable, violation_keys
    from pnr.connectivity_restore import lost_connections
    from pnr.native_drc import run_drc
    from pnr.shove.placement import check as placement_check, poses

    out = Path(out)
    a, b = facts(original, rules), facts(candidate, rules)
    origin = None
    if origin_board:
        from pnr.fab_profile import load_board

        origin = poses(load_board(origin_board))
    placement = placement_check(
        a["board"],
        b["board"],
        rules,
        constraints,
        placement_python,
        out.with_name(out.stem + ".placement"),
        origin,
    )
    before = run_drc(cli, Path(original), out.with_name(out.stem + ".before.drc.json"))
    after = run_drc(cli, Path(candidate), out.with_name(out.stem + ".after.drc.json"))
    checks = dict(
        preserved=preserved(a["partition"], b["partition"]),
        lost_connections=lost_connections(a["partition"], b["partition"]),
        lost_pad_entries=[
            i for i, v in a["entries"].items() if v and not b["entries"].get(i, False)
        ],
        new_bad_entries=[i for i, v in b["entries"].items() if not v and i not in a["entries"]],
        reference_failures=b["reference"],
        new_forbidden_smd_vias=sorted(set(b["forbidden"]) - set(a["forbidden"])),
        leftover_rule_areas=b["areas"],
    )
    moved = set(moved)
    grown = {
        u: n for u, n in per_uuid(after, moved).items() if n > per_uuid(before, moved).get(u, 0)
    }
    new_keys = violation_keys(after) - violation_keys(before)
    result = dict(
        checks=checks,
        opens_before=len(before["unconnected_items"]),
        opens_after=len(after["unconnected_items"]),
        violations_before=len(before["violations"]),
        violations_after=len(after["violations"]),
        new_violations=[dict(type=t, items=list(i)) for (t, i) in new_keys],
        moved_uuid_growth=grown,
        subwidth_before=a["subwidth"],
        subwidth_after=b["subwidth"],
        unjustified_subwidth_before=len(a["unjustified"]),
        unjustified_subwidth_after=len(b["unjustified"]),
        new_unjustified_subwidth=sorted(set(b["unjustified"]) - set(a["unjustified"])),
        placement=placement,
    )
    result["accepted"] = bool(
        acceptable(before, after, checks)
        and not checks["lost_connections"]
        and not checks["new_bad_entries"]
        and not checks["reference_failures"]
        and not checks["new_forbidden_smd_vias"]
        and not checks["leftover_rule_areas"]
        and not grown
        and result["opens_after"] < result["opens_before"]
        and len(b["unjustified"]) <= len(a["unjustified"])
        and placement["accepted"]
    )
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("original", type=Path)
    ap.add_argument("candidate", type=Path)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--kicad-cli", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--moved", type=Path)
    ap.add_argument(
        "--constraints", type=Path, help="loop constraints.yaml (required to accept any part nudge)"
    )
    ap.add_argument("--placement-python", help="PnR runtime python running pnr.shove.placement")
    ap.add_argument(
        "--origin-board", type=Path, help="the flow origin placement (cumulative nudge cap)"
    )
    a = ap.parse_args()
    moved = json.loads(a.moved.read_text()) if a.moved else []
    result = evaluate(
        a.original,
        a.candidate,
        json.loads(a.rules.read_text()),
        a.kicad_cli,
        a.out,
        moved,
        a.constraints,
        a.placement_python,
        a.origin_board,
    )
    a.out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("accepted", "opens_before", "opens_after", "new_violations")
            }
        )
    )


if __name__ == "__main__":
    main()
