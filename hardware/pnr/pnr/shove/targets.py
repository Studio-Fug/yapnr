"""Power target topology and search bounds for PNR_SHOVE=1 (design stage A2/A3).

Pure helpers used by pnr.native_loop's inspect worker and controller. Nothing
here changes a contract: widths, via arrays and pad entries still come from the
source current policy and are enforced by pnr.native_electrical.
"""
import math


def pad_label(pad):
    return pad.GetParentFootprint().GetReference() + '.' + pad.GetNumber()


def trunk_pairs(net, pads, rules, group_of, ratio=.5, hop=4.0):
    """``[(terminal_pad, pad)]`` hops of a power net's full-current trunk ([] if none).

    The terminal is the pad with the largest explicit terminal contract carrying at
    least ``ratio`` of the net envelope (a 5 A switch-node strip, never a 10 mA
    sense leaf); the carrier is the nearest pad named by a net-scope current intent
    (the inductor/shunt/source land) in another connected group, unless an
    uncontracted full-width land lies within ``hop`` mm of the terminal (the bulk
    capacitor at the switcher pins): the trunk then starts with that hop and
    continues to the carrier, both scheduled before any leaf. Leaf branches attach
    to whatever tree the trunk builds.
    """
    from pnr.electrical import net_policy, terminal_policy
    policy = net_policy(net, rules)
    if policy['mode'] != 'power' or not policy.get('current_known'):
        return []
    intents = [i for i in rules.get('current_intents', []) if i.get('net') == net]
    carriers = {(i['ref'], p) for i in intents if i.get('scope', 'net') == 'net' for p in i['pads']}
    best = None
    for pad in pads:
        ref, number = pad.GetParentFootprint().GetReference(), pad.GetNumber()
        if (ref, number) in carriers:
            continue
        try:
            contract = terminal_policy(ref, [number], net, rules)
        except ValueError:
            contract = None
        if not contract or contract['rms_current_a'] + 1e-12 < ratio * policy['rms_current_a']:
            continue
        key = (-contract['rms_current_a'], -contract['peak_current_a'], pad_label(pad))
        if best is None or key < best[0]:
            best = (key, pad)
    if best is None:
        return []
    terminal = best[1]
    xy = lambda p: (p.GetPosition().x / 1e6, p.GetPosition().y / 1e6)
    # First hop: an uncontracted full-width land (the output/input bulk capacitor
    # beside the switcher pins) within ``hop`` mm is where the trunk must go first;
    # routed later it may find no room left for its full-width landing.
    def contracted(p):
        try:
            return terminal_policy(p.GetParentFootprint().GetReference(), [p.GetNumber()], net, rules) is not None
        except ValueError:
            return True
    near = [p for p in pads if group_of(p) != group_of(terminal) and not contracted(p)
            and (p.GetParentFootprint().GetReference(), p.GetNumber()) not in carriers
            and math.dist(xy(p), xy(terminal)) <= hop]
    far = [p for p in pads if (p.GetParentFootprint().GetReference(), p.GetNumber()) in carriers
           and group_of(p) != group_of(terminal)]
    nearest = lambda options: min(options, key=lambda p: (math.dist(xy(p), xy(terminal)), pad_label(p)))
    out = []
    if near:
        out.append((terminal, nearest(near)))
    if far:
        carrier = nearest(far)
        if not out or group_of(carrier) != group_of(out[0][1]):
            out.append((terminal, carrier))
    return out


def group_box(board, indices, groups, net):
    """Bounding box (mm) of every pad and connected same-net copper item of the
    partition groups ``indices`` (native connectivity)."""
    wanted = {u for i in indices for u in groups[i]}
    cn = board.GetConnectivity()
    boxes = []
    for f in board.GetFootprints():
        for pad in f.Pads():
            if pad.m_Uuid.AsString() not in wanted:
                continue
            boxes.append(pad.GetBoundingBox())
            for item in cn.GetConnectedItems(pad):
                if item.GetNetname() == net and item.GetClass() in ('PCB_TRACK', 'PCB_VIA'):
                    boxes.append(item.GetBoundingBox())
    if not boxes:
        return None
    return [min(b.GetLeft() for b in boxes) / 1e6, min(b.GetTop() for b in boxes) / 1e6,
            max(b.GetRight() for b in boxes) / 1e6, max(b.GetBottom() for b in boxes) / 1e6]


def search_bounds(target, attempt, local, board, margin=1.0):
    """PNR_SHOVE=1 power search box (A3): a trunk searches the whole board from its
    first attempt; any other power/plane branch searches its local box united with
    the copper it may root on (``root_box`` + ``margin``), clipped to the board."""
    if target.get('trunk'):
        return list(board)
    box = list(local)
    root = target.get('root_box')
    if root:
        box = [min(box[0], root[0] - margin), min(box[1], root[1] - margin),
               max(box[2], root[2] + margin), max(box[3], root[3] + margin)]
    return [max(board[0], box[0]), max(board[1], box[1]), min(board[2], box[2]), min(board[3], box[3])]


def leaf_current(net, pads, rules):
    """Smallest explicit terminal contract (rms A) among ``pads``, or None.

    Reported on each power target (``leaf_rms_a``). It is deliberately not a
    scheduling key: routing sense leaves first helped one board state and hurt
    another in the case study, while :func:`reserve_sense_escapes` protects a
    sense leaf's escape whatever the order."""
    from pnr.electrical import terminal_policy
    best = None
    for pad in pads:
        try:
            contract = terminal_policy(pad.GetParentFootprint().GetReference(), [pad.GetNumber()], net, rules)
        except ValueError:
            contract = None
        if contract and (best is None or contract['rms_current_a'] < best):
            best = contract['rms_current_a']
    return best


def schedule_key(target):
    """Trunks first (their copper roots every later branch)."""
    return not target.get('trunk', False)


def reserve_sense_escapes(board, rules, oracle, routing_net, max_current=.05, stub=1.0):
    """Reserve, in ``oracle`` only, the in-pad escape of every isolated sense leaf.

    A sense leaf is a lone SMD land (its connected group is just itself) whose
    explicit terminal contract carries at most ``max_current`` A (ISP/ISN, FB, EN
    pins). Its first qualified in-pad site that is still legal gets a reserved via
    of the leaf's net plus a ``stub`` mm track at the leaf's own outer width on the
    other outer layer, pointing at the nearest same-net land of another group
    (turned up to 90 degrees if blocked). Routes of other nets then keep clear of
    it; nothing is written to the board. Returns the reservations made."""
    import pcbnew as k
    from pnr.electrical import terminal_policy
    from pnr.via_in_pad import attach_windows, pad_layer, is_smd
    from pnr.via_coalesce import partition
    if oracle.geometry.in_pad is None:
        return []
    groups = partition(board)
    member = {u: i for i, g in enumerate(groups) for u in g}
    sizes = {i: len(g) for i, g in enumerate(groups)}
    xy = lambda p: (p.GetPosition().x / 1e6, p.GetPosition().y / 1e6)
    pads = [p for f in board.GetFootprints() for p in f.Pads()]
    tracks = list(board.GetTracks())
    out = []
    for p in pads:
        net = p.GetNetname()
        u = p.m_Uuid.AsString()
        if not net or net == routing_net or not is_smd(p) or sizes.get(member.get(u), 0) != 1:
            continue
        try:
            contract = terminal_policy(p.GetParentFootprint().GetReference(), [p.GetNumber()], net, rules)
        except ValueError:
            contract = None
        if not contract or contract['rms_current_a'] > max_current:
            continue
        layer = pad_layer(p)
        shape = p.GetEffectiveShape(layer)
        if any(t.GetNetCode() == p.GetNetCode() and t.IsOnLayer(layer) and shape.Collide(t.GetEffectiveShape(layer), 0)
               for t in tracks):
            continue  # already has copper: its escape exists
        others = [q for q in pads if q.GetNetname() == net and member.get(q.m_Uuid.AsString()) != member.get(u)]
        if not others:
            continue
        goal = xy(min(others, key=lambda q: math.dist(xy(q), xy(p))))
        other_layer = k.B_Cu if pad_layer(p) == k.F_Cu else k.F_Cu
        width = contract['outer_width_mm']
        for window in attach_windows(board, p, 1, oracle.geometry):
            (site, diameter, drill), = window['vias']
            if not oracle.via(net, site, diameter, drill):
                continue
            base = math.atan2(goal[1] - site[1], goal[0] - site[0])
            end = None
            for turn in (0, 45, -45, 90, -90):
                angle = base + math.radians(turn)
                probe = (round(site[0] + stub * math.cos(angle), 6), round(site[1] + stub * math.sin(angle), 6))
                if oracle.clear(net, other_layer, site, probe, width):
                    end = probe
                    break
            if end is None:
                continue
            oracle.reserve_via(net, site, diameter, drill)
            oracle.reserve_track(net, other_layer, site, end, width)
            out.append(dict(pad=p.GetParentFootprint().GetReference() + '.' + p.GetNumber(), net=net,
                            via=list(site), stub_end=list(end), layer=board.GetLayerName(other_layer)))
            break
    return out


def runtime_bounds(board, groups, source, target, net, bounds, margin=1.0):
    """``bounds`` united with the copper both endpoints are connected to NOW (+
    ``margin``), clipped to the outline: a branch may root on trunk copper routed
    after the controller computed its box (A3 at worker time)."""
    from pnr.writeback import outline_bounds
    member = {u: i for i, g in enumerate(groups) for u in g}
    indices = [member[p.m_Uuid.AsString()] for p in (source, target) if p.m_Uuid.AsString() in member]
    box = group_box(board, indices, groups, net)
    if box is None:
        return list(bounds)
    edge = outline_bounds(board)
    outline = [edge.GetLeft() / 1e6, edge.GetTop() / 1e6, edge.GetRight() / 1e6, edge.GetBottom() / 1e6]
    out = [min(bounds[0], box[0] - margin), min(bounds[1], box[1] - margin),
           max(bounds[2], box[2] + margin), max(bounds[3], box[3] + margin)]
    return [max(outline[0], out[0]), max(outline[1], out[1]), min(outline[2], out[2]), min(outline[3], out[3])]
