"""Market data feed with staleness protection.

The feed maintains the latest ``MarketSnapshot`` and feeds the heartbeat
monitor. Consumers must call ``snapshot()`` and check ``is_stale()`` — a feed
that stops updating is treated exactly like a dead RPC: trading halts.

In live/paper mode the feed polls Jupiter's price API. In tests and backtests
snapshots are injected via ``ingest()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import MarketSnapshot

logger = logging.getLogger(__name__)


class PriceFeed:
    def __init__(self, jupiter, base_mint: str, quote_mint: str,
                 heartbeat=None, poll_interval_s: float = 2.0) -> None:
        self._jupiter = jupiter
        self._base_mint = base_mint
        self._quote_mint = quote_mint
        self._heartbeat = heartbeat
        self._poll_interval_s = poll_interval_s
        self._latest: Optional[MarketSnapshot] = None
        self._consecutive_failures = 0

    def snapshot(self) -> Optional[MarketSnapshot]:
        return self._latest

    def is_stale(self, now: Optional[float] = None) -> bool:
        if self._latest is None:
            return True
        return self._latest.age(now) > cfg.MAX_DATA_AGE_SECONDS

    def ingest(self, snapshot: MarketSnapshot) -> None:
        """Inject a snapshot (backtests, tests, alternative feeds)."""
        self._latest = snapshot
        if self._heartbeat is not None:
            self._heartbeat.beat_data(snapshot.timestamp)

    async def poll_once(self) -> Optional[MarketSnapshot]:
        try:
            price = await self._jupiter.get_price(self._base_mint, self._quote_mint)
            snap = MarketSnapshot(timestamp=time.time(), price=price, source="jupiter")
            self.ingest(snap)
            self._consecutive_failures = 0
            return snap
        except Exception as exc:  # noqa: BLE001 — count, report, keep looping
            self._consecutive_failures += 1
            logger.warning("Price poll failed (%d consecutive): %s",
                           self._consecutive_failures, exc)
            if self._consecutive_failures >= 3 and self._heartbeat is not None:
                self._heartbeat.report_feed_failure(str(exc))
            return None

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll until asked to stop. Failures degrade to a halt, never a crash."""
        while not stop_event.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_s)
            except asyncio.TimeoutError:
                pass
