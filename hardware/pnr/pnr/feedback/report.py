"""Mechanical progress report of feedback runs (read-only).

    python -m pnr.feedback.report <synth_native --out dir | halving --out dir> ...

Blocks (``*/library.json`` + ``trials.jsonl``): per template the evaluations,
complete layouts and complete-per-evaluation, best and top-4 median missing
(unconnected + violations); the rebase of stale-code imports (import -> this
code); per round the parents, per-arm child - parent missing, and PULL against
its matched RAND control as a sign test. Halving (``status.json`` +
``dataset.jsonl``): the rebase, per generation the parents, children, native
opens per arm, child - parent opens, the parent repeat delta, the pool best,
and the same sign test. Then the evaluation code keys, load and free disk.

The sign test pairs each RAND child with the one PULL child it was matched to
(the same parent's first PULL child: same displacement). Comparing the best of
several PULL children with one RAND child would favour PULL even when both arms
are equally good. With n decisive pairs no p below 2^(1-n) is reachable; the
report prints n and that bound.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import statistics
import sys
from pathlib import Path


def sign_test(wins, losses):
    """Two-sided exact sign test p-value (ties dropped)."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n)


def _missing(r):
    o = r.get("objective")
    return (o[5] + o[0]) if o else None


def _jsonl(path):
    out = []
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def pull_vs_rand(children, value, key_of):
    """(wins, losses, ties): each RAND child against the PULL child it was matched to.

    ``key_of(record)`` is the record's tag (blocks) or id (halving); a RAND
    record's ``matched`` names its PULL sibling. Records written before
    ``matched`` existed pair with the same parent's k=0 PULL child of the same
    generation. Pairs with a missing value are dropped."""
    by_key = {key_of(c): c for c in children}
    first = {}
    for c in children:
        if c.get("arm") == "pull" and c.get("k", _k_from_tag(c)) == 0:
            first.setdefault((_parent(c), c.get("gen")), c)
    w = l = t = 0
    for c in children:
        if c.get("arm") != "rand":
            continue
        m = (
            by_key.get(c.get("matched"))
            if c.get("matched")
            else first.get((_parent(c), c.get("gen")))
        )
        if m is None:
            continue
        a, b = value(m), value(c)
        if a is None or b is None:
            continue
        w, l, t = w + (a < b), l + (a > b), t + (a == b)
    return w, l, t


def _k_from_tag(c):
    m = re.search(r"-g\d+c(\d+)-", str(c.get("tag") or ""))
    return int(m.group(1)) if m else 0


def _parent(c):
    p = c.get("parent")
    return p.get("tag") if isinstance(p, dict) else p


def sign_line(w, l, t):
    """One report line for the matched PULL-vs-RAND sign test (n, p and the reachable bound)."""
    n = w + l
    if n == 0:
        return "  PULL vs matched RAND: %d tied, no decisive pair yet" % t
    bound = 2.0 ** (1 - n)
    return (
        "  PULL vs matched RAND: %d better, %d worse, %d tied; n=%d, sign test p=%.3g "
        "(smallest p reachable at n=%d: %.3g%s)"
        % (
            w,
            l,
            t,
            n,
            sign_test(w, l),
            n,
            bound,
            "; cannot reach p<0.05 yet" if bound > 0.05 else "",
        )
    )


def _codes(recs):
    """The evaluation code keys of ``recs``; a key of an older scheme names its scheme."""
    from pnr.feedback.signals import CODE_KEY_SCHEME, code_key_scheme

    codes = sorted({(r["code"], code_key_scheme(r)) for r in recs if r.get("code")}, key=str)
    if not codes:
        return ""
    return "evaluation code %s%s" % (
        ", ".join(c if s == CODE_KEY_SCHEME else "%s (key scheme %s)" % (c, s) for c, s in codes),
        " (MIXED: code changed during the run)" if len(codes) > 1 else "",
    )


def _load_range(recs):
    loads = [
        r["loadavg_start"][0]
        for r in recs
        if isinstance(r.get("loadavg_start"), list) and r["loadavg_start"]
    ]
    return (
        "load at start %.1f-%.1f (median %.1f)" % (min(loads), max(loads), statistics.median(loads))
        if loads
        else ""
    )


def blocks_report(root):
    lines, pooled, n_tpl = [], [0, 0, 0], 0
    for lib_path in sorted(Path(root).glob("*/library.json")):
        lib = json.loads(lib_path.read_text())
        ranked = lib.get("ranked") or []
        counts = lib.get("counts") or {}
        miss = [_missing(r) for r in ranked]
        top = [m for m in miss if m is not None][:4]
        n_eval = counts.get("evaluated", 0)
        lines.append(
            "%s %s: evaluated %d, complete %d (%.0f%% of evaluations), best missing %s, top-4 median %s"
            % (
                lib_path.parent.name,
                ",".join(lib.get("blocks") or []),
                n_eval,
                counts.get("complete", 0),
                100.0 * counts.get("complete", 0) / n_eval if n_eval else 0.0,
                top[0] if top else "-",
                statistics.median(top) if top else "-",
            )
        )
        native = [r for r in _jsonl(lib_path.parent / "trials.jsonl") if r.get("stage") == "native"]
        trials = [r for r in native if r.get("gen")]
        if lib.get("stale_imports"):
            lines.append("  stale-code imports (not ranked): %d" % lib["stale_imports"])
        rb = lib.get("rebase")
        if rb:
            lines.append(
                "  rebase (import -> this code, missing): %d of %d evaluated, complete %d -> %d, deltas %s"
                % (
                    rb.get("evaluated", 0),
                    rb.get("planned", 0),
                    rb.get("complete_import", 0),
                    rb.get("complete_rebase", 0),
                    rb.get("delta"),
                )
            )
        for g in lib.get("generations") or []:
            arms = ", ".join(
                "%s n=%d delta=%s" % (arm, e["n"], e["delta"])
                for arm, e in sorted((g.get("arms") or {}).items())
            )
            lines.append(
                "  round %d: parents %d, children %s%s; %s; best after %s %s"
                % (
                    g["gen"],
                    len(g.get("parents") or []),
                    g.get("children"),
                    (" STOP " + g["stop"]) if g.get("stop") else "",
                    arms or "-",
                    g.get("best_after", "-"),
                    g.get("best_objective_after", ""),
                )
            )
        from pnr.feedback.blocks import tag_of

        w, l, t = pull_vs_rand(trials, _missing, tag_of)
        if w + l + t:
            lines.append(sign_line(w, l, t))
            pooled = [pooled[0] + w, pooled[1] + l, pooled[2] + t]
            n_tpl += 1
        own = [r for r in native if not r.get("imported")]
        extra = ", ".join(x for x in (_codes(own), _load_range(own)) if x)
        if extra:
            lines.append("  " + extra)
    if n_tpl > 1:
        lines.append("all templates:" + sign_line(*pooled)[1:])
    return lines


def halving_report(root):
    root = Path(root)
    status = json.loads((root / "status.json").read_text())
    data = _jsonl(root / "dataset.jsonl")
    native = {r["id"]: r for r in data if r.get("stage") == "native"}
    lines = ["%s: stages %s" % (root.name, ", ".join(sorted(status.get("stages") or {})))]
    rb = (status.get("stages") or {}).get("rebase")
    if rb:
        lines.append(
            "  rebase (seed import -> this code, opens): %s; complete %d -> %d"
            % (rb.get("pairs"), rb.get("complete_import", 0), rb.get("complete_rebase", 0))
        )
    for name in sorted(k for k in (status.get("stages") or {}) if k.startswith("gen")):
        g = status["stages"][name]
        arms = ", ".join(
            "%s opens=%s delta=%s" % (arm, e["opens"], e["delta"])
            for arm, e in sorted((g.get("arms") or {}).items())
        )
        lines.append(
            "  %s: parents %s, children %s, entry %s%s; %s; repeat delta %s; best %s %s"
            % (
                name,
                g.get("parents"),
                g.get("children"),
                g.get("entry"),
                (" STOP " + g["stop"]) if g.get("stop") else "",
                arms or "-",
                g.get("repeat_delta"),
                g.get("best_after", g.get("best_before")),
                g.get("best_objective_after", g.get("best_objective_before")),
            )
        )
    kids = [r for r in native.values() if r.get("gen")]
    w, l, t = pull_vs_rand(
        kids, lambda r: (r.get("objective") or [None] * 6)[5], lambda c: c.get("id")
    )
    if w + l + t:
        lines.append(sign_line(w, l, t))
    own = [r for r in native.values() if not r.get("imported_from")]
    extra = ", ".join(x for x in (_codes(own), _load_range(own)) if x)
    if extra:
        lines.append("  " + extra)
    deep = status.get("stages", {}).get("deep")
    if deep:
        lines.append("  deep: best %s %s" % (deep.get("best"), deep.get("best_objective")))
    return lines


def main(argv=None):
    paths = argv if argv is not None else sys.argv[1:]
    lines = []
    for p in paths:
        p = Path(p)
        if (p / "status.json").exists():
            lines += halving_report(p)
        else:
            lines += blocks_report(p)
    try:
        load = os.getloadavg()
        lines.append("load %.2f %.2f %.2f" % load)
    except OSError:
        pass
    if paths:
        lines.append("free disk %.1f GiB" % (shutil.disk_usage(paths[0]).free / 2**30))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
