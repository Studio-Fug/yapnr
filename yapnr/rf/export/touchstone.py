"""Touchstone (version 1) S-parameter files (design §10.4).

`write_touchstone` writes `# GHz S RI R <z_ref>` with the engineering e^{+jωt} convention
(convert solver S-parameters with `sparams.to_engineering` first). Two-port files use the
Touchstone order S11 S21 S12 S22 on one line; files with three or more ports write each row of
the matrix starting on a new line, at most four pairs per line (the format's rule).
"""

from __future__ import annotations

import numpy as np


def _fmt(v: float) -> str:
    return f"{v: .9e}"


def write_touchstone(path: str, freqs_hz, s, *, z_ref: float = 50.0, comments=()) -> str:
    """Write (F, N, N) S-parameters at `freqs_hz`; returns the text."""
    f = np.asarray(freqs_hz, dtype=np.float64)
    s = np.asarray(s, dtype=np.complex128)
    if s.ndim != 3 or s.shape[1] != s.shape[2] or s.shape[0] != f.size:
        raise ValueError("s must be (F, N, N) with one matrix per frequency")
    n = s.shape[1]
    lines = [f"! {c}" for c in comments]
    lines.append(f"# GHz S RI R {z_ref:g}")
    for k in range(f.size):
        fk = f"{f[k] / 1e9:.9f}"
        if n <= 2:
            order = [(0, 0)] if n == 1 else [(0, 0), (1, 0), (0, 1), (1, 1)]
            vals = " ".join(f"{_fmt(s[k, i, j].real)} {_fmt(s[k, i, j].imag)}" for i, j in order)
            lines.append(f"{fk} {vals}")
            continue
        for i in range(n):
            pairs = [f"{_fmt(s[k, i, j].real)} {_fmt(s[k, i, j].imag)}" for j in range(n)]
            chunks = [pairs[c : c + 4] for c in range(0, n, 4)]
            for c, chunk in enumerate(chunks):
                lead = fk if (i == 0 and c == 0) else " " * len(fk)
                lines.append(f"{lead} {' '.join(chunk)}")
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def read_touchstone(path: str, n_ports: int | None = None):
    """(freqs_hz (F,), s (F, N, N), z_ref) from a Touchstone v1 file written in RI, MA or DB."""
    if n_ports is None:
        ext = path.rsplit(".", 1)[-1].lower()
        n_ports = int(ext[1:-1])
    unit, fmt, z_ref = 1e9, "RI", 50.0
    numbers: list[float] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("!", 1)[0].strip()
            if not line:
                continue
            if line.startswith("#"):
                tok = line[1:].upper().split()
                unit = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}.get(tok[0], 1e9)
                fmt = tok[2]
                if "R" in tok:
                    z_ref = float(tok[tok.index("R") + 1])
                continue
            numbers.extend(float(v) for v in line.split())
    per = 1 + 2 * n_ports * n_ports
    data = np.array(numbers).reshape(-1, per)
    freqs = data[:, 0] * unit
    a, b = data[:, 1::2], data[:, 2::2]
    if fmt == "RI":
        vals = a + 1j * b
    elif fmt == "MA":
        vals = a * np.exp(1j * np.radians(b))
    elif fmt == "DB":
        vals = 10 ** (a / 20) * np.exp(1j * np.radians(b))
    else:
        raise ValueError(f"unknown Touchstone format {fmt}")
    s = vals.reshape(-1, n_ports, n_ports)
    if n_ports == 2:
        s = np.swapaxes(s, 1, 2)  # S11 S21 S12 S22 is column-major
    return freqs, s, z_ref
