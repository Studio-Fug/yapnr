"""Unrepaired binary connectivity and native planar width/space export checks."""

import numpy as np


class BinaryGeometry:
    def __init__(self, problem, baseline):
        self.p = problem
        self.baseline = np.asarray(baseline, bool)
        i0, i1, j0, j1 = problem.domain.window
        xx, yy = np.meshgrid(
            problem.grid.x.centers[i0:i1] * 1e3, problem.grid.y.centers[j0:j1] * 1e3, indexing="ij"
        )
        self.anchors = []
        for port in problem.spec.ports:
            d = (xx - np.mean(port.x_mm)) ** 2 + (yy - np.mean(port.y_mm)) ** 2
            d = np.where(self.baseline, d, np.inf)
            self.anchors.append(tuple(np.unravel_index(np.argmin(d), d.shape)))
        if len(set(self.anchors)) != 4:
            raise ValueError("Four distinct frozen antenna anchors required")

    def __call__(self, rho, eta):
        from scipy.ndimage import label

        from yapnr.rf.export.drc import check_width_space
        from yapnr.rf.export.report import footprint_of

        p = self.p
        rho = np.asarray(rho)
        if not np.isin(rho, [0.0, 1.0]).all():
            raise ValueError("Binary geometry validator received gray copper")
        fixed_ok = not np.any(p.material.fixed & (rho != p.material.fixed_value))
        labels, count = label(rho > 0.5, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
        ids = [int(labels[a]) for a in self.anchors]
        connected = all(ids) and len(set(ids)) == 4 and count == 4
        _, polys = footprint_of(p, rho)
        drc = check_width_space(
            polys,
            p.spec.design_region,
            p.spec.grid.pitch_mm / p.refine,
            p.spec.rules.min_width_mm,
            p.spec.rules.min_space_mm,
        ).to_json()
        return dict(
            passed=bool(fixed_ok and connected and drc["ok"]),
            eta=eta,
            fixed_copper=fixed_ok,
            components=int(count),
            anchor_components=ids,
            planar_drc=drc,
            repair_applied=False,
            full_board_DRC=False,
        )
