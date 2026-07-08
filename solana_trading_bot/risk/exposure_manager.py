"""Total exposure control.

Independently of per-position limits, total non-quote exposure may never
exceed MAX_TOTAL_EXPOSURE_PCT of equity. Exposure-reducing orders are always
allowed — the bot can always de-risk, never over-risk.
"""

from __future__ import annotations

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import OrderRequest, ValidationResult


class ExposureManager:
    def check(self, order: OrderRequest, equity: float,
              current_exposure_notional: float) -> ValidationResult:
        if not order.is_entry:
            return ValidationResult.passed()
        if equity <= 0:
            return ValidationResult(False, "NO_EQUITY", "equity is zero or negative")

        projected = current_exposure_notional + order.notional
        limit = equity * cfg.MAX_TOTAL_EXPOSURE_PCT
        if projected > limit:
            return ValidationResult(
                False, "EXPOSURE_LIMIT_EXCEEDED",
                f"projected exposure {projected:.2f} > "
                f"{cfg.MAX_TOTAL_EXPOSURE_PCT:.0%} of equity ({limit:.2f})",
            )
        return ValidationResult.passed()
