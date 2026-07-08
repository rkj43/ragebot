"""Technical indicators.

Implemented directly on pandas/numpy (Wilder's RSI/ATR, standard EMA and
Bollinger Bands) so values are deterministic and auditable. Two entry points:

* ``compute_indicator_frame(df)`` — vectorized, adds indicator columns to an
  OHLCV frame (used by the backtester);
* ``IndicatorEngine.compute(df)`` — returns the latest ``IndicatorSet`` with a
  ``ready`` flag that stays False until enough history exists (EMA200 warmup).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from solana_trading_bot.domain import IndicatorSet

WARMUP_CANDLES = 200          # EMA200 needs this much history to be meaningful
VOL_PERCENTILE_LOOKBACK = 200 # candles used to rank current volatility


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(avg_loss.ne(0) | avg_gain.ne(0), 50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    return mid + num_std * std, mid, mid - num_std * std


def volatility_percentile(close: pd.Series, window: int = 20,
                          lookback: int = VOL_PERCENTILE_LOOKBACK) -> pd.Series:
    vol = close.pct_change().rolling(window).std()
    return vol.rolling(lookback, min_periods=window * 2).rank(pct=True)


def compute_indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with all indicator columns appended."""
    out = df.copy()
    close = out["close"]
    out["ema50"] = ema(close, 50)
    out["ema200"] = ema(close, 200)
    out["rsi"] = rsi(close)
    out["atr"] = atr(out)
    out["atr_pct"] = out["atr"] / close
    out["atr_sma"] = out["atr"].rolling(50).mean()
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bollinger(close)
    out["volume_sma"] = out["volume"].rolling(20).mean()
    out["vol_pctile"] = volatility_percentile(close)
    out["return_5"] = close.pct_change(5)
    return out


def indicator_set_from_row(row: pd.Series, ready: bool, timestamp: float) -> IndicatorSet:
    return IndicatorSet(
        ready=ready,
        timestamp=timestamp,
        price=float(row["close"]),
        ema50=float(row["ema50"]),
        ema200=float(row["ema200"]),
        rsi=float(row["rsi"]),
        atr=float(row["atr"]),
        atr_pct=float(row["atr_pct"]),
        atr_sma=float(row["atr_sma"]) if pd.notna(row["atr_sma"]) else float(row["atr"]),
        bb_upper=float(row["bb_upper"]),
        bb_mid=float(row["bb_mid"]),
        bb_lower=float(row["bb_lower"]),
        volume=float(row["volume"]),
        volume_sma=float(row["volume_sma"]) if pd.notna(row["volume_sma"]) else 0.0,
        volatility_percentile=float(row["vol_pctile"]) if pd.notna(row["vol_pctile"]) else 0.5,
        return_5=float(row["return_5"]) if pd.notna(row["return_5"]) else 0.0,
    )


class IndicatorEngine:
    """Computes the latest ``IndicatorSet`` from a candle DataFrame."""

    def compute(self, df: pd.DataFrame) -> IndicatorSet:
        if len(df) < WARMUP_CANDLES:
            return IndicatorSet(ready=False)
        frame = compute_indicator_frame(df)
        last = frame.iloc[-1]
        if last[["ema50", "ema200", "rsi", "atr", "bb_mid"]].isna().any():
            return IndicatorSet(ready=False)
        ts = frame.index[-1].timestamp() if hasattr(frame.index[-1], "timestamp") else 0.0
        return indicator_set_from_row(last, ready=True, timestamp=ts)
