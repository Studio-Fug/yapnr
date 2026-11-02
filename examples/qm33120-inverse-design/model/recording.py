"""Role-specific exact DTFT recording; does not modify material or source physics."""

import hashlib

import numpy as np

SCHEMA = "qm33120-exact-recording/1"


def role(task):
    if task["mode"] == "adjoint":
        if "direction" in task:
            raise ValueError("FD adjoint role is undefined")
        return "adjoint"
    if task["mode"] != "forward":
        raise ValueError("Unknown solve mode")
    return "fd_objective" if "direction" in task else "gradient_forward"


def selected_probes(p, task):
    kind = role(task)
    all_probes = p.port_probes + p.box_probes + p.design_probes + p.edge_probes
    byname = {q.name: q for q in all_probes}
    if len(byname) != len(all_probes):
        raise ValueError("Duplicate probe names")
    if kind == "adjoint":
        return p.design_probes + p.edge_probes
    if kind == "gradient_forward":
        chosen = all_probes
    else:
        # Keep EVERY original convergence probe, not just hard-coded ground names.
        required = {q.name for q in p.port_probes + p.box_probes} | set(p.watched)
        if not required <= byname.keys():
            raise ValueError("Missing convergence probe")
        chosen = [q for q in all_probes if q.name in required]
    if not set(p.watched) <= {q.name for q in chosen}:
        raise ValueError("Missing convergence probe")
    return chosen


def contract(p, task):
    kind = role(task)
    probes = selected_probes(p, task)

    def fingerprint(q):
        a = np.asarray(q.index)
        return dict(
            name=q.name,
            component=q.comp,
            count=int(a.size),
            index_dtype=str(a.dtype),
            index_sha256=hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest(),
        )

    return dict(
        schema=SCHEMA,
        role=kind,
        decimation=1,
        convergence_probes=list(p.watched) if kind != "adjoint" else [q.name for q in probes],
        probes=[fingerprint(q) for q in probes],
    )


def forward(p, task):
    if task["mode"] != "forward":
        raise ValueError("Forward dispatch required")
    if p._decimation() != 1:
        raise ValueError("Exact sampling required")
    from yapnr.rf.fdtd.stop import StopRule

    stop = StopRule(
        tol=p.tol,
        f_lo=float(p.omega.min()) / (2 * np.pi),
        max_steps=p.spec.solver.max_steps,
        probes=tuple(p.watched),
    )
    return p.sim.run(
        p.port_sources(task["port"]), selected_probes(p, task), p.omega, stop, decimation=1
    )
