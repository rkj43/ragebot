"""Birdeye historical OHLCV client — warmup bootstrap only.

Used for exactly one thing: fetching the ~200 one-minute candles the
indicator engine needs, once, at startup, so the bot doesn't spend 3+ hours
rebuilding history after every restart. The live feed remains Jupiter; this
client is never polled during trading. Failure here is non-fatal — the bot
falls back to the slow warmup.

Free-tier friendly: a single request per bot start (limit is 1 req/s and
30k compute units/month).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BirdeyeError(Exception):
    pass


class BirdeyeClient:
    def __init__(self, api_key: str,
                 base_url: str = "https://public-api.birdeye.so",
                 timeout_s: float = 20.0) -> None:
        if not api_key:
            raise BirdeyeError("Birdeye API key is empty")
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            headers={"X-API-KEY": api_key, "x-chain": "solana",
                     "accept": "application/json"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get_ohlcv_1m(self, mint: str, minutes: int,
                           now: Optional[float] = None) -> list[dict]:
        """Return 1-minute candles for the last ``minutes`` minutes, oldest
        first, as dicts with keys: start, open, high, low, close, volume."""
        now = now if now is not None else time.time()
        params = {
            "address": mint,
            "type": "1m",
            "time_from": int(now - minutes * 60),
            "time_to": int(now),
        }
        try:
            resp = await self._http.get(f"{self._base_url}/defi/ohlcv", params=params)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise BirdeyeError(f"OHLCV request failed: {exc}") from exc

        if not payload.get("success"):
            raise BirdeyeError(f"OHLCV response not successful: {payload}")
        items = (payload.get("data") or {}).get("items") or []
        candles = []
        for it in items:
            try:
                candles.append({
                    "start": float(it["unixTime"]),
                    "open": float(it["o"]),
                    "high": float(it["h"]),
                    "low": float(it["l"]),
                    "close": float(it["c"]),
                    "volume": float(it.get("v") or 0.0),
                })
            except (KeyError, TypeError, ValueError) as exc:
                raise BirdeyeError(f"malformed candle in response: {it}") from exc
        candles.sort(key=lambda c: c["start"])
        return candles
