"""Text commands understood by the terminal (and `pm cmd ...`)."""
from __future__ import annotations

import os

from .broker import make_broker
from .fees import fee_per_contract
from .kalshi import f
from .risk import KILL_FILE
from .strategies.base import Signal

HELP = """commands:
  scan                         run one agent cycle now
  pause | resume               stop / restart automatic order placement
  kill | unkill                hard kill switch (blocks all new orders, survives restarts)
  quote <TICKER>               live bid/ask, model probability and edge for both sides
  buy <TICKER> <yes|no> <N> [price]   manual trade through the current broker
  close <ID>                   sell an open position into the bid
  fill <ID> [price] [count]    confirm a Robinhood ticket you executed in the app
  reject <ID>                  discard a Robinhood ticket you did not execute
  set <key> <value>            change a config value (e.g. set min_edge 0.05) and save
  mode <paper|robinhood|kalshi>  switch execution back-end
  config                       show current config
  help"""


def _num(s):
    try:
        return float(s)
    except ValueError:
        return s


def run_command(agent, line: str) -> str:
    parts = line.strip().split()
    if not parts:
        return ""
    cmd, args = parts[0].lower(), parts[1:]
    cfg, db = agent.cfg, agent.db
    try:
        if cmd in ("help", "?"):
            return HELP
        if cmd == "scan":
            s = agent.cycle()
            return f"cycle done: {s['signals']} signals, {s['orders']} orders, {s['settled']} settled"
        if cmd == "pause":
            agent.paused = True
            return "paused: agent keeps scanning and settling but places no orders"
        if cmd == "resume":
            agent.paused = False
            return "resumed"
        if cmd == "kill":
            open(KILL_FILE, "w").write("killed from terminal\n")
            return "KILL SWITCH ON - no new orders. `unkill` to release."
        if cmd == "unkill":
            if os.path.exists(KILL_FILE):
                os.remove(KILL_FILE)
            return "kill switch released"
        if cmd == "quote":
            return quote(agent, args[0].upper())
        if cmd == "buy":
            ticker, side, n = args[0].upper(), args[1].lower(), int(args[2])
            m = agent.client.market(ticker)
            price = float(args[3]) if len(args) > 3 else f(m.get(f"{side}_ask_dollars"))
            if not 0 < price < 1:
                return f"no {side} ask for {ticker}"
            from .agent import Context
            ctx = Context(cfg, agent.client, agent.series, [m])
            ft, mult = ctx.fee_info(m)
            fee = fee_per_contract(price, cfg.fee_venue, True, ft, mult, cfg.rh_commission)
            sig = Signal("manual", ticker, m["event_ticker"], m.get("title", ""), ctx.category(m), side, price,
                         q=0, fee=fee, ev=0, max_count=n, fee_type=ft, fee_mult=mult, note="manual")
            if agent.risk.halted():
                return f"blocked: {agent.risk.halted()}"
            oid = agent.broker.buy(sig, n)
            return f"order #{oid}: {n} {ticker} {side.upper()} @ {price:.2f} ({agent.broker.mode})" if oid else "order not filled"
        if cmd == "close":
            return agent.broker.close(int(args[0]))
        if cmd in ("fill", "reject"):
            if agent.broker.mode != "robinhood":
                return "tickets only exist in robinhood mode"
            if cmd == "reject":
                return agent.broker.reject(int(args[0]))
            price = float(args[1]) if len(args) > 1 else None
            count = float(args[2]) if len(args) > 2 else None
            return agent.broker.confirm(int(args[0]), price, count)
        if cmd == "set":
            key, val = args[0], _num(" ".join(args[1:]))
            if not hasattr(cfg, key) or key.startswith("kalshi_key"):
                return f"unknown config key {key}"
            if isinstance(getattr(cfg, key), list):
                val = [v for v in str(val).split(",") if v]
            setattr(cfg, key, val)
            cfg.save()
            return f"{key} = {val!r} (saved)"
        if cmd == "mode":
            cfg.mode = args[0]
            agent.broker = make_broker(db, agent.client, cfg)
            cfg.save()
            return f"execution mode -> {cfg.mode}"
        if cmd == "config":
            return "\n".join(f"  {k} = {v!r}" for k, v in vars(cfg).items() if not k.startswith("kalshi_key"))
        return f"unknown command '{cmd}' (try help)"
    except (IndexError, ValueError) as e:
        return f"bad arguments ({e}); try help"
    except RuntimeError as e:
        return f"error: {e}"


def quote(agent, ticker: str) -> str:
    from .agent import Context
    m = agent.client.market(ticker)
    if not m:
        return "market not found"
    ctx = Context(agent.cfg, agent.client, agent.series, [m])
    cat, hrs = ctx.category(m), ctx.hours_to_close(m)
    ft, mult = ctx.fee_info(m)
    lines = [f"{ticker}  {m.get('title','')}", f"  category {cat} | closes in {hrs:.1f}h | status {m.get('status')}"]
    for side in ("yes", "no"):
        bid, ask = f(m.get(f"{side}_bid_dollars")), f(m.get(f"{side}_ask_dollars"))
        line = f"  {side.upper():3} bid {bid:.2f}  ask {ask:.2f}"
        if agent.model and 0 < ask < 1:
            q, neff = agent.model.predict("taker", side, ask, cat, hrs)
            fee = fee_per_contract(ask, agent.cfg.fee_venue, True, ft, mult, agent.cfg.rh_commission)
            line += f"  | model p(win) {q:.3f}  fee {fee:.3f}  EV/$ {(q-ask-fee)/(ask+fee):+.1%}  (n={neff:.0f})"
        lines.append(line)
    return "\n".join(lines)
