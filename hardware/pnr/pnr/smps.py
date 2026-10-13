"""Explicit SMPS layout requirements and fail-closed geometric screening.

Limits belong to the article: datasheet words such as 'close' do not establish
universal millimetre limits. Native copper, pad geometry and exact-board evidence
are distinct inputs. Geometry never certifies AC stability, EMI or thermals.
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import json
import math

KINDS = {
    "pad_distance": {"terminals", "max_mm"},
    "loop_geometry": {"terminals", "max_perimeter_mm", "max_area_mm2"},
    "route_path": {"terminals", "max_length_mm", "max_vias"},
    "net_geometry": {"net", "max_length_mm", "max_bbox_area_mm2", "max_vias", "min_width_mm"},
    "separation": {"nets", "aggressors", "min_mm"},
    "paired_routes": {"paths", "max_gap_mm", "max_skew_mm", "max_angle_deg"},
    "kelvin": {"terminals", "power_terminals"},
    "reference_plane": {"terminals", "net", "layer", "margin_mm", "max_uncovered_mm2"},
    "via_array": {"terminals", "net", "layers", "radius_mm", "min_count"},
    "evidence": {"metric", "min_value", "max_value", "unit", "corners"},
}
COMMON = {"id", "kind", "source", "rationale", "provisional", "refs"}


def parse_checks(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("smps_checks must be a list")
    seen = set()
    result = copy.deepcopy(raw)
    for check in result:
        if not isinstance(check, dict) or check.get("kind") not in KINDS:
            raise ValueError("Unknown SMPS check kind")
        if not isinstance(check.get("id"), str) or not check["id"] or check["id"] in seen:
            raise ValueError("SMPS check requires a unique id")
        seen.add(check["id"])
        if set(check) - COMMON - KINDS[check["kind"]]:
            raise ValueError("Unknown key in SMPS check " + check["id"])
        for name, value in check.items():
            if name.startswith(("min_", "max_")) or name in ("radius_mm", "margin_mm"):
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or (value < 0 and name not in ("min_value", "max_value"))
                ):
                    raise ValueError(
                        f"{check['id']}.{name} must be finite (geometry/count limits must be nonnegative)"
                    )
                if name in ("max_vias", "min_count") and type(value) is not int:
                    raise ValueError("via/count limits must be integers")
        if "provisional" in check and type(check["provisional"]) is not bool:
            raise ValueError("SMPS provisional must be boolean")
        if not isinstance(check.get("source"), str) or not check["source"]:
            raise ValueError("SMPS check requires its guidance/requirement source")
        # An absent numeric budget is admissible but never a passing check.
        if check["kind"] in (
            "pad_distance",
            "loop_geometry",
            "route_path",
            "kelvin",
            "reference_plane",
            "via_array",
        ):
            terms = check.get("terminals")
            if (
                not isinstance(terms, list)
                or len(terms) < 2
                or any(
                    not isinstance(t, str)
                    or (len(t.rsplit(":", 1)) != 2 or not all(t.rsplit(":", 1)))
                    for t in terms
                )
            ):
                raise ValueError("SMPS terminals must be [REF:PAD, ...]")
            if check["kind"] in ("pad_distance", "route_path", "kelvin") and len(terms) != 2:
                raise ValueError("SMPS path/distance needs two terminals")
        if check["kind"] == "paired_routes":
            paths = check.get("paths")
            if (
                not isinstance(paths, list)
                or len(paths) != 2
                or any(
                    not isinstance(p, list)
                    or len(p) != 2
                    or any(not isinstance(t, str) or ":" not in t for t in p)
                    for p in paths
                )
            ):
                raise ValueError("SMPS paired_routes needs two terminal pairs")
        if check["kind"] in ("net_geometry", "reference_plane", "via_array"):
            if not isinstance(check.get("net"), str) or not check["net"]:
                raise ValueError("SMPS copper check needs net")
        if check["kind"] == "reference_plane" and (
            not isinstance(check.get("layer"), str) or not check["layer"]
        ):
            raise ValueError("SMPS reference_plane needs layer")
        for key in ("nets", "aggressors", "power_terminals", "corners", "layers", "refs"):
            if key in check and (
                not isinstance(check[key], list)
                or not check[key]
                or any(not isinstance(n, str) or not n for n in check[key])
            ):
                raise ValueError("SMPS " + key + " must be a nonempty name list")
        if check["kind"] == "separation" and not all(check.get(k) for k in ("nets", "aggressors")):
            raise ValueError("SMPS separation needs nets and aggressors")
        if check["kind"] == "kelvin" and not check.get("power_terminals"):
            raise ValueError("SMPS kelvin needs power_terminals")
        if check["kind"] == "evidence" and not all(
            isinstance(check.get(k), str) and check[k] for k in ("metric", "unit")
        ):
            raise ValueError("SMPS evidence needs metric and unit")
    return result


def scope_checks(checks, refs, nets):
    """Keep wholly local checks; defer cross-boundary contracts to their parent.

    Evidence with no explicit refs belongs to the assembled board. Deferred IDs
    are retained as provenance; this is not a passing result for those checks.
    """
    refs, nets = set(refs), set(nets)
    local, deferred = [], []
    for check in checks:
        terms = check.get("terminals", []) + check.get("power_terminals", [])
        terms += [t for pair in check.get("paths", []) for t in pair]
        touched = {t.rsplit(":", 1)[0] for t in terms} | set(check.get("refs", []))
        named = set(check.get("nets", [])) | set(check.get("aggressors", []))
        if check.get("net"):
            named.add(check["net"])
        if (touched or named) and touched <= refs and named <= nets:
            local.append(copy.deepcopy(check))
        else:
            deferred.append(check["id"])
    return local, deferred


def _point(p):
    return p["x"], p["y"]


def _segment_distance(p, a, b):
    d = (b[0] - a[0], b[1] - a[1])
    length = d[0] * d[0] + d[1] * d[1]
    t = max(0, min(1, ((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / length)) if length else 0
    return math.dist(p, (a[0] + t * d[0], a[1] + t * d[1]))


def _segments_gap(a, b, c, d):
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    if (
        cross(a, b, c) * cross(a, b, d) <= 0
        and cross(c, d, a) * cross(c, d, b) <= 0
        and max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0]))
        and max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1]))
    ):
        return 0.0
    return min(
        _segment_distance(p, u, v) for p, u, v in ((a, c, d), (b, c, d), (c, a, b), (d, a, b))
    )


def _hull(points):
    points = sorted(set(points))

    def half(points):
        out = []
        for p in points:
            while (
                len(out) > 1
                and (out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])
                <= 0
            ):
                out.pop()
            out.append(p)
        return out

    return half(points)[:-1] + half(points[::-1])[:-1] if len(points) > 1 else points


class Unqualified(ValueError):
    pass


def audit(dump, checks, *, board_sha256=None, evidence=None):
    checks = parse_checks(checks)
    pads = {}
    for p in dump.get("pads", []):
        pads.setdefault(p["ref"] + ":" + p["name"], []).append(p)
    evidence = evidence or {}
    rows = []
    cache = {}

    def terminal(name):
        if name not in pads:
            raise Unqualified("Missing terminal " + name)
        hits = pads[name]
        if len({p["net"] for p in hits}) != 1:
            raise Unqualified("Ambiguous terminal net " + name)
        return hits

    def point(name):
        p = terminal(name)
        return tuple(sum(v[k] for v in p) / len(p) for k in ("x", "y"))

    def route(ends):
        from pnr.hier.power_quality import Copper

        aa, bb = map(terminal, ends)
        net = aa[0]["net"]
        if not net or net != bb[0]["net"]:
            raise ValueError("Route terminals are on different/empty nets")
        if dump.get("contact_errors"):
            raise Unqualified("Native copper contact extraction failed")
        if any(t.get("arc") for t in dump.get("tracks", []) if t["net"] == net):
            raise Unqualified("Arc path extraction is not supported by this screen")
        if net not in cache:
            # A plane island is NOT a straight-line zero-impedance route. Remove
            # plane shortcut edges and require an explicit track/pad/via path.
            no_planes = copy.deepcopy(dump)
            for p in no_planes.get("pads", []) + no_planes.get("vias", []):
                p["zone_hits"] = []
            cache[net] = (Copper(no_planes, net, exact_pad_contacts=True), no_planes)
        c, no_planes = cache[net]
        src = ends[0].rsplit(":", 1)
        dst = ends[1].rsplit(":", 1)
        starts = c.pads.get(tuple(src), [])
        targets = set(c.pads.get(tuple(dst), []))
        queue = [(0.0, n, []) for n in starts]
        heapq.heapify(queue)
        best = {n: 0.0 for n in starts}
        while queue:
            length, node, path = heapq.heappop(queue)
            if length > best[node] + 1e-9:
                continue
            path = path + [node]
            if node in targets:
                return dict(
                    length_mm=length,
                    vias=sum(c.kind[n] == "via" for n in path),
                    points=[c.xy[n] for n in path],
                    nodes=path,
                    copper=c,
                    net=net,
                )
            for next_node, step, _ in c.adj[node]:
                value = length + step
                if value < best.get(next_node, math.inf) - 1e-9:
                    best[next_node] = value
                    heapq.heappush(queue, (value, next_node, path))
        # The older ranking graph admits bounding-box pad contacts. They must
        # never certify a route through an empty corner of a rotated/round pad.
        approximate = Copper(no_planes, net)
        pending = list(approximate.pads.get(tuple(src), []))
        seen = set(pending)
        approximate_targets = set(approximate.pads.get(tuple(dst), []))
        while pending:
            node = pending.pop()
            if node in approximate_targets:
                raise Unqualified(
                    "Route depends on pad contacts not verified by native endpoint hit tests"
                )
            for neighbor, _, _ in approximate.adj[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        if any(z["net"] == net for z in dump.get("zones", [])):
            raise Unqualified(
                "Path uses or may require a filled plane; extracted return-path/impedance evidence required"
            )
        raise ValueError("No connected copper path between " + " and ".join(ends))

    def measure(check):
        kind = check["kind"]
        if kind == "pad_distance":
            a, b = map(terminal, check["terminals"])
            return dict(mm=min(math.dist(_point(p), _point(q)) for p in a for q in b))
        if kind == "loop_geometry":
            points = [point(t) for t in check["terminals"]]
            hull = _hull(points)
            return dict(
                perimeter_mm=sum(math.dist(a, b) for a, b in zip(points, points[1:] + points[:1])),
                area_mm2=abs(
                    sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(hull, hull[1:] + hull[:1]))
                )
                / 2,
                polygon=points,
                scope="terminal geometry only; no routed inductance claim",
            )
        if kind == "route_path":
            p = route(check["terminals"])
            return {k: p[k] for k in ("length_mm", "vias", "points")}
        if kind == "kelvin":
            p = route(check["terminals"])
            c = p["copper"]
            target = check["terminals"][1]
            if any(z["net"] == p["net"] for z in dump.get("zones", [])):
                raise Unqualified(
                    "Kelvin isolation on a filled plane requires shared-impedance extraction"
                )
            offenders = []
            for name in check["power_terminals"]:
                terminal(name)
                if name == target:
                    continue
                nodes = set(c.pads.get(tuple(name.rsplit(":", 1)), []))
                # A shortest route reaching another power pad first is not a
                # dedicated Kelvin lead. Filled-plane paths stay unqualified.
                if nodes.intersection(p["nodes"][1:-1]) or any(
                    _segment_distance(_point(pad), a, b)
                    <= 0.5 * min(pad["bbox"][2] - pad["bbox"][0], pad["bbox"][3] - pad["bbox"][1])
                    for pad in terminal(name)
                    for a, b in zip(p["points"], p["points"][1:])
                ):
                    offenders.append(name)
            return dict(shared_power_terminals=offenders, kelvin=not offenders)
        if kind == "net_geometry":
            net = check["net"]
            tracks = [t for t in dump.get("tracks", []) if t["net"] == net]
            ps = [p for p in dump.get("pads", []) if p["net"] == net]
            if not ps:
                raise Unqualified("Net is absent: " + net)
            if any(z["net"] == net for z in dump.get("zones", [])):
                raise Unqualified(
                    "Net includes filled copper; polygon union/extracted width evidence required"
                )
            if not tracks:
                raise Unqualified("No routed tracks on " + net)
            if any(t.get("arc") for t in tracks):
                raise Unqualified("Arc envelope is unavailable")
            boxes = [p["bbox"] for p in ps]
            for t in tracks:
                boxes.append(
                    [
                        min(t["x1"], t["x2"]) - t["w"] / 2,
                        min(t["y1"], t["y2"]) - t["w"] / 2,
                        max(t["x1"], t["x2"]) + t["w"] / 2,
                        max(t["y1"], t["y2"]) + t["w"] / 2,
                    ]
                )
            return dict(
                length_mm=sum(t["length"] for t in tracks),
                width_mm=min(t["w"] for t in tracks),
                vias=sum(v["net"] == net for v in dump.get("vias", [])),
                bbox_area_mm2=(max(b[2] for b in boxes) - min(b[0] for b in boxes))
                * (max(b[3] for b in boxes) - min(b[1] for b in boxes)),
            )
        if kind == "separation":

            def segments(nets):
                out = []
                for net in nets:
                    if not any(p["net"] == net for p in dump.get("pads", [])):
                        raise Unqualified("Missing net " + net)
                    if any(z["net"] == net for z in dump.get("zones", [])):
                        raise Unqualified("Filled-zone separation requires polygon geometry")
                    ts = [t for t in dump.get("tracks", []) if t["net"] == net]
                    if not ts or any(t.get("arc") for t in ts):
                        raise Unqualified("Missing straight routing geometry for " + net)
                    out += [((t["x1"], t["y1"]), (t["x2"], t["y2"]), t["w"] / 2) for t in ts]
                    # Circumscribed pad/via discs are conservative across layers;
                    # this screen never invents credit for a shielding plane.
                    out += [
                        (
                            _point(p),
                            _point(p),
                            math.hypot(p["bbox"][2] - p["bbox"][0], p["bbox"][3] - p["bbox"][1])
                            / 2,
                        )
                        for p in dump.get("pads", [])
                        if p["net"] == net
                    ]
                    out += [
                        (_point(v), _point(v), v["d"] / 2)
                        for v in dump.get("vias", [])
                        if v["net"] == net
                    ]
                return out

            a, b = segments(check["nets"]), segments(check["aggressors"])
            return dict(
                mm=max(0, min(_segments_gap(p, q, u, v) - r - s for p, q, r in a for u, v, s in b)),
                scope="conservative XY separation, no interlayer shielding credit",
            )
        if kind == "paired_routes":
            a, b = [route(p) for p in check["paths"]]

            def directed(points, other):
                segments = list(zip(other, other[1:]))
                if not segments:
                    raise Unqualified("Empty sense path")
                worst = 0
                angle = 0
                segments = [(u, v) for u, v in segments if math.dist(u, v) > 1e-9]
                if not segments:
                    raise Unqualified("Zero-length paired route")
                for p, q in zip(points, points[1:]):
                    n = max(1, math.ceil(math.dist(p, q) / 0.1))
                    if n > 10000:
                        raise Unqualified("Sense path exceeds sampling budget")
                    for i in range(n + 1):
                        x = (p[0] + (q[0] - p[0]) * i / n, p[1] + (q[1] - p[1]) * i / n)
                        worst = max(worst, min(_segment_distance(x, u, v) for u, v in segments))
                        u, v = min(segments, key=lambda seg: _segment_distance(x, *seg))
                        if math.dist(p, q) > 1e-9:
                            dot = abs(
                                (q[0] - p[0]) * (v[0] - u[0]) + (q[1] - p[1]) * (v[1] - u[1])
                            ) / (math.dist(p, q) * math.dist(u, v))
                            angle = max(angle, math.degrees(math.acos(min(1, dot))))
                # .05 mm bounds any unsampled peak by the distance function's
                # Lipschitz bound; passing is conservative, never undersampled.
                return worst + 0.05, angle

            ab, ba = directed(a["points"], b["points"]), directed(b["points"], a["points"])
            return dict(
                gap_mm=max(ab[0], ba[0]),
                angle_deg=max(ab[1], ba[1]),
                skew_mm=abs(a["length_mm"] - b["length_mm"]),
                scope="bidirectional route proximity; endpoints included",
            )
        if kind == "reference_plane":
            row = dump.get("plane_coverage", {}).get(check["id"])
            if not row or row.get("error"):
                raise Unqualified(
                    (row or {}).get("error", "Native filled-plane coverage unavailable")
                )
            return row
        if kind == "via_array":
            points = [point(t) for t in check["terminals"]]
            for t in check["terminals"]:
                if terminal(t)[0]["net"] != check.get("net"):
                    raise ValueError("Via array terminal net mismatch")
            radius = check.get("radius_mm")
            if radius is None or not check.get("layers"):
                raise Unqualified("Via-array radius/layers not specified")
            layers = dump.get("copper_layers", [])
            if any(layer not in layers for layer in check["layers"]):
                raise Unqualified("Requested via-array layer absent")
            count = 0
            for v in dump.get("vias", []):
                if (
                    v["net"] != check["net"]
                    or min(math.dist(_point(v), q) for q in points) > radius
                ):
                    continue
                if v.get("top") not in layers or v.get("bottom") not in layers:
                    raise Unqualified("Via span unavailable")
                a, b = sorted([layers.index(v["top"]), layers.index(v["bottom"])])
                if all(a <= layers.index(layer) <= b for layer in check["layers"]):
                    count += 1
            return dict(
                count=count,
                scope="via proximity/span only; pad connection and thermal capacity require separate checks",
            )
        if kind == "evidence":
            e = evidence.get(check["id"])
            if not board_sha256 or not e or e.get("board_sha256") != board_sha256:
                raise Unqualified("Missing or stale exact-board evidence")
            if e.get("unit") != check["unit"] or e.get("metric") != check["metric"]:
                raise Unqualified("Evidence metric/unit mismatch")
            if not isinstance(e.get("corners", []), list) or not set(
                check.get("corners", [])
            ) <= set(e.get("corners", [])):
                raise Unqualified("Evidence does not cover required operating corners")
            artifact_hash = e.get("artifact_sha256")
            if (
                e.get("status") != "complete"
                or not isinstance(e.get("method"), str)
                or not e["method"].strip()
                or not isinstance(artifact_hash, str)
                or len(artifact_hash) != 64
                or any(c not in "0123456789abcdef" for c in artifact_hash)
            ):
                raise Unqualified("Incomplete evidence provenance")
            value = e.get("value")
            if type(value) not in (int, float) or not math.isfinite(value):
                raise Unqualified("Evidence has no finite measured value")
            return dict(
                value=value,
                unit=e["unit"],
                method=e["method"],
                artifact_sha256=e["artifact_sha256"],
            )
        raise Unqualified("Unsupported check")

    for check in checks:
        row = {k: check[k] for k in COMMON if k in check}
        try:
            metrics = measure(check)

            def finite(value):
                if isinstance(value, dict):
                    return all(finite(v) for v in value.values())
                if isinstance(value, (list, tuple)):
                    return all(finite(v) for v in value)
                return math.isfinite(value) if type(value) in (int, float) else True

            if not finite(metrics):
                raise Unqualified("Non-finite extracted measurement")
            row["metrics"] = metrics
            failures = []
            limits = 0
            for key, limit in check.items():
                if key.startswith(("min_", "max_")):
                    limits += 1
                    value = metrics.get(key[4:])
                    if value is None:
                        raise Unqualified("Metric unavailable: " + key[4:])
                    if value > limit + 1e-9 if key.startswith("max_") else value < limit - 1e-9:
                        failures.append(
                            dict(metric=key[4:], actual=value, limit=limit, relation=key[:3])
                        )
            if check["kind"] == "kelvin":
                limits = 1
                if not metrics["kelvin"]:
                    failures.append(dict(shared_power_terminals=metrics["shared_power_terminals"]))
            if not limits:
                raise Unqualified("No authored acceptance limit")
            row.update(status="fail" if failures else "pass", failures=failures)
        except Unqualified as error:
            row.update(status="unqualified", reason=str(error))
        except (ValueError, KeyError, TypeError, IndexError) as error:
            row.update(status="fail", reason=str(error))
        rows.append(row)
    failures = sum(r["status"] == "fail" for r in rows)
    unknown = sum(r["status"] == "unqualified" for r in rows)
    return dict(
        schema="pnr-smps-layout-v1",
        board_sha256=board_sha256,
        checks=rows,
        failure_count=failures,
        unqualified_count=unknown,
        status=(
            "not_applicable"
            if not rows
            else "fail" if failures else "unqualified" if unknown else "pass"
        ),
        qualified=False,
        scope="SMPS requirement screening; native DRC and electrical qualification remain separate",
    )


def native_audit(board, rules, board_text="", *, evidence=None):
    from pnr.hier.power_quality import dump_board

    digest = hashlib.sha256(board_text.encode()).hexdigest() if board_text else None
    dump = dump_board(board)
    dump["plane_coverage"] = {}
    import pcbnew as k

    for check in parse_checks(rules.get("smps_checks", [])):
        if check["kind"] != "reference_plane":
            continue
        try:
            pads = []
            for term in check["terminals"]:
                ref, name = term.rsplit(":", 1)
                hits = [p for p in dump["pads"] if p["ref"] == ref and p["name"] == name]
                if not hits:
                    raise ValueError("Missing terminal " + term)
                pads.extend(hits)
            margin = float(check.get("margin_mm", 0))
            x0 = min(p["x"] for p in pads) - margin
            x1 = max(p["x"] for p in pads) + margin
            y0 = min(p["y"] for p in pads) - margin
            y1 = max(p["y"] for p in pads) + margin
            if x0 >= x1 or y0 >= y1:
                raise ValueError("Plane reference region has no area")
            layer = board.GetLayerID(check["layer"])
            if layer not in board.GetEnabledLayers().CuStack():
                raise ValueError("Reference plane layer absent")
            region = k.SHAPE_POLY_SET()
            region.NewOutline()
            for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
                region.Append(k.VECTOR2I(k.FromMM(x), k.FromMM(y)))
            for zone in board.Zones():
                if (
                    not zone.GetIsRuleArea()
                    and zone.GetNetname() == check["net"]
                    and zone.IsOnLayer(layer)
                ):
                    region.BooleanSubtract(zone.GetFilledPolysList(layer))
            dump["plane_coverage"][check["id"]] = dict(
                uncovered_mm2=region.Area() / 1e12,
                region=[x0, y0, x1, y1],
                scope="exact filled-polygon coverage; no inductance/impedance claim",
            )
        except Exception as error:
            dump["plane_coverage"][check["id"]] = dict(error=str(error))
    return audit(dump, rules.get("smps_checks", []), board_sha256=digest, evidence=evidence)


def main():
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--evidence", type=Path, help="Exact-board measurement receipts keyed by check ID"
    )
    a = ap.parse_args()
    import pcbnew

    board = pcbnew.LoadBoard(str(a.board))
    board.BuildConnectivity()
    result = native_audit(
        board,
        json.loads(a.rules.read_text()),
        a.board.read_text(),
        evidence=json.loads(a.evidence.read_text()) if a.evidence else None,
    )
    a.out.write_text(json.dumps(result, indent=2) + "\n")
    raise SystemExit(0 if result["status"] in ("pass", "not_applicable") else 1)


if __name__ == "__main__":
    main()
