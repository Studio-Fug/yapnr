"""The make-room ladder for one blocked power/plane target (KiCad Python, headless).

Rungs, cheapest first, each a complete transaction judged against the ORIGINAL
board (:mod:`pnr.shove.gates`) after the native worker's own acceptance:

* L1 copper: a delta-relaxed wish plan (radius 2.5 mm, delta <= 0.25) and the
  make-room QP over existing copper only.
* L2 parts: radius 3.5 mm, delta <= 0.35, small passives near the terminals may be
  nudged with their private fanout, at most 0.5 mm from their ORIGIN pose (the
  placement the routing flow started from, ``--origin-board``) summed over every
  transaction; courtyards of both sides are QP rows, DRC (silk included) must add
  nothing, and the nudged placement must add no hard placement violation
  (``pnr.shove.placement``: group radii, rows, keepouts, outline, fixed poses).
  Locked and source-owned (plane-access power array / copper keepout) parts never
  move.
* L4 rip-reroute: signal nets whose copper the plan must cross are ripped
  whole (at most 3, never pair/length-match/source-protected nets), the target is
  routed by the production worker, and every ripped connection is restored by the
  regional signal adapter (keyhole_region). Any unrestored connection discards
  everything.

A solved shove is committed by ``python -m pnr.native_electrical --replay-plan``:
the plan found on the pre-shove board is added verbatim at its solved position and
judged by the worker's partition, pad-entry, reference, in-pad, DRC and open-count
checks. Contracts are never relaxed: widths, via sizes, arrays, necks and
clearances come from the source current policy; only positions change.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pnr.shove.world import line_length_limit


def _xy(p):
    return (p.x / 1e6, p.y / 1e6)




def without_fills(oracle, board):
    """``oracle`` with filled copper-pour obstacles dropped (rule areas kept)."""
    from pnr.native_electrical import uid
    fills = {uid(z) for z in board.Zones() if not z.GetIsRuleArea()}
    drop = {i for i, entry in enumerate(oracle.obstacles) if entry[3] in fills}
    for key in list(oracle.buckets):
        oracle.buckets[key] -= drop
    oracle.cache.clear()
    return oracle


class Transaction:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out_dir)
        self.rules_path = Path(a.rules).resolve()
        self.rules = json.loads(self.rules_path.read_text())
        self.target = json.loads(Path(a.target_json).read_text())
        self.board_path = Path(a.board).resolve()
        self.started = time.monotonic()
        self.deadline = self.started + a.seconds
        self.result = dict(accepted=False, status='not_attempted', target=self.target, rungs=[],
                           moved=[], nudges=[], ripped=[], restored=[], certificate=[], solid_blockers={},
                           solid_parts=[], gates={})
        self.origin = None  # {ref: (x, y, rot, layer)} of the flow's origin placement

    def load_origin(self):
        from pnr.fab_profile import load_board
        from pnr.shove.placement import poses
        source = self.a.origin_board or self.board_path
        self.origin = poses(load_board(source))
        self.result['origin_board'] = str(source)

    def nudgeable(self, board, parts):
        """``parts`` that may move in this transaction: present, unlocked, not
        source-owned, with at least 0.02 mm of cumulative budget left."""
        from pnr.shove.placement import protected_refs, origin_offsets, TOTAL_CAP_MM
        footprints = {f.GetReference(): f for f in board.GetFootprints()}
        owned = protected_refs(self.rules)
        drift = origin_offsets(board, self.origin)
        keep, dropped = [], {}
        for ref in parts:
            f = footprints.get(ref)
            if f is None:
                dropped[ref] = 'absent'
            elif f.IsLocked():
                dropped[ref] = 'locked'
            elif ref in owned:
                dropped[ref] = 'source_owned'
            elif math.hypot(*drift[ref]) > TOTAL_CAP_MM - .02:
                dropped[ref] = 'cumulative_cap'
            else:
                keep.append(ref)
        return keep, dropped, {ref: drift[ref] for ref in keep}

    def placement_gate(self, folder, before, after):
        """G0: nudged parts against the hard placement constraints (see
        :mod:`pnr.shove.placement`)."""
        from pnr.shove.placement import check
        return check(before, after, self.rules, self.a.constraints, self.a.placement_python, Path(folder) / 'placement',
                     self.origin)

    def remaining(self):
        return self.deadline - time.monotonic()

    def rip_reserve(self):
        """Seconds kept back for the rip-and-reroute rung: 35% of the transaction, >= 30 s."""
        return max(30., .35 * (self.deadline - self.started))

    # ------------------------------------------------------------------ helpers
    def run(self, cmd, log, timeout):
        from pnr.proc import run
        with Path(log).open('w') as stream:
            code = run(cmd, timeout=max(10., timeout), stdout=stream, stderr=subprocess.STDOUT,
                       env=dict(os.environ, PNR_SHOVE='1'))
        return code

    def copy_project(self, source, dest):
        dest = Path(dest)
        shutil.copyfile(Path(source).with_suffix('.kicad_pro'), dest.with_suffix('.kicad_pro'))
        table = Path(source).parent / 'fp-lib-table'
        if table.exists() and not (dest.parent / 'fp-lib-table').exists():
            (dest.parent / 'fp-lib-table').write_text(table.read_text().replace('${KIPRJMOD}', str(Path(source).parent.resolve())))

    def save(self, board, path):
        import pcbnew as k
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        board.BuildConnectivity()
        k.ZONE_FILLER(board).Fill(board.Zones())
        board.BuildConnectivity()
        k.SaveBoard(str(path), board)
        self.copy_project(self.board_path, path)
        return path

    def load(self):
        from pnr.fab_profile import load_board
        board = load_board(self.board_path)
        board.BuildConnectivity()
        return board

    def endpoints(self, board):
        from pnr.pad_identity import resolve_pad
        t = self.target
        source = resolve_pad(board, t['source'], t.get('source_uuid'))
        target = resolve_pad(board, t['target'], t.get('target_uuid'))
        return source, target

    def bounds(self, board):
        from pnr.writeback import outline_bounds
        box = outline_bounds(board)
        return [box.GetLeft() / 1e6, box.GetTop() / 1e6, box.GetRight() / 1e6, box.GetBottom() / 1e6]

    def protected_nets(self):
        nets = {n for p in self.rules.get('diff_pairs', []) for n in (p['p'], p['n'])}
        nets |= {n for g in self.rules.get('length_match', []) for n in g['nets']}
        return nets

    def names(self, board):
        out = {}
        for t in board.GetTracks():
            out[t.m_Uuid.AsString()] = t.GetClass()[4:] + ':' + t.GetNetname() + (':' + t.GetLayerName() if t.GetClass() == 'PCB_TRACK' else '')
        for f in board.GetFootprints():
            for p in f.Pads():
                out[p.m_Uuid.AsString()] = 'PAD ' + f.GetReference() + '.' + p.GetNumber()
        return out

    def commit(self, folder, board_path, plan, moved_ids):
        """Replay ``plan`` on ``board_path`` with the production worker, then gate
        the candidate against the original board."""
        from pnr.shove.relax import serialise
        folder = Path(folder)
        spec = folder / 'replay-plan.json'
        spec.write_text(json.dumps(serialise(plan), indent=1))
        t = self.target
        work = folder / 'commit'
        cmd = [sys.executable, '-m', 'pnr.native_electrical', str(board_path), '--rules', str(self.rules_path),
               '--out-dir', str(work), '--net', t['net'], '--source-pad', t['source'], '--target-pad', t['target'],
               '--bounds', *map(str, self.bounds_box), '--seconds', '30', '--kicad-cli', self.a.kicad_cli,
               '--replay-plan', str(spec)]
        for end in ('source', 'target'):
            if t.get(end + '_uuid'):
                cmd += ['--' + end + '-pad-uuid', t[end + '_uuid']]
        self.run(cmd, folder / 'commit.log', 120)
        worker = json.loads((work / 'result.json').read_text()) if (work / 'result.json').exists() else dict(status='worker_error')
        step = dict(worker_status=worker.get('status'), worker_accepted=worker.get('accepted', False),
                    worker_checks=worker.get('checks'), worker_opens=[worker.get('before_opens'), worker.get('after_opens')])
        if not worker.get('accepted'):
            return None, step
        candidate = work / 'candidate.kicad_pcb'
        gate = self.gate(folder, candidate, moved_ids)
        step['gate'] = gate
        return (candidate if gate.get('accepted') else None), step

    def gate(self, folder, candidate, moved_ids):
        folder = Path(folder)
        moved = folder / 'moved-uuids.json'
        moved.write_text(json.dumps(sorted(moved_ids)))
        out = folder / 'gate.json'
        cmd = [sys.executable, '-m', 'pnr.shove.gates', str(self.board_path), str(candidate), '--rules', str(self.rules_path),
               '--kicad-cli', self.a.kicad_cli, '--out', str(out), '--moved', str(moved)]
        if self.a.constraints:
            cmd += ['--constraints', str(self.a.constraints)]
        if self.a.placement_python:
            cmd += ['--placement-python', str(self.a.placement_python)]
        if self.a.origin_board:
            cmd += ['--origin-board', str(self.a.origin_board)]
        self.run(cmd, folder / 'gate.log', 300)
        return json.loads(out.read_text()) if out.exists() else dict(accepted=False, status='gate_error')

    def publish(self, candidate):
        """Expose an accepted candidate where native_loop's outer gate reads it."""
        candidate = Path(candidate)
        final = self.out / 'candidate.kicad_pcb'
        shutil.copyfile(candidate, final)
        for ext in ('.kicad_pro', '.kicad_dru'):
            if candidate.with_suffix(ext).exists():
                shutil.copyfile(candidate.with_suffix(ext), final.with_suffix(ext))
        table = candidate.parent / 'fp-lib-table'
        if table.exists():
            shutil.copyfile(table, self.out / 'fp-lib-table')
        from pnr.native_drc import run_drc
        run_drc(self.a.kicad_cli, final, final.with_suffix('.drc.json'))

    # ------------------------------------------------------------------ G0/G1
    def invariants(self, world, solved):
        """G1: moved power lines keep their length within bounds (per line
        min(0.5 mm, 5 %), per net max(0.5 mm, 3 %)); necks never move; nudged
        parts stay within the cumulative cap of their origin pose."""
        from pnr.shove.placement import TOTAL_CAP_MM
        problems = []
        per_net = {}
        necks = set(getattr(world, 'rigid_necks', ()))
        for m in world.moved():
            if m['kind'] != 'track' or m.get('mode') not in ('power', 'plane'):
                continue
            if m['uid'] in necks and abs(m['length_delta_mm']) > 1e-6:
                problems.append(dict(uid=m['uid'], net=m['net'], neck_length_delta_mm=m['length_delta_mm']))
            limit = line_length_limit(m['length_mm'])
            if abs(m['length_delta_mm']) > limit + 1e-9:
                problems.append(dict(uid=m['uid'], net=m['net'], length_delta_mm=m['length_delta_mm'], limit_mm=round(limit, 4)))
            per_net[m['net']] = per_net.get(m['net'], 0) + abs(m['length_delta_mm'])
        for net, total in per_net.items():
            length = sum(t.GetLength() / 1e6 for t in world.tracks if t.GetNetname() == net and t.GetClass() == 'PCB_TRACK')
            if total > max(.5, .03 * length) + 1e-9:
                problems.append(dict(net=net, total_length_delta_mm=round(total, 4), limit_mm=round(max(.5, .03 * length), 4)))
        for n in world.nudges():
            if n['total_disp_mm'] > TOTAL_CAP_MM + 1e-6:
                problems.append(dict(ref=n['ref'], total_disp_mm=n['total_disp_mm'], cap_mm=TOTAL_CAP_MM))
        return problems

    def exact_check(self, board, world, plan):
        """G0: a fresh Oracle on the applied board (moved items ignored) must clear
        every moved track/via and every claim of the solved plan."""
        import pcbnew as k
        from pnr.native_electrical import Oracle, xy
        moved = world.moved()
        ids = {m['uid'] for m in moved}
        # Copper pours are refilled around the result (design B1: fills are not
        # obstacles); rule areas, pads, tracks, vias and holes all stay exact.
        oracle = without_fills(Oracle(board, self.rules, ignored=ids), board)
        bad = []
        items = {t.m_Uuid.AsString(): t for t in board.GetTracks()}
        for m in moved:
            t = items.get(m['uid'])
            if t is None:
                continue
            if t.GetClass() == 'PCB_VIA':
                if not oracle.via(t.GetNetname(), xy(t.GetPosition()), t.GetWidth(k.F_Cu) / 1e6, t.GetDrillValue() / 1e6):
                    bad.append(dict(uid=m['uid'], kind='via'))
            elif not oracle.clear(t.GetNetname(), t.GetLayer(), xy(t.GetStart()), xy(t.GetEnd()), t.GetWidth() / 1e6):
                bad.append(dict(uid=m['uid'], kind='track', blockers=list(oracle.hits)))
        full = without_fills(Oracle(board, self.rules), board)
        net = self.target['net']
        for la, a, z, w in plan.get('tracks', []):
            full.hits.clear()
            if not full.clear(net, la, a, z, w):
                bad.append(dict(claim='track', layer=board.GetLayerName(la), a=list(a), z=list(z), width=w,
                                blockers=list(full.hits)))
        sizing = (plan.get('policy') or {}).get('via_array') or {}
        for center, points, layers in plan.get('banks', []):
            for p in points:
                for la, w in layers:
                    full.hits.clear()
                    if math.dist(center, p) > 1e-9 and not full.clear(net, la, center, p, w):
                        bad.append(dict(claim='bank feed', layer=board.GetLayerName(la), a=list(center), z=list(p),
                                        width=w, blockers=list(full.hits)))
            for p in points:
                full.via_hits.clear()
                if not full.via(net, p, sizing.get('diameter_mm', .45), sizing.get('drill_mm', .3)):
                    bad.append(dict(claim='via', at=list(p), blockers=list(full.via_hits)))
        return bad

    # ------------------------------------------------------------------ rungs
    def make_room(self, rung, radius, deltas, parts):
        from pnr.shove.world import World, soft_set
        from pnr.shove.relax import wish_plan
        folder = self.out / ('rung-%d' % rung)
        folder.mkdir(parents=True, exist_ok=True)
        board = self.load()
        source, target = self.endpoints(board)
        centers = [_xy(source.GetPosition()), _xy(target.GetPosition())]
        protected = self.protected_nets()
        parts, dropped, drift = self.nudgeable(board, parts)
        soft = soft_set(board, self.target['net'], centers, radius, protected, parts)
        record = dict(rung=rung, radius=radius, deltas=deltas, parts=sorted(parts), soft=len(soft), status='no_wish_plan')
        if dropped:
            record['parts_dropped'] = dropped
        self.result['rungs'].append(record)
        if rung == 2 and not parts:
            record['status'] = 'no_nudgeable_parts'
            return None
        # Rungs 1-2 may not spend the share reserved for rip-and-reroute (rung 4).
        budget = self.remaining() - (self.rip_reserve() if rung < 4 else 0.)
        if budget < 5.:
            record['status'] = 'time_reserved_for_rip'
            return None
        seconds = max(5., min(60., budget / 3))
        plan, tiers = wish_plan(board, self.board_path, self.rules, self.target['net'], source, target, soft, deltas,
                                self.bounds_box, self.a.pitch, seconds, self.names(board), parts)
        record['wish_tiers'] = tiers
        if plan is None:
            return None
        record['plan'] = dict(delta=plan.get('delta'), tracks=len(plan.get('tracks', [])), banks=len(plan.get('banks', [])),
                              in_pad=bool(plan.get('in_pad_vias')), forced_in_pad=plan.get('forced_in_pad', False))
        # 2 um solver margin: the QP tolerance (10 nm) must never land inside the exact rule.
        from pnr.shove.placement import TOTAL_CAP_MM
        world = World(board, self.rules, self.target['net'], plan, centers, radius, parts=parts, protected_nets=protected,
                      margin=.002, part_drift=drift, total_part_cap=TOTAL_CAP_MM)
        record['rigid_necks'] = len(world.log.get('rigid_necks', ()))
        conflicts = world.fixed_claim_conflicts()
        record['fixed_claim_conflicts'] = conflicts[:12]
        budget = self.remaining() - (self.rip_reserve() if rung < 4 else 0.)
        solved = world.solve(deadline=time.monotonic() + max(5., min(40., budget / 3)))
        record.update(variables=solved['variables'], pairs=solved['pairs'], equalities=solved['equalities'],
                      sqp=solved['history'], final_violations=solved['final_violations'][:12],
                      certificate=solved['certificate'])
        self.result['certificate'] = solved['certificate']
        self._solid(world, solved, conflicts)
        if conflicts or solved['final_violations']:
            record['status'] = 'qp_infeasible' if solved['final_violations'] else 'fixed_claim_conflict'
            return None
        moved, nudges = world.moved(), world.nudges()
        record.update(moved=moved, nudges=nudges,
                      max_disp_mm=round(max([m['disp_mm'] for m in moved] + [n['disp_mm'] for n in nudges] + [0]), 4))
        problems = self.invariants(world, solved)
        record['invariants'] = problems
        if problems:
            record['status'] = 'invariant_violation'
            return None
        final_plan = world.moved_plan()
        world.apply()
        bad = self.exact_check(board, world, final_plan)
        record['exact_check'] = bad[:12]
        if bad:
            record['status'] = 'exact_check_failed'
            return None
        if nudges:
            # G0: the nudged placement against the hard placement constraints,
            # before anything is committed (the whole-transaction gate repeats it).
            placement = self.placement_gate(folder, self.load(), board)
            record['placement'] = placement
            if not placement['accepted']:
                record['status'] = 'placement_violation'
                return None
        shoved = self.save(board, folder / 'shoved.kicad_pcb')
        moved_ids = {m['uid'] for m in moved}
        candidate, step = self.commit(folder, shoved, final_plan, moved_ids)
        record['commit'] = step
        if candidate is None:
            record['status'] = 'commit_rejected'
            return None
        record['status'] = 'accepted'
        self.result.update(moved=moved, nudges=nudges)
        return candidate

    def _solid(self, world, solved, conflicts):
        """Owners of the constraints the QP could not move: the placement signal."""
        solid = self.result['solid_blockers']
        parts = set(self.result['solid_parts'])
        for row in solved['certificate']:
            for key, fixed in (('a', row.get('a_fixed')), ('b', row.get('b_fixed'))):
                ident = row.get(key)
                if not fixed or not ident or ident.startswith('claim'):
                    continue
                if ident.startswith('crt:'):
                    parts.add(ident.split(':')[1])
                    continue
                ident = ident.split(':')[0]
                solid[ident] = round(solid.get(ident, 0) + row['multiplier'], 4)
        for c in conflicts:
            ident = c['item'].split(':')[0]
            if ident.startswith('crt'):
                continue
            solid[ident] = round(solid.get(ident, 0) + 1, 4)
        pads = {p.m_Uuid.AsString(): f.GetReference() for f in world.b.GetFootprints() for p in f.Pads()}
        parts |= {pads[u] for u in solid if u in pads}
        self.result['solid_parts'] = sorted(parts)

    def rip_reroute(self, radius, max_nets=3, max_extra=2):
        """L4: rip whole signal nets the plan must cross, route the target, restore.

        A restore that fails because the ripped net's own pad is trapped by other
        local signals widens the rip set once by those neighbours (at most
        ``max_extra`` nets) and restores the trapped net first: a bounded local
        negotiation, never a global rip-up."""
        from pnr.shove.relax import ignoring_oracle, plan_route, plan_collisions, crossing_nets, distance_to
        from pnr.electrical import net_policy
        from pnr.via_coalesce import protected as source_protected, partition
        folder = self.out / 'rung-4'
        folder.mkdir(parents=True, exist_ok=True)
        record = dict(rung=4, radius=radius, status='no_rip_plan', attempts=[])
        self.result['rungs'].append(record)
        board = self.load()
        source, target = self.endpoints(board)
        centers = [_xy(source.GetPosition()), _xy(target.GetPosition())]
        self.centers = centers
        excluded, _ = source_protected(board, self.rules, self.a.annotation_source)
        pads_by_net = {}
        for f in board.GetFootprints():
            for p in f.Pads():
                pads_by_net.setdefault(p.GetNetname(), []).append(p)
        modes = {}

        def rippable(net):
            if net not in modes:
                policy = net_policy(net, self.rules)
                modes[net] = bool(net and net != self.target['net'] and net not in excluded and policy['mode'] == 'signal'
                                  and not policy.get('pair') and len(pads_by_net.get(net, [])) <= 8)
            return modes[net]
        self.rippable = rippable
        eligible = {t.m_Uuid.AsString(): t for t in board.GetTracks()
                    if not t.IsLocked() and rippable(t.GetNetname()) and distance_to(centers, t) <= radius}
        # One local power cluster may also be ripped: branch copper narrower than
        # its net's trunk width (a leaf/branch built for a terminal contract), never
        # a full-width trunk, a bank feed at trunk width or an in-pad array.
        branches = self.branch_items(board, radius, centers, excluded)
        record['eligible_items'] = len(eligible)
        record['eligible_branch_items'] = len(branches)
        oracle = ignoring_oracle(board, self.rules, set(eligible) | set(branches),
                                 time.monotonic() + max(5., min(60., self.remaining() / 4)))
        plan = plan_route(board, self.rules, oracle, self.target['net'], source, target, self.bounds_box, self.a.pitch)
        record['soft_plan_status'] = plan.get('status')
        if plan.get('status') != 'routed':
            return None
        hits = plan_collisions(board, self.rules, plan, eligible, self.target['net'])
        crossing = crossing_nets(plan, board, eligible)
        by_net = {}
        for identity, n in hits.items():
            net = eligible[identity].GetNetname()
            by_net[net] = by_net.get(net, 0) + n
        ranked = sorted(by_net, key=lambda n: (n not in crossing, -by_net[n], n))
        branch_hits = plan_collisions(board, self.rules, plan, branches, self.target['net'])
        power = {}
        for identity in branch_hits:
            power.setdefault(branches[identity].GetNetname(), set()).add(identity)
        record.update(collisions=by_net, crossing=sorted(crossing), power_collisions={n: len(v) for n, v in power.items()})
        if not ranked and not power:
            record['status'] = 'no_rippable_blocker'
            return None
        if len(ranked) > max_nets or len(power) > 1:
            record.update(status='too_many_rip_nets', rip_nets=ranked, power_nets=sorted(power))
            return None
        # The whole connected branch is ripped (no radius), so no stub or via is stranded.
        everywhere = self.branch_items(board, None, centers, excluded)
        power_items = {net: self.branch_component(board, seeds, everywhere) for net, seeds in power.items()}
        record['power_rip'] = {net: len(items) for net, items in power_items.items()}
        self.original_partition = partition(board)
        rip, priority, added = list(ranked), [], 0
        for attempt in range(3):
            outcome = self._rip_attempt(folder / ('try-%d' % attempt), rip, priority, power_items)
            record['attempts'].append({k: v for k, v in outcome.items() if k != 'candidate'})
            record.update(rip_nets=rip, status=outcome['status'])
            if outcome['status'] == 'accepted':
                return outcome['candidate']
            if outcome['status'] != 'restore_failed' or self.remaining() < 60:
                return None
            failed = outcome['failed_net']
            extras = [n for n in self.neighbour_nets(outcome['board'], outcome['failed_pads']) if n not in rip]
            extras = extras if added + len(extras) <= max_extra else []
            record.setdefault('negotiation', []).append(dict(failed=failed, extra=extras))
            order = [failed] + [n for n in (priority or rip) if n != failed]
            if not extras and order == priority:
                return None  # nothing new to try: same rip set, same order
            priority = order + [n for n in rip if n not in order]
            rip = rip + extras
            added += len(extras)
        return None

    def neighbour_nets(self, board_path, pads, reach=1.0):
        """Rippable signal nets with copper within ``reach`` of the trapped pads."""
        from pnr.fab_profile import load_board
        from pnr.shove.relax import distance_to
        board = load_board(board_path)
        points = [_xy(p.GetPosition()) for f in board.GetFootprints() for p in f.Pads() if p.m_Uuid.AsString() in pads]
        # The trapped end is the one inside the transaction (nearest the target).
        points = sorted(points, key=lambda q: min(math.dist(q, c) for c in self.centers))[:1]
        near = {}
        for t in board.GetTracks():
            net = t.GetNetname()
            if points and self.rippable(net) and not t.IsLocked():
                d = distance_to(points, t)
                if d <= reach:
                    near[net] = min(near.get(net, 9), d)
        self._keep_neighbour = board
        return sorted(near, key=lambda n: (near[n], n))

    def branch_items(self, board, radius, centers, excluded):
        """Unlocked sub-trunk-width power copper (tracks, and vias whose every
        track is such copper) of foreign power nets within ``radius``."""
        import pcbnew as k
        from pnr.electrical import net_policy
        from pnr.shove.relax import distance_to
        from pnr.via_in_pad import removes_unused_pads
        pairs = self.protected_nets()
        out = {}
        tracks = [t for t in board.GetTracks() if t.GetClass() == 'PCB_TRACK']
        for t in tracks:
            net = t.GetNetname()
            if not net or net == self.target['net'] or net in pairs or t.IsLocked():
                continue
            policy = net_policy(net, self.rules)
            if policy['mode'] != 'power':
                continue
            full = policy['outer_width_mm'] if t.GetLayer() in (k.F_Cu, k.B_Cu) else policy['inner_width_mm']
            if t.GetWidth() / 1e6 + 1e-6 >= full or (radius is not None and distance_to(centers, t) > radius):
                continue
            out[t.m_Uuid.AsString()] = t
        for v in board.GetTracks():
            if v.GetClass() != 'PCB_VIA' or v.IsLocked() or removes_unused_pads(v) or v.GetNetname() in pairs:
                continue
            touching = [t for t in tracks if t.GetNetCode() == v.GetNetCode()
                        and any((v.GetPosition() - e).EuclideanNorm() < 1000 for e in (t.GetStart(), t.GetEnd()))]
            if touching and all(t.m_Uuid.AsString() in out for t in touching):
                out[v.m_Uuid.AsString()] = v
        return out

    def branch_component(self, board, seeds, branches):
        """The connected branch copper around ``seeds`` (endpoint/via contact).
        Contact inside a same-net pad does not join: two branches landing on one
        terminal are separate branches."""
        import pcbnew as k
        net_pads = {}
        for f in board.GetFootprints():
            for p in f.Pads():
                net_pads.setdefault(p.GetNetCode(), []).append(p)

        def points(t):
            if t.GetClass() == 'PCB_VIA':
                return [_xy(t.GetPosition())]
            return [_xy(t.GetStart()), _xy(t.GetEnd())]

        def on_pad(code, p):
            v = k.VECTOR2I(round(p[0] * 1e6), round(p[1] * 1e6))
            return any(pad.HitTest(v) for pad in net_pads.get(code, []))
        todo, seen = list(seeds), set(seeds)
        while todo:
            item = branches[todo.pop()]
            for identity, other in branches.items():
                if identity in seen or other.GetNetCode() != item.GetNetCode():
                    continue
                if any(math.dist(p, q) < 1e-3 and not on_pad(item.GetNetCode(), p)
                       for p in points(item) for q in points(other)):
                    seen.add(identity)
                    todo.append(identity)
        return sorted(seen)

    def _rip_attempt(self, folder, rip, priority, power_items=None):
        folder.mkdir(parents=True, exist_ok=True)
        power_items = power_items or {}
        out = dict(rip_nets=list(rip), power_nets=sorted(power_items), restored=[])
        board = self.load()
        branch_ids = {u for items in power_items.values() for u in items}
        removed = [t for t in board.GetTracks() if not t.IsLocked()
                   and (t.GetNetname() in rip or t.m_Uuid.AsString() in branch_ids)]
        out['ripped_items'] = len(removed)
        for t in removed:
            board.Remove(t)
        ripped = self.save(board, folder / 'ripped.kicad_pcb')
        self.result['ripped'] = [dict(net=n, items=sum(1 for t in removed if t.GetNetname() == n),
                                      kind='power_branch' if n in power_items else 'signal_net')
                                 for n in list(rip) + sorted(power_items)]
        rip = list(rip) + sorted(power_items)
        t = self.target
        work = folder / 'target'
        cmd = [sys.executable, '-m', 'pnr.native_electrical', str(ripped), '--rules', str(self.rules_path),
               '--out-dir', str(work), '--net', t['net'], '--source-pad', t['source'], '--target-pad', t['target'],
               '--bounds', *map(str, self.bounds_box), '--seconds', str(max(10., min(90., self.remaining() / 3))),
               '--pitch', str(self.a.pitch), '--kicad-cli', self.a.kicad_cli]
        for end in ('source', 'target'):
            if t.get(end + '_uuid'):
                cmd += ['--' + end + '-pad-uuid', t[end + '_uuid']]
        self.run(cmd, folder / 'target.log', self.remaining())
        outcome = json.loads((work / 'result.json').read_text()) if (work / 'result.json').exists() else dict(status='worker_error')
        out['target_status'] = outcome.get('status')
        if not outcome.get('accepted'):
            out['status'] = 'target_failed_after_rip'
            return out
        current = work / 'candidate.kicad_pcb'
        for step in range(32):
            if self.remaining() < 20:
                out['status'] = 'time_budget'
                return out
            pair = self.split_pair(current, self.original_partition, rip, priority)
            if pair is None:
                break
            net, a_label, a_uid, b_label, b_uid, box = pair
            done = False
            if net in power_items:
                # A ripped power branch is restored by the production power worker
                # at its full current contract, like any other power target.
                rd = folder / ('restore-%02d-power' % step)
                cmd = [sys.executable, '-m', 'pnr.native_electrical', str(current), '--rules', str(self.rules_path),
                       '--out-dir', str(rd), '--net', net, '--source-pad', a_label, '--target-pad', b_label,
                       '--source-pad-uuid', a_uid, '--target-pad-uuid', b_uid, '--bounds', *map(str, self.bounds_box),
                       '--seconds', str(max(10., min(90., self.remaining() / 3))), '--pitch', str(self.a.pitch),
                       '--kicad-cli', self.a.kicad_cli]
                self.run(cmd, str(rd) + '.log', self.remaining())
                res = json.loads((rd / 'result.json').read_text()) if (rd / 'result.json').exists() else dict(accepted=False)
                out['restored'].append(dict(net=net, source=a_label, target=b_label, attempt=0,
                                            accepted=res.get('accepted', False), status=res.get('status')))
                if res.get('accepted'):
                    current = rd / 'candidate.kicad_pcb'
                    continue
                out.update(status='restore_failed', failed_net=net, failed_pads=[a_uid, b_uid], board=str(current))
                self.result['restored'] = out['restored']
                return out
            for attempt, (grow, pitch) in enumerate(((2., .1), (4., .05))):
                rd = folder / ('restore-%02d-%d' % (step, attempt))
                bounds = [max(self.bounds_box[0], box[0] - grow), max(self.bounds_box[1], box[1] - grow),
                          min(self.bounds_box[2], box[2] + grow), min(self.bounds_box[3], box[3] + grow)]
                cmd = [sys.executable, self.a.adapter, str(current), '--out-dir', str(rd), '--net', net,
                       '--source-pad', a_label, '--target-pad', b_label, '--source-pad-uuid', a_uid, '--target-pad-uuid', b_uid,
                       '--bounds', *map(str, bounds), '--pitch', str(pitch), '--layers', '--joint', '--preserve-copper',
                       '--max-expansions', '150000', '--max-orders', '32',
                       '--max-seconds', str(max(5., min(60., self.remaining() / 4))), '--kicad-cli', self.a.kicad_cli,
                       '--rules', str(self.rules_path)]
                for s in self.a.annotation_source:
                    cmd += ['--annotation-source', str(s)]
                self.run(cmd, str(rd) + '.log', self.remaining())
                res = json.loads((rd / 'result.json').read_text()) if (rd / 'result.json').exists() else dict(accepted=False)
                out['restored'].append(dict(net=net, source=a_label, target=b_label, attempt=attempt,
                                            accepted=res.get('accepted', False), status=res.get('status')))
                if res.get('accepted'):
                    current = rd / 'candidate.kicad_pcb'
                    done = True
                    break
            if not done:
                out.update(status='restore_failed', failed_net=net, failed_pads=[a_uid, b_uid], board=str(current))
                self.result['restored'] = out['restored']
                return out
        self.result['restored'] = out['restored']
        gate = self.gate(folder, current, [])
        out['gate'] = gate
        if not gate.get('accepted'):
            out['status'] = 'gate_rejected'
            return out
        out.update(status='accepted', candidate=current)
        return out

    def split_pair(self, board_path, original, nets, priority=()):
        """Nearest pad pair of ``nets`` (the first ``priority`` net with a split
        first) that the original board connected and the current one does not:
        ``(net, label, uuid, label, uuid, box)`` or None."""
        import pcbnew as k
        from pnr.via_coalesce import partition
        from pnr.fab_profile import load_board
        board = load_board(board_path)
        board.BuildConnectivity()
        now = {i: j for j, g in enumerate(partition(board)) for i in g}
        old = {i: j for j, g in enumerate(original) for i in g}
        best = None
        for net in nets:
            pads = [(f.GetReference() + '.' + p.GetNumber(), p.m_Uuid.AsString(), _xy(p.GetPosition()))
                    for f in board.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
            for i, (la, ua, pa) in enumerate(pads):
                for lb, ub, pb in pads[i + 1:]:
                    if old.get(ua) == old.get(ub) and now.get(ua) != now.get(ub):
                        d = (priority.index(net) if net in priority else len(priority), math.dist(pa, pb))
                        if best is None or d < best[0]:
                            box = [min(pa[0], pb[0]), min(pa[1], pb[1]), max(pa[0], pb[0]), max(pa[1], pb[1])]
                            best = (d, (net, la, ua, lb, ub, box))
        self._keep = board
        return best[1] if best else None


def run(a):
    tx = Transaction(a)
    tx.out.mkdir(parents=True, exist_ok=False)
    result = tx.result
    board = tx.load()
    tx.load_origin()
    tx.bounds_box = list(a.bounds) if a.bounds else tx.bounds(board)
    if a.bounds:
        from pnr.via_coalesce import partition
        from pnr.shove.targets import runtime_bounds
        source, target = tx.endpoints(board)
        tx.bounds_box = runtime_bounds(board, partition(board), source, target, tx.target['net'], tx.bounds_box)
    result['bounds'] = tx.bounds_box
    parts = [p for p in (a.parts or '').split(',') if p]
    rungs = [int(r) for r in a.rungs.split(',')]
    candidate = None
    for rung in rungs:
        if tx.remaining() < 30:
            result['status'] = 'time_budget'
            break
        try:
            if rung == 1:
                candidate = tx.make_room(1, 2.5, [.15, .25], [])
            elif rung == 2:
                if not parts:
                    result['rungs'].append(dict(rung=2, status='no_nudgeable_parts'))
                    continue
                candidate = tx.make_room(2, 3.5, [.15, .25, .35], parts)
            elif rung == 4:
                candidate = tx.rip_reroute(a.rip_radius)
        except (TimeoutError, ValueError, RuntimeError) as error:
            result['rungs'].append(dict(rung=rung, status='error', error=repr(error)))
            candidate = None
        if candidate is not None:
            tx.publish(candidate)
            result.update(accepted=True, status='routed', rung=rung)
            break
    if not result['accepted'] and result['status'] == 'not_attempted':
        result['status'] = 'no_make_room'
    result['seconds'] = round(time.monotonic() - tx.started, 2)
    (tx.out / 'result.json').write_text(json.dumps(result, indent=2, default=str) + '\n')
    print(json.dumps({k: result[k] for k in ('accepted', 'status', 'seconds')} | dict(
        rungs=[(r.get('rung'), r.get('status')) for r in result['rungs']])))
    return result
