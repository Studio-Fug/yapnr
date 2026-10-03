"""Measurement sessions (design §7.6): file names, the manifest, loading.

Layout::

    <session>/
      session.yaml          # yapnr-coupon-session/1 (session.json is accepted too)
      A-03_A05-P-L40_r1.s2p # <board>-<serial>_<stick>-<family>-<what>_r<repeat>
      dc.csv                # stick, layer, width_mm, current_a, voltage_v, temp_c
      microsection.json     # optional readings {"pp1.h": [value, sigma], ...}
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from yapnr.rf.coupons import SCHEMA_SESSION, touchstone

NAME = re.compile(
    r"^(?P<board>[A-Z])-(?P<serial>[0-9A-Za-z]+)_(?P<stick>[A-Z][0-9]{2})-(?P<family>[A-Za-z0-9.]+)"
    r"-(?P<what>[A-Za-z0-9.]+)_r(?P<repeat>[0-9]+)\.s(?P<ports>[12])p$"
)


def file_name(board: str, serial: str, stick: str, family: str, what: str, repeat: int) -> str:
    fam = family.replace("-", "")
    return f"{board}-{serial}_{stick}-{fam}-{what}_r{repeat}.s2p"


def parse_name(name: str) -> Optional[dict]:
    m = NAME.match(os.path.basename(name))
    if not m:
        return None
    d = m.groupdict()
    d["repeat"] = int(d["repeat"])
    return d


@dataclass
class Measurement:
    stick: str
    serial: str
    repeat: int
    f: np.ndarray
    s: np.ndarray
    path: str


@dataclass
class Session:
    path: str
    manifest: dict
    meas: Dict[str, List[Measurement]] = field(default_factory=dict)  # stick -> repeats
    dc: List[dict] = field(default_factory=list)
    microsection: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    unparsed: List[str] = field(default_factory=list)

    def first(self, stick: str) -> Measurement:
        return sorted(self.meas[stick], key=lambda m: m.repeat)[0]

    def mean(self, stick: str) -> np.ndarray:
        return np.mean([m.s for m in self.meas[stick]], axis=0)


def _load_manifest(path: str) -> dict:
    for name in ("session.yaml", "session.yml", "session.json"):
        p = os.path.join(path, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                if name.endswith(".json"):
                    doc = json.load(f)
                else:
                    import yaml

                    doc = yaml.safe_load(f)
            if doc.get("schema") != SCHEMA_SESSION:
                raise ValueError(f"{p}: schema must be {SCHEMA_SESSION}")
            return doc
    raise FileNotFoundError(f"{path}: no session.yaml or session.json")


def load(path: str, serial: Optional[str] = None) -> Session:
    """Load a session directory; `serial` restricts it to one board."""
    man = _load_manifest(path)
    ses = Session(path, man)
    for name in sorted(os.listdir(path)):
        if not name.lower().endswith((".s2p", ".s1p")):
            continue
        d = parse_name(name)
        if d is None:
            ses.unparsed.append(name)
            continue
        if serial and d["serial"] != serial:
            continue
        f, s, ref = touchstone.read(os.path.join(path, name))
        if abs(ref - 50.0) > 1e-9:
            raise ValueError(f"{name}: reference {ref} Ω; the tier-1 data must be 50 Ω")
        ses.meas.setdefault(d["stick"], []).append(
            Measurement(d["stick"], d["serial"], d["repeat"], f, s, os.path.join(path, name))
        )
    dc = os.path.join(path, "dc.csv")
    if os.path.exists(dc):
        with open(dc, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if serial and row.get("serial") and row["serial"] != serial:
                    continue
                ses.dc.append(
                    dict(
                        stick=row["stick"],
                        layer=int(row["layer"]),
                        width_mm=float(row["width_mm"]),
                        current_a=float(row["current_a"]),
                        voltage_v=float(row["voltage_v"]),
                        temp_c=float(row.get("temp_c") or 20.0),
                    )
                )
    ms = os.path.join(path, "microsection.json")
    if os.path.exists(ms):
        with open(ms, encoding="utf-8") as f:
            ses.microsection = {k: tuple(v) for k, v in json.load(f).items()}
    return ses


def check(ses: Session, board) -> List[str]:
    """Problems: catalogue sticks without data, unparsed names, grids that differ."""
    out = [f"unparsed file name: {n}" for n in ses.unparsed]
    for s in board.sticks:
        if s.generated and s.ports == 2 and s.id not in ses.meas:
            out.append(f"no measurement of {s.id} ({s.kind} {s.family})")
    grids = {(len(m.f), float(m.f[0]), float(m.f[-1])) for ms in ses.meas.values() for m in ms}
    if len(grids) > 1:
        out.append(f"frequency grids differ: {sorted(grids)}")
    return out


def write_manifest(path: str, doc: dict) -> None:
    doc = dict(doc)
    doc["schema"] = SCHEMA_SESSION
    try:
        import yaml

        with open(os.path.join(path, "session.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False)
    except ImportError:  # pragma: no cover
        with open(os.path.join(path, "session.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)


def write_dc(path: str, rows: List[dict]) -> None:
    with open(os.path.join(path, "dc.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["serial", "stick", "layer", "width_mm", "current_a", "voltage_v", "temp_c"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)
