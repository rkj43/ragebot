"""Layer 2 — risk-based position sizing. Never fixed lot sizes.

    risk_amount   = equity * RISK_PER_TRADE_PCT      (1%)
    position_size = risk_amount / stop_distance

A wider stop (higher volatility) automatically produces a smaller position.
The result is additionally capped at MAX_POSITION_PCT (5%) of equity — the
sizer caps proactively, and the risk manager independently rejects anything
above the cap, so a bug in one layer cannot defeat the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from solana_trading_bot.config import settings as cfg


@dataclass(frozen=True)
class SizingResult:
    ok: bool
    size_base: float
    notional: float
    capped: bool
    reason: str


class PositionSizer:
    def size_position(self, equity: float, entry_price: float,
                      stop_price: Optional[float]) -> SizingResult:
        if equity <= 0:
            return SizingResult(False, 0.0, 0.0, False, "non-positive equity")
        if entry_price <= 0:
            return SizingResult(False, 0.0, 0.0, False, "non-positive entry price")
        if stop_price is None:
            return SizingResult(False, 0.0, 0.0, False, "no stop price: every position requires a stop")

        stop_distance = entry_price - stop_price
        if stop_distance <= 0:
            return SizingResult(False, 0.0, 0.0, False,
                                f"stop {stop_price} not below entry {entry_price}")

        risk_amount = equity * cfg.RISK_PER_TRADE_PCT
        size_base = risk_amount / stop_distance
        notional = size_base * entry_price

        max_notional = equity * cfg.MAX_POSITION_PCT
        capped = False
        if notional > max_notional:
            size_base = max_notional / entry_price
            notional = max_notional
            capped = True

        return SizingResult(
            ok=True,
            size_base=size_base,
            notional=notional,
            capped=capped,
            reason=(
                f"risk {cfg.RISK_PER_TRADE_PCT:.0%} of equity {equity:.2f} over "
                f"stop distance {stop_distance:.4f}"
                + ("; capped at position limit" if capped else "")
            ),
        )
