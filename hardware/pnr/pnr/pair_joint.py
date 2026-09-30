"""Candidate junctions for a duplicated-contact differential connector tree.

src13: hand_first_order / first_joint_hand expose the worker's joint ordering so
paired_bootstrap (PNR_PAIR_HAND_SWAP_TRIAL) derives which hand runs first.
"""
import math


def point_at_fraction(path, fraction):
    if not 0<=fraction<=1:
        raise ValueError('fraction must be in [0,1]')
    if len(path)<2 or any(a[2]!=z[2] for a,z in zip(path,path[1:])):
        raise ValueError('junction requires a nonempty single-layer branch')
    lengths=[math.dist(a[:2],z[:2]) for a,z in zip(path,path[1:])]
    total=sum(lengths);walk=fraction*total
    for index,(a,z) in enumerate(zip(path,path[1:])):
        distance=lengths[index]
        if walk<=distance+1e-10:
            t=min(1.,walk/distance) if distance else 0.
            return tuple(a[i]+t*(z[i]-a[i]) for i in (0,1)),fraction*total,(1-fraction)*total
        walk-=distance
    raise ValueError('empty branch')


def branch_join_port(pair, paths, oracle, rules, fraction=.5, *, budget_scope="combined"):
    """Keep a literal branch tree; use its interior point as a through-via root.

    Both actual contact-to-root lengths always count toward timing. The caller
    must select the budget scope: combined charges the longest contact leg to
    the trunk budget; separate retains the independently authored auxiliary
    max_length_mm limit. The caller checks that full auxiliary limit before
    invoking this helper. Neither mode changes a numeric source constraint.
    A new via requires the native all-layer oracle to accept its site.
    """
    if budget_scope not in ("combined","separate"):raise ValueError("unknown auxiliary budget scope")
    from pnr.native_electrical import pair_via_geometry
    diameter,drill=pair_via_geometry(rules);sites={};declared={};alternate={}
    for net in (pair['p'],pair['n']):
        try:point,source_length,target_length=point_at_fraction(paths[net],fraction)
        except ValueError as exc:return dict(status='pair_joint_unsupported_branch',reason=str(exc))
        sites[net]=point;declared[net]=target_length;alternate[net]=source_length
        if not oracle.via(net,point,diameter,drill):return dict(status='pair_joint_via_blocked',net=net,site=point)
    clearance=max(rules.get('fab',{}).get('clearance_mm',.15),pair['gap_mm'])
    if math.dist(*sites.values())<diameter+clearance+.002:return dict(status='pair_joint_via_spacing')
    budget=max(list(declared.values())+list(alternate.values()))
    if budget_scope not in ("combined","separate"):raise ValueError("unknown auxiliary budget scope")
    if budget_scope=="combined" and budget>=pair.get("max_uncoupled_mm",2):return dict(status="pair_joint_fanout_budget",length_mm=budget)
    # Explicit interpretation: the explicitly bounded auxiliary bridge is
    # separate from the departing trunk's uncoupled fanout. Preserve its actual
    # lengths in both timing origins and report this scope as unqualified.
    port=dict(sites=sites,paths={n:[] for n in sites},lengths={n:0 for n in sites},entry_budget_mm=budget if budget_scope=="combined" else 0,auxiliary_contact_length_mm=budget,reuse=False,auxiliary_budget_scope=budget_scope)
    return dict(status='routed',port=port,declared_offsets=declared,alternate_offsets=alternate)


def joint_topologies(pair, positions, *, max_trials=6, budget_scope="separate"):
    """Enumerate rigid-transform invariant connector junction candidates.

    These are finite search seeds, not altered electrical limits. Only an
    explicitly annotated auxiliary pair ending at the declared timing origin
    qualifies. Intermediate devices remain in their caller-supplied poses.
    Both embedding hands are always paired in the enumeration. A further
    placement retry is still required if an intermediate device blocks them.
    """
    if budget_scope not in ("combined", "separate"):
        raise ValueError("unknown auxiliary budget scope")
    if isinstance(max_trials, bool) or not isinstance(max_trials, int) or not 1 <= max_trials <= 18:
        raise ValueError("joint topology max_trials must be an integer from 1 to 18")
    chain = pair.get("terminal_chain", [])
    if len(chain) < 2:
        return []
    groups = [g for g in pair.get("auxiliary_pairs", []) if g.get("target") == chain[0]]
    if len(groups) != 1:
        return []
    group = groups[0]
    try:
        spans = [math.dist(positions[group["source"][key]], positions[group["target"][key]]) for key in ("p", "n")]
        limit = float(group["max_length_mm"])
        skew = float(pair["skew_mm"])
    except (KeyError, TypeError, ValueError):
        return []
    if not all(math.isfinite(v) and v > 0 for v in spans + [limit, skew]):
        return []
    # Leave 5% room under the authored length cap for an orthogonal bridge.
    # Exact routed arc length is checked independently before accepting it.
    depth = min(min(spans) * .95, (limit - max(spans)) * .475)
    if depth <= 0:
        return []
    candidates = []
    for fraction in (.65, .35, .5):
        for target in (skew, -skew, 0.):
            for hand in (1, -1):
                candidates.append(dict(bridge_hand=hand, takeoff="bridge_join_via",
                    auxiliary_order=("p", "n"), bridge_depth_mm=depth,
                    join_fraction=fraction, prefix_timing_target_mm=target,
                    auxiliary_budget_scope=budget_scope))
    return candidates[:max_trials]


def hand_first_order(joint, first):
    """PNR_PAIR_JOINT_HAND_FIRST=+1|-1: within each (join fraction, timing target)
    seed the named bridge hand runs first. Empty/None keeps the enumeration order
    (pnr.native_electrical.pair_plan applies this to joint_topologies)."""
    if not first:
        return joint
    hand = int(first)
    if hand not in (-1, 1):
        raise ValueError('PNR_PAIR_JOINT_HAND_FIRST must be +1 or -1')
    seeds = {}
    for c in joint:
        seeds.setdefault((c.get('join_fraction'), c.get('prefix_timing_target_mm')), len(seeds))
    return sorted(joint, key=lambda c: (seeds[c.get('join_fraction'), c.get('prefix_timing_target_mm')], c.get('bridge_hand') != hand))


def first_joint_hand(pair, positions, *, hand_first=None, max_trials=6, budget_scope="separate"):
    """Bridge hand of the first joint configuration pair_plan will run, or None.

    Derived from the same enumeration and ordering the worker uses
    (joint_topologies + hand_first_order), so a caller that wants the other hand
    first (paired_bootstrap PNR_PAIR_HAND_SWAP_TRIAL) never has to assume it.
    None when the pair has no joint-topology configuration at all.
    """
    joint = hand_first_order(joint_topologies(pair, positions, max_trials=max_trials, budget_scope=budget_scope), hand_first)
    return joint[0]['bridge_hand'] if joint else None
