from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


@dataclass
class Config:
    # execution
    mode: str = "paper"              # paper | robinhood (manual tickets) | kalshi (live API)
    fee_venue: str = "robinhood"     # whose fee schedule to price edges with: robinhood | kalshi
    rh_commission: float = 0.01      # Robinhood $/contract on top of exchange fee
    kalshi_env: str = "demo"         # demo | prod  (only for mode=kalshi)
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = ""

    # bankroll & sizing
    bankroll: float = 1000.0
    kelly_fraction: float = 0.25     # fractional Kelly; full Kelly is far too aggressive with model error
    max_stake_pct: float = 0.01      # per trade, of bankroll
    max_event_pct: float = 0.03      # per event (correlated outcomes)
    max_exposure_pct: float = 0.60   # total open cost
    daily_loss_limit_pct: float = 0.05
    max_trades_per_cycle: int = 25

    # signal filters
    # "calibration" (taking liquidity) lost money out-of-sample, so it is off by default. See RESULTS.md.
    strategies: list = field(default_factory=lambda: ["maker", "arb"])
    min_edge: float = 0.03           # min expected return per $ after fees
    min_neff: float = 30.0           # min effective markets behind the estimate
    min_price: float = 0.03
    max_price: float = 0.97
    min_hours: float = 0.25
    max_hours: float = 24 * 14
    min_ask_size: float = 5
    categories: list = field(default_factory=list)       # empty = use research-approved list
    blocked_categories: list = field(default_factory=list)
    arb_min_profit: float = 0.01     # $ per basket after fees

    # maker (resting limit orders)
    maker_min_edge: float = 0.0      # 0 = use the research-validated threshold
    maker_categories: list = field(default_factory=list)  # empty = research-approved list
    maker_max_contracts: float = 200
    maker_max_new_per_cycle: int = 20
    maker_ttl_min: float = 30        # cancel unfilled remainder after this long
    max_resting: int = 60

    # loop
    scan_interval_sec: int = 180

    @property
    def path(self) -> str:
        return os.path.join(DATA, "config.json")

    @classmethod
    def load(cls) -> "Config":
        p = os.path.join(DATA, "config.json")
        c = cls()
        if os.path.exists(p):
            with open(p) as fh:
                for k, v in json.load(fh).items():
                    if hasattr(c, k):
                        setattr(c, k, v)
        c.kalshi_key_id = os.environ.get("KALSHI_KEY_ID", c.kalshi_key_id)
        c.kalshi_private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", c.kalshi_private_key_path)
        return c

    def save(self) -> None:
        os.makedirs(DATA, exist_ok=True)
        d = asdict(self)
        if os.environ.get("KALSHI_KEY_ID"):
            d.pop("kalshi_key_id", None)   # keep env-provided credentials out of the file
        with open(self.path, "w") as fh:
            json.dump(d, fh, indent=2)
