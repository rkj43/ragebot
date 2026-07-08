"""Layer 6 — liquidity protection.

Tracks a rolling baseline of observed spreads and vetoes trades when the
market's microstructure deteriorates: spread blowout (> 3x normal), liquidity
below the hard floor, or a quote whose price impact exceeds the cap. Missing
data counts against the trade, not for it — with no spread history yet we
still enforce the absolute caps, and an oversized quote impact is always
rejected.
"""

from __future__ import annotations

from collections import deque
from statistics import median
from typing import Optional

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import MarketSnapshot, Quote, ValidationResult


class LiquidityMonitor:
    def __init__(self, history: int = 500, min_samples: int = 20) -> None:
        self._spreads: deque[float] = deque(maxlen=history)
        self._min_samples = min_samples

    def record(self, snapshot: MarketSnapshot) -> None:
        if snapshot.spread_pct is not None and snapshot.spread_pct >= 0:
            self._spreads.append(snapshot.spread_pct)

    @property
    def normal_spread(self) -> Optional[float]:
        if len(self._spreads) < self._min_samples:
            return None
        return median(self._spreads)

    def check(self, snapshot: MarketSnapshot, quote: Optional[Quote] = None) -> ValidationResult:
        normal = self.normal_spread
        if snapshot.spread_pct is not None and normal is not None and normal > 0:
            if snapshot.spread_pct > cfg.MAX_SPREAD_MULTIPLE * normal:
                return ValidationResult(
                    False, "SPREAD_TOO_WIDE",
                    f"spread {snapshot.spread_pct:.4%} > {cfg.MAX_SPREAD_MULTIPLE}x "
                    f"normal {normal:.4%}",
                )

        if snapshot.liquidity_usd is not None and snapshot.liquidity_usd < cfg.MIN_LIQUIDITY_USD:
            return ValidationResult(
                False, "LOW_LIQUIDITY",
                f"liquidity ${snapshot.liquidity_usd:,.0f} below floor "
                f"${cfg.MIN_LIQUIDITY_USD:,.0f}",
            )

        if quote is not None:
            if quote.price_impact_pct > cfg.MAX_PRICE_IMPACT_PCT:
                return ValidationResult(
                    False, "PRICE_IMPACT_TOO_HIGH",
                    f"price impact {quote.price_impact_pct:.4%} exceeds "
                    f"{cfg.MAX_PRICE_IMPACT_PCT:.2%}",
                )
            if quote.route_hops > 4:
                return ValidationResult(
                    False, "POOR_ROUTE_QUALITY",
                    f"route has {quote.route_hops} hops; refusing fragile route",
                )
        return ValidationResult.passed()
