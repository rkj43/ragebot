"""Pre-execution order validation.

Checks the mechanics of a specific order against the current wallet, market
snapshot, and (when available) a fresh quote:

* wallet identity matches the configured expected address;
* balances cover the trade;
* order size and prices are sane;
* quoted execution price within 0.5% of the expected mid;
* quoted price impact within the cap.

Validation is about "can this order execute as intended" — portfolio-level
risk is the risk manager's job, and passing validation grants nothing until
risk approval also passes.
"""

from __future__ import annotations

import time
from typing import Optional

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import (
    MarketSnapshot, OrderRequest, Quote, Side, ValidationResult,
)


class OrderValidator:
    def __init__(self, expected_wallet_address: Optional[str] = None) -> None:
        self._expected_wallet = expected_wallet_address

    def validate(
        self,
        order: OrderRequest,
        snapshot: Optional[MarketSnapshot],
        quote_balance: float,
        base_balance: float,
        wallet_address: Optional[str] = None,
        quote: Optional[Quote] = None,
        now: Optional[float] = None,
    ) -> ValidationResult:
        now = now if now is not None else time.time()

        # Wallet identity — a mismatched wallet is an immediate hard reject.
        if self._expected_wallet and wallet_address and wallet_address != self._expected_wallet:
            return ValidationResult(
                False, "WALLET_MISMATCH",
                "active wallet does not match EXPECTED_WALLET_ADDRESS",
            )

        # Order shape.
        if order.size_base <= 0:
            return ValidationResult(False, "INVALID_SIZE", f"size {order.size_base} <= 0")
        if order.entry_price <= 0:
            return ValidationResult(False, "INVALID_PRICE", f"price {order.entry_price} <= 0")

        # Freshness — validated again by the risk manager; cheap and critical.
        if snapshot is None or snapshot.age(now) > cfg.MAX_DATA_AGE_SECONDS:
            return ValidationResult(False, "STALE_DATA", "no fresh market snapshot")

        # Balance, with a 1% buffer for fees/slippage on buys.
        if order.side is Side.BUY:
            required = order.notional * 1.01
            if quote_balance < required:
                return ValidationResult(
                    False, "INSUFFICIENT_BALANCE",
                    f"need {required:.2f} quote units, have {quote_balance:.2f}",
                )
        else:
            if base_balance + 1e-12 < order.size_base:
                return ValidationResult(
                    False, "INSUFFICIENT_BALANCE",
                    f"need {order.size_base:.6f} base units, have {base_balance:.6f}",
                )

        # Quote-level checks (skipped when no quote exists, e.g. paper mode —
        # the live backend re-checks these against a fresh quote regardless).
        if quote is not None:
            if quote.price_impact_pct > cfg.MAX_PRICE_IMPACT_PCT:
                return ValidationResult(
                    False, "PRICE_IMPACT_TOO_HIGH",
                    f"impact {quote.price_impact_pct:.4%} > {cfg.MAX_PRICE_IMPACT_PCT:.2%}",
                )
            deviation = abs(quote.expected_price / snapshot.price - 1.0)
            if deviation > cfg.MAX_EXECUTION_DEVIATION_PCT:
                return ValidationResult(
                    False, "EXECUTION_PRICE_DEVIATION",
                    f"quoted price {quote.expected_price:.4f} deviates {deviation:.4%} "
                    f"from mid {snapshot.price:.4f} "
                    f"(limit {cfg.MAX_EXECUTION_DEVIATION_PCT:.2%})",
                )
            if quote.out_amount <= 0:
                return ValidationResult(False, "ZERO_OUTPUT", "quote returns zero output")

        return ValidationResult.passed()
