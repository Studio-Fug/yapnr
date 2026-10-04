"""Palace configuration files from a planar model and its mesh record.

``driven`` writes a frequency-domain S-parameter run (uniform or adaptive sweep, optional
adaptive mesh refinement); ``boundary_mode`` the 2D mode solve on one wave-port face of the same
3D mesh (``Solver.BoundaryMode.Attributes``), which gives a line's propagation constant and Z_PV.
The mapping, attribute by attribute (``mesh.json``):

- ``air``, ``diel:<name>``: ``Domains.Materials`` (Permittivity, LossTan);
- ``cond:<layer>:<net>``: ``Boundaries.PEC`` for a ``pec`` layer, else ``Boundaries.Conductivity``
  with sigma / rough_k^2 and, for a ``sheet``, the copper thickness (Palace's finite-thickness
  surface impedance; the sheet is internal, so ``External`` is false); ``solid`` copper is the
  outer surface of the removed copper (``External``, thick-conductor impedance);
- ``via:<net>``: PEC;
- ``wall:<face>``: per the document's boundaries (``abc1``/``abc2`` -> ``Absorbing``, ``pec``,
  ``pmc``); walls that touch wave ports are also ``WavePortPEC``, so each port face is a shielded
  guide in its 2D mode solve;
- ``port:<name>``: ``WavePort`` (``Offset`` back to the reference plane, ``VoltagePath`` from the
  strip to its reference for polarity and Z_PV) or ``LumpedPort`` (``R`` = z0, ``Direction`` from
  the strip down to its reference).

Lengths are mm (``Model.L0`` 1e-3), frequencies GHz. ``validate`` checks the result against the
vendored Palace schema (``schema.py``).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional, Sequence

from yapnr.rf.palace import schema
from yapnr.rf.planar import model

L0 = 1.0e-3  # mesh units: mm

LINEAR_DEFAULT = dict(Type="Default", KSPType="GMRES", Tol=1.0e-6, MaxIts=400)


def _groups(rec: Dict[str, Any], prefix: str) -> Dict[str, int]:
    return {
        k[len(prefix) :]: int(v["tag"]) for k, v in rec["groups"].items() if k.startswith(prefix)
    }


def _materials(doc: Dict[str, Any], rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    vols = _groups(rec, "diel:")
    for d in doc["stack"]["dielectrics"]:
        if d["name"] in vols:
            out.append(
                dict(
                    Attributes=[vols[d["name"]]],
                    Permeability=1.0,
                    Permittivity=float(d["eps_r"]),
                    LossTan=float(d.get("tan_d", 0.0)),
                )
            )
    if "air" in rec["groups"]:
        out.insert(
            0,
            dict(
                Attributes=[int(rec["groups"]["air"]["tag"])],
                Permeability=1.0,
                Permittivity=1.0,
                LossTan=0.0,
            ),
        )
    return out


def _boundaries(
    doc: Dict[str, Any],
    rec: Dict[str, Any],
    excite: Optional[Iterable[str]],
    absorbing_order: Optional[int],
) -> Dict[str, Any]:
    layers = {la["name"]: la for la in doc["stack"]["layers"]}
    pec: List[int] = []
    pmc: List[int] = []
    absorbing: List[int] = []
    order = 1
    cond: List[Dict[str, Any]] = []
    for key, tag in sorted(_groups(rec, "cond:").items(), key=lambda kv: kv[1]):
        lay = layers[key.split(":", 1)[0]]
        if lay["model"] == "pec":
            pec.append(tag)
        elif lay["model"] == "sheet":
            cond.append(
                dict(
                    Attributes=[tag],
                    Conductivity=model.sigma_eff(lay),
                    Thickness=float(lay["t"]),
                    External=False,
                )
            )
        else:
            cond.append(dict(Attributes=[tag], Conductivity=model.sigma_eff(lay), External=True))
    pec += sorted(_groups(rec, "via:").values())
    walls = _groups(rec, "wall:")
    kinds = doc["domain"]["boundaries"]
    for face, tag in sorted(walls.items(), key=lambda kv: kv[1]):
        k = kinds[face]
        if k == "pec":
            pec.append(tag)
        elif k == "pmc":
            pmc.append(tag)
        else:
            absorbing.append(tag)
            order = max(order, 2 if k == "abc2" else 1)
    if absorbing_order is not None:
        order = absorbing_order
    out: Dict[str, Any] = {}
    if pec:
        out["PEC"] = dict(Attributes=sorted(pec))
    if pmc:
        out["PMC"] = dict(Attributes=sorted(pmc))
    if absorbing:
        out["Absorbing"] = dict(Attributes=sorted(absorbing), Order=order)
    if cond:
        out["Conductivity"] = cond
    ports = model.port_geometry(doc)
    port_tags = _groups(rec, "port:")
    want = None if excite is None else set(excite)
    wave, lumped, port_walls = [], [], set()
    for index, p in enumerate(doc.get("ports", []), start=1):
        g = ports[p["name"]]
        on = p.get("excite", False) if want is None else p["name"] in want
        entry: Dict[str, Any] = dict(Index=index, Attributes=[port_tags[p["name"]]])
        if p["kind"] == "wave":
            entry.update(
                Mode=1,
                Offset=round(float(g["offset"]), 9),
                Excitation=index if on else 0,
                VoltagePath=g["voltage_path"],
            )
            wave.append(entry)
            port_walls.add(g["wall"])
        else:
            entry.update(
                R=float(p.get("z0", 50.0)),
                Direction="-Z" if g["z_sig"] > g["z_ref"] else "+Z",
                Excitation=index if on else 0,
            )
            lumped.append(entry)
    if wave:
        out["WavePort"] = wave
        guard = sorted(walls[w] for w in walls if w in port_walls and kinds[w] != "pec")
        if guard:
            out["WavePortPEC"] = dict(Attributes=guard)
    if lumped:
        out["LumpedPort"] = lumped
    return out


def port_index(doc: Dict[str, Any]) -> Dict[str, int]:
    """Palace's port Index of each port: its position in ``doc['ports']``, from 1."""
    return {p["name"]: i for i, p in enumerate(doc.get("ports", []), start=1)}


def driven(
    doc: Dict[str, Any],
    rec: Dict[str, Any],
    f_min: float,
    f_max: float,
    f_step: float,
    *,
    adaptive_tol: Optional[float] = None,
    excite: Optional[Iterable[str]] = None,
    order: int = 2,
    amr: Optional[Dict[str, Any]] = None,
    amr_freqs: Optional[Sequence[float]] = None,
    save: Optional[Sequence[float]] = None,
    mesh_file: Optional[str] = None,
    output: str = "postpro",
    absorbing_order: Optional[int] = None,
    linear: Optional[Dict[str, Any]] = None,
    verbose: int = 2,
) -> Dict[str, Any]:
    """A Driven configuration: S-parameters from ``f_min`` to ``f_max`` (GHz) every ``f_step``.

    ``adaptive_tol`` turns on Palace's adaptive fast sweep. ``amr`` (``Refinement`` keys: Tol,
    MaxIts, MaxSize, UpdateFraction ...) turns on adaptive mesh refinement; since Palace sums the
    error estimate over every solved frequency, ``amr_freqs`` (GHz) replaces the sweep with those
    points for the refinement run (then sweep the saved mesh in a second run).
    """
    if not f_max > f_min > 0 or f_step <= 0:
        raise ValueError("need 0 < f_min < f_max and f_step > 0")
    refinement: Dict[str, Any] = {}
    if amr:
        refinement = dict(
            Tol=1e-2, MaxIts=6, UpdateFraction=0.7, Nonconformal=True, SaveAdaptMesh=True
        )
        refinement.update(amr)
    if amr and amr_freqs:
        samples = [dict(Type="Point", Freq=[float(f) for f in amr_freqs], SaveStep=0)]
    else:
        samples = [
            dict(
                Type="Linear",
                MinFreq=float(f_min),
                MaxFreq=float(f_max),
                FreqStep=float(f_step),
                SaveStep=0,
            )
        ]
    drv: Dict[str, Any] = dict(Samples=samples)
    if adaptive_tol:
        drv["AdaptiveTol"] = float(adaptive_tol)
    if save:
        drv["Save"] = [float(f) for f in save]
    cfg = dict(
        Problem=dict(Type="Driven", Verbose=int(verbose), Output=output),
        Model=dict(Mesh=mesh_file or rec.get("file", "mesh.msh"), L0=L0, Refinement=refinement),
        Domains=dict(Materials=_materials(doc, rec)),
        Boundaries=_boundaries(doc, rec, excite, absorbing_order),
        Solver=dict(
            Order=int(order),
            Device="CPU",
            Driven=drv,
            Linear=dict(LINEAR_DEFAULT, **(linear or {})),
        ),
    )
    if not any(
        p.get("Excitation")
        for kind in ("WavePort", "LumpedPort")
        for p in cfg["Boundaries"].get(kind, [])
    ):
        raise ValueError("no port is excited")
    return cfg


def boundary_mode(
    doc: Dict[str, Any],
    rec: Dict[str, Any],
    port: str,
    freq: float,
    *,
    n: int = 1,
    order: int = 2,
    mesh_file: Optional[str] = None,
    output: str = "postpro",
    verbose: int = 2,
) -> Dict[str, Any]:
    """The 2D mode solve on wave port ``port``'s face of the 3D mesh at ``freq`` GHz: effective
    index, propagation constant and Z_PV along the port's voltage path."""
    tags = _groups(rec, "port:")
    if port not in tags:
        raise KeyError(f"no port face {port!r} in the mesh")
    g = model.port_geometry(doc)[port]
    if g["kind"] != "wave":
        raise ValueError(f"port {port} is not a wave port")
    bnd = _boundaries(doc, rec, [], None)
    keep = {k: bnd[k] for k in ("PEC", "PMC", "Conductivity", "Absorbing") if k in bnd}
    keep["PEC"] = dict(
        Attributes=sorted(
            set(keep.get("PEC", {}).get("Attributes", []))
            | set(bnd.get("WavePortPEC", {}).get("Attributes", []))
        )
    )
    keep["Postprocessing"] = dict(
        Impedance=[dict(Index=1, VoltagePath=g["voltage_path"], NSamples=200)]
    )
    return dict(
        Problem=dict(Type="BoundaryMode", Verbose=int(verbose), Output=output),
        Model=dict(Mesh=mesh_file or rec.get("file", "mesh.msh"), L0=L0, Refinement=dict(MaxIts=0)),
        Domains=dict(Materials=_materials(doc, rec)),
        Boundaries=keep,
        Solver=dict(
            Order=int(order),
            Device="CPU",
            BoundaryMode=dict(Freq=float(freq), N=int(n), Save=int(n), Attributes=[tags[port]]),
            Linear=dict(Tol=1e-9),
        ),
    )


def validate(cfg: Dict[str, Any]) -> Optional[List[str]]:
    """Findings against the vendored Palace schema (None: no schema in this installation)."""
    return schema.validate(copy.deepcopy(cfg))


def check(cfg: Dict[str, Any]) -> Dict[str, Any]:
    errs = validate(cfg)
    if errs:
        raise ValueError("Palace configuration does not match the schema:\n  " + "\n  ".join(errs))
    return cfg
