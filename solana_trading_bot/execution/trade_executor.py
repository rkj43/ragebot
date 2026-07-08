"""Trade executor: the only path from a signal to a filled order.

Invariants enforced here:

* duplicate submissions (same ``client_order_id``) are ignored, once;
* nothing reaches the execution backend without passing the order validator
  AND receiving an approved ``RiskDecision`` — there is no bypass parameter;
* every attempt, rejection, failure, and fill is written to the database.

``PaperExecutionBackend`` fills at the snapshot price with a simple fee +
slippage model, so paper mode exercises the entire pipeline except the chain.
"""

from __future__ import annotations

import logging
from typing import Optional, Set

from solana_trading_bot.domain import (
    ExecutionResult,
    IndicatorSet,
    MarketSnapshot,
    OrderRequest,
    Quote,
    Side,
)

logger = logging.getLogger(__name__)


class PaperExecutionBackend:
    """Simulated fills for paper trading: mid price ± slippage, taker fee."""

    def __init__(self, price_feed, fee_bps: float = 10.0, slippage_bps: float = 5.0) -> None:
        self._feed = price_feed
        self._fee_bps = fee_bps
        self._slippage_bps = slippage_bps

    async def execute(self, order: OrderRequest) -> ExecutionResult:
        snap = self._feed.snapshot()
        if snap is None or self._feed.is_stale():
            return ExecutionResult(success=False, error="paper fill refused: stale data")
        slip = snap.price * self._slippage_bps / 10_000
        price = snap.price + slip if order.side is Side.BUY else snap.price - slip
        fee = order.size_base * price * self._fee_bps / 10_000
        return ExecutionResult(
            success=True,
            filled_price=price,
            filled_size_base=order.size_base,
            fee_quote=fee,
            signature=f"paper-{order.client_order_id}",
        )


class TradeExecutor:
    def __init__(self, validator, risk_manager, backend, portfolio, trade_log,
                 wallet_address: Optional[str] = None) -> None:
        self._validator = validator
        self._risk = risk_manager
        self._backend = backend
        self._portfolio = portfolio
        self._log = trade_log
        self._wallet_address = wallet_address
        self._seen_order_ids: Set[str] = set()

    async def submit(
        self,
        order: OrderRequest,
        snapshot: Optional[MarketSnapshot],
        indicators: Optional[IndicatorSet] = None,
        quote: Optional[Quote] = None,
    ) -> ExecutionResult:
        # Duplicate protection: same order id can never execute twice.
        if order.client_order_id in self._seen_order_ids:
            logger.warning("Duplicate order %s ignored", order.client_order_id)
            self._log.record_order(order.side.value, "IGNORED", "duplicate client_order_id")
            return ExecutionResult(success=False, error="duplicate order ignored")
        self._seen_order_ids.add(order.client_order_id)

        # Gate 1: mechanical validation.
        validation = self._validator.validate(
            order,
            snapshot,
            quote_balance=self._portfolio.quote_balance,
            base_balance=self._portfolio.base_balance,
            wallet_address=self._wallet_address,
            quote=quote,
        )
        if not validation.ok:
            logger.warning("Order %s rejected by validator [%s]: %s",
                           order.client_order_id, validation.code, validation.reason)
            self._log.record_order(order.side.value, "REJECTED",
                                   f"{validation.code}: {validation.reason}")
            return ExecutionResult(success=False, error=f"{validation.code}: {validation.reason}")

        # Gate 2: risk approval. THE RISK ENGINE OVERRIDES EVERYTHING.
        decision = self._risk.approve(
            order,
            equity=self._portfolio.equity(snapshot.price if snapshot else 0.0),
            current_exposure_notional=self._portfolio.exposure_notional(
                snapshot.price if snapshot else 0.0),
            snapshot=snapshot,
            indicators=indicators,
            quote=quote,
        )
        if not decision.approved:
            self._log.record_order(order.side.value, "REJECTED",
                                   f"{decision.code}: {decision.reason}")
            return ExecutionResult(success=False, error=f"{decision.code}: {decision.reason}")

        # Execute.
        self._log.record_order(order.side.value, "SUBMITTED",
                               f"{order.strategy}: {order.reason}")
        result = await self._backend.execute(order)

        if not result.success:
            logger.error("Execution FAILED for %s: %s", order.client_order_id, result.error)
            self._log.record_order(order.side.value, "FAILED", result.error or "unknown error")
            return result

        self._portfolio.apply_fill(order, result)
        self._log.record_order(order.side.value, "FILLED",
                               f"{result.filled_size_base:.6f} @ {result.filled_price:.4f} "
                               f"(fee {result.fee_quote:.4f})")
        return result
