"""Research data collection: settled markets + their trade prints.

Every Kalshi trade print records the price and which side was the taker, so
we can measure realised returns for takers and makers at each price level,
by category and by time-to-close, after fees.
"""
from __future__ import annotations

import random
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from .kalshi import KalshiClient, f, series_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS series(ticker TEXT PRIMARY KEY, category TEXT, fee_type TEXT, fee_mult REAL, title TEXT);
CREATE TABLE IF NOT EXISTS rmarkets(ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, category TEXT,
    result TEXT, open_ts REAL, close_ts REAL, volume REAL, title TEXT, historical INTEGER, fetched INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS rtrades(trade_id TEXT PRIMARY KEY, ticker TEXT, ts REAL, yes_price REAL,
    count REAL, taker_side TEXT);
CREATE INDEX IF NOT EXISTS rtrades_ticker ON rtrades(ticker);
"""


def ts(s: str | None) -> float:
    if not s:
        return 0.0
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def load_series(c: KalshiClient, con: sqlite3.Connection) -> dict[str, tuple]:
    rows = []
    for s in c.series_list():
        rows.append((s["ticker"], s.get("category") or "Other", s.get("fee_type") or "quadratic",
                     f(s.get("fee_multiplier"), 1.0), s.get("title", "")))
    con.executemany("INSERT OR REPLACE INTO series VALUES (?,?,?,?,?)", rows)
    con.commit()
    return {r[0]: r for r in rows}


def collect_markets(c: KalshiClient, con: sqlite3.Connection, series: dict, max_pages: int,
                    historical: bool, log=print) -> int:
    path = "/historical/markets" if historical else "/markets"
    params = {"limit": 1000, "mve_filter": "exclude"}
    if not historical:
        params["status"] = "settled"
    n = 0
    for page, m in enumerate(c.paginate(path, "markets", max_pages=max_pages, **params)):
        res = m.get("result")
        if res not in ("yes", "no"):
            continue
        vol = f(m.get("volume_fp"))
        if vol < 50:
            continue
        sr = series_of(m["event_ticker"])
        cat = series.get(sr, (None, "Other"))[1]
        con.execute("INSERT OR IGNORE INTO rmarkets(ticker,event_ticker,series,category,result,open_ts,close_ts,"
                    "volume,title,historical) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (m["ticker"], m["event_ticker"], sr, cat, res, ts(m.get("open_time")),
                     ts(m.get("close_time")), vol, m.get("title", ""), int(historical)))
        n += 1
        if n % 2000 == 0:
            con.commit()
            log(f"  markets kept: {n}")
    con.commit()
    return n


def choose_sample(con: sqlite3.Connection, per_series: int, total: int, seed: int = 7) -> list[tuple]:
    rows = con.execute("SELECT ticker, series, historical FROM rmarkets WHERE fetched=0").fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r[1]].append(r)
    rnd = random.Random(seed)
    done = dict(con.execute("SELECT series, COUNT(*) FROM rmarkets WHERE fetched=1 GROUP BY series").fetchall())
    out = []
    for s, lst in by.items():
        rnd.shuffle(lst)
        out += lst[: max(0, per_series - done.get(s, 0))]
    rnd.shuffle(out)
    return out[:total]


def collect_trades(c: KalshiClient, con: sqlite3.Connection, sample: list[tuple], log=print,
                   workers: int = 6) -> None:
    """Requests run in parallel threads (latency-bound); the shared RateLimiter caps throughput."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(item):
        ticker, _s, hist = item
        try:
            return item, c.trades(ticker=ticker, limit=1000, max_pages=1, historical=bool(hist))
        except RuntimeError as e:
            log(f"  skip {ticker}: {e}")
            return item, []

    t0 = time.time()
    with ThreadPoolExecutor(workers) as pool:
        results = pool.map(fetch, sample)
        for i, ((ticker, _s, _h), tr) in enumerate(results):
            _store(con, ticker, tr)
            if i % 250 == 0:
                con.commit()
                log(f"  trades: {i}/{len(sample)} markets  ({time.time()-t0:.0f}s)")
    con.commit()


def _store(con: sqlite3.Connection, ticker: str, tr: list[dict]) -> None:
    con.executemany("INSERT OR IGNORE INTO rtrades VALUES (?,?,?,?,?,?)",
                    [(t["trade_id"], ticker, ts(t["created_time"]), f(t.get("yes_price_dollars")),
                      f(t.get("count_fp")), t.get("taker_side")) for t in tr])
    con.execute("UPDATE rmarkets SET fetched=1 WHERE ticker=?", (ticker,))


def run(db: str, market_pages: int = 60, hist_pages: int = 60, per_series: int = 25,
        total: int = 8000, log=print) -> None:
    c = KalshiClient(reads_per_sec=9)
    con = connect(db)
    log("loading series ...")
    series = load_series(c, con)
    log(f"  {len(series)} series")
    log("collecting settled markets (recent) ...")
    collect_markets(c, con, series, market_pages, historical=False, log=log)
    if hist_pages:
        log("collecting settled markets (historical archive) ...")
        try:
            collect_markets(c, con, series, hist_pages, historical=True, log=log)
        except RuntimeError as e:
            log(f"  historical archive unavailable: {e}")
    sample = choose_sample(con, per_series, total)
    log(f"fetching trades for {len(sample)} markets ...")
    collect_trades(c, con, sample, log=log)
    n = con.execute("SELECT COUNT(*) FROM rtrades").fetchone()[0]
    log(f"done. {n} trades stored in {db}")
