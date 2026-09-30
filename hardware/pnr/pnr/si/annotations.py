"""``@pnr-si`` bindings and ``@pnr-si-waiver`` lines: parse and resolve (pure Python).

One physical ``.ato`` comment line each (atopile ignores ``#`` comments; the engine
and viewer parsers are line-based)::

    # @pnr-si {"name":"led0_data","profile":"ws2812b_din","driver":"led0.shifter:4","series":"led0.term","connector":"led0.conn:2","return":"led0.conn:3"}
    # @pnr-si-waiver {"name":"led0_data","reason":"...","metrics":["rise_10_90_ns"],"cable_m":[3]}

``driver``/``connector``/``return`` are ``target:pin`` with ``target`` an instance
address suffix (``led0.shifter`` matches ``board.led0.shifter``), resolved like
``@pnr-current`` targets: exactly one component, never a designator. ``series`` is one
target or a list, walked in order from the driver net to the connector net; each
must be a 2-pad part with one pad on the current net. Every other pad on the chain
nets becomes a shunt load. The profile id names ``si_models/profile-*.json``; all
external models (cable, connector, receiver) are JSON there, never in ``.ato``.

A waiver names a requirement and a reason; optional ``metrics``, ``corners``,
``cable_m`` and ``cable_z0_ohm`` (the cable impedance corners) narrow it. It only excuses *design* failures at compile time; it never
turns an error into a pass and never hides a layout failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

PREFIX = "# @pnr-si "
WAIVER_PREFIX = "# @pnr-si-waiver "
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PIN_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]+:[A-Za-z0-9_]+$")
REQ_KEYS = {
    "name",
    "profile",
    "driver",
    "series",
    "connector",
    "return",
    "measure_at",
    "justification",
}
WAIVER_KEYS = {"name", "reason", "metrics", "corners", "cable_m", "cable_z0_ohm"}


class AnnotationError(ValueError):
    """A malformed or unresolvable SI annotation (fails rules compilation)."""


def _where(src):
    return "%s:%s" % (src["path"], src["line"])


def parse(paths):
    """Read every ``@pnr-si`` / ``@pnr-si-waiver`` line. Returns (requirements, waivers)."""
    reqs, waivers = [], []
    for path in paths:
        path = Path(path)
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        for line, text in enumerate(raw.decode().splitlines(), 1):
            s = text.strip()
            if s.startswith(PREFIX):
                kind, body = "req", s[len(PREFIX) :]
            elif s.startswith(WAIVER_PREFIX):
                kind, body = "waiver", s[len(WAIVER_PREFIX) :]
            else:
                continue
            src = dict(path=str(path), line=line, sha256=sha)
            try:
                a = json.loads(body)
            except json.JSONDecodeError as error:
                raise AnnotationError(
                    "%s: invalid JSON in SI annotation: %s" % (_where(src), error)
                ) from None
            if not isinstance(a, dict):
                raise AnnotationError("%s: SI annotation must be a JSON object" % _where(src))
            (reqs if kind == "req" else waivers).append(_validate(a, kind, src))
    names = [r["name"] for r in reqs]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise AnnotationError("duplicate @pnr-si names: %s" % dup)
    for w in waivers:
        if w["name"] not in names:
            raise AnnotationError(
                "%s: waiver for unknown requirement %r" % (_where(w["source"]), w["name"])
            )
    return reqs, waivers


def _validate(a, kind, src):
    allowed = REQ_KEYS if kind == "req" else WAIVER_KEYS
    extra = sorted(set(a) - allowed)
    if extra:
        raise AnnotationError("%s: unknown keys %s" % (_where(src), extra))
    name = a.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise AnnotationError("%s: name required ([A-Za-z0-9_.-]+)" % _where(src))
    out = dict(a, source=src)
    if kind == "waiver":
        if not isinstance(a.get("reason"), str) or len(a["reason"].strip()) < 10:
            raise AnnotationError("%s: waiver needs a reason (>= 10 characters)" % _where(src))
        for key in ("metrics", "corners", "cable_m", "cable_z0_ohm"):
            if key in a and (not isinstance(a[key], list) or not a[key]):
                raise AnnotationError("%s: waiver %s must be a non-empty list" % (_where(src), key))
        return out
    if not isinstance(a.get("profile"), str) or not a["profile"]:
        raise AnnotationError("%s: profile required" % _where(src))
    for key in ("driver", "connector") + (("return",) if "return" in a else ()):
        if not isinstance(a.get(key), str) or not PIN_RE.match(a[key]):
            raise AnnotationError('%s: %s must be "target:pin"' % (_where(src), key))
    series = a.get("series")
    series = [series] if isinstance(series, str) else series
    if not series or not all(isinstance(s, str) and s and ":" not in s for s in series):
        raise AnnotationError(
            "%s: series must be a target or a non-empty list of targets" % _where(src)
        )
    out["series"] = list(series)
    if a.get("measure_at", "receiver") != "receiver":
        raise AnnotationError(
            '%s: measure_at other than "receiver" is not supported in SI v1' % _where(src)
        )
    return out


def _match(components, target):
    target = target.strip(".")
    return [
        c
        for c in components
        if (c.address or "").removesuffix("._p") == target
        or (c.address or "").removesuffix("._p").endswith("." + target)
    ]


def _part(c):
    return (c.footprint or "").split(":", 1)[0]


def resolve(reqs, waivers, components, library, env=None):
    """Resolve parsed requirements against graph components; returns ``si_intents``.

    Under ``PNR_SUBBOARD=1`` a requirement with any target outside the block is
    skipped (the LED chains are top-level). Anything else unresolvable raises
    :class:`AnnotationError` (fail closed).
    """
    env = os.environ if env is None else env
    subboard = env.get("PNR_SUBBOARD") == "1"
    out = []
    for r in reqs:
        where = _where(r["source"])
        try:
            profile = library.get("profile", r["profile"])
        except Exception as error:
            raise AnnotationError("%s: %s" % (where, error)) from None
        targets = [r["driver"].rsplit(":", 1)[0], r["connector"].rsplit(":", 1)[0]] + list(
            r["series"]
        )
        if "return" in r:
            targets.append(r["return"].rsplit(":", 1)[0])
        found = {t: _match(components, t) for t in targets}
        if subboard and any(not v for v in found.values()):
            continue
        for t, cs in found.items():
            if len(cs) != 1:
                raise AnnotationError(
                    "%s: %s SI target %r" % (where, "ambiguous" if cs else "missing", t)
                )

        def pin(spec, role):
            t, num = spec.rsplit(":", 1)
            c = found[t][0]
            pads = [p for p in c.pads if p.name == num]
            if not pads:
                raise AnnotationError("%s: %s %s has no pad %s" % (where, role, t, num))
            if len({p.net for p in pads}) != 1 or not pads[0].net:
                raise AnnotationError(
                    "%s: %s pad %s:%s unconnected or on several nets" % (where, role, t, num)
                )
            return dict(
                target=t, address=c.address, ref=c.ref, pad=num, net=pads[0].net, part=_part(c)
            )

        driver = pin(r["driver"], "driver")
        dpart = library.part(driver["part"])
        if not dpart or dpart.get("kind") != "driver":
            raise AnnotationError(
                "%s: driver part %s has no SI driver model (si_models/parts.json)"
                % (where, driver["part"])
            )
        manifest = library.get("driver", dpart["model"])
        if driver["pad"] not in manifest["pins"]:
            raise AnnotationError(
                "%s: driver model %s has no pin %s" % (where, dpart["model"], driver["pad"])
            )
        driver["model"] = dpart["model"]
        connector = pin(r["connector"], "connector")
        cpart = library.part(connector["part"])
        connector["model"] = (
            (cpart or {}).get("model") if (cpart or {}).get("kind") == "connector" else None
        )
        ret = pin(r["return"], "return") if "return" in r else None
        chain, net, used = (
            [],
            driver["net"],
            {(driver["ref"], driver["pad"]), (connector["ref"], connector["pad"])},
        )
        nets = [net]
        for t in r["series"]:
            c = found[t][0]
            if len(c.pads) != 2:
                raise AnnotationError("%s: series part %s must have 2 pads" % (where, t))
            a, b = c.pads
            if a.net == net and b.net != net:
                pin_in, pin_out = a, b
            elif b.net == net and a.net != net:
                pin_in, pin_out = b, a
            else:
                raise AnnotationError("%s: series part %s is not on net %s" % (where, t, net))
            info = library.part(_part(c))
            if not info or info.get("kind") not in ("R", "L", "FB", "0R"):
                raise AnnotationError(
                    "%s: series part %s (%s) has no value in si_models/parts.json"
                    % (where, t, _part(c))
                )
            chain.append(
                dict(
                    target=t,
                    address=c.address,
                    ref=c.ref,
                    part=_part(c),
                    kind=info["kind"],
                    ohm=float(info.get("ohm", 0.0)),
                    esl_nh=float(info.get("esl_nh", 0.0)),
                    value_source=info.get("source", ""),
                    derived=bool(info.get("derived")),
                    in_pad=pin_in.name,
                    out_pad=pin_out.name,
                    in_net=pin_in.net,
                    out_net=pin_out.net,
                )
            )
            used |= {(c.ref, pin_in.name), (c.ref, pin_out.name)}
            net = pin_out.net
            nets.append(net)
        if net != connector["net"]:
            raise AnnotationError(
                "%s: chain ends on net %s, connector pad is on %s" % (where, net, connector["net"])
            )
        shunts = []
        for c in components:
            for p in c.pads:
                if p.net in nets and (c.ref, p.name) not in used:
                    shunts.append(
                        dict(ref=c.ref, pad=p.name, net=p.net, part=_part(c), address=c.address)
                    )
        warnings = []
        for s in shunts:
            if not library.part(s["part"]):
                warnings.append(
                    "shunt pad %s.%s (%s) has no SI model: default %.2f pF"
                    % (s["ref"], s["pad"], s["part"], library.default_pad_pf())
                )
        if connector["model"] is None:
            warnings.append(
                "connector part %s not in si_models/parts.json: profile connector model used"
                % connector["part"]
            )
        intent = dict(
            name=r["name"],
            profile=r["profile"],
            source=r["source"],
            driver=driver,
            series=chain,
            connector=connector,
            nets=nets,
            shunts=sorted(shunts, key=lambda s: (s["ref"], s["pad"])),
            waivers=[w for w in waivers if w["name"] == r["name"]],
            warnings=warnings,
            justification=r.get("justification", profile.get("justification", "")),
        )
        intent["return"] = ret
        out.append(intent)
    return out


def waived(intent, failure):
    """The waiver of ``intent`` that excuses one failing deck, or None.

    ``failure``: dict(corner, cable_m, cable_z0_ohm, metrics=[failed gated metric names]).
    A waiver with ``cable_z0_ohm`` covers only those impedance corners (a 0 m case has
    no cable and is never narrowed out by it).
    """
    for w in intent.get("waivers", []):
        if "corners" in w and failure["corner"] not in w["corners"]:
            continue
        if "cable_m" in w and not any(
            abs(float(x) - float(failure["cable_m"])) < 1e-9 for x in w["cable_m"]
        ):
            continue
        z0 = failure.get("cable_z0_ohm")
        if (
            "cable_z0_ohm" in w
            and float(failure["cable_m"]) > 0
            and z0 is not None
            and not any(abs(float(x) - float(z0)) < 1e-9 for x in w["cable_z0_ohm"])
        ):
            continue
        if "metrics" in w and not set(failure["metrics"]) <= set(w["metrics"]):
            continue
        return w
    return None
