"""Live execution backend: quote → final safety re-check → sign → send → confirm.

This is the only module that broadcasts real transactions. It re-validates
the *fresh* quote immediately before signing (price impact and deviation from
the expected price), because market state may have changed since the order
was validated and risk-approved. "Never blindly execute."
"""

from __future__ import annotations

import logging

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import ExecutionResult, OrderRequest, Side

logger = logging.getLogger(__name__)


class LiveSwapExecutor:
    """Implements the ``ExecutionBackend`` protocol against Jupiter + Solana."""

    def __init__(self, jupiter, rpc, wallet, tx_builder, settings) -> None:
        self._jupiter = jupiter
        self._rpc = rpc
        self._wallet = wallet
        self._builder = tx_builder
        self._settings = settings

    async def execute(self, order: OrderRequest) -> ExecutionResult:
        s = self._settings
        try:
            if order.side is Side.BUY:
                input_mint, output_mint = s.pair_quote_mint, s.pair_base_mint
                amount_raw = int(order.notional * 10**s.quote_decimals)
            else:
                input_mint, output_mint = s.pair_base_mint, s.pair_quote_mint
                amount_raw = int(order.size_base * 10**s.base_decimals)
            if amount_raw <= 0:
                return ExecutionResult(success=False, error="zero-size order")

            quote = await self._jupiter.get_quote(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_raw=amount_raw,
                slippage_bps=s.slippage_bps,
                base_decimals=s.base_decimals,
                quote_decimals=s.quote_decimals,
                base_mint=s.pair_base_mint,
            )

            # Final pre-broadcast checks against the FRESH quote.
            if quote.price_impact_pct > cfg.MAX_PRICE_IMPACT_PCT:
                return ExecutionResult(
                    success=False,
                    error=f"price impact {quote.price_impact_pct:.4%} exceeds "
                          f"{cfg.MAX_PRICE_IMPACT_PCT:.2%} at execution time",
                )
            deviation = abs(quote.expected_price / order.entry_price - 1.0)
            if deviation > cfg.MAX_EXECUTION_DEVIATION_PCT:
                return ExecutionResult(
                    success=False,
                    error=f"execution price deviates {deviation:.4%} from expected "
                          f"mid (limit {cfg.MAX_EXECUTION_DEVIATION_PCT:.2%})",
                )

            tx_b64 = await self._jupiter.get_swap_transaction(quote, self._wallet.address)
            raw_signed = self._builder.build_signed_swap(tx_b64)
            signature = await self._rpc.send_raw_transaction(raw_signed)
            confirmed = await self._rpc.confirm_transaction(signature)
            if not confirmed:
                return ExecutionResult(
                    success=False,
                    signature=signature,
                    error="transaction not confirmed before timeout",
                )

            if order.side is Side.BUY:
                filled_base = quote.out_amount / 10**s.base_decimals
            else:
                filled_base = quote.in_amount / 10**s.base_decimals
            logger.info("Swap confirmed %s: %s %.6f base @ ~%.4f",
                        signature, order.side.value, filled_base, quote.expected_price)
            return ExecutionResult(
                success=True,
                filled_price=quote.expected_price,
                filled_size_base=filled_base,
                signature=signature,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a failed execution
            logger.exception("Swap execution failed for order %s", order.client_order_id)
            return ExecutionResult(success=False, error=str(exc))
