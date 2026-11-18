"""Basket arbitrage on mutually exclusive events.

NO basket: buy NO on every outcome of an event in which at most one outcome
can win. Payout >= N-1 dollars no matter what happens (N if none wins), so if
total cost + fees < N-1 the profit is locked in at entry. This holds even if
the outcome list is not exhaustive, which makes it the safe variant.

YES basket: buy YES on every outcome; pays exactly $1 only if the outcomes are
exhaustive. Restricted to numeric strike ladders (a 'less' + 'between'* +
'greater' chain) where exhaustiveness can be checked.

Main risk is leg risk: books move between legs. Every leg is sized to the
thinnest book and sent immediate-or-cancel.
"""
from __future__ import annotations

import uuid
from collections import defaultdict

from ..fees import fee_per_contract
from ..kalshi import f
from .base import Signal, Strategy


def _ladder_complete(ms: list[dict], integer_outcomes: bool = False) -> bool:
    types = sorted(m.get("strike_type") for m in ms)
    if "less" not in types or "greater" not in types:
        return False
    if any(t not in ("less", "greater", "between") for t in types):
        return False
    lo = [f(m.get("cap_strike")) for m in ms if m.get("strike_type") == "less"]
    hi = [f(m.get("floor_strike")) for m in ms if m.get("strike_type") == "greater"]
    mids = sorted((f(m.get("floor_strike")), f(m.get("cap_strike"))) for m in ms if m.get("strike_type") == "between")
    edges = [lo[0]] + [x for a, b in mids for x in (a, b)] + [hi[0]]
    # Each bracket must start where the previous ends: a gap of up to 1 unit is
    # only safe for integer-valued outcomes (whole-degree temperatures), else <= 1 cent.
    integer = integer_outcomes and all(abs(x - round(x)) < 1e-9 for x in edges)
    max_gap = 1.0001 if integer else 0.0101
    for i in range(1, len(edges), 2):
        gap = edges[i] - edges[i - 1]
        if gap < 0 or gap > max_gap:
            return False
    return len(lo) == 1 and len(hi) == 1


class BasketArb(Strategy):
    name = "arb"

    def scan(self, ctx) -> list[Signal]:
        cfg = ctx.cfg
        by_event = defaultdict(list)
        for m in ctx.markets:
            by_event[m["event_ticker"]].append(m)
        out = []
        for ev, ms in by_event.items():
            if len(ms) < 2:
                continue
            for side in ("no", "yes"):
                legs = []
                for m in ms:
                    p = f(m.get(f"{side}_ask_dollars"))
                    size = f(m.get("yes_ask_size_fp")) if side == "yes" else f(m.get("yes_bid_size_fp"))
                    if p <= 0 or p >= 1 or size <= 0:
                        legs = []
                        break
                    ft, mult = ctx.fee_info(m)
                    legs.append((m, p, size, fee_per_contract(p, cfg.fee_venue, True, ft, mult, cfg.rh_commission), ft, mult))
                if not legs:
                    continue
                cost = sum(p + fe for _, p, _, fe, _, _ in legs)
                payout = (len(legs) - 1) if side == "no" else 1.0
                profit = payout - cost
                if profit < cfg.arb_min_profit:
                    continue
                # cheap pre-check passed; now confirm structure from the event endpoint
                if not ctx.is_mutually_exclusive(ev):
                    continue
                if side == "yes" and not _ladder_complete(ms, ctx.category(ms[0]) == "Climate and Weather"):
                    continue
                if ctx.event_market_count(ev) != len(ms):
                    continue   # some outcomes not tradable/listed right now
                bid = uuid.uuid4().hex[:8]
                depth = min(s for _, _, s, _, _, _ in legs)
                for m, p, size, fe, ft, mult in legs:
                    out.append(Signal(self.name, m["ticker"], ev, m.get("title", ""), ctx.category(m), side, p,
                                      q=1.0, fee=fe, ev=profit / cost, neff=1e9, hours=ctx.hours_to_close(m),
                                      max_count=depth, basket=bid, fee_type=ft, fee_mult=mult,
                                      note=f"{side.upper()} basket {len(legs)} legs, cost {cost:.3f} -> pays {payout:.0f}"))
        return out
