"""Edge metrics on simulated waveforms (pure Python, any float sequences).

All metrics are taken at the measurement node (the first pixel's DIN) within the
two stimulus windows: rising ``[t_rise, t_fall)`` and falling ``[t_fall, t_end]``.
Levels are the nominal rails 0 V and ``rail`` (not the settled values).

* ``rise_10_90_ns``: first up-crossing of 90 % minus the last up-crossing of 10 %
  before it; ``fall_10_90_ns`` mirrors it. ``None`` when the level is never reached.
* ``delay_ns``: receiver VIH (VIL on the fall) crossing minus the driver pin's 50 %
  crossing; the larger of the two edges.
* ``overshoot_v`` = max(v) - rail, ``undershoot_v`` = -min(v) over the whole run.
* ``nonmonotonic_edges``: edges (0-2) that cross VIL or VIH more than once (or never):
  the signal must pass the receiver's threshold band exactly once per edge.
* ``ringback_margin_v``: once the edge has arrived (first 90 % / 10 % crossing, else the
  first VIH / VIL crossing), how far the signal stays beyond VIH (VIL) until the
  window ends; negative = it came back into the band.
* ``pulse_width_error_ns``: receiver high time (VIH up to VIL down) minus the stimulus
  high time.
"""

from __future__ import annotations

import bisect
import math


def crossings(t, v, level, t0=None, t1=None):
    """[(time, +1|-1)] linear-interpolated crossings of ``level`` within [t0, t1]."""
    n = len(t)
    i0 = 1 if t0 is None else max(1, bisect.bisect_left(t, t0))
    i1 = n if t1 is None else min(n, bisect.bisect_right(t, t1) + 1)
    out = []
    for i in range(i0, i1):
        a, b = v[i - 1] - level, v[i] - level
        if a < 0 <= b or a >= 0 > b:
            tc = t[i - 1] + (t[i] - t[i - 1]) * (-a) / (b - a)
            if (t0 is None or tc >= t0) and (t1 is None or tc <= t1):
                out.append((tc, 1 if a < 0 else -1))
    return out


def value_at(t, v, x):
    """Linear interpolation of v at time x (clamped to the ends)."""
    i = bisect.bisect_left(t, x)
    if i <= 0:
        return v[0]
    if i >= len(t):
        return v[-1]
    if t[i] == t[i - 1]:
        return v[i]
    return v[i - 1] + (v[i] - v[i - 1]) * (x - t[i - 1]) / (t[i] - t[i - 1])


def _window(t, v, t0, t1):
    i0, i1 = bisect.bisect_left(t, t0), bisect.bisect_right(t, t1)
    return v[i0:i1]


def _first(cs, d):
    return next((x for x, s in cs if s == d), None)


def edge_metrics(t, rx, drv, *, rail, vih, vil, t_rise, t_fall, t_end):
    """Metrics dict for one run (times in s in, ns out)."""
    ns = 1e9
    m = {}
    lo10, hi90, mid = 0.1 * rail, 0.9 * rail, 0.5 * rail
    # 10-90 on each edge
    up90 = _first(crossings(t, rx, hi90, t_rise, t_fall), 1)
    if up90 is None:
        m["rise_10_90_ns"] = None
    else:
        ups10 = [x for x, s in crossings(t, rx, lo10, t_rise, up90) if s == 1]
        m["rise_10_90_ns"] = (up90 - ups10[-1]) * ns if ups10 else None
    dn10 = _first(crossings(t, rx, lo10, t_fall, t_end), -1)
    if dn10 is None:
        m["fall_10_90_ns"] = None
    else:
        dns90 = [x for x, s in crossings(t, rx, hi90, t_fall, dn10) if s == -1]
        m["fall_10_90_ns"] = (dn10 - dns90[-1]) * ns if dns90 else None
    # threshold band per edge
    edges = {}
    for name, (a, b, d) in dict(rise=(t_rise, t_fall, 1), fall=(t_fall, t_end, -1)).items():
        c_il, c_ih = crossings(t, rx, vil, a, b), crossings(t, rx, vih, a, b)
        edges[name] = dict(
            vil_crossings=len(c_il),
            vih_crossings=len(c_ih),
            monotonic=len(c_il) == 1 and len(c_ih) == 1,
        )
    m["band_crossings"] = edges
    m["nonmonotonic_edges"] = sum(not e["monotonic"] for e in edges.values())
    # delays
    d_r = _first(crossings(t, drv, mid, t_rise, t_fall), 1)
    d_f = _first(crossings(t, drv, mid, t_fall, t_end), -1)
    r_ih = _first(crossings(t, rx, vih, t_rise, t_fall), 1)
    r_il = _first(crossings(t, rx, vil, t_fall, t_end), -1)
    delays = [
        (r - d) * ns for r, d in ((r_ih, d_r), (r_il, d_f)) if r is not None and d is not None
    ]
    m["delay_rise_ns"] = (r_ih - d_r) * ns if r_ih is not None and d_r is not None else None
    m["delay_fall_ns"] = (r_il - d_f) * ns if r_il is not None and d_f is not None else None
    m["delay_ns"] = max(delays) if len(delays) == 2 else None
    # levels
    m["vmax_v"] = max(rx)
    m["vmin_v"] = min(rx)
    m["overshoot_v"] = m["vmax_v"] - rail
    m["undershoot_v"] = -m["vmin_v"]
    # ringback: from the edge's arrival (90 % / 10 % crossing, else the band crossing)
    margins = []
    if r_ih is not None:
        after = _window(t, rx, up90 if up90 is not None else r_ih, t_fall)
        margins.append((min(after) if len(after) else vih) - vih)
    if r_il is not None:
        after = _window(t, rx, dn10 if dn10 is not None else r_il, t_end)
        margins.append(vil - (max(after) if len(after) else vil))
    m["ringback_margin_v"] = min(margins) if len(margins) == 2 else None
    m["pulse_width_error_ns"] = (
        ((r_il - r_ih) - (t_fall - t_rise)) * ns if r_ih is not None and r_il is not None else None
    )
    m["v_end_high_v"] = value_at(t, rx, t_fall)
    m["v_end_low_v"] = value_at(t, rx, t_end)
    return m


def sanity(t, rx, drv, *, rail, t_rise, t_fall, t_end, tol=0.1):
    """Reason string when the run is garbage (driver pin never reaches its rail, flat
    receiver, empty windows); None when plausible. Garbage counts as an error."""
    if len(t) < 20 or t[-1] < t_fall:
        return "waveform too short"
    hi = value_at(t, drv, t_fall)
    lo = value_at(t, drv, t_end)
    if abs(hi - rail) > tol * rail:
        return "driver pin at %.3f V at the end of the rising window, rail %.3f V" % (hi, rail)
    if abs(lo) > tol * rail:
        return "driver pin at %.3f V at the end of the falling window" % lo
    if max(rx) - min(rx) < 0.2 * rail:
        return "flat receiver waveform (swing %.3f V)" % (max(rx) - min(rx))
    for x in (hi, lo):
        if not math.isfinite(x):
            return "non-finite driver level"
    return None


def judge(metrics, limits):
    """Apply profile limits. Returns dict(passed, gate_failures, report_violations, checks).

    ``limits``: {metric: {'max'|'min'|'eq': x, 'gate': bool}}. A gated metric that
    could not be measured (``None``) fails.
    """
    checks, gate, report = [], [], []
    for name, lim in sorted(limits.items()):
        val = metrics.get(name)
        ok, margin = True, None
        if val is None:
            ok = False
        else:
            if "max" in lim:
                margin = lim["max"] - val
                ok = ok and val <= lim["max"]
            if "min" in lim:
                mm = val - lim["min"]
                margin = mm if margin is None else min(margin, mm)
                ok = ok and val >= lim["min"]
            if "eq" in lim:
                ok = ok and val == lim["eq"]
        rec = dict(
            metric=name,
            value=val,
            limit={k: v for k, v in lim.items() if k in ("max", "min", "eq")},
            gate=bool(lim.get("gate", True)),
            ok=ok,
            margin=margin,
        )
        checks.append(rec)
        if not ok:
            (gate if rec["gate"] else report).append(rec)
    return dict(passed=not gate, gate_failures=gate, report_violations=report, checks=checks)


def decimate_wave(t, ys, max_points=2000):
    """Uniform-in-index thinning keeping extrema per bucket, for stored waveforms."""
    n = len(t)
    if n <= max_points:
        return list(t), {k: list(v) for k, v in ys.items()}
    step = n / float(max_points // 2)
    idx = set([0, n - 1])
    ref = next(iter(ys.values()))
    for b in range(max_points // 2):
        i0, i1 = int(b * step), min(n, int((b + 1) * step))
        if i1 <= i0:
            continue
        seg = range(i0, i1)
        idx.add(min(seg, key=lambda i: ref[i]))
        idx.add(max(seg, key=lambda i: ref[i]))
    order = sorted(idx)
    return [t[i] for i in order], {k: [v[i] for i in order] for k, v in ys.items()}
