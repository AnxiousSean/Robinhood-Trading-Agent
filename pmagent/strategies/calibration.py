"""Calibration edge: buy contracts whose price is systematically below their
realised win rate (favorite-longshot bias and its category/horizon variants),
using the walk-forward-validated CalibrationModel."""
from __future__ import annotations

from ..fees import fee_per_contract
from ..kalshi import f
from .base import Signal, Strategy


class CalibrationEdge(Strategy):
    name = "calibration"

    def __init__(self, model, approved: dict | None = None):
        self.model = model
        # approved = {"categories": [...]} from the research report; empty -> all
        self.approved = approved or {}

    def scan(self, ctx) -> list[Signal]:
        cfg = ctx.cfg
        cats = set(cfg.categories or self.approved.get("categories") or [])
        blocked = set(cfg.blocked_categories)
        out = []
        for m in ctx.markets:
            cat = ctx.category(m)
            if (cats and cat not in cats) or cat in blocked:
                continue
            hours = ctx.hours_to_close(m)
            if not (cfg.min_hours <= hours <= cfg.max_hours):
                continue
            fee_type, mult = ctx.fee_info(m)
            for side in ("yes", "no"):
                price = f(m.get(f"{side}_ask_dollars"))
                size = f(m.get(f"{side}_ask_size_fp")) if side == "yes" else f(m.get("no_ask_size_fp"), 0)
                if side == "no" and not size:
                    # Kalshi quotes size on the YES side; NO ask size = YES bid size
                    size = f(m.get("yes_bid_size_fp"))
                if not (cfg.min_price <= price <= cfg.max_price) or size < cfg.min_ask_size:
                    continue
                q, neff = self.model.predict("taker", side, price, cat, hours)
                fee = fee_per_contract(price, cfg.fee_venue, True, fee_type, mult, cfg.rh_commission)
                ev = (q - price - fee) / (price + fee)
                if ev >= cfg.min_edge and neff >= cfg.min_neff:
                    out.append(Signal(self.name, m["ticker"], m["event_ticker"], m.get("title", ""), cat, side,
                                      price, q, fee, ev, neff, hours, size, fee_type=fee_type, fee_mult=mult,
                                      note=f"model q={q:.3f} vs price {price:.2f}"))
        out.sort(key=lambda s: -s.ev)
        return out
