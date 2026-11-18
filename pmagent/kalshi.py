"""Kalshi Trade API v2 client.

Robinhood's prediction markets list Kalshi contracts, and Robinhood has no
public API for event contracts, so Kalshi's API is the data source (and the
optional live execution venue). Public endpoints need no credentials.
Authenticated endpoints (portfolio / orders) use RSA-PSS request signing.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid
from typing import Any, Iterator

import requests

PROD = "https://api.elections.kalshi.com/trade-api/v2"
DEMO = "https://demo-api.kalshi.co/trade-api/v2"


class RateLimiter:
    def __init__(self, per_sec: float):
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_t:
                time.sleep(self.next_t - now)
            self.next_t = max(now, self.next_t) + self.interval


class KalshiClient:
    def __init__(self, base_url: str = PROD, key_id: str | None = None,
                 private_key_path: str | None = None, reads_per_sec: float = 8.0):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "pm-agent/0.1"
        self.rl = RateLimiter(reads_per_sec)
        self.key_id = key_id
        self._pkey = None
        if private_key_path:
            from cryptography.hazmat.primitives import serialization
            with open(private_key_path, "rb") as f:
                self._pkey = serialization.load_pem_private_key(f.read(), password=None)

    # ---------- transport ----------
    @property
    def authenticated(self) -> bool:
        return bool(self.key_id and self._pkey)

    def _sign_headers(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        full_path = "/trade-api/v2" + path.split("?")[0]
        msg = (ts + method.upper() + full_path).encode()
        sig = self._pkey.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def request(self, method: str, path: str, params: dict | None = None,
                json: dict | None = None, auth: bool = False, retries: int = 6) -> dict:
        url = self.base + path
        for attempt in range(retries):
            self.rl.wait()
            headers = self._sign_headers(method, path) if auth else {}
            try:
                r = self.s.request(method, url, params=params, json=json, headers=headers, timeout=30)
            except requests.RequestException:
                time.sleep(min(2 ** attempt, 20))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(1.5 * 2 ** attempt, 30))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise RuntimeError(f"Kalshi {method} {path} failed after {retries} retries")

    def get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        return self.request("GET", path, params=params)

    def paginate(self, path: str, key: str, max_pages: int = 1000, **params) -> Iterator[dict]:
        cursor = None
        for _ in range(max_pages):
            data = self.get(path, cursor=cursor, **params)
            for item in data.get(key, []) or []:
                yield item
            cursor = data.get("cursor")
            if not cursor:
                break

    # ---------- public market data ----------
    def exchange_status(self) -> dict:
        return self.get("/exchange/status")

    def markets(self, **params) -> Iterator[dict]:
        params.setdefault("limit", 1000)
        params.setdefault("mve_filter", "exclude")
        return self.paginate("/markets", "markets", **params)

    def market(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}").get("market", {})

    def event(self, event_ticker: str, nested: bool = True) -> dict:
        return self.get(f"/events/{event_ticker}", with_nested_markets=str(nested).lower())

    def events(self, **params) -> Iterator[dict]:
        params.setdefault("limit", 200)
        return self.paginate("/events", "events", **params)

    def series_list(self) -> list[dict]:
        return self.get("/series").get("series", []) or []

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self.get(f"/markets/{ticker}/orderbook", depth=depth).get("orderbook_fp", {})

    def trades(self, ticker: str | None = None, limit: int = 1000, max_pages: int = 1,
               historical: bool = False, **params) -> list[dict]:
        path = "/historical/trades" if historical else "/markets/trades"
        return list(self.paginate(path, "trades", max_pages=max_pages, ticker=ticker, limit=limit, **params))

    # ---------- authenticated: portfolio & orders ----------
    def balance(self) -> dict:
        return self.request("GET", "/portfolio/balance", auth=True)

    def positions(self) -> dict:
        return self.request("GET", "/portfolio/positions", params={"limit": 1000}, auth=True)

    def fills(self, limit: int = 200) -> dict:
        return self.request("GET", "/portfolio/fills", params={"limit": limit}, auth=True)

    def place_order(self, ticker: str, side: str, count: int, price_cents: int,
                    action: str = "buy", tif: str = "immediate_or_cancel",
                    client_order_id: str | None = None, post_only: bool = False,
                    expiration_ts: int | None = None) -> dict:
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": int(count),
            "type": "limit",
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "time_in_force": tif,
        }
        body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
        if post_only:
            body["post_only"] = True
        if expiration_ts:
            body["expiration_ts"] = int(expiration_ts)
        return self.request("POST", "/portfolio/orders", json=body, auth=True)

    def cancel_order(self, order_id: str) -> dict:
        return self.request("DELETE", f"/portfolio/orders/{order_id}", auth=True)


def f(x: Any, default: float = 0.0) -> float:
    """Parse Kalshi's string dollar / fixed-point fields."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def series_of(event_ticker: str) -> str:
    return event_ticker.split("-")[0]
