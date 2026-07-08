"""Performance metrics for backtests and live review.

All ratios are computed from the equity curve's periodic returns; annualization
assumes the candle interval passed in. Degenerate inputs (no trades, zero
variance) return 0.0 rather than NaN so reports stay readable — but the trade
count is always reported alongside, so an empty backtest can't masquerade as
a safe one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class PerformanceReport:
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    win_rate_pct: float
    profit_factor: float
    num_trades: int
    failed_transactions: int = 0

    def to_dict(self) -> dict:
        return {k: round(float(v), 4) if not isinstance(v, int) else v
                for k, v in asdict(self).items()}


def max_drawdown(equity: Sequence[float]) -> float:
    arr = np.asarray(equity, dtype=float)
    if arr.size < 2:
        return 0.0
    peaks = np.maximum.accumulate(arr)
    drawdowns = (peaks - arr) / peaks
    return float(np.max(drawdowns))


def _annualization_factor(candle_interval_seconds: int) -> float:
    periods_per_year = (365 * 24 * 3600) / max(candle_interval_seconds, 1)
    return float(np.sqrt(periods_per_year))


def sharpe_ratio(returns: np.ndarray, candle_interval_seconds: int) -> float:
    if returns.size < 2 or np.std(returns, ddof=1) == 0:
        return 0.0
    return float(np.mean(returns) / np.std(returns, ddof=1)
                 * _annualization_factor(candle_interval_seconds))


def sortino_ratio(returns: np.ndarray, candle_interval_seconds: int) -> float:
    if returns.size < 2:
        return 0.0
    downside = returns[returns < 0]
    if downside.size == 0:
        return float("inf") if np.mean(returns) > 0 else 0.0
    downside_dev = np.sqrt(np.mean(downside**2))
    if downside_dev == 0:
        return 0.0
    return float(np.mean(returns) / downside_dev
                 * _annualization_factor(candle_interval_seconds))


def compute_report(
    equity_curve: Sequence[float],
    trade_pnls: Sequence[float],
    candle_interval_seconds: int = 60,
    failed_transactions: int = 0,
) -> PerformanceReport:
    equity = np.asarray(equity_curve, dtype=float)
    pnls = np.asarray(trade_pnls, dtype=float)

    total_return = float(equity[-1] / equity[0] - 1.0) if equity.size >= 2 and equity[0] > 0 else 0.0
    returns = np.diff(equity) / equity[:-1] if equity.size >= 2 else np.array([])

    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_rate = (wins.size / pnls.size * 100) if pnls.size else 0.0
    gross_loss = float(np.abs(losses).sum())
    profit_factor = float(wins.sum() / gross_loss) if gross_loss > 0 else (
        float("inf") if wins.size else 0.0)

    return PerformanceReport(
        total_return_pct=total_return * 100,
        max_drawdown_pct=max_drawdown(equity) * 100,
        sharpe_ratio=sharpe_ratio(returns, candle_interval_seconds),
        sortino_ratio=sortino_ratio(returns, candle_interval_seconds),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        num_trades=int(pnls.size),
        failed_transactions=failed_transactions,
    )
