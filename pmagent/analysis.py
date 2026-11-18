"""Strategy research: fit, select, and test out-of-sample.

    train (oldest 50% of events)   -> fit CalibrationModel
    validate (next 25%)             -> choose min_edge + approved categories
    test (newest 25%)               -> final, untouched performance estimate
Then refit on everything for live use.
"""
from __future__ import annotations

import json
import os

import pandas as pd

from .config import DATA
from .edge import (CalibrationModel, backtest, calibration_table, category_scan, load_observations,
                   split_by_time)


def baseline(test: pd.DataFrame, venue: str, name: str, side: str, lo: float, hi: float) -> dict:
    """Naive rule: take every print of `side` with price in [lo, hi] (one per market)."""
    from .edge import add_fees
    o = add_fees(test[(test.role == "taker") & (test.side == side) & test.price.between(lo, hi)], venue)
    o = o.sort_values("ts").groupby("ticker").head(1)
    cost = o.price + o.fee
    pnl = (o.pnl / cost).sum()
    return {"name": name, "trades": len(o), "roi": pnl / len(o) if len(o) else 0, "pnl": pnl,
            "win_rate": o.win.mean() if len(o) else 0}


def run(db: str, venue: str = "robinhood", log=print) -> dict:
    obs = load_observations(db)
    if obs.empty:
        raise SystemExit("no research data; run `pm research collect` first")
    n_mk = obs.ticker.nunique()
    log(f"{len(obs)//2} trade prints across {n_mk} settled markets, {obs.event_ticker.nunique()} events")
    train, rest = split_by_time(obs, 0.5)
    val, test = split_by_time(rest, 0.5)
    log(f"split by time: train {train.ticker.nunique()} / validate {val.ticker.nunique()} / test {test.ticker.nunique()} markets")
    model = CalibrationModel().fit(train)

    report: dict = {"venue": venue, "n_markets": int(n_mk), "n_prints": int(len(obs) // 2)}
    report["calibration"] = {v: calibration_table(obs, v).to_dict("records") for v in ("robinhood", "kalshi")}

    # ---- choose parameters on validation, separately for taker and maker ----
    grid, approved, final = [], {}, {}
    for role in ("taker", "maker"):
        rows = []
        for me in (0.02, 0.03, 0.05, 0.08, 0.12, 0.16):
            r = backtest(val, model, venue, role, me, name=f"{role} edge>={me}")
            rows.append({"role": role, "min_edge": me, **r.row()})
        grid += rows
        ok = [g for g in rows if g["trades"] >= 50]
        me = max(ok, key=lambda g: g["roi_lo"])["min_edge"] if ok else 0.05
        cats = category_scan(train, val, model, venue, role, me)
        good = cats[(cats.trades >= 15) & (cats.roi > 0)]["name"].tolist() if not cats.empty else []
        approved[role] = {"categories": good, "min_edge": me,
                          "validation_categories": cats.to_dict("records") if not cats.empty else []}
        log(f"validation [{role}]: min_edge={me}, categories={good}")
        # ---- final, untouched test ----
        final[f"{role}_all"] = backtest(test, model, venue, role, me, name=f"{role}: all categories")
        if good:
            final[f"{role}_approved"] = backtest(test, model, venue, role, me, categories=set(good),
                                                 name=f"{role}: approved categories (LIVE)")
            final[f"{role}_kalshi"] = backtest(test, model, "kalshi", role, me, categories=set(good),
                                               name=f"{role}: approved, Kalshi-direct fees")
    report["validation_grid"] = grid
    # live trading uses the taker settings for the calibration strategy and maker settings for the maker strategy
    report["approved"] = approved["taker"]
    report["approved_maker"] = approved["maker"]
    report["test"] = {k: v.row() for k, v in final.items()}
    report["test_equity"] = {k: v.equity for k, v in final.items()}
    report["baselines"] = [
        baseline(test, venue, "buy NO on longshots (YES<=10c)", "no", 0.90, 0.97),
        baseline(test, venue, "buy YES longshots (<=10c)", "yes", 0.01, 0.10),
        baseline(test, venue, "buy YES favorites (>=90c)", "yes", 0.90, 0.97),
        baseline(test, venue, "buy YES coin-flips (40-60c)", "yes", 0.40, 0.60),
    ]
    report["test_categories"] = {r: category_scan(train, test, model, venue, r, approved[r]["min_edge"]).to_dict("records")
                                 for r in ("taker", "maker")}

    # ---- refit on everything for live trading ----
    CalibrationModel().fit(obs).save(os.path.join(DATA, "model.json"))
    with open(os.path.join(DATA, "research_report.json"), "w") as fh:
        json.dump(report, fh, indent=1, default=float)
    for k, v in report["test"].items():
        log(f"TEST {v['name']}: {v['trades']} trades, ROI {v['roi']:+.2%} "
            f"[95% CI {v['roi_lo']:+.2%}, {v['roi_hi']:+.2%}], win {v['win_rate']:.1%}")
    return report
