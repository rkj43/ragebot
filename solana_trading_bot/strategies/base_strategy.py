"""Strategy contract and regime-based routing.

Strategies are pure decision functions: given indicators, the latest market
snapshot and the current position, they emit a ``Signal``. They never size
positions, never touch execution, and never see the wallet — those powers
belong to the risk and execution layers. HOLD is always a valid output, and
an unmatched regime (UNKNOWN, or no strategy registered) routes to HOLD.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional, Sequence

from solana_trading_bot.domain import (
    IndicatorSet,
    MarketSnapshot,
    Position,
    Regime,
    RegimeState,
    Signal,
)


class BaseStrategy(ABC):
    #: regimes in which this strategy is allowed to act
    active_regimes: tuple[Regime, ...] = ()

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def evaluate(
        self,
        indicators: IndicatorSet,
        snapshot: MarketSnapshot,
        position: Optional[Position],
    ) -> Signal:
        """Return a trading decision. Must not raise on odd inputs — return HOLD."""


class StrategyRouter:
    """Maps the detected regime to the single strategy allowed to act in it."""

    def __init__(self, strategies: Sequence[BaseStrategy]) -> None:
        self._by_regime: Dict[Regime, BaseStrategy] = {}
        for strat in strategies:
            for regime in strat.active_regimes:
                if regime in self._by_regime:
                    raise ValueError(
                        f"Both {self._by_regime[regime].name} and {strat.name} "
                        f"claim regime {regime}; routing must be unambiguous."
                    )
                self._by_regime[regime] = strat

    def route(self, regime_state: RegimeState) -> Optional[BaseStrategy]:
        return self._by_regime.get(regime_state.regime)

    def decide(
        self,
        regime_state: RegimeState,
        indicators: IndicatorSet,
        snapshot: MarketSnapshot,
        position: Optional[Position],
    ) -> Signal:
        strategy = self.route(regime_state)
        if strategy is None:
            return Signal.hold(
                "router",
                f"no strategy active in regime {regime_state.regime.value} "
                f"({regime_state.reason})",
            )
        return strategy.evaluate(indicators, snapshot, position)
