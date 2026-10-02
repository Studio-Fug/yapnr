"""``yapnr fab check``: native KiCad DRC under the vendor's rules, plus what KiCad cannot express.

Design §7. The board is never touched: a scratch copy gets the profile's rules (the stricter of
the project's own board constraints and the profile's, and the profile's custom rules ahead of
any hand-written ones), then ``kicad-cli pcb drc --refill-zones --save-board --severity-all``
judges it. KiCad's DRC is the judge; no finding is relaxed or suppressed. The export then uses
that refilled copy, so the files are exactly what DRC judged.

:func:`evaluate` is pure (board facts, KiCad's stats and DRC report, the data in, findings out),
so every ``FAB-*`` code is tested on synthetic facts.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from yapnr.fab import board as board_mod
from yapnr.fab import capability, stackups
from yapnr.fab.profiles import Profile, fab_profile_module

SEVERITIES = ("error", "warning", "info")
RF_TOLERANCE = 0.01  # FAB-RF: assumed er and h within 1 % of the order's stackup
THICKNESS_TOLERANCE_MM = 0.05


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    source: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        if not out["data"]:
            del out["data"]
        if out["source"] is None:
            del out["source"]
        return out


@dataclass
class Options:
    qty: Optional[int] = None
    assembly: bool = False
    allow_draft: bool = False
    stackup_explicit: bool = False
    today: Optional[str] = None  # YYYY-MM-DD for FAB-SOURCES (tests); None = today


def errors(findings: Iterable[Finding]) -> List[Finding]:
    return [f for f in findings if f.severity == "error"]


def summary(findings: Sequence[Finding]) -> Dict[str, int]:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}


# ------------------------------------------------------------------------- the scratch copy


def stricter_rules(project: Dict[str, Any], profile_rules: Dict[str, float]) -> Dict[str, Any]:
    """``board.design_settings.rules``: every profile minimum raised to the project's when the
    project is stricter (all of these keys are minimums)."""
    rules = dict(project)
    for key, value in profile_rules.items():
        current = rules.get(key)
        rules[key] = value if not isinstance(current, (int, float)) else max(current, value)
    return rules


def merged_dru(profile_text: Optional[str], existing: Optional[str]) -> Optional[str]:
    """The profile's custom rules, then any hand-written rules (a generated file is replaced)."""
    fp = fab_profile_module()
    if existing is None or fp.DRU_HEADER in existing:
        return profile_text
    hand = "\n".join(
        line for line in existing.splitlines() if not line.strip().startswith("(version")
    ).strip()
    if profile_text is None:
        return existing
    return profile_text + "# Hand-written rules of the project, kept:\n" + hand + "\n"


def prepare(
    board: Path,
    profile: Profile,
    workdir: Path,
    name: str,
    no_project: bool = False,
) -> Dict[str, Any]:
    """Copy the board (and project) to ``workdir/<name>.kicad_pcb`` with the profile's rules.

    Returns what was done: the scratch board, the profile the board's own rules name, notes.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    scratch = workdir / f"{name}.kicad_pcb"
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
        stale = scratch.with_suffix(suffix)
        if stale.exists():
            stale.unlink()
    shutil.copyfile(board, scratch)
    notes: List[str] = []
    pro_src = board.with_suffix(".kicad_pro")
    if pro_src.is_file():
        pro = json.loads(pro_src.read_text(encoding="utf-8"))
    elif no_project:
        pro = {}
        notes.append("no .kicad_pro beside the board: KiCad's default netclasses were used")
    else:
        raise FileNotFoundError(
            f"{pro_src.name} not found beside the board: it carries the netclasses DRC needs "
            "(--no-project checks without it)"
        )
    design = pro.setdefault("board", {}).setdefault("design_settings", {})
    design["rules"] = stricter_rules(design.get("rules") or {}, profile.board_constraints())
    if "meta" not in pro:
        pro["meta"] = {"filename": scratch.with_suffix(".kicad_pro").name, "version": 3}
    else:
        pro["meta"]["filename"] = scratch.with_suffix(".kicad_pro").name
    scratch.with_suffix(".kicad_pro").write_text(json.dumps(pro, indent=2) + "\n")
    classes = (pro.get("net_settings") or {}).get("classes") or []
    clearances = {c["name"]: c.get("clearance") for c in classes if isinstance(c, dict)}
    dru_src = board.with_suffix(".kicad_dru")
    existing = dru_src.read_text(encoding="utf-8") if dru_src.is_file() else None
    routed_under = board_mod.rules_profile(existing)
    stroke = board_mod.read(board).outline.stroke_mm
    text = merged_dru(profile.dru_text(clearances, stroke), existing)
    if text is not None:
        scratch.with_suffix(".kicad_dru").write_text(text)
    return {"board": scratch, "routed_under": routed_under, "notes": notes}


# ------------------------------------------------------------------------------ evaluation


def _limit(profile: Profile, key: str) -> Dict[str, Any]:
    return profile.limits.get(key) or {}


def _drc_findings(drc: Dict[str, Any], profile: Profile) -> List[Finding]:
    out: List[Finding] = []
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for v in drc.get("violations") or []:
        severity = v.get("severity", "error")
        if v.get("excluded") or severity == "exclusion":
            severity = "info"
        severity = severity if severity in SEVERITIES else "error"
        groups.setdefault((severity, v.get("type", "?")), []).append(v)
    for (severity, kind), items in sorted(groups.items()):
        first = items[0]
        where = ", ".join(i.get("description", "") for i in (first.get("items") or [])[:2])
        out.append(
            Finding(
                "FAB-DRC",
                severity,
                f"KiCad DRC under {profile.name}: {len(items)} x {kind}: "
                f"{first.get('description', '')}" + (f" ({where})" if where else ""),
                f"profiles/{profile.name}.json",
                {"type": kind, "count": len(items)},
            )
        )
    unconnected = drc.get("unconnected_items") or []
    if unconnected:
        out.append(
            Finding(
                "FAB-DRC",
                "error",
                f"KiCad DRC: {len(unconnected)} unconnected items (the board is not fully routed)",
                None,
                {"type": "unconnected_items", "count": len(unconnected)},
            )
        )
    ignored = [c.get("key") for c in drc.get("ignored_checks") or []]
    if ignored:
        out.append(
            Finding(
                "FAB-DRC",
                "info",
                f"the project ignores {len(ignored)} KiCad checks: {', '.join(sorted(ignored))}",
                None,
                {"ignored": sorted(ignored)},
            )
        )
    return out


def _drill_holes(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    holes = []
    for h in stats.get("drill_holes") or []:
        x = board_mod.stats_value(h.get("x_size")) or 0.0
        y = board_mod.stats_value(h.get("y_size")) or 0.0
        holes.append(dict(h, x=x, y=y))
    return holes


def evaluate(
    board: board_mod.Board,
    stats: Dict[str, Any],
    drc: Dict[str, Any],
    profile: Profile,
    stackup: stackups.Stackup,
    vendor: Dict[str, Any],
    options: Options,
    extra: Sequence[Finding] = (),
    routed_under: Optional[str] = None,
) -> List[Finding]:
    """Every finding for one board under one profile and stackup (§7.2)."""
    out: List[Finding] = []
    src = f"profiles/{profile.name}.json"

    if profile.draft:
        out.append(
            Finding(
                "FAB-PROFILE-DRAFT",
                "warning" if options.allow_draft else "error",
                f"{profile.name} is a draft profile (published values not yet transcribed)"
                + ("" if options.allow_draft else "; --allow-draft to use it anyway"),
                src,
            )
        )
    if routed_under and routed_under != profile.name:
        out.append(
            Finding(
                "FAB-DRC",
                "info",
                f"the board's own rules are {routed_under}; it is judged under {profile.name}",
            )
        )
    out += _drc_findings(drc, profile)

    # Layers.
    n_stack = len(stackup.copper_layers())
    if board.layer_count != n_stack or board.layer_count != profile.copper_layers:
        out.append(
            Finding(
                "FAB-LAYERS",
                "error",
                f"the board has {board.layer_count} copper layers; {profile.name} and "
                f"{stackup.id} have {profile.copper_layers} and {n_stack}",
                src,
            )
        )

    # Outline and size.
    o = board.outline
    has_outline = (stats.get("board") or {}).get("has_outline", o.closed_loops > 0)
    outer = o.outer_loops()
    if not has_outline or not outer:
        out.append(Finding("FAB-OUTLINE", "error", "no closed board outline on Edge.Cuts"))
    if o.open_chains:
        out.append(Finding("FAB-OUTLINE", "error", f"{o.open_chains} open Edge.Cuts chains (gaps)"))
    if len(outer) > 1:
        panel = _limit(profile, "panel")
        note = (
            f"; the vendor's panel rules: {panel.get('outline_gap_mm')} mm between outlines, "
            f"frame at least {panel.get('frame_min_mm')} mm"
            if panel
            else ""
        )
        out.append(
            Finding(
                "FAB-OUTLINE",
                "warning",
                f"{len(outer)} separate board outlines: a panel{note}",
                panel.get("src"),
            )
        )
    size = o.size_mm
    if size:
        dims = sorted(size)
        lo = sorted(_limit(profile, "board_min_mm").get("value") or [0, 0])
        hi = sorted(_limit(profile, "board_max_mm").get("value") or [1e9, 1e9])
        if dims[0] < lo[0] - 1e-6 or dims[1] < lo[1] - 1e-6:
            out.append(
                Finding(
                    "FAB-SIZE",
                    "error",
                    f"board {size[0]:.2f} x {size[1]:.2f} mm is under the minimum {lo[0]} x {lo[1]} mm",
                    _limit(profile, "board_min_mm").get("src"),
                )
            )
        if dims[0] > hi[0] + 1e-6 or dims[1] > hi[1] + 1e-6:
            out.append(
                Finding(
                    "FAB-SIZE",
                    "error",
                    f"board {size[0]:.2f} x {size[1]:.2f} mm is over the maximum {hi[0]} x {hi[1]} mm",
                    _limit(profile, "board_max_mm").get("src"),
                )
            )

    # Vias.
    vias = stats.get("vias") or {}
    allowed = (_limit(profile, "via_types").get("value")) or ["through"]
    for kind in ("blind", "buried", "micro"):
        if int(vias.get(kind) or 0) and kind not in allowed:
            out.append(
                Finding(
                    "FAB-VIA-TYPE",
                    "error",
                    f"{vias[kind]} {kind} vias; {profile.name} offers {', '.join(allowed)} vias only",
                    _limit(profile, "via_types").get("src"),
                )
            )

    # Drills and slots.
    holes = _drill_holes(stats)
    max_drill = _limit(profile, "max_drilled_hole_mm")
    if holes and max_drill.get("value"):
        big = [h for h in holes if max(h["x"], h["y"]) > max_drill["value"] + 1e-6]
        if big:
            out.append(
                Finding(
                    "FAB-DRILL",
                    "warning",
                    f"{sum(h.get('count', 1) for h in big)} holes over {max_drill['value']} mm: "
                    f"{max_drill.get('note', 'check the vendor note')}",
                    max_drill.get("src"),
                )
            )
    slots = [
        h for h in holes if abs(h["x"] - h["y"]) > 1e-6 or h.get("shape") not in (None, "Round")
    ]
    if slots:
        min_slot = _limit(profile, "min_slot_mm")
        narrow = [h for h in slots if min(h["x"], h["y"]) < (min_slot.get("value") or 0) - 1e-6]
        sourced = capability.source_key(min_slot.get("src"), profile.doc["sources"]) not in (
            None,
            "derived",
        )
        out.append(
            Finding(
                "FAB-DRILL",
                "error" if narrow and sourced else "warning",
                f"{sum(h.get('count', 1) for h in slots)} slots"
                + (
                    f"; {len(narrow)} narrower than the {min_slot.get('value')} mm minimum slot"
                    if narrow
                    else "; check them in the vendor's drill preview"
                ),
                min_slot.get("src"),
            )
        )
    drill_spec = vendor.get("drill") or {}
    npth = [h for h in holes if h.get("plated") is False]
    if npth and drill_spec.get("separate_th") is False:
        out.append(
            Finding(
                "FAB-DRILL",
                "warning",
                f"{sum(h.get('count', 1) for h in npth)} non-plated holes share the one merged "
                f"drill file; {vendor['title']} does not document how it plates them: check them "
                "in its drill preview",
                drill_spec.get("src"),
            )
        )

    # Castellation.
    castellated = board.castellated()
    n_cast = len(castellated) or int((stats.get("pads") or {}).get("castellated") or 0)
    if n_cast:
        rule = _limit(profile, "castellation")
        if rule.get("value") == "not_guaranteed":
            out.append(
                Finding(
                    "FAB-CASTELLATED",
                    "warning",
                    f"{n_cast} castellated pads: {rule.get('note', 'not guaranteed')}",
                    rule.get("src"),
                )
            )
        else:
            small = [
                (ref, p)
                for ref, p in castellated
                if rule.get("min_hole_mm") and min(p.drill) < rule["min_hole_mm"] - 1e-6
            ]
            out.append(
                Finding(
                    "FAB-CASTELLATED",
                    "error" if small else "warning",
                    f"{n_cast} castellated pads: choose the castellated-holes option"
                    + (
                        f"; {len(small)} holes under {rule['min_hole_mm']} mm "
                        f"({', '.join(sorted({r for r, _ in small}))})"
                        if small
                        else ""
                    ),
                    rule.get("src"),
                )
            )

    # Stackup.
    if abs(board.thickness_mm - stackup.thickness_mm) > THICKNESS_TOLERANCE_MM:
        out.append(
            Finding(
                "FAB-STACKUP",
                "warning",
                f"the board says {board.thickness_mm} mm thick; {stackup.id} is "
                f"{stackup.thickness_mm} mm",
                f"stackups/{stackup.id}.json",
            )
        )
    board_diel = [layer for layer in board.stackup if layer.get("type") in ("core", "prepreg")]
    order_diel = [layer for layer in stackup.layers if layer["kind"] == "dielectric"]
    if board_diel and len(board_diel) == len(order_diel):
        for i, (a, b) in enumerate(zip(board_diel, order_diel)):
            for key_a, key_b, what in (
                ("epsilon_r", "er", "Dk"),
                ("thickness", "thickness_mm", "h"),
            ):
                va, vb = a.get(key_a), b.get(key_b)
                if va and vb and abs(va - vb) / vb > 0.02:
                    out.append(
                        Finding(
                            "FAB-STACKUP",
                            "warning",
                            f"dielectric {i + 1}: the board's KiCad stackup has {what} {va:g}, "
                            f"{stackup.id} {vb:g}",
                            f"stackups/{stackup.id}.json",
                        )
                    )
    elif board_diel:
        out.append(
            Finding(
                "FAB-STACKUP",
                "info",
                f"the board's KiCad stackup has {len(board_diel)} dielectrics, {stackup.id} "
                f"{len(order_diel)}; not compared",
            )
        )

    # RF structures.
    rf = board.rf_footprints()
    impedance_items = [f"{fp.reference} ({fp.rf['name']})" for fp in rf]
    impedance_items += [
        f"netclass {c.get('name')}"
        for c in board.netclasses
        if any("impedance" in str(k).lower() and c.get(k) for k in c)
    ]
    if rf and not options.stackup_explicit:
        out.append(
            Finding(
                "FAB-RF",
                "error",
                f"{len(rf)} RF footprints: name the stackup with --stackup "
                f"({', '.join(profile.allowed_stackups)})",
            )
        )
    for fp in rf:
        assumed = fp.rf
        try:
            m = stackup.microstrip(fp.layer if fp.layer in ("F.Cu", "B.Cu") else "F.Cu")
        except stackups.StackupError as err:
            out.append(Finding("FAB-RF", "error", f"{fp.reference}: {err}"))
            continue
        problems = []
        if m.er is None:
            problems.append(f"{stackup.id} publishes no Dk under {m.layer}")
        elif abs(assumed["er"] - m.er) / m.er > RF_TOLERANCE:
            problems.append(f"er {assumed['er']:g} vs {m.er:g}")
        if abs(assumed["h_mm"] - m.h_mm) / m.h_mm > RF_TOLERANCE:
            problems.append(f"h {assumed['h_mm']:g} mm vs {m.h_mm:g} mm")
        if problems:
            out.append(
                Finding(
                    "FAB-RF",
                    "error",
                    f"{fp.reference} ('{assumed['name']}') was designed for er {assumed['er']:g} on "
                    f"{assumed['h_mm']:g} mm; {stackup.id}: " + "; ".join(problems),
                    f"stackups/{stackup.id}.json",
                    {"spec_sha256": assumed["spec_sha256"]},
                )
            )
    if impedance_items:
        control = _limit(profile, "impedance_control")
        if control.get("value"):
            out.append(
                Finding(
                    "FAB-IMPEDANCE",
                    "info",
                    f"impedance-relevant items ({', '.join(impedance_items)}): order impedance "
                    f"control on {stackup.vendor_name} (+-{control.get('tolerance_pct')} %)",
                    control.get("src"),
                )
            )
        else:
            out.append(
                Finding(
                    "FAB-IMPEDANCE",
                    "warning",
                    f"impedance-relevant items ({', '.join(impedance_items)}) on a service without "
                    "impedance control: nominal stackup only",
                    control.get("src"),
                )
            )
        for alt in profile.limits.get("alternates") or []:
            out.append(
                Finding(
                    "FAB-ALTERNATE",
                    "info",
                    f"{vendor['title']} offers {alt['stackup']} {alt.get('offered', '')}; this "
                    f"order is {stackup.id}. At checkout: {stackup.checkout}",
                    alt.get("src"),
                )
            )

    # Quantity.
    qty_rule = _limit(profile, "qty")
    qty = options.qty if options.qty is not None else profile.defaults.get("qty")
    if qty is not None and qty_rule:
        if qty < qty_rule.get("min", 1) or qty % qty_rule.get("multiple", 1):
            out.append(
                Finding(
                    "FAB-QTY",
                    "error",
                    f"quantity {qty}: {vendor['title']} takes at least {qty_rule.get('min')}"
                    + (
                        f", in multiples of {qty_rule['multiple']}"
                        if qty_rule.get("multiple", 1) > 1
                        else ""
                    ),
                    qty_rule.get("src"),
                )
            )

    # Order number marking (assembly vendors).
    marking = _limit(profile, "order_number_marking")
    if marking.get("value"):
        option = (
            f"'{marking['option']}' ({marking['choices']})"
            if marking.get("option") and marking.get("choices")
            else "the marking option"
        )
        out.append(
            Finding(
                "FAB-MARKING",
                "info",
                f"{vendor['title']} can print its order number on the board: choose {option} on "
                "the quote page",
                marking.get("src"),
            )
        )

    # Data age.
    import datetime

    today = datetime.date.fromisoformat(options.today) if options.today else None
    stale = capability.stale_sources([profile.doc, stackup.doc, vendor], today)
    if stale:
        out.append(
            Finding(
                "FAB-SOURCES",
                "warning",
                f"vendor data older than {capability.STALE_DAYS} days: {', '.join(stale)}; "
                "re-verify before relying on it (yapnr fab show --sources)",
            )
        )
    out += list(extra)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(out, key=lambda f: (order[f.severity], f.code))
