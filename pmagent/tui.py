"""Trading terminal (Textual)."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (DataTable, Footer, Header, Input, RichLog, Sparkline, Static, TabbedContent,
                             TabPane)

from .agent import REPORT_PATH, Agent
from .commands import HELP, run_command
from .risk import KILL_FILE


def hhmm(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else ""


def money(x: float) -> Text:
    return Text(f"{x:+,.2f}", style="green" if x > 0 else ("red" if x < 0 else ""))


class Tile(Static):
    DEFAULT_CSS = """
    Tile { border: round $primary-darken-2; padding: 0 1; height: 4; width: 1fr; }
    """

    def set(self, label: str, value, style: str = "") -> None:
        self.update(Text.assemble((label + "\n", "dim"), (str(value), style or "bold")))


class Terminal(App):
    TITLE = "PM Agent - prediction market trading terminal"
    CSS = """
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    #tiles { height: 4; }
    #spark { height: 5; border: round $primary-darken-2; }
    #cyc { height: 3; padding: 0 1; }
    DataTable { height: 1fr; }
    #out { height: 9; border: round $primary-darken-3; }
    #by_strategy, #r_test, #r_base { height: auto; max-height: 12; margin-bottom: 1; }
    """
    BINDINGS = [("f5", "scan", "Scan now"), ("f6", "toggle_pause", "Pause/Resume"),
                ("f8", "toggle_kill", "Kill switch"), ("ctrl+q", "quit", "Quit")]

    def __init__(self, autorun: bool = True):
        super().__init__()
        self.agent = Agent()
        self.autorun = autorun
        self._seen_log = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Dashboard", id="dash"):
                with Horizontal(id="tiles"):
                    for t in ("mode", "equity", "cash", "exposure", "realized", "today", "winrate", "trades"):
                        yield Tile(id=f"t_{t}")
                yield Sparkline([0], id="spark")
                yield Static(id="cyc")
                yield DataTable(id="pos_dash")
            with TabPane("Signals", id="sig"):
                yield DataTable(id="signals")
            with TabPane("Positions", id="pos"):
                yield DataTable(id="positions")
            with TabPane("Tickets", id="tix"):
                yield Static("Robinhood mode: place these in the Robinhood app, then `fill <id> [price] [count]` "
                             "or `reject <id>`. Tickets expire after 30 min.", classes="hint")
                yield DataTable(id="tickets")
            with TabPane("History", id="hist"):
                yield DataTable(id="history")
            with TabPane("Strategy P&L", id="strat"):
                yield DataTable(id="by_strategy")
                yield DataTable(id="by_category")
            with TabPane("Research", id="research"):
                yield DataTable(id="r_test")
                yield DataTable(id="r_base")
                yield DataTable(id="r_cal")
            with TabPane("Log", id="logtab"):
                yield RichLog(id="log", wrap=True, markup=False)
        yield RichLog(id="out", wrap=True, markup=False)
        yield Input(placeholder="command (help, quote TICKER, buy TICKER yes 10, close ID, fill ID, set min_edge 0.05 ...)",
                    id="cmd")
        yield Footer()

    def on_mount(self) -> None:
        cols = {
            "pos_dash": ("#", "opened", "strategy", "ticker", "side", "qty", "price", "cost", "model p", "EV/$"),
            "positions": ("#", "opened", "strategy", "status", "ticker", "title", "side", "qty", "price", "cost", "model p", "EV/$"),
            "signals": ("strategy", "ticker", "title", "cat", "side", "price", "model p", "fee", "EV/$", "n", "hrs", "depth"),
            "tickets": ("#", "created", "ticker", "title", "side", "qty", "limit price", "cost", "EV/$"),
            "history": ("#", "closed", "strategy", "ticker", "side", "qty", "entry", "exit", "fee", "pnl", "result"),
            "by_strategy": ("strategy", "trades", "staked", "pnl", "ROI", "win rate", "open"),
            "by_category": ("category", "trades", "staked", "pnl", "ROI", "win rate"),
            "r_test": ("out-of-sample test", "trades", "markets", "ROI", "95% CI", "win", "bankroll P&L", "max DD"),
            "r_base": ("naive baseline (test set)", "trades", "ROI", "win"),
            "r_cal": ("role", "price bucket", "markets", "avg price", "win rate", "edge pre-fee", "fee", "ROI after fee"),
        }
        for tid, c in cols.items():
            t = self.query_one(f"#{tid}", DataTable)
            t.add_columns(*c)
            t.cursor_type = "row"
            t.zebra_stripes = True
        self.query_one("#spark", Sparkline).border_title = "realized P&L"
        self.load_research()
        self.refresh_all()
        self.set_interval(3, self.refresh_all)
        out = self.query_one("#out", RichLog)
        out.write("Welcome. Type `help` for commands. Mode: " + self.agent.cfg.mode +
                  ("" if self.agent.model else "  (no model yet - run `pm research` for the calibration strategy)"))
        if self.autorun:
            threading.Thread(target=self.agent.run_forever, daemon=True).start()

    # ---------- actions ----------
    def action_scan(self) -> None:
        self.run_cmd("scan")

    def action_toggle_pause(self) -> None:
        self.run_cmd("resume" if self.agent.paused else "pause")

    def action_toggle_kill(self) -> None:
        self.run_cmd("unkill" if os.path.exists(KILL_FILE) else "kill")

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        line = ev.value.strip()
        ev.input.value = ""
        if line:
            self.query_one("#out", RichLog).write(f"> {line}")
            self.run_cmd(line)

    @work(thread=True)
    def run_cmd(self, line: str) -> None:
        res = run_command(self.agent, line)
        self.call_from_thread(self.query_one("#out", RichLog).write, res)
        self.call_from_thread(self.refresh_all)

    # ---------- refresh ----------
    def refresh_all(self) -> None:
        a, db, cfg = self.agent, self.agent.db, self.agent.cfg
        st = db.stats()
        eq = a.risk.equity()
        today = db.realized_since(a.risk.day_start())
        mode = cfg.mode.upper() + (" PAUSED" if a.paused else "") + (" KILLED" if os.path.exists(KILL_FILE) else "")
        self.query_one("#t_mode", Tile).set("mode", mode, "bold yellow" if "PAPER" in mode else "bold red")
        self.query_one("#t_equity", Tile).set("equity", f"${eq:,.2f}")
        self.query_one("#t_cash", Tile).set("cash", f"${a.risk.cash():,.2f}")
        self.query_one("#t_exposure", Tile).set("open exposure", f"${db.exposure():,.2f}")
        self.query_one("#t_realized", Tile).set("realized P&L", f"{st['pnl']:+,.2f}", "bold green" if st["pnl"] >= 0 else "bold red")
        self.query_one("#t_today", Tile).set("today", f"{today:+,.2f}", "bold green" if today >= 0 else "bold red")
        self.query_one("#t_winrate", Tile).set("win rate / ROI", f"{st['win_rate']:.0%} / {st['roi']:+.1%}")
        self.query_one("#t_trades", Tile).set("closed / open / bids",
                                              f"{st['closed']} / {len(db.open_orders())} / {len(db.resting_orders())}")
        eqs = [r[0] for r in db.q("SELECT realized FROM equity ORDER BY ts DESC LIMIT 300")][::-1]
        self.query_one("#spark", Sparkline).data = eqs or [0]
        lc = a.last_cycle
        self.query_one("#cyc", Static).update(
            f"last cycle {hhmm(lc.get('ts', 0))}: {lc.get('markets', 0)} markets scanned, {lc.get('signals', 0)} signals, "
            f"{lc.get('orders', 0)} orders, {lc.get('settled', 0)} settled ({lc.get('secs', 0)}s)"
            + (f"  HALTED: {lc['halt']}" if lc.get("halt") else "") if lc else "waiting for first scan ...")

        opens = db.open_orders() + db.resting_orders()
        self._fill("pos_dash", [(o["id"], hhmm(o["ts"]), o["strategy"], o["ticker"], o["side"], f"{o['count']:.0f}",
                                 f"{o['price']:.2f}", f"{o['count']*o['price']+o['fee']:.2f}", f"{o['q'] or 0:.3f}",
                                 f"{o['ev'] or 0:+.1%}") for o in opens[-15:][::-1]])
        self._fill("positions", [(o["id"], hhmm(o["ts"]), o["strategy"],
                                  o["status"] + (f" {o['filled'] or 0:.0f}/{o['count']:.0f}" if o["status"] == "resting" else ""),
                                  o["ticker"], (o["title"] or "")[:40], o["side"],
                                  f"{o['count']:.0f}", f"{o['price']:.2f}", f"{o['count']*o['price']+o['fee']:.2f}",
                                  f"{o['q'] or 0:.3f}", f"{o['ev'] or 0:+.1%}") for o in opens[::-1]])
        self._fill("signals", [(s.strategy, s.ticker, s.title[:38], s.category[:12], s.side, f"{s.price:.2f}",
                                f"{s.q:.3f}", f"{s.fee:.3f}", f"{s.ev:+.1%}", f"{min(s.neff, 99999):.0f}",
                                f"{s.hours:.1f}", f"{s.max_count:.0f}") for s in a.last_signals[:300]])
        self._fill("tickets", [(o["id"], hhmm(o["ts"]), o["ticker"], (o["title"] or "")[:40], o["side"].upper(),
                                f"{o['count']:.0f}", f"{o['price']:.2f}", f"{o['count']*o['price']+o['fee']:.2f}",
                                f"{o['ev'] or 0:+.1%}") for o in db.q("SELECT * FROM orders WHERE status='ticket' ORDER BY ts DESC")])
        self._fill("history", [(o["id"], hhmm(o["closed_ts"]), o["strategy"], o["ticker"], o["side"], f"{o['count']:.0f}",
                                f"{o['price']:.2f}", f"{o['exit_price'] or 0:.2f}", f"{o['fee']:.2f}", money(o["pnl"] or 0),
                                o["result"] or o["status"])
                               for o in db.q("SELECT * FROM orders WHERE status IN ('settled','closed') ORDER BY closed_ts DESC LIMIT 500")])
        self._grouped("by_strategy", "strategy", with_open=True)
        self._grouped("by_category", "category")
        log = self.query_one("#log", RichLog)
        for r in db.q("SELECT * FROM logs WHERE ts>? ORDER BY ts LIMIT 500", (self._seen_log,)):
            style = {"error": "bold red", "warn": "yellow"}.get(r["level"], "")
            log.write(Text(f"{hhmm(r['ts'])}  {r['msg']}", style=style))
            self._seen_log = r["ts"]

    def _grouped(self, tid: str, col: str, with_open: bool = False) -> None:
        rows = []
        for r in self.agent.db.q(
                f"SELECT {col} k, COUNT(*) n, SUM(count*price+fee) staked, SUM(pnl) pnl, "
                f"SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) w FROM orders WHERE status IN ('settled','closed') "
                f"GROUP BY {col} ORDER BY pnl DESC"):
            row = [r["k"], r["n"], f"{r['staked']:.2f}", money(r["pnl"]), f"{r['pnl']/r['staked']:+.1%}" if r["staked"] else "",
                   f"{r['w']/r['n']:.0%}"]
            if with_open:
                row.append(self.agent.db.q(f"SELECT COUNT(*) FROM orders WHERE status='open' AND {col}=?", (r["k"],))[0][0])
            rows.append(row)
        self._fill(tid, rows)

    def _fill(self, tid: str, rows: list) -> None:
        t = self.query_one(f"#{tid}", DataTable)
        t.clear()
        for r in rows:
            t.add_row(*[x if isinstance(x, Text) else str(x) for x in r])

    def load_research(self) -> None:
        if not os.path.exists(REPORT_PATH):
            return
        rep = json.load(open(REPORT_PATH))
        self._fill("r_test", [(v["name"], v["trades"], v["markets"], Text(f"{v['roi']:+.2%}", style="green" if v["roi"] > 0 else "red"),
                               f"[{v['roi_lo']:+.1%}, {v['roi_hi']:+.1%}]", f"{v['win_rate']:.1%}", f"{v['pnl']:+.0%}", f"{v['max_dd']:.0%}")
                              for v in rep.get("test", {}).values()])
        self._fill("r_base", [(b["name"], b["trades"], f"{b['roi']:+.2%}", f"{b['win_rate']:.1%}") for b in rep.get("baselines", [])])
        self._fill("r_cal", [(c["role"], c["bucket"], f"{c['markets']:.0f}", f"{c['avg_price']:.3f}", f"{c['win_rate']:.3f}",
                              f"{c['edge_pre_fee']:+.3f}", f"{c['fee']:.3f}",
                              Text(f"{c['roi_after_fee']:+.1%}", style="green" if c["roi_after_fee"] > 0 else "red"))
                             for c in rep.get("calibration", {}).get(rep.get("venue", "robinhood"), [])])


def main(autorun: bool = True) -> None:
    Terminal(autorun).run()
