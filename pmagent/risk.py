"""Position sizing and hard risk limits. Every order passes through here."""
from __future__ import annotations

import math
import os
import time
from datetime import datetime

from .config import DATA, Config
from .db import DB

KILL_FILE = os.path.join(DATA, "KILL")


def kelly_fraction(q: float, cost: float) -> float:
    """Full-Kelly fraction of bankroll for a contract costing `cost` (incl. fee) that pays $1 w.p. q."""
    if cost <= 0 or cost >= 1:
        return 0.0
    return max(0.0, (q - cost) / (1 - cost))


class Risk:
    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db

    def equity(self) -> float:
        return self.cfg.bankroll + self.db.realized()

    def cash(self) -> float:
        return self.equity() - self.db.exposure()

    def day_start(self) -> float:
        d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def halted(self) -> str | None:
        if os.path.exists(KILL_FILE):
            return "kill switch engaged (data/KILL exists)"
        today = self.db.realized_since(self.day_start())
        if today <= -self.cfg.daily_loss_limit_pct * self.cfg.bankroll:
            return f"daily loss limit hit ({today:.2f})"
        return None

    def _room(self) -> float:
        eq = self.equity()
        return min(self.cfg.max_exposure_pct * eq - self.db.exposure(), self.cash())

    def size(self, sig) -> tuple[int, str]:
        """Contracts for a single directional signal (fractional Kelly, capped). 0 = rejected."""
        cfg = self.cfg
        if (h := self.halted()):
            return 0, h
        if self.db.holds(sig.ticker):
            return 0, "already positioned"
        cost = sig.price + sig.fee
        eq = self.equity()
        stake = min(cfg.kelly_fraction * kelly_fraction(sig.q, cost) * eq,
                    cfg.max_stake_pct * eq,
                    cfg.max_event_pct * eq - self.db.event_exposure(sig.event_ticker),
                    self._room())
        n = min(int(math.floor(stake / cost)) if stake > 0 else 0, int(sig.max_count))
        return (n, "ok") if n >= 1 else (0, "size < 1 contract after limits")

    def size_basket(self, legs: list) -> tuple[int, str]:
        """Equal contract count on every leg of an arbitrage basket."""
        if (h := self.halted()):
            return 0, h
        if any(self.db.holds(s.ticker) for s in legs):
            return 0, "already positioned in a leg"
        unit = sum(s.price + s.fee for s in legs)
        budget = min(self.cfg.max_event_pct * self.equity() * 3, self._room())   # locked profit -> 3x event cap
        n = min(int(budget // unit), int(min(s.max_count for s in legs)))
        return (n, "ok") if n >= 1 else (0, "basket size < 1 after limits")
