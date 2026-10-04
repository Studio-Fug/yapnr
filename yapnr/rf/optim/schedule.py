"""β continuation and the end-of-epoch rule (design §7.3).

β runs through epochs (8, 16, 32, 64, 128 by default). An epoch ends at its iteration cap, or
once at least `min_iterations` are done and the epigraph value has settled,
|t_k − t_{k−5}| < t_tol (1 + |t_k|), with the design moving less than `drho_tol` (max |Δx|).
MMA's state resets at each β change (the driver), and the move limit drops from `move` to
`move_late` from β = `late_beta` on. The length-scale constraints are active in the last epoch.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BetaSchedule:
    betas: tuple[float, ...] = (8.0, 16.0, 32.0, 64.0, 128.0)
    iterations: tuple[int, ...] | int = 30
    min_iterations: int = 8
    lookback: int = 5
    t_tol: float = 1e-3
    drho_tol: float = 0.01
    move: float = 0.2
    move_late: float = 0.1
    late_beta: float = 32.0

    def __post_init__(self) -> None:
        if not self.betas:
            raise ValueError("the β schedule needs at least one epoch")
        if not isinstance(self.iterations, int) and len(self.iterations) != len(self.betas):
            raise ValueError("one iteration cap per β, or a single cap")

    @property
    def epochs(self) -> int:
        return len(self.betas)

    def cap(self, epoch: int) -> int:
        if isinstance(self.iterations, int):
            return self.iterations
        return int(self.iterations[epoch])

    def move_for(self, epoch: int) -> float:
        return self.move_late if self.betas[epoch] >= self.late_beta else self.move

    def is_last(self, epoch: int) -> bool:
        return epoch == len(self.betas) - 1

    def epoch_done(self, epoch: int, t_history: list[float], last_change: float) -> bool:
        """True when the epoch has used its cap, or has converged (see the module doc)."""
        n = len(t_history)
        if n >= self.cap(epoch):
            return True
        if n < max(self.min_iterations, self.lookback + 1):
            return False
        t, t_old = t_history[-1], t_history[-1 - self.lookback]
        return abs(t - t_old) < self.t_tol * (1.0 + abs(t)) and last_change < self.drho_tol

    def to_json(self) -> dict:
        its = self.iterations if isinstance(self.iterations, int) else list(self.iterations)
        return {
            "betas": list(self.betas),
            "iterations": its,
            "min_iterations": self.min_iterations,
            "lookback": self.lookback,
            "t_tol": self.t_tol,
            "drho_tol": self.drho_tol,
            "move": self.move,
            "move_late": self.move_late,
            "late_beta": self.late_beta,
        }
