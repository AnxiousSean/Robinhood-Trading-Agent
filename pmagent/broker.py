"""Execution back-ends.

PaperBroker      - simulated fills at the live ask (taker), no money moves.
RobinhoodTickets - Robinhood has no event-contract API, so the agent writes a
                   trade ticket; you place it in the Robinhood app and confirm
                   the fill in the terminal (or reject it).
KalshiBroker     - live immediate-or-cancel limit orders through Kalshi's API
                   (same contracts Robinhood lists). Use the demo env first.

All brokers share settlement: open positions are marked closed when the
market resolves, using the official result from the market data API.
"""
from __future__ import annotations

import time

from .db import DB
from .fees import total_fee
from .kalshi import DEMO, PROD, KalshiClient, f


class BaseBroker:
    mode = "base"

    def __init__(self, db: DB, data: KalshiClient, cfg):
        self.db, self.data, self.cfg = db, data, cfg

    def _fee(self, sig, n: int, venue: str) -> float:
        return total_fee(sig.price, n, venue, True, sig.fee_type, sig.fee_mult, self.cfg.rh_commission)

    def _record(self, sig, n: int, status: str, fee: float, ext_id: str = "", price: float | None = None,
                **extra) -> int:
        return self.db.insert_order(strategy=sig.strategy, mode=self.mode, ticker=sig.ticker,
                                    event_ticker=sig.event_ticker, title=sig.title, category=sig.category,
                                    side=sig.side, action="buy", count=n, price=price or sig.price, fee=fee,
                                    q=sig.q, ev=sig.ev, note=sig.note, status=status, ext_id=ext_id,
                                    basket=sig.basket, **extra)

    def _maker_fee(self, price: float, n: float, fee_type: str = "quadratic", mult: float = 1.0) -> float:
        return total_fee(price, n, self.cfg.fee_venue, False, fee_type, mult, self.cfg.rh_commission)

    def buy(self, sig, n: int) -> int | None:
        raise NotImplementedError

    # ---------- maker (resting limit orders) ----------
    def rest(self, sig, n: int) -> int | None:
        """Post a resting limit buy. Default: simulated (paper)."""
        q_ahead = (sig.legs[0].get("queue_ahead", 0.0) if sig.legs else 0.0)
        return self._record(sig, n, "resting", self._maker_fee(sig.price, n, sig.fee_type, sig.fee_mult),
                            role="maker", filled=0, queue_ahead=q_ahead,
                            expires_ts=time.time() + self.cfg.maker_ttl_min * 60)

    def _finalize_resting(self, o, filled: float, reason: str) -> str:
        if filled >= 1:
            fee = self._maker_fee(o["price"], filled)
            self.db.update_order(o["id"], status="open", count=filled, filled=filled, fee=fee)
            return f"maker #{o['id']} {o['ticker']} {o['side']} filled {filled:.0f}/{o['count']:.0f} @ {o['price']:.2f} ({reason})"
        self.db.update_order(o["id"], status="cancelled", filled=0, fee=0, pnl=0, closed_ts=time.time())
        return ""

    def sync_resting(self) -> list[str]:
        """Paper fill simulation from real trade prints after the order was placed.

        Our bid for side S at price c is hit when a taker sells S, i.e. buys the
        other side. A print strictly through c means our level was swept (full
        fill). Prints exactly at c first consume the `queue_ahead` contracts
        that were resting before us (price-time priority)."""
        msgs = []
        for o in self.db.resting_orders():
            try:
                prints = self.data.trades(ticker=o["ticker"], limit=1000, max_pages=3, min_ts=int(o["ts"]))
            except RuntimeError:
                continue
            at = through = 0.0
            for t in prints:
                yp = f(t.get("yes_price_dollars"))
                side_px = yp if o["side"] == "yes" else round(1 - yp, 2)
                if t.get("taker_side") == o["side"]:
                    continue                      # taker bought our side: not a fill for a bid
                if side_px < o["price"] - 1e-9:
                    through += f(t.get("count_fp"))
                elif abs(side_px - o["price"]) < 1e-9:
                    at += f(t.get("count_fp"))
            filled = o["count"] if through > 0 else min(o["count"], max(0.0, at - (o["queue_ahead"] or 0)))
            filled = float(int(filled))
            if filled >= o["count"]:
                msgs.append(self._finalize_resting(o, filled, "full"))
            elif time.time() >= (o["expires_ts"] or 0):
                m = self._finalize_resting(o, filled, "expired, remainder cancelled")
                if m:
                    msgs.append(m)
            elif filled != (o["filled"] or 0):
                self.db.update_order(o["id"], filled=filled)
        return [m for m in msgs if m]

    def close(self, order_id: int) -> str:
        """Exit a position early by selling into the bid."""
        o = self.db.q("SELECT * FROM orders WHERE id=?", (order_id,))
        if not o or o[0]["status"] != "open":
            return "no open position with that id"
        o = o[0]
        m = self.data.market(o["ticker"])
        bid = f(m.get(f"{o['side']}_bid_dollars"))
        if bid <= 0:
            return "no bid to sell into"
        fee = total_fee(bid, o["count"], self.cfg.fee_venue, True, "quadratic", 1.0, self.cfg.rh_commission)
        msg = self._execute_sell(o, bid)
        if msg != "ok":
            return msg
        pnl = o["count"] * (bid - o["price"]) - o["fee"] - fee
        self.db.update_order(o["id"], status="closed", exit_price=bid, pnl=round(pnl, 4), closed_ts=time.time(),
                             fee=o["fee"] + fee)
        return f"closed #{o['id']} {o['count']:.0f} @ {bid:.2f}  pnl {pnl:+.2f}"

    def _execute_sell(self, o, bid: float) -> str:
        return "ok"

    def settle(self) -> list[str]:
        msgs = []
        by_ticker: dict[str, list] = {}
        for o in self.db.open_orders():
            by_ticker.setdefault(o["ticker"], []).append(o)
        for t, orders in by_ticker.items():
            try:
                m = self.data.market(t)
            except RuntimeError:
                continue
            res = m.get("result")
            if m.get("status") not in ("settled", "finalized", "determined") and res not in ("yes", "no"):
                continue
            if res not in ("yes", "no"):
                # voided / scalar settlement: pay settlement value
                val = f(m.get("settlement_value_dollars"), None)
                if val is None:
                    continue
            for o in orders:
                if res in ("yes", "no"):
                    payout = 1.0 if res == o["side"] else 0.0
                else:
                    payout = val if o["side"] == "yes" else 1 - val
                pnl = o["count"] * (payout - o["price"]) - o["fee"]
                self.db.update_order(o["id"], status="settled", result=res or f"{val}", exit_price=payout,
                                     pnl=round(pnl, 4), closed_ts=time.time())
                msgs.append(f"settled #{o['id']} {t} {o['side']} -> {res}  pnl {pnl:+.2f}")
        return msgs


class PaperBroker(BaseBroker):
    mode = "paper"

    def buy(self, sig, n: int) -> int | None:
        return self._record(sig, n, "open", self._fee(sig, n, self.cfg.fee_venue))


class RobinhoodTickets(BaseBroker):
    mode = "robinhood"

    def buy(self, sig, n: int) -> int | None:
        return self._record(sig, n, "ticket", self._fee(sig, n, "robinhood"),
                            expires_ts=time.time() + 1800)

    def rest(self, sig, n: int) -> int | None:
        # You place a LIMIT order in the app at this price; confirm with `fill` once (partly) filled.
        return self._record(sig, n, "ticket", self._maker_fee(sig.price, n, sig.fee_type, sig.fee_mult),
                            role="maker", expires_ts=time.time() + self.cfg.maker_ttl_min * 60 * 4)

    def sync_resting(self) -> list[str]:
        return []

    def confirm(self, order_id: int, fill_price: float | None = None, count: float | None = None) -> str:
        o = self.db.q("SELECT * FROM orders WHERE id=? AND status='ticket'", (order_id,))
        if not o:
            return "no pending ticket with that id"
        o = o[0]
        price = fill_price or o["price"]
        n = count or o["count"]
        fee = total_fee(price, n, "robinhood", o["role"] != "maker", "quadratic", 1.0, self.cfg.rh_commission)
        self.db.update_order(order_id, status="open", price=price, count=n, fee=fee, filled=n)
        return f"ticket #{order_id} confirmed: {n:.0f} @ {price:.2f}"

    def reject(self, order_id: int) -> str:
        self.db.update_order(order_id, status="rejected", closed_ts=time.time(), pnl=0)
        return f"ticket #{order_id} rejected"

    def expire_stale(self, max_age_sec: float = 1800) -> None:
        for o in self.db.q("SELECT id, ts, expires_ts FROM orders WHERE status='ticket'"):
            if time.time() > (o["expires_ts"] or o["ts"] + max_age_sec):
                self.db.update_order(o["id"], status="rejected", note="expired: price likely moved", pnl=0,
                                     closed_ts=time.time())


class KalshiBroker(BaseBroker):
    mode = "kalshi"

    def __init__(self, db, data, cfg):
        super().__init__(db, data, cfg)
        if not (cfg.kalshi_key_id and cfg.kalshi_private_key_path):
            raise RuntimeError("mode=kalshi needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        base = PROD if cfg.kalshi_env == "prod" else DEMO
        self.api = KalshiClient(base, cfg.kalshi_key_id, cfg.kalshi_private_key_path, reads_per_sec=8)

    def buy(self, sig, n: int) -> int | None:
        cents = int(round(sig.price * 100))
        try:
            r = self.api.place_order(sig.ticker, sig.side, n, cents, tif="immediate_or_cancel")
        except RuntimeError as e:
            self.db.log(f"order failed {sig.ticker}: {e}", "error")
            self._record(sig, n, "failed", 0.0)
            return None
        od = r.get("order", {})
        filled = f(od.get("fill_count_fp") or od.get("fill_count"), 0) or (n - f(od.get("remaining_count_fp") or od.get("remaining_count"), n))
        if filled < 1:
            self._record(sig, n, "failed", 0.0, od.get("order_id", ""))
            self.db.log(f"no fill {sig.ticker} @ {sig.price:.2f}", "warn")
            return None
        fee = f(od.get("taker_fees_dollars"), None)
        if fee is None:
            fee = self._fee(sig, int(filled), "kalshi")
        return self._record(sig, int(filled), "open", fee, od.get("order_id", ""))

    def rest(self, sig, n: int) -> int | None:
        exp = int(time.time() + self.cfg.maker_ttl_min * 60)
        try:
            r = self.api.place_order(sig.ticker, sig.side, n, int(round(sig.price * 100)),
                                     tif="good_till_canceled", post_only=True, expiration_ts=exp)
        except RuntimeError as e:
            self.db.log(f"resting order failed {sig.ticker}: {e}", "error")
            return None
        od = r.get("order", {})
        return self._record(sig, n, "resting", self._maker_fee(sig.price, n, sig.fee_type, sig.fee_mult),
                            od.get("order_id", ""), role="maker", expires_ts=exp)

    def sync_resting(self) -> list[str]:
        msgs = []
        for o in self.db.resting_orders():
            try:
                od = self.api.request("GET", f"/portfolio/orders/{o['ext_id']}", auth=True).get("order", {})
            except RuntimeError:
                continue
            filled = float(int(f(od.get("fill_count_fp") or od.get("fill_count"), 0)))
            done = od.get("status") in ("executed", "canceled", "cancelled", "expired") or filled >= o["count"]
            if not done and time.time() >= (o["expires_ts"] or 0):
                try:
                    self.api.cancel_order(o["ext_id"])
                except RuntimeError:
                    pass
                done = True
            if done:
                m = self._finalize_resting(o, filled, od.get("status", "expired"))
                if m:
                    msgs.append(m)
            elif filled != (o["filled"] or 0):
                self.db.update_order(o["id"], filled=filled)
        return msgs

    def _execute_sell(self, o, bid: float) -> str:
        try:
            self.api.place_order(o["ticker"], o["side"], int(o["count"]), int(round(bid * 100)),
                                 action="sell", tif="immediate_or_cancel")
            return "ok"
        except RuntimeError as e:
            return f"sell failed: {e}"


def make_broker(db: DB, data: KalshiClient, cfg) -> BaseBroker:
    return {"paper": PaperBroker, "robinhood": RobinhoodTickets, "kalshi": KalshiBroker}[cfg.mode](db, data, cfg)
