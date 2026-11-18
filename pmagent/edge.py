"""Edge model + walk-forward backtest.

Each trade print gives two observations:
  * the taker bought `taker_side` at price c (the ask at that moment)
  * the maker bought the opposite side at 1-c (their resting limit price)
For each we know whether the contract paid $1.

CalibrationModel estimates P(win | side, price bucket, category, horizon)
with hierarchical shrinkage (global -> category -> category x horizon), so
sparse cells fall back to broader evidence instead of overfitting.
Train/test is split by event close time (walk-forward, no leakage), and
observations are weighted so every market counts once, not once per print.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .fees import fee_per_contract

PRICE_EDGES = [0.0, 0.03, 0.06, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.94, 0.97, 1.0]
HORIZONS = [(0, 1, "<1h"), (1, 6, "1-6h"), (6, 24, "6-24h"), (24, 24 * 7, "1-7d"), (24 * 7, 1e9, ">7d")]


def pbucket(c: float) -> int:
    i = int(np.searchsorted(PRICE_EDGES, c, side="right")) - 1
    return min(max(i, 0), len(PRICE_EDGES) - 2)


def hbucket(hours: float) -> str:
    for lo, hi, name in HORIZONS:
        if lo <= hours < hi:
            return name
    return ">7d"


def load_observations(db: str) -> pd.DataFrame:
    con = sqlite3.connect(db)
    q = """SELECT t.ticker, t.ts, t.yes_price, t.count, t.taker_side, m.event_ticker, m.series,
                  m.category, m.result, m.close_ts, s.fee_type, s.fee_mult
           FROM rtrades t JOIN rmarkets m ON m.ticker=t.ticker
           LEFT JOIN series s ON s.ticker=m.series
           WHERE t.yes_price > 0 AND t.yes_price < 1"""
    df = pd.read_sql_query(q, con)
    con.close()
    if df.empty:
        return df
    df["fee_type"] = df["fee_type"].fillna("quadratic")
    df["fee_mult"] = df["fee_mult"].fillna(1.0)
    df["hours"] = ((df["close_ts"] - df["ts"]) / 3600).clip(lower=0)
    yes_won = (df["result"] == "yes").astype(float)
    taker = pd.DataFrame({
        "role": "taker", "side": df["taker_side"],
        "price": np.where(df["taker_side"] == "yes", df["yes_price"], 1 - df["yes_price"]),
        "win": np.where(df["taker_side"] == "yes", yes_won, 1 - yes_won),
    })
    maker = pd.DataFrame({
        "role": "maker", "side": np.where(df["taker_side"] == "yes", "no", "yes"),
        "price": np.where(df["taker_side"] == "yes", 1 - df["yes_price"], df["yes_price"]),
        "win": np.where(df["taker_side"] == "yes", 1 - yes_won, yes_won),
    })
    common = df[["ticker", "ts", "event_ticker", "series", "category", "close_ts", "hours", "fee_type", "fee_mult"]]
    out = pd.concat([pd.concat([common, taker], axis=1), pd.concat([common, maker], axis=1)], ignore_index=True)
    out["price"] = out["price"].round(2)
    out = out[(out["price"] > 0) & (out["price"] < 1)].copy()   # sub-cent prints round to 0/1: not tradable
    out["pb"] = out["price"].map(pbucket)
    out["hb"] = out["hours"].map(hbucket)
    # each (market, role) gets total weight 1
    out["w"] = 1.0 / out.groupby(["ticker", "role"])["price"].transform("size")
    return out


@dataclass
class CalibrationModel:
    k_cat: float = 8.0      # pseudo-markets of shrinkage toward the global cell
    k_hor: float = 8.0      # pseudo-markets of shrinkage toward the category cell
    cells: dict = field(default_factory=dict)

    @staticmethod
    def _agg(df: pd.DataFrame, keys: list[str]) -> dict:
        g = df.assign(ww=df["w"] * df["win"], wp=df["w"] * df["price"]).groupby(keys)[["w", "ww", "wp"]].sum()
        return {("|".join(map(str, k if isinstance(k, tuple) else (k,)))): (r.w, r.ww / r.w, r.wp / r.w)
                for k, r in g.iterrows() if r.w > 0}

    def fit(self, obs: pd.DataFrame) -> "CalibrationModel":
        self.cells = {
            "g": self._agg(obs, ["role", "side", "pb"]),
            "c": self._agg(obs, ["role", "side", "pb", "category"]),
            "h": self._agg(obs, ["role", "side", "pb", "category", "hb"]),
        }
        return self

    def predict(self, role: str, side: str, price: float, category: str, hours: float) -> tuple[float, float]:
        """Return (estimated win prob, effective sample size in markets)."""
        pb = pbucket(price)
        base = f"{role}|{side}|{pb}"
        g = self.cells["g"].get(base)
        if g is None:
            return price, 0.0
        # Express everything as a probability *offset* from price, so cells with
        # different average prices inside the same bucket stay comparable.
        n, q, p = g
        off, neff = q - p, n
        c = self.cells["c"].get(f"{base}|{category}")
        if c:
            nc, qc, pc = c
            off = (nc * (qc - pc) + self.k_cat * off) / (nc + self.k_cat)
            neff = nc + min(self.k_cat, n)
            h = self.cells["h"].get(f"{base}|{category}|{hbucket(hours)}")
            if h:
                nh, qh, ph = h
                off = (nh * (qh - ph) + self.k_hor * off) / (nh + self.k_hor)
                neff = nh + min(self.k_hor, neff)
        return float(min(max(price + off, 0.001), 0.999)), float(neff)

    def to_json(self) -> str:
        return json.dumps({"k_cat": self.k_cat, "k_hor": self.k_hor,
                           "cells": {lvl: {k: list(v) for k, v in d.items()} for lvl, d in self.cells.items()}})

    @classmethod
    def from_json(cls, s: str) -> "CalibrationModel":
        d = json.loads(s)
        m = cls(d["k_cat"], d["k_hor"])
        m.cells = {lvl: {k: tuple(v) for k, v in cd.items()} for lvl, cd in d["cells"].items()}
        return m

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            fh.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "CalibrationModel":
        with open(path) as fh:
            return cls.from_json(fh.read())


def split_by_time(obs: pd.DataFrame, test_frac: float = 0.35) -> tuple[pd.DataFrame, pd.DataFrame]:
    ev_close = obs.groupby("event_ticker")["close_ts"].max().sort_values()
    cut = ev_close.iloc[int(len(ev_close) * (1 - test_frac))]
    train_ev = set(ev_close[ev_close < cut].index)
    m = obs["event_ticker"].isin(train_ev)
    return obs[m], obs[~m]


def add_fees(obs: pd.DataFrame, venue: str, rh_commission: float = 0.01) -> pd.DataFrame:
    obs = obs.copy()
    obs["fee"] = [fee_per_contract(p, venue, r == "taker", ft, fm, rh_commission)
                  for p, r, ft, fm in zip(obs["price"], obs["role"], obs["fee_type"], obs["fee_mult"])]
    obs["pnl"] = obs["win"] - obs["price"] - obs["fee"]
    return obs


def calibration_table(obs: pd.DataFrame, venue: str = "robinhood", role: str | None = None) -> pd.DataFrame:
    o = add_fees(obs, venue)
    if role:
        o = o[o["role"] == role]
    o = o.assign(bucket=o["pb"].map(lambda i: f"{PRICE_EDGES[i]:.2f}-{PRICE_EDGES[i+1]:.2f}"))
    rows = []
    for (r, b), g in o.groupby(["role", "bucket"]):
        w = g["w"].sum()
        price = (g["price"] * g["w"]).sum() / w
        win = (g["win"] * g["w"]).sum() / w
        fee = (g["fee"] * g["w"]).sum() / w
        rows.append({"role": r, "bucket": b, "markets": round(w, 1), "avg_price": price, "win_rate": win,
                     "edge_pre_fee": win - price, "fee": fee, "roi_after_fee": (win - price - fee) / (price + fee)})
    return pd.DataFrame(rows)


def _event_bootstrap(trades: pd.DataFrame, n: int = 400, seed: int = 1) -> tuple[float, float]:
    """95% CI of ROI, resampling whole events (outcomes within an event are correlated)."""
    if trades.empty:
        return (float("nan"), float("nan"))
    ev = trades.groupby("event_ticker").agg(pnl=("pnl_w", "sum"), cost=("cost_w", "sum"))
    rng = np.random.default_rng(seed)
    pnl, cost = ev["pnl"].to_numpy(), ev["cost"].to_numpy()
    rois = []
    for _ in range(n):
        idx = rng.integers(0, len(ev), len(ev))
        rois.append(pnl[idx].sum() / max(cost[idx].sum(), 1e-9))
    return float(np.percentile(rois, 2.5)), float(np.percentile(rois, 97.5))


@dataclass
class BacktestResult:
    name: str
    trades: int
    markets: int
    events: int
    staked: float          # total stake, in bankroll units
    pnl: float
    roi: float
    roi_lo: float
    roi_hi: float
    win_rate: float
    max_dd: float
    equity: list

    def row(self) -> dict:
        d = self.__dict__.copy()
        d.pop("equity")
        return d


def backtest(test: pd.DataFrame, model: CalibrationModel, venue: str = "robinhood", role: str = "taker",
             min_edge: float = 0.02, min_neff: float = 30, sides: tuple = ("yes", "no"),
             categories: set | None = None, one_per_market: bool = True, name: str = "",
             kelly_mult: float = 0.25, max_stake: float = 0.01) -> BacktestResult:
    """Replay test prints: trade whenever model EV after fees >= min_edge (per $ risked).

    roi   = P&L / total staked (Kelly-weighted, like the live agent)
    pnl   = P&L in units of starting bankroll over the test period (non-compounded)
    max_dd= worst peak-to-trough, same units"""
    o = add_fees(test[(test["role"] == role) & (test["side"].isin(sides))], venue)
    if categories:
        o = o[o["category"].isin(categories)]
    o = o.sort_values("ts")
    preds = [model.predict(role, s, p, c, h) for s, p, c, h in zip(o["side"], o["price"], o["category"], o["hours"])]
    o["q"] = [q for q, _ in preds]
    o["neff"] = [n for _, n in preds]
    o["ev"] = (o["q"] - o["price"] - o["fee"]) / (o["price"] + o["fee"])
    sel = o[(o["ev"] >= min_edge) & (o["neff"] >= min_neff)]
    if one_per_market:
        sel = sel.groupby(["ticker", "side"], as_index=False).head(1)
    # Size every trade the way the live agent does: 1/4-Kelly, capped at 1% of bankroll.
    # Results are therefore in units of bankroll: pnl 0.05 = +5% of starting bankroll.
    cost = sel["price"] + sel["fee"]
    full_kelly = ((sel["q"] - cost) / (1 - cost)).clip(lower=0)
    stake = np.minimum(kelly_mult * full_kelly, max_stake)
    sel = sel.assign(cost_w=stake, pnl_w=stake * sel["pnl"] / cost)
    eq = sel["pnl_w"].cumsum().to_numpy()
    dd = float((np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])).max()) if len(eq) else 0.0
    lo, hi = _event_bootstrap(sel)
    n = len(sel)
    staked = float(sel["cost_w"].sum())
    return BacktestResult(name or f"{role}-{venue}-edge{min_edge}", n, sel["ticker"].nunique(),
                          sel["event_ticker"].nunique(), staked, float(sel["pnl_w"].sum()),
                          float(sel["pnl_w"].sum() / staked) if staked else 0.0, lo, hi,
                          float(sel["win"].mean()) if n else 0.0, dd,
                          eq[:: max(1, len(eq) // 400)].round(4).tolist())


def category_scan(train: pd.DataFrame, test: pd.DataFrame, model: CalibrationModel, venue: str,
                  role: str, min_edge: float) -> pd.DataFrame:
    rows = []
    for cat in sorted(test["category"].unique()):
        r = backtest(test, model, venue, role, min_edge, categories={cat}, name=cat)
        if r.trades:
            rows.append(r.row())
    return pd.DataFrame(rows).sort_values("pnl", ascending=False) if rows else pd.DataFrame()
