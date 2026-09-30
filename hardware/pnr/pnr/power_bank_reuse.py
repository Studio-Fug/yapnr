"""Conservative whole-current-bank reuse proposals; acceptance needs native gates.

Distance only generates proposals. Source current determines every barrel count
and feed width. Reuse preserves one complete bank and joins its other-layer feed
with a full-current route. Inner-layer ports and unsupported copper are excluded.
"""
import math
from pnr.electrical import net_policy, current_width
from pnr.plane_intent import size_array
from pnr.native_electrical import uid, xy, Oracle, add_track
from pnr.pad_entry import closest
from pnr.route.detail.regional import segment_distance
from pnr.route.detail.keyhole import elbows, legal, length


def connected_tracks(tracks, starts):
    seen = {uid(t) for t in starts}
    queue = list(starts)
    while queue:
        t = queue.pop()
        for other in tracks:
            if uid(other) not in seen and segment_distance(
                    xy(t.GetStart()), xy(t.GetEnd()),
                    xy(other.GetStart()), xy(other.GetEnd())) < 1e-6:
                seen.add(uid(other))
                queue.append(other)
    return seen


def banks(board, rules, spacing=.8):
    import pcbnew as k
    all_tracks = list(board.GetTracks())
    vias = [v for v in all_tracks if v.GetClass() == 'PCB_VIA']
    tracks = [t for t in all_tracks if t.GetClass() == 'PCB_TRACK']
    pads = [p for f in board.GetFootprints() for p in f.Pads()]
    unused = {uid(v): v for v in vias}
    result = []
    while unused:
        _, first = unused.popitem()
        group, queue = [first], [first]
        while queue:
            v = queue.pop()
            nearby = [identity for identity, other in unused.items()
                      if other.GetNetCode() == v.GetNetCode()
                      and math.dist(xy(other.GetPosition()), xy(v.GetPosition())) <= spacing]
            for identity in nearby:
                other = unused.pop(identity)
                group.append(other)
                queue.append(other)
        policy = net_policy(first.GetNetname(), rules)
        if policy['mode'] != 'power' or not policy['current_known'] or not policy.get('sources'):
            continue
        sizing = size_array(policy, rules['electrical_fab'])
        if len(group) < 2 or len(group) != sizing['count']:
            continue
        if any(v.IsLocked() or v.GetViaType() != k.VIATYPE_THROUGH
               or abs(v.GetDrill()/1e6 - sizing['drill_mm']) > 1e-6
               or abs(v.GetWidth(k.F_Cu)/1e6 - sizing['diameter_mm']) > 1e-6 for v in group):
            continue
        # A full-bank source current applies to all barrels together. Do not
        # divide current by pad count or infer current from old track width.
        feed_width = current_width(policy['rms_current_a'],
                                   rules['electrical_fab']['outer_copper_oz'],
                                   rules['electrical_fab']['delta_t_c'])
        ports, valid = {}, True
        for layer in board.GetEnabledLayers().CuStack():
            def touches(item):
                return item.GetNetCode() == first.GetNetCode() and item.IsOnLayer(layer) and any(
                    item.GetEffectiveShape(layer).Collide(v.GetEffectiveShape(layer), 0) for v in group)
            touching = [t for t in tracks if touches(t)]
            if layer not in (k.F_Cu, k.B_Cu):
                if touching or any(touches(p) for p in pads) or any(
                        not z.GetIsRuleArea() and z.GetNetCode() == first.GetNetCode()
                        and z.IsOnLayer(layer) and any(z.GetFilledPolysList(layer).Collide(
                            v.GetEffectiveShape(layer), 0) for v in group) for z in board.Zones()):
                    valid = False
                    break
                continue
            if any(touches(t) for t in all_tracks if t.GetClass() == 'PCB_ARC'):
                valid = False
                break
            covering = []
            for v in group:
                feeds = [t for t in touching if t.GetWidth()/1e6 + 1e-6 >= feed_width
                         and math.dist(xy(v.GetPosition()), closest(xy(v.GetPosition()),
                           xy(t.GetStart()), xy(t.GetEnd()))) + v.GetWidth(layer)/2e6
                         <= t.GetWidth()/2e6 + 1e-6]
                if not feeds:
                    valid = False
                    break
                covering.append(feeds)
            if not valid:
                break
            wide = [t for t in tracks if t.GetNetCode() == first.GetNetCode()
                    and t.GetLayer() == layer and t.GetWidth()/1e6 + 1e-6 >= feed_width]
            component = connected_tracks(wide, covering[0][:1])
            if any(not any(uid(t) in component for t in feeds) for feeds in covering):
                valid = False
                break
            ports[layer] = [t for t in touching if uid(t) in component]
        if valid:
            result.append(dict(vias=group, net=first.GetNetname(), policy=policy,
                               feed_width=feed_width, ports=ports))
    return result


def proposals(board, rules, radius=5.):
    import pcbnew as k
    candidates = banks(board, rules)
    tracks = [t for t in board.GetTracks() if t.GetClass() == 'PCB_TRACK']
    result = []
    for remove in candidates:
        for keep in candidates:
            if remove is keep or remove['net'] != keep['net']:
                continue
            if min(math.dist(xy(v.GetPosition()), xy(w.GetPosition()))
                   for v in remove['vias'] for w in keep['vias']) > radius:
                continue
            front = [t for t in tracks if t.GetNetname() == remove['net']
                     and t.GetLayer() == k.F_Cu and t.GetWidth()/1e6 + 1e-6 >= remove['feed_width']]
            component = connected_tracks(front, remove['ports'][k.F_Cu][:1])
            if not all(uid(t) in component for t in keep['ports'][k.F_Cu]):
                continue
            removed = sorted(uid(v) for v in remove['vias'])
            retained = sorted(uid(v) for v in keep['vias'])
            oracle = Oracle(board, rules, ignored=removed)
            width = remove['policy']['outer_width_mm']
            for v in remove['vias']:
                for w in keep['vias']:
                    for path in elbows(xy(v.GetPosition()), xy(w.GetPosition())):
                        if legal(path, lambda a,b: oracle.clear(remove['net'], k.B_Cu, a,b,width)):
                            result.append(dict(net=remove['net'], removed_vias=removed,
                               retained_vias=retained, path=[list(q) for q in path], width=width,
                               full_net_current=remove['policy'], length=length(path)))
    return sorted(result, key=lambda p:(p['length'],p['removed_vias'],p['retained_vias'],p['path']))


def apply(board, proposal, rules):
    """Recheck serialized proposals against current geometry before mutation."""
    import pcbnew as k
    match = next((p for p in proposals(board,rules)
                  if p['removed_vias'] == proposal['removed_vias']
                  and p['retained_vias'] == proposal['retained_vias']
                  and p['path'] == proposal['path'] and p['width'] == proposal['width']), None)
    if match is None:
        raise ValueError('stale or unsupported bank reuse')
    items = {uid(t):t for t in board.GetTracks()}
    removed = [items[u] for u in match['removed_vias']]
    for item in removed:
        board.Delete(item)  # discarded bank vias (Delete, not Remove)
    for a,b in zip(match['path'],match['path'][1:]):
        item = add_track(board,match['net'],k.B_Cu,a,b,match['width'])
        if item:
            item.thisown = False
    items.clear()
    # The vias are deleted: return their uuids, never the (invalid) wrappers.
    return list(match['removed_vias'])
