"""Post-route quality analysis (design §9.6 — the Phase 6 quality pass).

Once the board is routed (FreeRouting), this measures the things a placement/route
loop can't see until copper exists: **routed length** and **via count** per net,
**differential-pair length skew**, and **length-match** group compliance. It turns
the ``diff_pair`` / ``length_match`` / ``net_class`` guidance (see
``docs/hardware/pnr-inputs.md``) into concrete pass/fail checks, and reports the
totals used for via/length optimization.

Two layers, mirroring the rest of the engine:

- :func:`analyze` is **pure** (per-net length/via dicts + the resolved rules → a
  :class:`QualityReport`), so it is unit-testable with no KiCad.
- :func:`net_lengths` sums each net's ``PCB_TRACK`` / ``PCB_ARC`` lengths and counts
  its vias via ``pcbnew`` (lazy import, ``@kicad_python``): the totals.
- :func:`kicad_net_lengths` measures the pair and group members the way KiCad's DRC
  does (:mod:`pnr.length_model`: in-pad parts straightened, via spans through the
  board's stackup) and, for budgets in ps, their delays: the pair and group checks.

Pair and group results carry their **margin** (budget minus skew or spread), in mm,
or in ps when the rule gives ``skew_ps`` / ``tolerance_ps``.

The rules come from ``rules.json`` (emitted by ``pnr.route`` from the compiled
constraints), so this step needs neither pyyaml nor torch — it runs under the same
KiCad python as ingest/writeback.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_NM_PER_MM = 1_000_000.0


@dataclass
class DiffPairResult:
    name: str
    p: str
    n: str
    len_p_mm: float
    len_n_mm: float
    skew_mm: float
    tol_mm: float
    routed: bool  # both nets have copper
    # A budget in ps (``skew_ps``): the pair is judged on delay instead.
    skew_ps: Optional[float] = None
    tol_ps: Optional[float] = None

    @property
    def ok(self) -> bool:
        if self.tol_ps is not None:
            return self.routed and self.skew_ps is not None and self.skew_ps <= self.tol_ps + 1e-6
        return self.routed and self.skew_mm <= self.tol_mm + 1e-6

    @property
    def margin(self) -> Optional[float]:
        """Budget minus skew, in the budget's unit."""
        if not self.routed:
            return None
        if self.tol_ps is not None:
            return None if self.skew_ps is None else self.tol_ps - self.skew_ps
        return self.tol_mm - self.skew_mm


@dataclass
class LengthMatchResult:
    name: str
    nets: List[str]
    lengths_mm: List[float]
    spread_mm: float
    tol_mm: float
    routed: bool  # every member has copper
    spread_ps: Optional[float] = None
    tol_ps: Optional[float] = None

    @property
    def ok(self) -> bool:
        if self.tol_ps is not None:
            return (
                self.routed and self.spread_ps is not None and self.spread_ps <= self.tol_ps + 1e-6
            )
        return self.routed and self.spread_mm <= self.tol_mm + 1e-6

    @property
    def margin(self) -> Optional[float]:
        """Tolerance minus spread, in the tolerance's unit."""
        if not self.routed:
            return None
        if self.tol_ps is not None:
            return None if self.spread_ps is None else self.tol_ps - self.spread_ps
        return self.tol_mm - self.spread_mm


@dataclass
class QualityReport:
    total_length_mm: float
    total_vias: int
    routed_nets: int
    unrouted: int = 0  # remaining ratsnest connections (0 == fully routed)
    diff_pairs: List[DiffPairResult] = field(default_factory=list)
    length_matches: List[LengthMatchResult] = field(default_factory=list)
    net_class_length_mm: Dict[str, float] = field(default_factory=dict)
    electrical: Optional[Dict] = None
    electrical_required: bool = False

    @property
    def fully_routed(self) -> bool:
        return self.unrouted == 0

    @property
    def ok(self) -> bool:
        """Fully routed AND all diff-pair + length-match checks pass."""
        return (
            self.fully_routed
            and (
                not self.electrical_required
                or bool(self.electrical and self.electrical.get("qualified"))
            )
            and all(d.ok for d in self.diff_pairs)
            and all(m.ok for m in self.length_matches)
        )

    def summary(self) -> str:
        route = "fully routed" if self.fully_routed else f"{self.unrouted} UNROUTED connections"
        lines = [
            f"routed length {self.total_length_mm:.0f} mm, {self.total_vias} vias, "
            f"{self.routed_nets} nets with copper — {route}"
        ]
        for c, ln in sorted(self.net_class_length_mm.items()):
            lines.append(f"  net-class {c}: {ln:.0f} mm")
        for d in self.diff_pairs:
            status = (
                "OK"
                if d.ok
                else (
                    ("UNVERIFIED" if self.electrical_required else "UNROUTED")
                    if not d.routed
                    else "FAIL"
                )
            )
            if d.tol_ps is not None and d.skew_ps is not None:
                lines.append(
                    f"  diff-pair {d.name}: skew {d.skew_ps:.2f} ps ({d.skew_mm:.3f} mm) "
                    f"(tol {d.tol_ps:.2f} ps, margin {d.margin:.2f} ps) [{status}]"
                )
            else:
                margin = "" if d.margin is None else f", margin {d.margin:.3f}"
                lines.append(
                    f"  diff-pair {d.name}: skew {d.skew_mm:.3f} mm "
                    f"(tol {d.tol_mm:.2f}{margin}) [{status}]"
                )
        for m in self.length_matches:
            status = "OK" if m.ok else ("UNROUTED" if not m.routed else "FAIL")
            if m.tol_ps is not None and m.spread_ps is not None:
                lines.append(
                    f"  length-match {m.name}: spread {m.spread_ps:.2f} ps ({m.spread_mm:.3f} mm) "
                    f"(tol {m.tol_ps:.2f} ps, margin {m.margin:.2f} ps) [{status}]"
                )
            else:
                margin = "" if m.margin is None else f", margin {m.margin:.3f}"
                lines.append(
                    f"  length-match {m.name}: spread {m.spread_mm:.3f} mm "
                    f"(tol {m.tol_mm:.2f}{margin}) [{status}]"
                )
        if self.electrical_required:
            lines.append(
                "  electrical qualification: "
                + (
                    "PASS"
                    if self.electrical and self.electrical.get("qualified")
                    else "PENDING / NOT QUALIFIED"
                )
            )
        lines.append(f"quality: {'PASS' if self.ok else 'FAIL'}")
        return "\n".join(lines)


def analyze(
    lengths: Dict[str, float],
    vias: Dict[str, int],
    rules: Dict,
    unrouted: int = 0,
    delays: Optional[Dict[str, float]] = None,
    matched_lengths: Optional[Dict[str, float]] = None,
) -> QualityReport:
    """Score routed per-net ``lengths`` (mm) + ``vias`` against ``rules`` (the
    ``rules.json`` dict). ``unrouted`` is the remaining ratsnest count (0 == fully
    routed). ``matched_lengths`` (KiCad's DRC length of the pair and group members,
    :func:`kicad_net_lengths`) judge the pairs and groups in place of ``lengths``,
    which give the totals. ``delays`` (ps per net) judge the rules that give a budget
    in ps; such a rule without delays fails. Pure — no KiCad."""
    delays = delays or {}
    total_len = float(sum(lengths.values()))
    total_vias = int(sum(vias.values()))
    routed = sum(1 for v in lengths.values() if v > 0)
    member = dict(lengths)
    member.update(matched_lengths or {})

    diff_pairs: List[DiffPairResult] = []
    for dp in rules.get("diff_pairs", []):
        lp = member.get(dp["p"], 0.0)
        ln = member.get(dp["n"], 0.0)
        is_routed = lp > 0 and ln > 0
        tol_ps = dp.get("skew_ps")
        skew_ps = None
        if tol_ps is not None and dp["p"] in delays and dp["n"] in delays:
            skew_ps = abs(delays[dp["p"]] - delays[dp["n"]])
        diff_pairs.append(
            DiffPairResult(
                name=dp["name"],
                p=dp["p"],
                n=dp["n"],
                len_p_mm=lp,
                len_n_mm=ln,
                skew_mm=abs(lp - ln),
                tol_mm=float(dp.get("skew_mm", 0.5)),
                routed=is_routed,
                skew_ps=skew_ps,
                tol_ps=None if tol_ps is None else float(tol_ps),
            )
        )

    length_matches: List[LengthMatchResult] = []
    for lm in rules.get("length_match", []):
        nets = list(lm.get("nets", []))
        lns = [member.get(n, 0.0) for n in nets]
        is_routed = all(v > 0 for v in lns) and len(lns) >= 2
        spread = (max(lns) - min(lns)) if lns else 0.0
        tol_ps = lm.get("tolerance_ps")
        spread_ps = None
        if tol_ps is not None and nets and all(n in delays for n in nets):
            ds = [delays[n] for n in nets]
            spread_ps = max(ds) - min(ds)
        length_matches.append(
            LengthMatchResult(
                name=lm["name"],
                nets=nets,
                lengths_mm=lns,
                spread_mm=spread,
                tol_mm=float(lm.get("tolerance_mm", 1.0)),
                routed=is_routed,
                spread_ps=spread_ps,
                tol_ps=None if tol_ps is None else float(tol_ps),
            )
        )

    nc_len: Dict[str, float] = {}
    for nc in rules.get("net_classes", []):
        nc_len[nc["name"]] = float(sum(lengths.get(n, 0.0) for n in nc.get("nets", [])))

    return QualityReport(
        total_length_mm=total_len,
        total_vias=total_vias,
        routed_nets=routed,
        unrouted=int(unrouted),
        diff_pairs=diff_pairs,
        length_matches=length_matches,
        net_class_length_mm=nc_len,
        electrical_required=bool(rules.get("electrical_fab")),
    )


def net_lengths(board) -> Tuple[Dict[str, float], Dict[str, int]]:
    """Per-net routed track length (mm) and via count from an open ``pcbnew.BOARD``."""
    import pcbnew

    lengths: Dict[str, float] = {}
    vias: Dict[str, int] = {}
    for t in board.GetTracks():
        net = t.GetNetname()
        if isinstance(t, pcbnew.PCB_VIA):
            vias[net] = vias.get(net, 0) + 1
        else:
            lengths[net] = lengths.get(net, 0.0) + t.GetLength() / _NM_PER_MM
    return lengths, vias


def board_geometry(board, nets=None):
    """``(tracks, vias, pads, stackup)`` of an open ``pcbnew.BOARD`` for
    :mod:`pnr.length_model`, in the board's own frame (mm): tracks per net as
    ``(layer, a, b, width)``, via centres per net, every pad's copper outline."""
    import pcbnew

    from pnr import length_model as lm

    wanted = None if nets is None else set(nets)
    tracks: Dict[str, list] = {}
    vias: Dict[str, list] = {}
    mm = 1.0 / _NM_PER_MM
    for t in board.GetTracks():
        net = t.GetNetname()
        if wanted is not None and net not in wanted:
            continue
        if isinstance(t, pcbnew.PCB_VIA):
            p = t.GetPosition()
            vias.setdefault(net, []).append((p.x * mm, p.y * mm, t.GetWidth(pcbnew.F_Cu) * mm / 2))
        else:
            layer = board.GetLayerName(t.GetLayer())
            for a, b in _track_pieces(t, pcbnew):
                tracks.setdefault(net, []).append(
                    (layer, (a[0] * mm, a[1] * mm), (b[0] * mm, b[1] * mm), t.GetWidth() * mm)
                )
    pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            net = pad.GetNetname()
            if not net or (wanted is not None and net not in wanted):
                continue
            layers = [board.GetLayerName(x) for x in pad.GetLayerSet().CuStack()]
            if not layers:
                continue
            poly = pad.GetEffectivePolygon(board.GetLayerID(layers[0]), pcbnew.ERROR_INSIDE)
            outline = poly.Outline(0)
            pts = [
                (outline.CPoint(i).x * mm, outline.CPoint(i).y * mm)
                for i in range(outline.PointCount())
            ]
            # KiCad's frame is y-down: give the model a counter-clockwise outline.
            area = sum(
                pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                for i in range(len(pts))
            )
            if area < 0:
                pts.reverse()
            p = pad.GetPosition()
            smd = pad.GetAttribute() in (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD_ATTRIB_CONN)
            side = layers[0] if smd else board.GetLayerName(fp.GetLayer())
            pads.append(
                lm.PadCopper(net, (p.x * mm, p.y * mm), frozenset(layers), tuple(pts), side)
            )
    return tracks, vias, pads


# An arc is measured as chords of at most this angle (radians): about 1e-4 short of
# the arc's length.
ARC_CHORD_RAD = math.pi / 64


def _track_pieces(t, pcbnew):
    """A track's straight pieces in nanometres: the segment itself, or an arc's
    chords (:data:`ARC_CHORD_RAD`; the length model takes straight lines)."""
    a, b = t.GetStart(), t.GetEnd()
    if not isinstance(t, getattr(pcbnew, "PCB_ARC", ())):
        return [((a.x, a.y), (b.x, b.y))]
    c = t.GetCenter()
    r = math.dist((a.x, a.y), (c.x, c.y))
    start = math.atan2(a.y - c.y, a.x - c.x)
    sweep = t.GetAngle().AsRadians()
    chords = max(4, math.ceil(abs(sweep) / ARC_CHORD_RAD))
    pts = [(a.x, a.y)]
    for k in range(1, chords):
        th = start + sweep * k / chords
        pts.append((c.x + r * math.cos(th), c.y + r * math.sin(th)))
    pts.append((b.x, b.y))
    return list(zip(pts, pts[1:]))


def matched_nets(rules: Dict) -> List[str]:
    """The members of the rules' pairs and groups."""
    out = [n for dp in rules.get("diff_pairs", []) for n in (dp.get("p"), dp.get("n")) if n]
    out += [n for g in rules.get("length_match", []) for n in g.get("nets", [])]
    return list(dict.fromkeys(out))


def kicad_net_lengths(board, board_text: str, nets=None, delays: bool = False):
    """Per-net KiCad-equivalent length (mm), via count and (with ``delays``) delay
    (ps) of ``nets`` (every net when None) on an open ``pcbnew.BOARD`` whose file
    text is ``board_text`` (:mod:`pnr.length_model`)."""
    from pnr import length_model as lm

    tracks, vias, pads = board_geometry(board, nets)
    st = lm.read_stackup(board_text) or lm.default_stackup(max(2, board.GetCopperLayerCount()))
    delay = lm.DelayModel(st) if delays else None
    names = sorted(set(tracks) | set(vias))
    lengths, via_count, delay_ps = {}, {}, {}
    for net in names:
        r = lm.net_length(net, tracks.get(net, []), vias.get(net, []), pads, st, delay)
        lengths[net] = r.total_mm
        via_count[net] = r.vias
        if delay is not None:
            delay_ps[net] = r.delay_ps
    return lengths, via_count, delay_ps


def unrouted_count(board) -> int:
    """Remaining ratsnest connections (unrouted pin pairs). 0 == fully routed."""
    try:
        conn = board.GetConnectivity()
        return int(conn.GetUnconnectedCount(True))
    except Exception as exc:
        raise RuntimeError(
            "Cannot verify board connectivity; refusing to report it routed"
        ) from exc


def load(pcb_path: str, rules: Optional[Dict] = None, delays: bool = False):
    """``(lengths, vias, unrouted, matched_lengths, delays)`` of a routed board: raw
    per-net track sums and via counts (:func:`net_lengths`), and the lengths (and with
    ``delays`` the delays) of the pair and group members as KiCad's DRC measures them
    (:func:`kicad_net_lengths`)."""
    import pcbnew

    board = pcbnew.LoadBoard(pcb_path)
    board.BuildConnectivity()
    lengths, vias = net_lengths(board)
    members = matched_nets(rules or {})
    matched, delay_ps = {}, {}
    if members:
        with open(pcb_path, encoding="utf-8") as fh:
            text = fh.read()
        matched, _count, delay_ps = kicad_net_lengths(board, text, nets=members, delays=delays)
    return lengths, vias, unrouted_count(board), matched, delay_ps


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pcb", help="the routed .kicad_pcb")
    ap.add_argument("--rules", help="rules.json (pnr.route --dump-rules); optional")
    ap.add_argument("--out", metavar="PATH", help="write the report text")
    ap.add_argument("--gate", action="store_true", help="exit nonzero if any check fails")
    ap.add_argument(
        "--require-routed",
        action="store_true",
        help="exit nonzero if any net is unrouted (the routing-completeness gate)",
    )
    args = ap.parse_args(argv)

    rules = {}
    if args.rules:
        with open(args.rules, encoding="utf-8") as fh:
            rules = json.load(fh)
    timed = any(dp.get("skew_ps") is not None for dp in rules.get("diff_pairs", [])) or any(
        lm.get("tolerance_ps") is not None for lm in rules.get("length_match", [])
    )
    lengths, vias, unrouted, matched, delays = load(args.pcb, rules, delays=timed)

    report = analyze(
        lengths, vias, rules, unrouted=unrouted, delays=delays, matched_lengths=matched
    )
    if report.electrical_required:
        from pathlib import Path

        import pcbnew

        from pnr.electrical_audit import audit_board

        board = pcbnew.LoadBoard(args.pcb)
        board.BuildConnectivity()
        report.electrical = audit_board(board, rules, Path(args.pcb).read_text())
        reports = {p["name"]: p for p in report.electrical["pairs"]}
        for pair in report.diff_pairs:
            checked = reports.get(pair.name, {})
            metrics = checked.get("endpoint_metrics", {})
            pair.routed = all(metrics.get(n, {}).get("valid", False) for n in (pair.p, pair.n))
            if pair.routed:
                pair.len_p_mm = metrics[pair.p]["length_mm"]
                pair.len_n_mm = metrics[pair.n]["length_mm"]
                pair.skew_mm = abs(pair.len_p_mm - pair.len_n_mm)
            else:
                pair.skew_mm = float("inf")
                pair.skew_ps = None  # no delay for a pair the audit finds open
    text = report.summary()
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    if args.require_routed and not report.fully_routed:
        sys.stderr.write(
            "pnr: routing incomplete — %d unrouted connection(s); board is not "
            "fab-ready\n" % report.unrouted
        )
        return 4
    if args.gate and not report.ok:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
