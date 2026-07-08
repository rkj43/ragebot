"""Strategy 1 — trend following. Active only in BULL_TREND.

Entry: EMA50 above EMA200, RSI between 40 and 65 (established trend, not
overbought), and volume above its 20-period average. Exit when EMA50 crosses
back below EMA200. Stops, take profit, and the trailing ATR stop are enforced
by the risk engine using the levels attached to the signal.
"""

from __future__ import annotations

from typing import Optional

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import (
    Action, IndicatorSet, MarketSnapshot, Position, Regime, Signal,
)
from solana_trading_bot.strategies.base_strategy import BaseStrategy

RSI_MIN = 40.0
RSI_MAX = 65.0


class TrendFollowingStrategy(BaseStrategy):
    active_regimes = (Regime.BULL_TREND,)

    def evaluate(
        self,
        indicators: IndicatorSet,
        snapshot: MarketSnapshot,
        position: Optional[Position],
    ) -> Signal:
        if not indicators.ready:
            return Signal.hold(self.name, "indicators not ready")

        if position is not None:
            if indicators.ema50 < indicators.ema200:
                return Signal(
                    action=Action.EXIT, strategy=self.name,
                    reason="EMA50 crossed below EMA200",
                )
            return Signal.hold(self.name, "holding: trend intact")

        if indicators.ema50 <= indicators.ema200:
            return Signal.hold(self.name, "EMA50 not above EMA200")
        if not RSI_MIN <= indicators.rsi <= RSI_MAX:
            return Signal.hold(
                self.name, f"RSI {indicators.rsi:.1f} outside [{RSI_MIN}, {RSI_MAX}]"
            )
        if indicators.volume_sma <= 0 or indicators.volume <= indicators.volume_sma:
            return Signal.hold(self.name, "volume not increasing")

        price = snapshot.price
        return Signal(
            action=Action.BUY,
            strategy=self.name,
            reason=(
                f"bull trend entry: EMA50>EMA200, RSI {indicators.rsi:.1f}, "
                f"volume {indicators.volume:.0f} > avg {indicators.volume_sma:.0f}"
            ),
            stop_price=price - cfg.STOP_ATR_MULT * indicators.atr,
            take_profit=price + cfg.TAKE_PROFIT_ATR_MULT * indicators.atr,
        )
