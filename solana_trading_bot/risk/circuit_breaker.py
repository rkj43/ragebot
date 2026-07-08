"""Layer 4 — daily loss circuit breaker.

Tracks equity against the day's starting equity (UTC days). At a 3% daily
loss the breaker trips: new trades stop, open positions are flagged for
closing, and the bot enters SLEEP_MODE. SLEEP_MODE never clears on its own —
``manual_restart()`` must be called by a human. Sleeping through a rollover
does not wake the bot.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from solana_trading_bot.config import settings as cfg

logger = logging.getLogger(__name__)


class BreakerState(str, Enum):
    ACTIVE = "ACTIVE"
    SLEEP_MODE = "SLEEP_MODE"


class CircuitBreaker:
    def __init__(self, event_recorder=None) -> None:
        self._record = event_recorder  # callable(event, details) or None
        self.state = BreakerState.ACTIVE
        self._day: Optional[str] = None
        self._day_start_equity: Optional[float] = None
        self.trip_reason: Optional[str] = None

    @staticmethod
    def _day_key(now: float) -> str:
        return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    def daily_loss_pct(self, equity: float) -> float:
        if not self._day_start_equity:
            return 0.0
        return max(0.0, (self._day_start_equity - equity) / self._day_start_equity)

    def check(self, equity: float, now: Optional[float] = None) -> BreakerState:
        """Called at least every 5 minutes and before every trade."""
        now = now if now is not None else time.time()
        day = self._day_key(now)
        if day != self._day:
            self._day = day
            self._day_start_equity = equity
            # A new day never auto-clears SLEEP_MODE: manual restart required.

        if self.state is BreakerState.SLEEP_MODE:
            return self.state

        loss = self.daily_loss_pct(equity)
        if loss >= cfg.DAILY_LOSS_LIMIT_PCT:
            self._trip(f"daily loss {loss:.2%} >= limit {cfg.DAILY_LOSS_LIMIT_PCT:.0%}")
        return self.state

    def _trip(self, reason: str) -> None:
        self.state = BreakerState.SLEEP_MODE
        self.trip_reason = reason
        logger.critical("CIRCUIT_BREAKER_TRIGGERED: %s — entering SLEEP_MODE, "
                        "manual restart required", reason)
        if self._record is not None:
            self._record("CIRCUIT_BREAKER_TRIGGERED", reason)

    @property
    def allows_new_trades(self) -> bool:
        return self.state is BreakerState.ACTIVE

    @property
    def must_close_positions(self) -> bool:
        return self.state is BreakerState.SLEEP_MODE

    def manual_restart(self, equity: float, now: Optional[float] = None) -> None:
        """Human-initiated reset. The new day baseline is current equity."""
        now = now if now is not None else time.time()
        self.state = BreakerState.ACTIVE
        self.trip_reason = None
        self._day = self._day_key(now)
        self._day_start_equity = equity
        logger.warning("Circuit breaker manually restarted; baseline equity %.2f", equity)
        if self._record is not None:
            self._record("CIRCUIT_BREAKER_RESET", f"manual restart, equity {equity:.2f}")
