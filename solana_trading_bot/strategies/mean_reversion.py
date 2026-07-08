"""Strategy 2 — mean reversion. Active only in SIDEWAYS regimes.

Entry: price closes below the lower Bollinger Band with RSI under 35
(stretched, quiet market). Exit: price reverts to the middle band — the take
profit is set at the band midline, and the ATR stop below protects against
the range breaking down.
"""

from __future__ import annotations

from typing import Optional

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import (
    Action, IndicatorSet, MarketSnapshot, Position, Regime, Signal,
)
from solana_trading_bot.strategies.base_strategy import BaseStrategy

RSI_OVERSOLD = 35.0


class MeanReversionStrategy(BaseStrategy):
    active_regimes = (Regime.SIDEWAYS,)

    def evaluate(
        self,
        indicators: IndicatorSet,
        snapshot: MarketSnapshot,
        position: Optional[Position],
    ) -> Signal:
        if not indicators.ready:
            return Signal.hold(self.name, "indicators not ready")

        price = snapshot.price

        if position is not None:
            if price >= indicators.bb_mid:
                return Signal(
                    action=Action.EXIT, strategy=self.name,
                    reason=f"price {price:.4f} reverted to moving average {indicators.bb_mid:.4f}",
                )
            return Signal.hold(self.name, "holding: awaiting reversion to mean")

        if price >= indicators.bb_lower:
            return Signal.hold(self.name, "price not below lower Bollinger Band")
        if indicators.rsi >= RSI_OVERSOLD:
            return Signal.hold(self.name, f"RSI {indicators.rsi:.1f} not oversold")

        return Signal(
            action=Action.BUY,
            strategy=self.name,
            reason=(
                f"mean reversion entry: price {price:.4f} below BB lower "
                f"{indicators.bb_lower:.4f}, RSI {indicators.rsi:.1f}"
            ),
            stop_price=price - cfg.STOP_ATR_MULT * indicators.atr,
            take_profit=indicators.bb_mid,
        )
