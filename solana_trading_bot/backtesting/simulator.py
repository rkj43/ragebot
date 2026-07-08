"""Execution simulator for backtests: fills are never free.

Every simulated fill pays a taker fee and slippage; slippage grows with order
size relative to available liquidity (square-root impact model); and a
configurable fraction of transactions simply fail, as they do on-chain. The
random source is seeded so backtests are reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from solana_trading_bot.domain import ExecutionResult, OrderRequest, Side


@dataclass(frozen=True)
class SimulatorConfig:
    fee_bps: float = 10.0            # taker fee, basis points of notional
    base_slippage_bps: float = 5.0   # slippage floor
    liquidity_usd: float = 5_000_000.0  # depth used by the impact model
    impact_coefficient: float = 30.0    # bps of impact at notional == liquidity
    failure_rate: float = 0.02       # fraction of transactions that fail
    seed: int = 7


class ExecutionSimulator:
    def __init__(self, config: SimulatorConfig | None = None) -> None:
        self.config = config or SimulatorConfig()
        self._rng = random.Random(self.config.seed)
        self.failed_transactions = 0

    def slippage_bps(self, notional: float) -> float:
        c = self.config
        depth_ratio = max(notional, 0.0) / max(c.liquidity_usd, 1.0)
        return c.base_slippage_bps + c.impact_coefficient * (depth_ratio ** 0.5) * 100

    def execute(self, order: OrderRequest, market_price: float) -> ExecutionResult:
        c = self.config
        if self._rng.random() < c.failure_rate:
            self.failed_transactions += 1
            return ExecutionResult(success=False, error="simulated transaction failure")

        slip = market_price * self.slippage_bps(order.notional) / 10_000
        price = market_price + slip if order.side is Side.BUY else market_price - slip
        fee = order.size_base * price * c.fee_bps / 10_000
        return ExecutionResult(
            success=True,
            filled_price=price,
            filled_size_base=order.size_base,
            fee_quote=fee,
            signature=f"sim-{order.client_order_id}",
        )
