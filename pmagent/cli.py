"""pm - command line entry point."""
from __future__ import annotations

import argparse
import os
import sys

from .config import DATA, Config


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="pm", description="Prediction-market trading agent (Kalshi data, Robinhood/Kalshi/paper execution)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("research", help="collect settled-market data and/or fit + backtest the edge model")
    r.add_argument("step", choices=["collect", "analyze", "all"], nargs="?", default="all")
    r.add_argument("--markets", type=int, default=9000, help="markets to pull trades for")
    r.add_argument("--per-series", type=int, default=25)
    t = sub.add_parser("tui", help="open the trading terminal (agent runs inside it)")
    t.add_argument("--no-autorun", action="store_true", help="don't start the auto-trading loop")
    sub.add_parser("run", help="run the agent headless (logs to data/agent.db)")
    sub.add_parser("scan", help="one cycle, print signals, place nothing")
    sub.add_parser("status", help="print account / P&L summary")
    c = sub.add_parser("cmd", help="run a single terminal command, e.g. pm cmd quote KXHIGHNY-26NOV16-B52.5")
    c.add_argument("line", nargs=argparse.REMAINDER)
    sub.add_parser("init", help="write a default data/config.json")
    a = p.parse_args(argv)
    os.makedirs(DATA, exist_ok=True)

    if a.cmd == "init":
        Config.load().save()
        print(f"wrote {os.path.join(DATA, 'config.json')}")
    elif a.cmd == "research":
        db = os.path.join(DATA, "research.db")
        if a.step in ("collect", "all"):
            from . import research
            research.run(db, per_series=a.per_series, total=a.markets)
        if a.step in ("analyze", "all"):
            from . import analysis
            analysis.run(db, Config.load().fee_venue)
    elif a.cmd == "tui":
        from .tui import main as tui
        tui(not a.no_autorun)
    elif a.cmd == "run":
        from .agent import Agent
        ag = Agent()
        print(f"agent running in {ag.cfg.mode} mode, every {ag.cfg.scan_interval_sec}s. Ctrl-C to stop.")
        ag.run_forever(on_cycle=lambda s: print(s, flush=True))
    elif a.cmd == "scan":
        from .agent import Agent
        ag = Agent()
        ag.paused = True
        s = ag.cycle()
        print(s)
        for sg in ag.last_signals[:60]:
            print(f"{sg.strategy:11} {sg.ticker:42} {sg.side:3} @{sg.price:.2f}  p={sg.q:.3f}  EV/$ {sg.ev:+.1%}  {sg.note}")
    elif a.cmd == "status":
        from .agent import Agent
        ag = Agent()
        print(ag.db.stats(), f"equity={ag.risk.equity():.2f} cash={ag.risk.cash():.2f} exposure={ag.db.exposure():.2f}")
    elif a.cmd == "cmd":
        from .agent import Agent
        from .commands import run_command
        print(run_command(Agent(), " ".join(a.line)))


if __name__ == "__main__":
    main(sys.argv[1:])
