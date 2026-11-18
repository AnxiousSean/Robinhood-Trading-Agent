"""Fee model.

Kalshi trading fee (per order, rounded up to the next cent):
    taker: ceil(0.07   * mult * C * P * (1-P))
    maker: ceil(0.0175 * mult * C * P * (1-P))  only on series whose fee_type has maker fees
Robinhood passes the exchange fee through and adds its own per-contract
commission (default $0.01). Check both schedules before going live; they change.
"""
from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def exchange_fee(price: float, count: float, taker: bool = True,
                 fee_type: str = "quadratic", mult: float = 1.0) -> float:
    if count <= 0:
        return 0.0
    if taker:
        rate = TAKER_RATE
    elif "maker" in (fee_type or ""):
        rate = MAKER_RATE
    else:
        return 0.0
    raw = rate * mult * count * price * (1.0 - price)
    return math.ceil(round(raw * 100, 6)) / 100.0


def total_fee(price: float, count: float, venue: str = "robinhood", taker: bool = True,
              fee_type: str = "quadratic", mult: float = 1.0,
              rh_commission: float = 0.01) -> float:
    fee = exchange_fee(price, count, taker, fee_type, mult)
    if venue == "robinhood":
        fee += rh_commission * count
    return round(fee, 4)


def fee_per_contract(price: float, venue: str = "robinhood", taker: bool = True,
                     fee_type: str = "quadratic", mult: float = 1.0,
                     rh_commission: float = 0.01) -> float:
    """Per-contract fee for a large order (rounding amortised away)."""
    rate = TAKER_RATE if taker else (MAKER_RATE if "maker" in (fee_type or "") else 0.0)
    fee = rate * mult * price * (1.0 - price)
    if venue == "robinhood":
        fee += rh_commission
    return fee
