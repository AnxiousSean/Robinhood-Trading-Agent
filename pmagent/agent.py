"""The trading agent: scan -> signal -> risk -> execute -> settle -> record."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime

from .broker import make_broker
from .config import DATA, Config
from .db import DB
from .edge import CalibrationModel
from .kalshi import KalshiClient, f, series_of
from .risk import Risk
from .strategies.arb import BasketArb
from .strategies.calibration import CalibrationEdge
from .strategies.maker import MakerQuotes

MODEL_PATH = os.path.join(DATA, "model.json")
REPORT_PATH = os.path.join(DATA, "research_report.json")
SERIES_PATH = os.path.join(DATA, "series_cache.json")


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else 0.0


class Context:
    def __init__(self, cfg: Config, client: KalshiClient, series: dict, markets: list[dict]):
        self.cfg, self.client, self.series, self.markets = cfg, client, series, markets
        self.now = time.time()
        self._events: dict[str, dict] = {}

    def category(self, m) -> str:
        return self.series.get(series_of(m["event_ticker"]), {}).get("category", "Other")

    def fee_info(self, m) -> tuple[str, float]:
        s = self.series.get(series_of(m["event_ticker"]), {})
        return s.get("fee_type", "quadratic"), float(s.get("fee_mult", 1.0))

    def hours_to_close(self, m) -> float:
        return max(0.0, (_ts(m.get("close_time")) - self.now) / 3600)

    def _event(self, ev: str) -> dict:
        if ev not in self._events:
            try:
                self._events[ev] = self.client.event(ev)
            except RuntimeError:
                self._events[ev] = {}
        return self._events[ev]

    def is_mutually_exclusive(self, ev: str) -> bool:
        return bool(self._event(ev).get("event", {}).get("mutually_exclusive"))

    def event_market_count(self, ev: str) -> int:
        return len(self._event(ev).get("markets", []) or [])


class Agent:
    def __init__(self, cfg: Config | None = None, db: DB | None = None):
        self.cfg = cfg or Config.load()
        self.db = db or DB()
        self.client = KalshiClient(reads_per_sec=8)
        self.broker = make_broker(self.db, self.client, self.cfg)
        self.risk = Risk(self.cfg, self.db)
        self.paused = False
        self.last_cycle: dict = {}
        self.last_signals: list = []
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()   # manual `scan` and the loop must never overlap
        self.model, self.report = self._load_model()
        self.strategies = self._build_strategies()
        self.series = self._load_series()
        self._warn_unproven()

    def _warn_unproven(self) -> None:
        """Real-money modes get a loud warning for any strategy without a positive held-out test."""
        if self.cfg.mode == "paper":
            return
        test = self.report.get("test", {})
        keys = {"calibration": "taker_approved", "maker": "maker_approved"}
        for s in self.strategies:
            k = keys.get(s.name)
            if not k:
                continue
            t = test.get(k) or test.get(k.replace("approved", "all"))
            if not t or t.get("roi", 0) <= 0 or t.get("roi_lo", -1) <= 0:
                roi = f"{t['roi']:+.1%} (95% CI {t['roi_lo']:+.1%}..{t['roi_hi']:+.1%})" if t else "untested"
                self.db.log(f"WARNING: real-money mode '{self.cfg.mode}' with strategy '{s.name}' whose out-of-sample "
                            f"test ROI is {roi}. It has no proven edge; expect to lose fees.", "warn")

    # ---------- setup ----------
    def _load_model(self):
        model = CalibrationModel.load(MODEL_PATH) if os.path.exists(MODEL_PATH) else None
        report = json.load(open(REPORT_PATH)) if os.path.exists(REPORT_PATH) else {}
        return model, report

    def _build_strategies(self):
        out = []
        if "calibration" in self.cfg.strategies:
            if self.model is None:
                self.db.log("calibration strategy disabled: run `pm research` first to build data/model.json", "warn")
            else:
                out.append(CalibrationEdge(self.model, self.report.get("approved", {})))
        if "maker" in self.cfg.strategies and self.model is not None:
            out.append(MakerQuotes(self.model, self.report.get("approved_maker", {})))
        if "arb" in self.cfg.strategies:
            out.append(BasketArb())
        return out

    def _load_series(self) -> dict:
        if os.path.exists(SERIES_PATH) and time.time() - os.path.getmtime(SERIES_PATH) < 86400:
            return json.load(open(SERIES_PATH))
        s = {x["ticker"]: {"category": x.get("category") or "Other", "fee_type": x.get("fee_type") or "quadratic",
                           "fee_mult": f(x.get("fee_multiplier"), 1.0)} for x in self.client.series_list()}
        json.dump(s, open(SERIES_PATH, "w"))
        return s

    # ---------- one cycle ----------
    def fetch_markets(self) -> list[dict]:
        max_close = int(time.time() + self.cfg.max_hours * 3600)
        ms = list(self.client.markets(status="open", max_close_ts=max_close, max_pages=200))
        return [m for m in ms if m.get("market_type", "binary") == "binary"]

    def cycle(self) -> dict:
        with self._cycle_lock:
            return self._cycle()

    def _cycle(self) -> dict:
        t0 = time.time()
        summary = {"ts": t0, "markets": 0, "signals": 0, "orders": 0, "settled": 0, "halt": None}
        for msg in self.broker.sync_resting():
            self.db.log(msg)
        for msg in self.broker.settle():
            self.db.log(msg)
            summary["settled"] += 1
        if hasattr(self.broker, "expire_stale"):
            self.broker.expire_stale()
        markets = self.fetch_markets()
        summary["markets"] = len(markets)
        ctx = Context(self.cfg, self.client, self.series, markets)
        signals = []
        for strat in self.strategies:
            try:
                signals += strat.scan(ctx)
            except Exception as e:  # a broken strategy must not kill the loop
                self.db.log(f"strategy {strat.name} error: {e!r}", "error")
        summary["signals"] = len(signals)
        self.last_signals = signals
        halt = self.risk.halted()
        summary["halt"] = halt
        if not self.paused and not halt:
            summary["orders"] = self.execute(signals)
        self.snapshot()
        summary["secs"] = round(time.time() - t0, 1)
        self.last_cycle = summary
        self.db.log(f"cycle: {summary['markets']} mkts, {summary['signals']} signals, "
                    f"{summary['orders']} orders, {summary['settled']} settled, {summary['secs']}s"
                    + (f" HALT: {halt}" if halt else "") + (" [paused]" if self.paused else ""))
        return summary

    def execute(self, signals) -> int:
        placed = 0
        baskets = defaultdict(list)
        singles = []
        for s in signals:
            (baskets[s.basket] if s.basket else singles).append(s)
        for bid, legs in baskets.items():
            n, why = self.risk.size_basket(legs)
            self._log_signal(legs[0], "basket" if n else "skip", why)
            if n:
                for leg in legs:
                    if self.broker.buy(leg, n):
                        placed += 1
                self.db.log(f"ARB {legs[0].note} x{n}")
        resting = len(self.db.resting_orders())
        for s in singles:
            if placed >= self.cfg.max_trades_per_cycle:
                break
            maker = s.strategy == "maker"
            if maker and resting >= self.cfg.max_resting:
                self._log_signal(s, "skip", "max resting orders")
                continue
            n, why = self.risk.size(s)
            self._log_signal(s, ("bid" if maker else "buy") if n else "skip", why)
            if not n:
                continue
            oid = self.broker.rest(s, n) if maker else self.broker.buy(s, n)
            if oid:
                placed += 1
                resting += maker
                verb = "TICKET" if self.cfg.mode == "robinhood" else ("BID" if maker else "BUY")
                kind = "LIMIT " if maker else ""
                self.db.log(f"{verb} {kind}{n} {s.ticker} {s.side.upper()} @ {s.price:.2f}  ev {s.ev:+.1%}  ({s.strategy})")
        return placed

    def _log_signal(self, s, action: str, reason: str) -> None:
        self.db.x("INSERT INTO signals(ts,strategy,ticker,title,side,price,q,ev,neff,action,reason) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), s.strategy, s.ticker, s.title, s.side, s.price, s.q, s.ev, s.neff, action, reason))

    def snapshot(self) -> None:
        self.db.x("INSERT INTO equity VALUES (?,?,?,?,?)",
                  (time.time(), self.risk.cash(), self.db.exposure(), self.db.realized(), len(self.db.open_orders())))

    # ---------- loop ----------
    def run_forever(self, on_cycle=None) -> None:
        while not self._stop.is_set():
            try:
                s = self.cycle()
                if on_cycle:
                    on_cycle(s)
            except Exception as e:
                self.db.log(f"cycle error: {e!r}", "error")
            self._stop.wait(self.cfg.scan_interval_sec)

    def stop(self) -> None:
        self._stop.set()
