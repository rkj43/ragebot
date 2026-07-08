"""Strategy 3 — defensive mode. Active in HIGH_VOLATILITY and BEAR_TREND.

This strategy never opens trades. Holding a position, it exits toward USDC;
flat, it holds. Spot-only means bear markets and volatility spikes are
survived in the quote currency, not traded.
"""

from __future__ import annotations

from typing import Optional

from solana_trading_bot.domain import (
    Action, IndicatorSet, MarketSnapshot, Position, Regime, Signal,
)
from solana_trading_bot.strategies.base_strategy import BaseStrategy


class DefensiveStrategy(BaseStrategy):
    active_regimes = (Regime.HIGH_VOLATILITY, Regime.BEAR_TREND)

    def evaluate(
        self,
        indicators: IndicatorSet,
        snapshot: MarketSnapshot,
        position: Optional[Position],
    ) -> Signal:
        if position is not None:
            return Signal(
                action=Action.EXIT,
                strategy=self.name,
                reason="defensive mode: reducing exposure, moving to USDC",
            )
        return Signal.hold(self.name, "defensive mode: no new trades")
