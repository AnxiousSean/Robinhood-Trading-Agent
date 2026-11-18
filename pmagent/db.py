"""Agent state: every signal, order, fill, settlement and equity snapshot."""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, strategy TEXT, mode TEXT,
  ticker TEXT, event_ticker TEXT, title TEXT, category TEXT, side TEXT, action TEXT,
  count REAL, price REAL, fee REAL, q REAL, ev REAL, note TEXT,
  status TEXT,             -- resting | ticket | open | closed | settled | rejected | cancelled | failed
  exit_price REAL, pnl REAL, result TEXT, closed_ts REAL, ext_id TEXT, basket TEXT,
  role TEXT DEFAULT 'taker', filled REAL DEFAULT 0, queue_ahead REAL DEFAULT 0, expires_ts REAL);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, strategy TEXT, ticker TEXT, title TEXT, side TEXT,
  price REAL, q REAL, ev REAL, neff REAL, action TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS equity(ts REAL, cash REAL, exposure REAL, realized REAL, open_positions INTEGER);
CREATE TABLE IF NOT EXISTS logs(ts REAL, level TEXT, msg TEXT);
"""


class DB:
    def __init__(self, path: str | None = None):
        os.makedirs(DATA, exist_ok=True)
        self.path = path or os.path.join(DATA, "agent.db")
        self.lock = threading.RLock()
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        have = {r[1] for r in self.con.execute("PRAGMA table_info(orders)")}
        for col, typ in (("role", "TEXT DEFAULT 'taker'"), ("filled", "REAL DEFAULT 0"),
                         ("queue_ahead", "REAL DEFAULT 0"), ("expires_ts", "REAL")):
            if col not in have:   # migrate databases created by older versions
                self.con.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
        self.con.commit()

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def log(self, msg: str, level: str = "info") -> None:
        self.x("INSERT INTO logs VALUES (?,?,?)", (time.time(), level, msg))

    def insert_order(self, **kw) -> int:
        kw.setdefault("ts", time.time())
        cols = ",".join(kw)
        return self.x(f"INSERT INTO orders({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values())).lastrowid

    def update_order(self, oid: int, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE orders SET {sets} WHERE id=?", (*kw.values(), oid))

    # ---- accounting ----
    def realized(self) -> float:
        return float(self.q("SELECT COALESCE(SUM(pnl),0) FROM orders WHERE status IN ('settled','closed')")[0][0])

    def realized_since(self, t0: float) -> float:
        return float(self.q("SELECT COALESCE(SUM(pnl),0) FROM orders WHERE status IN ('settled','closed') "
                            "AND closed_ts>=?", (t0,))[0][0])

    def resting_orders(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM orders WHERE status='resting' ORDER BY ts")

    def open_orders(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM orders WHERE status='open' ORDER BY ts")

    def exposure(self) -> float:
        return float(self.q("SELECT COALESCE(SUM(count*price+fee),0) FROM orders WHERE status IN ('open','ticket','resting')")[0][0])

    def event_exposure(self, event_ticker: str) -> float:
        return float(self.q("SELECT COALESCE(SUM(count*price+fee),0) FROM orders WHERE status IN ('open','ticket','resting') "
                            "AND event_ticker=?", (event_ticker,))[0][0])

    def holds(self, ticker: str) -> bool:
        return bool(self.q("SELECT 1 FROM orders WHERE status IN ('open','ticket','resting') AND ticker=? LIMIT 1", (ticker,)))

    def stats(self) -> dict:
        r = self.q("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(count*price+fee),0) staked, "
                   "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) wins, COALESCE(SUM(fee),0) fees "
                   "FROM orders WHERE status IN ('settled','closed')")[0]
        n = r["n"]
        return {"closed": n, "pnl": r["pnl"], "staked": r["staked"], "fees": r["fees"],
                "win_rate": r["wins"] / n if n else 0.0, "roi": r["pnl"] / r["staked"] if r["staked"] else 0.0}
