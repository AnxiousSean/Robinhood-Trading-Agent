"""Maker strategy: rest limit orders where the *maker* side is historically
profitable, and let takers come to you.

On Kalshi, takers lose on average and makers earn the spread plus the
favorite-longshot premium. The research shows this most strongly when bidding
for favorites (50-94c). For each market and side we consider bidding one cent
above the current best bid (or joining it if the spread is 1 cent), then ask
the maker calibration model: given that a maker bid at this price got filled,
how often did it win? Fills are adverse-selected (you get filled most when the
price is about to move against you), and the model is fit on *filled* maker
prints, so that adverse selection is already in the estimate.
"""
from __future__ import annotations

from ..fees import fee_per_contract
from ..kalshi import f
from .base import Signal, Strategy


class MakerQuotes(Strategy):
    name = "maker"

    def __init__(self, model, approved: dict | None = None):
        self.model = model
        self.approved = approved or {}

    def scan(self, ctx) -> list[Signal]:
        cfg = ctx.cfg
        cats = set(cfg.maker_categories or self.approved.get("categories") or [])
        min_edge = cfg.maker_min_edge or self.approved.get("min_edge", 0.05)
        out = []
        for m in ctx.markets:
            cat = ctx.category(m)
            if (cats and cat not in cats) or cat in set(cfg.blocked_categories):
                continue
            hours = ctx.hours_to_close(m)
            if not (cfg.min_hours <= hours <= cfg.max_hours):
                continue
            ft, mult = ctx.fee_info(m)
            yb, ya = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars"))
            if ya <= 0 or ya >= 1:
                continue
            books = {
                # side: (best bid for that side, best ask for that side, size resting at the bid)
                "yes": (yb, ya, f(m.get("yes_bid_size_fp"))),
                "no": (round(1 - ya, 2), round(1 - yb, 2) if yb > 0 else 1.0, f(m.get("yes_ask_size_fp"))),
            }
            for side, (bid, ask, bid_size) in books.items():
                if bid <= 0:
                    continue
                improve = ask - bid > 0.011
                price = round(bid + 0.01, 2) if improve else bid
                if not (cfg.min_price <= price <= cfg.max_price):
                    continue
                q, neff = self.model.predict("maker", side, price, cat, hours)
                fee = fee_per_contract(price, cfg.fee_venue, False, ft, mult, cfg.rh_commission)
                ev = (q - price - fee) / (price + fee)
                if ev >= min_edge and neff >= cfg.min_neff:
                    out.append(Signal(self.name, m["ticker"], m["event_ticker"], m.get("title", ""), cat, side,
                                      price, q, fee, ev, neff, hours,
                                      max_count=cfg.maker_max_contracts,
                                      fee_type=ft, fee_mult=mult,
                                      note=f"LIMIT bid {price:.2f} ({'improve' if improve else 'join'}; "
                                           f"{0 if improve else bid_size:.0f} ahead)",
                                      legs=[{"queue_ahead": 0.0 if improve else bid_size, "maker": True}]))
        out.sort(key=lambda s: -s.ev)
        return out[: cfg.maker_max_new_per_cycle]
