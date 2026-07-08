"""Monte Carlo robustness testing.

A single backtest is one draw from a distribution. Here we bootstrap the
sequence of trade P&Ls (sampling with replacement, seeded) to answer:

* does performance survive a different trade *order*?
* how deep do drawdowns get across thousands of alternate histories?
* what is the probability of hitting a ruin threshold?
* does the edge survive systematically *worse execution* (extra cost per trade)?

If the median path barely survives, the strategy does not go live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from solana_trading_bot.backtesting.metrics import max_drawdown


@dataclass(frozen=True)
class MonteCarloReport:
    n_simulations: int
    n_trades_per_path: int
    median_return_pct: float
    p5_return_pct: float          # 5th percentile — a bad-luck outcome
    p95_return_pct: float
    median_max_drawdown_pct: float
    worst_max_drawdown_pct: float
    prob_ruin_pct: float          # probability of breaching the ruin threshold
    ruin_threshold_pct: float

    def to_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v
                for k, v in self.__dict__.items()}


class MonteCarloTester:
    def __init__(self, starting_equity: float = 1_000.0,
                 n_simulations: int = 2_000, seed: int = 7) -> None:
        self.starting_equity = starting_equity
        self.n_simulations = n_simulations
        self._rng = np.random.default_rng(seed)

    def run(
        self,
        trade_pnls: Sequence[float],
        ruin_threshold_pct: float = 0.20,
        extra_cost_per_trade: float = 0.0,
    ) -> MonteCarloReport:
        """Bootstrap trade sequences. ``extra_cost_per_trade`` (quote units)
        stresses execution: every resampled trade is degraded by that amount."""
        pnls = np.asarray(trade_pnls, dtype=float)
        if pnls.size < 5:
            raise ValueError("need at least 5 trades for a meaningful Monte Carlo run")
        pnls = pnls - extra_cost_per_trade

        n = pnls.size
        finals = np.empty(self.n_simulations)
        drawdowns = np.empty(self.n_simulations)
        ruined = 0
        ruin_equity = self.starting_equity * (1.0 - ruin_threshold_pct)

        for i in range(self.n_simulations):
            sample = self._rng.choice(pnls, size=n, replace=True)
            equity = self.starting_equity + np.cumsum(sample)
            equity = np.concatenate(([self.starting_equity], equity))
            finals[i] = equity[-1]
            drawdowns[i] = max_drawdown(np.maximum(equity, 1e-9))
            if equity.min() <= ruin_equity:
                ruined += 1

        returns_pct = (finals / self.starting_equity - 1.0) * 100
        return MonteCarloReport(
            n_simulations=self.n_simulations,
            n_trades_per_path=n,
            median_return_pct=float(np.median(returns_pct)),
            p5_return_pct=float(np.percentile(returns_pct, 5)),
            p95_return_pct=float(np.percentile(returns_pct, 95)),
            median_max_drawdown_pct=float(np.median(drawdowns) * 100),
            worst_max_drawdown_pct=float(np.max(drawdowns) * 100),
            prob_ruin_pct=ruined / self.n_simulations * 100,
            ruin_threshold_pct=ruin_threshold_pct * 100,
        )

    def losing_streak_stats(self, trade_pnls: Sequence[float]) -> dict:
        """Distribution of the longest losing streak across resampled paths."""
        pnls = np.asarray(trade_pnls, dtype=float)
        streaks = np.empty(self.n_simulations)
        for i in range(self.n_simulations):
            sample = self._rng.choice(pnls, size=pnls.size, replace=True)
            longest = current = 0
            for pnl in sample:
                current = current + 1 if pnl < 0 else 0
                longest = max(longest, current)
            streaks[i] = longest
        return {
            "median_longest_losing_streak": float(np.median(streaks)),
            "p95_longest_losing_streak": float(np.percentile(streaks, 95)),
            "max_longest_losing_streak": float(np.max(streaks)),
        }
