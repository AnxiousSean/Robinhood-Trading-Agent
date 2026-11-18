import os
import tempfile

import pandas as pd
import pytest

from pmagent.config import Config
from pmagent.db import DB
from pmagent.edge import CalibrationModel, pbucket
from pmagent.fees import exchange_fee, fee_per_contract, total_fee
from pmagent.risk import Risk, kelly_fraction
from pmagent.strategies.arb import _ladder_complete
from pmagent.strategies.base import Signal


def test_kalshi_fee_matches_schedule():
    # 100 contracts at 50c: 0.07 * 100 * .5 * .5 = 1.75
    assert exchange_fee(0.50, 100) == 1.75
    # rounds up to next cent: 1 contract at 50c = 0.0175 -> 0.02
    assert exchange_fee(0.50, 1) == 0.02
    assert exchange_fee(0.50, 100, taker=False) == 0.0                      # no maker fee on plain quadratic
    assert exchange_fee(0.50, 100, taker=False, fee_type="quadratic_with_maker_fees") == 0.44
    assert total_fee(0.50, 100, "robinhood") == pytest.approx(2.75)        # + $0.01/contract
    assert fee_per_contract(0.9, "kalshi") == pytest.approx(0.0063)


def test_kelly():
    assert kelly_fraction(0.5, 0.5) == 0
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 0.5) == 0


def _lad(t, lo=None, hi=None):
    return {"strike_type": t, "floor_strike": lo, "cap_strike": hi}


def test_ladder_complete():
    weather = [_lad("less", hi=52), _lad("between", 52, 53), _lad("between", 54, 55), _lad("greater", lo=55)]
    assert _ladder_complete(weather, integer_outcomes=True)
    assert not _ladder_complete(weather, integer_outcomes=False)            # 53->54 gap is a hole for continuous
    holey = [_lad("less", hi=52), _lad("between", 52, 53), _lad("greater", lo=58)]
    assert not _ladder_complete(holey, integer_outcomes=True)
    assert not _ladder_complete([_lad("between", 1, 2), _lad("greater", lo=2)])


def test_model_shrinks_toward_global():
    rows = []
    for i in range(400):   # globally, 10c NO contracts win 20% of the time
        rows.append(dict(role="taker", side="no", price=0.10, pb=pbucket(0.10), category="A", hb="1-7d", w=1.0, win=float(i % 5 == 0)))
    rows.append(dict(role="taker", side="no", price=0.10, pb=pbucket(0.10), category="B", hb="1-7d", w=1.0, win=1.0))
    m = CalibrationModel(k_cat=8, k_hor=8).fit(pd.DataFrame(rows))
    qa, _ = m.predict("taker", "no", 0.10, "A", 48)
    qb, nb = m.predict("taker", "no", 0.10, "B", 48)
    assert qa == pytest.approx(0.20, abs=0.01)
    assert 0.2 < qb < 0.4          # one lucky market barely moves the estimate
    assert m.predict("taker", "yes", 0.5, "A", 1)[1] == 0                   # unseen cell -> no confidence


def test_risk_and_settlement_accounting():
    with tempfile.TemporaryDirectory() as d:
        db = DB(os.path.join(d, "t.db"))
        cfg = Config(bankroll=1000, max_stake_pct=0.01, kelly_fraction=0.25)
        r = Risk(cfg, db)
        s = Signal("t", "TK", "EV", "x", "Sports", "no", 0.90, q=0.97, fee=0.01, ev=0.066, max_count=1000)
        n, why = r.size(s)
        assert why == "ok" and n == int(10 // 0.91)                         # capped at 1% of bankroll
        oid = db.insert_order(strategy="t", mode="paper", ticker="TK", event_ticker="EV", side="no", action="buy",
                              count=n, price=0.90, fee=0.11, status="open")
        assert r.size(s)[1] == "already positioned"
        db.update_order(oid, status="settled", pnl=n * 0.10 - 0.11, closed_ts=1)
        assert db.realized() == pytest.approx(n * 0.10 - 0.11)


class _FakeData:
    def __init__(self, prints):
        self.prints = prints

    def trades(self, **kw):
        return self.prints


def _resting(db, side, price, count, queue_ahead, expires):
    return db.insert_order(strategy="maker", mode="paper", ticker="TK", event_ticker="EV", side=side, action="buy",
                           count=count, price=price, fee=0, status="resting", role="maker", filled=0,
                           queue_ahead=queue_ahead, expires_ts=expires)


def test_paper_maker_fill_simulation():
    import time
    from pmagent.broker import PaperBroker
    with tempfile.TemporaryDirectory() as d:
        db = DB(os.path.join(d, "t.db"))
        cfg = Config()
        # YES bid at 0.80 with 50 ahead; takers sell YES (buy NO) 30 + 40 at 0.80 -> 20 of ours fill
        prints = [{"yes_price_dollars": "0.80", "count_fp": "30", "taker_side": "no"},
                  {"yes_price_dollars": "0.80", "count_fp": "40", "taker_side": "no"},
                  {"yes_price_dollars": "0.81", "count_fp": "500", "taker_side": "yes"}]   # buyers: irrelevant
        b = PaperBroker(db, _FakeData(prints), cfg)
        oid = _resting(db, "yes", 0.80, 100, 50, time.time() - 1)    # already expired -> finalize
        b.sync_resting()
        o = db.q("SELECT * FROM orders WHERE id=?", (oid,))[0]
        assert o["status"] == "open" and o["count"] == 20
        # NO bid at 0.10: a taker buying YES at 0.89 (NO at 0.11) doesn't reach us; at 0.91 (NO 0.09) sweeps through us
        b.data = _FakeData([{"yes_price_dollars": "0.89", "count_fp": "999", "taker_side": "yes"}])
        oid2 = _resting(db, "no", 0.10, 10, 0, time.time() + 999)
        b.sync_resting()
        assert db.q("SELECT status FROM orders WHERE id=?", (oid2,))[0][0] == "resting"
        b.data = _FakeData([{"yes_price_dollars": "0.91", "count_fp": "1", "taker_side": "yes"}])
        b.sync_resting()
        o2 = db.q("SELECT * FROM orders WHERE id=?", (oid2,))[0]
        assert o2["status"] == "open" and o2["count"] == 10
