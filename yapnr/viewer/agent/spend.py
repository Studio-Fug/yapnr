"""The server's spend cap for paid ``claude`` CLI calls (Ask turns and AI net labels).

Every call has a budget, passed to the CLI as ``--max-budget-usd``. Before a call starts it
reserves that whole budget; when it ends it is charged the cost the CLI reported, or its whole
budget when the CLI reported none (a turn stopped on timeout, on cancel, by the unsafe-tool check,
or a CLI that died). A call starts only while the cap still covers its budget on top of what was
spent and what the running calls hold, so concurrent calls cannot pass the cap together.

The CLI enforces the per-call budget itself; this meter trusts its reports. It lives in memory, so
a restart resets it.
"""

from __future__ import annotations

import threading
from typing import Optional

# Float slack when a budget exactly fills the cap.
EPS = 1e-9


class SpendMeter:
    """Spend of one server process against an optional cap (None or 0: no cap)."""

    def __init__(self, cap_usd: Optional[float] = None):
        self.cap: Optional[float] = float(cap_usd) if cap_usd else None
        self.spent = 0.0
        self.held = 0.0
        self.calls = 0
        self.lock = threading.Lock()

    def reserve(self, budget: float) -> bool:
        """Hold ``budget`` for a call that is about to start; False when the cap does not cover it.

        Every True must be followed by exactly one settle(budget, cost)."""
        budget = float(budget)
        with self.lock:
            if self.cap is not None and self.spent + self.held + budget > self.cap + EPS:
                return False
            self.held += budget
            return True

    def settle(self, budget: float, cost: Optional[float]) -> float:
        """Release a reservation and charge the call: its reported cost, or ``budget`` when the
        cost is unknown (None or not a number). Returns the amount charged."""
        budget = float(budget)
        try:
            charged = budget if cost is None else max(0.0, float(cost))
        except (TypeError, ValueError):
            charged = budget
        if charged != charged:  # NaN
            charged = budget
        with self.lock:
            self.held = max(0.0, self.held - budget)
            self.spent += charged
            self.calls += 1
        return charged

    def exhausted(self, budget: float) -> bool:
        """No call with this budget can start again in this process, even once the running calls
        end (a restart resets the meter)."""
        with self.lock:
            return self.cap is not None and self.spent + float(budget) > self.cap + EPS

    def snapshot(self) -> dict:
        with self.lock:
            return dict(
                spent_usd=round(self.spent, 4),
                held_usd=round(self.held, 4),
                cap_usd=self.cap,
                calls=self.calls,
            )
