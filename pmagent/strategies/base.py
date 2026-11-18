from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    strategy: str
    ticker: str
    event_ticker: str
    title: str
    category: str
    side: str            # yes | no
    price: float         # price per contract we'd pay
    q: float             # model probability the contract pays $1
    fee: float           # fee per contract
    ev: float            # expected return per $ risked, after fees
    neff: float = 0.0
    hours: float = 0.0
    max_count: float = 0.0   # liquidity available at that price
    basket: str = ""         # legs of an arb share a basket id
    fee_type: str = "quadratic"
    fee_mult: float = 1.0
    note: str = ""
    legs: list = field(default_factory=list)


class Strategy:
    name = "base"

    def scan(self, ctx) -> list[Signal]:
        raise NotImplementedError
