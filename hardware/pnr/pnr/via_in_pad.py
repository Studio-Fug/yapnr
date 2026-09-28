"""Filled via-in-pad on native KiCad items (docs/fab-comparison.md 5A / 5B).

Profile numbers live in :mod:`pnr.fab_profile` (``Geometry.via_to_smd_pad``,
``Geometry.in_pad``, :func:`pnr.fab_profile.in_pad_fit`). This module applies them
to pcbnew pads and vias, for every engine path that places or audits a via:

* :func:`smd_keepout_violated` is THE via-versus-SMD-pad test (native Oracle,
  keyhole regions, plane fanout). Legacy (no profile keys) is the exact
  pre-profile rule: no via copper within 0.05 mm of any SMD pad. Under jlc-pofv a
  via keeps 5A 0.127 from every SMD pad unless it is a qualified 5B in-pad via of
  that pad's own net.
* :func:`qualifies` is the 5B geometry: in-pad class (0.20/0.35, alternative
  0.20/0.45), hole edge >= 0.09 from the pad edge, copper inside the pad or centred.
* :func:`in_pad_size` sizes a via emitted inside a same-net SMD pad, and refuses
  (None) when no in-pad class qualifies there, so emitters fall back to the default
  via that the generated DRU rejects inside a pad;
  :func:`style_in_pad_via` removes its unused inner pads (5B "Inner layers").
* :func:`hole_keepouts` / :func:`clearance_shapes`: where such a via has no pad
  its copper is the bare hole, and foreign copper keeps the via hole clearance
  (0.20) from the wall, more than net clearance (0.127) from the hole.
* :func:`pad_array` / :func:`plan_pad` count and plan in-pad arrays against a
  pad's current contract with the engine's own barrel model (reports);
  :func:`audit` reports every via touching an SMD pad and :func:`new_forbidden`
  makes new rule breaks block a transaction.
* Terminal in-pad attach (:func:`array_attach`, :func:`attach_windows`): a
  current-carrying terminal whose land cannot take a surface trunk/neck may be
  attached by a current-sized row of qualified 5B vias whose disks lie inside
  full-width same-net trunk copper on another layer. Barrels in a pad are never
  an entry by themselves; only :func:`array_attach` (vias inside the pad per 5B,
  connected to one full-width trunk on the other layer, combined barrel capacity
  >= the pad's budget) qualifies the terminal (pad_entry, native_electrical).

KiCad's own DRC cannot express the 5B in-pad geometry (the generated .kicad_dru
only forbids non-0.20-drill vias within 0.127 of SMD pads), so the engine checks it.
"""
import math
from collections import defaultdict

from pnr.fab_profile import geometry, in_pad_fit, in_pad_sites

# Pre-profile via to SMD pad keep-away (any net), nm.
LEGACY_KEEP_NM = 50000


def _xy(p):
    return (p.x / 1e6, p.y / 1e6)


def _vec(p):
    import pcbnew
    return pcbnew.VECTOR2I(round(p[0] * 1e6), round(p[1] * 1e6))


def is_smd(pad):
    import pcbnew
    return pad.GetClass() == 'PAD' and pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD


def pad_layer(pad):
    import pcbnew
    return pcbnew.F_Cu if pad.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu


def pad_frame(pad, layer=None):
    """``(centre, (w, h), angle_deg, corner)`` of an SMD land in its own frame, or
    None for shapes without an analytic model (chamfered, general custom).

    ``corner`` is the corner radius (mm): 0 for rectangles, None (unknown rounding,
    treated as a stadium by :func:`pnr.fab_profile.in_pad_fit`) for ovals/circles.
    A land drawn offset from its anchor has no frame here (general copper path).
    """
    import pcbnew as k
    from pnr.pad_entry import rectangular_custom_land
    layer = pad_layer(pad) if layer is None else layer
    shape = pad.GetShape()
    angle = pad.GetOrientation().AsDegrees()
    if shape == k.PAD_SHAPE_CUSTOM:
        land = rectangular_custom_land(pad, layer)
        return None if land is None else (land[0], land[1], 0.0, 0.0)
    offset = pad.GetOffset()
    if offset.x or offset.y:
        return None
    size = _xy(pad.GetSize())
    if min(size) <= 0:
        return None
    if shape == k.PAD_SHAPE_RECT:
        corner = 0.0
    elif shape == k.PAD_SHAPE_ROUNDRECT:
        corner = pad.GetRoundRectCornerRadius() / 1e6
    elif shape in (k.PAD_SHAPE_CIRCLE, k.PAD_SHAPE_OVAL):
        corner = None
    else:
        return None
    return _xy(pad.GetPosition()), size, angle, corner


def local(point, centre, angle):
    """Board point (mm) in a pad frame: KiCad orientation ``angle`` turns the pad's
    local x axis to board (cos, -sin) (board y points down)."""
    dx, dy = point[0] - centre[0], point[1] - centre[1]
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (dx * c - dy * s, dx * s + dy * c)


def qualifies(g, pad, point, diameter, drill, layer=None):
    """A via of ``diameter``/``drill`` centred at ``point`` (mm) is a qualified 5B
    in-pad via of SMD ``pad`` (net ownership is the caller's check)."""
    ip = g.in_pad
    if ip is None or not ip.allows(diameter, drill) or not is_smd(pad):
        return False
    frame = pad_frame(pad, layer)
    if frame is not None:
        centre, size, angle, corner = frame
        return in_pad_fit(ip, size, local(point, centre, angle), diameter, drill, corner)
    # General copper: the hole disk plus its margin and the via copper must lie in
    # the actual land (no "centred" relaxation without an axis).
    import pcbnew as k
    layer = pad_layer(pad) if layer is None else layer
    for radius in (drill / 2 + ip.hole_margin, diameter / 2):
        poly = k.SHAPE_POLY_SET()
        pad.TransformShapeToPolygon(poly, layer, 0, 1000, k.ERROR_INSIDE)
        poly.Inflate(-round(radius * 1e6) - 1000, k.CORNER_STRATEGY_ROUND_ALL_CORNERS, 1000)
        if poly.IsEmpty() or not poly.Contains(_vec(point)):
            return False
    return True


def smd_keepout_violated(g, pad, pad_shape, via_shape, net, point, diameter, drill, layer=None):
    """True when a new via (``net`` at ``point``) breaks the via-to-SMD-pad rule
    against ``pad`` (whose shape on the tested layer is ``pad_shape``).

    Legacy: via copper within 0.05 mm of any SMD pad (the pre-profile test,
    unchanged). Profile: within 5A "Via copper to SMD pad" (0.127, plus the engine's
    1 um margin) unless it is a qualified 5B in-pad via of the pad's own net.
    """
    if g.via_to_smd_pad is None:
        return pad_shape.Collide(via_shape, LEGACY_KEEP_NM)
    if not pad_shape.Collide(via_shape, round((g.via_to_smd_pad + .001) * 1e6)):
        return False
    return not (pad.GetNetname() == net and qualifies(g, pad, point, diameter, drill, layer))


def containing_pads(pads, point, net=None):
    """SMD pads (optionally of ``net``) whose copper contains ``point`` (mm)."""
    import pcbnew as k
    probe = k.SHAPE_CIRCLE(_vec(point), 1)
    return [p for p in pads if is_smd(p) and (net is None or p.GetNetname() == net)
            and p.GetEffectiveShape(pad_layer(p)).Collide(probe, 0)]


def in_pad_size(g, pads, net, point):
    """``(diameter, drill)`` for a via of ``net`` emitted at ``point`` inside a
    same-net SMD pad: the first 5B class (0.35, then 0.45) that qualifies in every
    containing pad (exact native land, :func:`qualifies`).

    None outside same-net SMD pads, without a via-in-pad policy, and whenever no
    class qualifies: the site is then refused as an in-pad site. An emitter that
    still places its default via there (drill 0.30 > the 0.20 in-pad drill) is
    rejected by the generated DRU's via_to_smd_pad rule and by :func:`new_forbidden`.
    """
    ip = g.in_pad
    if ip is None:
        return None
    inside = containing_pads(pads, point, net)
    if not inside:
        return None
    for d in ip.diameters:
        if all(qualifies(g, p, point, d, ip.drill) for p in inside):
            return d, ip.drill
    return None


def style_in_pad_via(g, via):
    """5B "Inner layers": remove unused inner pads of an in-pad class via, so its
    inner antipad is hole + 2 x hole clearance (0.60) instead of pad + clearance.
    No-op for legacy and for vias outside the class. Returns True if applied."""
    import pcbnew as k
    ip = g.in_pad
    if ip is None or not ip.remove_unused_inner_pads:
        return False
    if not ip.allows(via.GetWidth(k.F_Cu) / 1e6, via.GetDrillValue() / 1e6):
        return False
    via.Padstack().SetUnconnectedLayerMode(k.UNCONNECTED_LAYER_MODE_REMOVE_EXCEPT_START_AND_END)
    return True


def removes_unused_pads(item):
    """True for a via whose padstack drops its pads on unconnected layers
    (:func:`style_in_pad_via`). Pre-profile boards never have one (every via keeps
    all its pads), so legacy obstacle models are unchanged by the keepouts below."""
    import pcbnew as k
    return (item.GetClass() == 'PCB_VIA'
            and item.Padstack().UnconnectedLayerMode() != k.UNCONNECTED_LAYER_MODE_KEEP_ALL)


def hole_keepouts(g, item, layers):
    """``[(layer, hole_shape, gap_mm)]``: the hole-wall obstacle of a via with
    removed unused pads, on every layer it spans.

    Where such a via has no pad (KiCad's ``FlashLayer`` is False, e.g. In1/In2 of
    a 5B in-pad via) its effective shape is the bare hole, so net clearance from
    that shape lets foreign copper come to 0.127 from the drill wall. KiCad applies
    the via hole clearance there (``<profile>_via_hole_clearance``, 5A 0.20), so
    obstacle models add this entry with ``gap_mm`` = hole clearance (+1 um engine
    margin). Where the via keeps its pad the pad entry dominates (0.35/2 + 0.127
    >= 0.20/2 + 0.20). Empty for every other item.
    """
    if not removes_unused_pads(item):
        return []
    hole = item.GetEffectiveHoleShape()
    gap = round(g.hole_clearance + .001, 9)
    return [(la, hole, gap) for la in layers if item.IsOnLayer(la)]


def clearance_shapes(item, layer, gap_mm, hole_clearance_mm):
    """Shapes of ``item`` on ``layer`` that foreign copper must keep ``gap_mm``
    from: its effective shape, plus (a via with removed unused pads,
    :func:`hole_keepouts`) its hole grown by ``hole_clearance_mm - gap_mm``, so
    that ``gap_mm`` from it is the via hole clearance from the drill wall. For
    obstacle checks that use one gap for every item."""
    import pcbnew as k
    shapes = [item.GetEffectiveShape(layer)]
    extra = hole_clearance_mm - gap_mm
    if extra > 0 and removes_unused_pads(item) and item.IsOnLayer(layer):
        shapes.append(k.SHAPE_CIRCLE(item.GetPosition(), round(item.GetDrillValue() / 2 + extra * 1e6)))
    return shapes


def pad_vias(board, pad, g=None, layer=None):
    """Same-net vias whose centre lies in ``pad``, with their 5B qualification:
    ``[(via, qualified)]``."""
    import pcbnew as k
    g = g or geometry()
    layer = pad_layer(pad) if layer is None else layer
    shape = pad.GetEffectiveShape(layer)
    out = []
    for v in board.GetTracks():
        if v.GetClass() != 'PCB_VIA' or v.GetNetCode() != pad.GetNetCode():
            continue
        if not shape.Collide(k.SHAPE_CIRCLE(v.GetPosition(), 1), 0):
            continue
        out.append((v, qualifies(g, pad, _xy(v.GetPosition()), v.GetWidth(layer) / 1e6,
                                 v.GetDrillValue() / 1e6, layer)))
    return out


def pad_policy(pad, rules):
    """The current contract of one pad: its terminal budget (the whole declared
    group budget; no implicit sharing) else a known net envelope, else None."""
    from pnr.electrical import net_policy, terminal_policy
    if not rules.get('electrical_fab'):
        return None
    ref = pad.GetParentFootprint().GetReference()
    policy = terminal_policy(ref, [pad.GetNumber()], pad.GetNetname(), rules)
    if policy is None:
        policy = net_policy(pad.GetNetname(), rules)
    return policy if policy.get('current_known') else None


def array_requirement(policy, rules, g):
    """In-pad vias the engine's barrel model (``size_array``) needs for ``policy``
    at the in-pad drill, with the capacity of each count."""
    from pnr.plane_intent import size_array
    fab = dict(rules['electrical_fab'], via_drill_mm=g.in_pad.drill, via_diameter_mm=g.in_pad.diameters[0])
    return size_array(policy, fab)


def pad_array(board, pad, rules, g=None):
    """Qualified in-pad array of a current-carrying pad against its contract, or
    None (no policy / no current contract / no in-pad via).

    ``carries`` counts barrels in the pad, not what they connect to on other
    layers, so no pad-entry or current gate may treat it as an entry. ``attach``
    is :func:`array_attach` (the barrels that reach one full-width trunk on another
    layer), the only in-pad terminal attach that pad_entry qualifies.
    """
    from pnr.plane_intent import array_capacity
    g = g or geometry(rules)
    if g.in_pad is None:
        return None
    policy = pad_policy(pad, rules)
    if policy is None:
        return None
    vias = [v for v, ok in pad_vias(board, pad, g) if ok]
    if not vias:
        return None
    sizing = array_requirement(policy, rules, g)
    capacity = array_capacity(rules['electrical_fab'], g.in_pad.drill, len(vias))
    return dict(count=len(vias), required=sizing['count'], rms_current_a=policy['rms_current_a'],
                peak_current_a=policy['peak_current_a'], carries=len(vias) >= sizing['count'], **capacity,
                attach=array_attach(board, pad, rules, g))


# ------------------------------------------------------------------ terminal in-pad attach

def layer_width(policy, layer):
    """The policy's full current width (mm) on copper ``layer`` (outer / inner)."""
    import pcbnew as k
    return policy['outer_width_mm'] if layer in (k.F_Cu, k.B_Cu) else policy['inner_width_mm']


def trunk_contact(point, diameter, a, z, width):
    """A via disk (``diameter`` at ``point``, mm) lies inside the copper of the
    track a-z of ``width``: the whole via pad on that layer is trunk copper. A
    track narrower than the via must run through the via centre (contact of the
    track's own width, as :func:`pnr.pad_entry.witness` for lands smaller than it)."""
    from pnr.pad_entry import closest
    contact = min(diameter, width)
    return math.dist(point, closest(point, a, z)) <= (width - contact) / 2 + 1e-6


def full_width_groups(tracks, width):
    """Connected groups of full-width tracks ``[(key, a, z, w)]`` (mm): two tracks
    join only where their copper overlaps by a full ``width`` contact (as
    native_electrical.qualified_tree_pads), never by a grazing touch."""
    from pnr.route.detail.regional import segment_distance
    parent = list(range(len(tracks)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, (_, a, z, w) in enumerate(tracks):
        for j in range(i + 1, len(tracks)):
            _, c, d, v = tracks[j]
            if segment_distance(a, z, c, d) <= (w + v) / 2 - min(width, w, v) + 1e-6:
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, t in enumerate(tracks):
        groups[find(i)].append(t)
    return list(groups.values())


def array_attach(board, pad, rules, g=None):
    """The terminal attach of SMD ``pad`` by its filled in-pad via array, or None
    (legacy / no via-in-pad policy / no current contract / no qualified in-pad via).

    The terminal qualifies (``qualified``) only when (1) the vias are qualified 5B
    in-pad through vias of the pad's net (:func:`qualifies`: class, hole edge 0.09
    from the pad edge, centred), (2) on one other copper layer each counted via's
    disk lies inside same-net track copper at least the contract's full width on
    that layer (:func:`layer_width`: outer / 0.5 oz inner), all of it one connected
    full-width group (:func:`full_width_groups`), and (3) the combined barrel
    capacity of those connected vias (``plane_intent.array_capacity``, the inverse
    of the ``size_array`` model that sizes the array) meets the pad's rms and peak
    budget. Vias that reach no full-width trunk count for nothing.
    """
    import pcbnew as k
    from pnr.plane_intent import array_capacity
    g = g or geometry(rules)
    if g.in_pad is None or not is_smd(pad):
        return None
    policy = pad_policy(pad, rules)
    if policy is None:
        return None
    vias = [v for v, ok in pad_vias(board, pad, g) if ok and v.GetViaType() == k.VIATYPE_THROUGH]
    if not vias:
        return None
    need = array_requirement(policy, rules, g)['count']
    own = pad_layer(pad)
    best = (0, None, None, [])
    for layer in board.GetEnabledLayers().CuStack():
        if layer == own:
            continue
        width = layer_width(policy, layer)
        tracks = [(t.m_Uuid.AsString(), _xy(t.GetStart()), _xy(t.GetEnd()), t.GetWidth() / 1e6)
                  for t in board.GetTracks() if t.GetClass() == 'PCB_TRACK' and t.GetLayer() == layer
                  and t.GetNetCode() == pad.GetNetCode() and t.GetWidth() / 1e6 + 1e-6 >= width]
        for group in full_width_groups(tracks, width):
            reached = [v for v in vias if any(
                trunk_contact(_xy(v.GetPosition()), max(v.GetWidth(layer), v.GetWidth(k.F_Cu)) / 1e6, a, z, w)
                for _, a, z, w in group)]
            if len(reached) > best[0]:
                best = (len(reached), layer, width, reached)
    connected, layer, width, reached = best
    capacity = array_capacity(rules['electrical_fab'], g.in_pad.drill, connected) if connected else None
    qualified = bool(capacity and capacity['max_rms_current_a'] + 1e-9 >= policy['rms_current_a']
                     and capacity['max_peak_current_a'] + 1e-9 >= policy['peak_current_a'])
    return dict(pad=pad.GetParentFootprint().GetReference() + '.' + pad.GetNumber(), pad_uuid=pad.m_Uuid.AsString(),
                net=pad.GetNetname(), in_pad_vias=len(vias), connected=connected, required=need,
                layer=board.GetLayerName(layer) if layer is not None else None, trunk_width_mm=width,
                rms_current_a=policy['rms_current_a'], peak_current_a=policy['peak_current_a'],
                capacity=capacity, vias=sorted(v.m_Uuid.AsString() for v in reached), qualified=qualified)


def attach_axes(pad):
    """``(centre, long-axis unit vector, across unit vector, (long, short), corner)``
    of an SMD land in board mm (KiCad y down), or None without an analytic frame."""
    frame = pad_frame(pad)
    if frame is None:
        return None
    centre, size, angle, corner = frame
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    x_axis, y_axis = (c, -s), (s, c)  # pad-local x / y in board coordinates (see local)
    along, across = (x_axis, y_axis) if size[0] >= size[1] else (y_axis, x_axis)
    return centre, along, across, (max(size), min(size)), corner


def attach_windows(board, pad, count, g):
    """Candidate rows of ``count`` filled 5B in-pad vias for a terminal attach of
    ``pad``, most preferred first: ``[dict(variant, offsets_mm, across, vias=[(point,
    diameter, drill)])]``.

    Sites are the pad's centred 5B row (:func:`pnr.fab_profile.in_pad_sites`, row
    pitch 0.50): first staggered 0.25 against different-net vias on neighbouring
    strips (5B "Inner layers"), else the unstaggered row (``variant`` records
    which). A window is ``count`` consecutive sites, most centred first, and every
    site must be a qualified in-pad site of the exact land (:func:`in_pad_size`).
    """
    ip = g.in_pad
    axes = attach_axes(pad)
    if ip is None or axes is None or count < 1:
        return []
    centre, along, across, size, corner = axes
    avoid = neighbour_offsets(board, pad)
    rows = ([('staggered', in_pad_sites(ip, size, corner, avoid=avoid))] if avoid else []) + \
        [('row_pitch', in_pad_sites(ip, size, corner))]
    out, seen = [], set()
    for variant, sites in rows:
        windows = [sites[i:i + count] for i in range(len(sites) - count + 1)]
        for window in sorted(windows, key=lambda w: (round(abs(sum(w) / len(w)), 6), w)):
            if tuple(window) in seen:
                continue
            seen.add(tuple(window))
            points = [(round(centre[0] + u * along[0], 6), round(centre[1] + u * along[1], 6)) for u in window]
            sizes = [in_pad_size(g, [pad], pad.GetNetname(), p) for p in points]
            if any(s is None for s in sizes):
                continue
            out.append(dict(variant=variant, offsets_mm=list(window), across=across, neighbour_offsets_mm=avoid,
                            vias=[(p, s[0], s[1]) for p, s in zip(points, sizes)]))
    return out


def neighbour_offsets(board, pad, reach_mm=1.0):
    """Long-axis offsets (mm) of different-net vias and via-class footprint drills
    on neighbouring lands within ``reach_mm`` across ``pad`` (5B stagger input)."""
    frame = pad_frame(pad)
    if frame is None:
        return []
    centre, size, angle, _ = frame
    wide = size[0] >= size[1]
    long_half, short_half = max(size) / 2, min(size) / 2
    points = [(_xy(v.GetPosition()), v.GetNetCode()) for v in board.GetTracks() if v.GetClass() == 'PCB_VIA']
    points += [(_xy(p.GetPosition()), p.GetNetCode()) for f in board.GetFootprints() for p in f.Pads()
               if max(p.GetDrillSize().x, p.GetDrillSize().y) > 0 and p.GetNetCode() != pad.GetNetCode()]
    out = []
    for point, net in points:
        if net == pad.GetNetCode():
            continue
        u, v = local(point, centre, angle)
        along, across = (u, v) if wide else (v, u)
        if abs(along) <= long_half and short_half < abs(across) <= short_half + reach_mm:
            out.append(round(along, 6))
    return sorted(set(out))


def plan_pad(board, pad, rules, g=None):
    """5B in-pad array plan for one SMD pad: how many in-pad vias fit (row pitch,
    minimum pitch, and staggered against neighbouring different-net vias), what
    they carry in the engine's barrel model, and what the pad's contract needs."""
    from pnr.plane_intent import array_capacity
    g = g or geometry(rules)
    ip = g.in_pad
    frame = pad_frame(pad)
    if ip is None or frame is None:
        return None
    _, size, _, corner = frame
    avoid = neighbour_offsets(board, pad)
    rows = dict(row_pitch=in_pad_sites(ip, size, corner),
                min_pitch=in_pad_sites(ip, size, corner, pitch=ip.min_pitch),
                staggered=in_pad_sites(ip, size, corner, avoid=avoid) if avoid else None)
    fab = rules.get('electrical_fab')
    policy = pad_policy(pad, rules)
    out = dict(pad=pad.GetParentFootprint().GetReference() + '.' + pad.GetNumber(), net=pad.GetNetname(),
               size_mm=list(size), via=dict(diameter_mm=ip.diameters[0], drill_mm=ip.drill),
               neighbour_offsets_mm=avoid, sites_mm=rows)
    if fab:
        out['capacity'] = {name: dict(count=len(sites), **array_capacity(fab, ip.drill, len(sites)))
                           for name, sites in rows.items() if sites is not None}
    if policy:
        need = array_requirement(policy, rules, g)
        out['contract'] = dict(rms_current_a=policy['rms_current_a'], peak_current_a=policy['peak_current_a'],
                               required_count=need['count'], sources=policy.get('terminal_sources') or policy.get('sources'))
        out['carries'] = {name: len(sites) >= need['count'] for name, sites in rows.items() if sites is not None}
    return out


def _via_rows(board, g):
    """``(rows, qualified pads by label)`` of :func:`audit`."""
    import pcbnew as k
    keep = LEGACY_KEEP_NM if g.via_to_smd_pad is None else round((g.via_to_smd_pad + .001) * 1e6)
    buckets = {}
    pads = []
    for f in board.GetFootprints():
        for p in f.Pads():
            if not is_smd(p):
                continue
            pb = p.GetBoundingBox()
            box = (pb.GetLeft() - keep, pb.GetTop() - keep, pb.GetRight() + keep, pb.GetBottom() + keep)
            index = len(pads)
            pads.append((p, box))
            for x in range(math.floor(box[0] / 1e6), math.floor(box[2] / 1e6) + 1):
                for y in range(math.floor(box[1] / 1e6), math.floor(box[3] / 1e6) + 1):
                    buckets.setdefault((x, y), []).append(index)
    rows = []
    arrays = {}
    for v in board.GetTracks():
        if v.GetClass() != 'PCB_VIA':
            continue
        point = _xy(v.GetPosition())
        box = v.GetBoundingBox()
        near = set()
        for x in range(math.floor(box.GetLeft() / 1e6), math.floor(box.GetRight() / 1e6) + 1):
            for y in range(math.floor(box.GetTop() / 1e6), math.floor(box.GetBottom() / 1e6) + 1):
                near.update(buckets.get((x, y), ()))
        for index in sorted(near):
            pad, pb = pads[index]
            layer = pad_layer(pad)
            if (pb[2] < box.GetLeft() or box.GetRight() < pb[0] or
                    pb[3] < box.GetTop() or box.GetBottom() < pb[1]):
                continue
            shape = pad.GetEffectiveShape(layer)
            vshape = v.GetEffectiveShape(layer)
            if not shape.Collide(vshape, keep):
                continue
            d, h = v.GetWidth(layer) / 1e6, v.GetDrillValue() / 1e6
            same = v.GetNetCode() == pad.GetNetCode()
            qualified = same and qualifies(g, pad, point, d, h, layer)
            violated = smd_keepout_violated(g, pad, shape, vshape, v.GetNetname(), point, d, h, layer)
            label = pad.GetParentFootprint().GetReference() + '.' + pad.GetNumber()
            rows.append(dict(pad=label, pad_net=pad.GetNetname(), via=v.m_Uuid.AsString(), via_net=v.GetNetname(),
                             position=point, diameter_mm=d, drill_mm=h,
                             centre_in_pad=shape.Collide(k.SHAPE_CIRCLE(v.GetPosition(), 1), 0),
                             in_pad_qualified=qualified, allowed=not violated))
            if qualified and label not in arrays:
                arrays[label] = pad
    return rows, arrays


def audit(board, rules):
    """Every via touching (or nearer than the keep-away to) an SMD pad, judged
    under the rules' profile: ``allowed`` is False where a via breaks the
    via-to-SMD-pad rule (legacy: any via within 0.05 mm of an SMD pad)."""
    g = geometry(rules)
    rows, arrays = _via_rows(board, g)
    current = {}
    for label, pad in sorted(arrays.items()):
        report = pad_array(board, pad, rules, g)
        if report:
            current[label] = report
    report = dict(profile_in_pad=g.in_pad is not None, via_to_smd_pad_mm=g.via_to_smd_pad,
                  vias=rows, forbidden=sum(not r['allowed'] for r in rows),
                  in_pad_qualified=sum(r['in_pad_qualified'] for r in rows), in_pad_arrays=current)
    if g.in_pad is not None:
        # Pads whose terminal is attached by its in-pad array (array_attach).
        report['in_pad_terminal_attaches'] = sorted(label for label, r in current.items()
                                                    if (r.get('attach') or {}).get('qualified'))
    return report


def forbidden_vias(board, g):
    """UUIDs of vias that break the via-to-SMD-pad rule (:func:`audit` rows with
    ``allowed`` False)."""
    return {r['via'] for r in _via_rows(board, g)[0] if not r['allowed']}


def new_forbidden(before, after, rules):
    """Vias of board ``after`` that break the profile's via-to-SMD-pad rule (5A
    0.127 from SMD pads, 5B in-pad geometry) and are not the same (UUID) forbidden
    via of board ``before``: a transaction must add none.

    KiCad cannot see a same-net in-pad-class via (drill 0.20) that breaks the 5B
    geometry (the DRU exempts that class), so this is its acceptance gate. Legacy
    rules (no profile key) return [] without looking: KiCad DRC is their whole rule.
    """
    g = geometry(rules)
    if g.via_to_smd_pad is None:
        return []
    return sorted(forbidden_vias(after, g) - forbidden_vias(before, g))
