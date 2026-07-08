"""Aggregate price ticks into fixed-interval OHLCV candles.

Keeps a bounded history (default 1,000 candles — enough for EMA200 plus
volatility percentile lookback) and exposes it as a pandas DataFrame for the
indicator engine. Candles can also be loaded directly for backtesting.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class Candle:
    start: float   # unix seconds, aligned to interval
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class CandleManager:
    def __init__(self, interval_seconds: int = 60, max_candles: int = 1000) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval = interval_seconds
        self._candles: deque[Candle] = deque(maxlen=max_candles)
        self._current: Optional[Candle] = None

    def __len__(self) -> int:
        return len(self._candles) + (1 if self._current else 0)

    def add_tick(self, timestamp: float, price: float, volume: float = 0.0) -> None:
        if price <= 0:
            raise ValueError(f"non-positive price tick: {price}")
        bucket = timestamp - (timestamp % self.interval)
        if self._current is None or bucket > self._current.start:
            if self._current is not None:
                self._candles.append(self._current)
            self._current = Candle(bucket, price, price, price, price, volume)
        elif bucket < self._current.start:
            return  # out-of-order tick from the past: ignore, never rewrite history
        else:
            c = self._current
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += volume

    def add_candle(self, start: float, open_: float, high: float, low: float,
                   close: float, volume: float = 0.0) -> None:
        """Load a completed candle directly (bootstrap / backtest)."""
        self._candles.append(Candle(start, open_, high, low, close, volume))

    def dataframe(self, include_current: bool = True) -> pd.DataFrame:
        rows = list(self._candles)
        if include_current and self._current is not None:
            rows.append(self._current)
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(
            {
                "open": [c.open for c in rows],
                "high": [c.high for c in rows],
                "low": [c.low for c in rows],
                "close": [c.close for c in rows],
                "volume": [c.volume for c in rows],
            },
            index=pd.to_datetime([c.start for c in rows], unit="s"),
        )
        return df
