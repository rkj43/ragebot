"""Portfolio state: balances, the open position, equity, and realized P&L.

Spot-only, single pair. Equity = quote balance + base balance * price. Fills
flow in only from the trade executor; nothing else mutates balances. Closed
round-trips are recorded to the trades table with realized profit/loss.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from solana_trading_bot.domain import (
    ExecutionResult, OrderRequest, Position, Side,
)

logger = logging.getLogger(__name__)


class PortfolioManager:
    def __init__(self, quote_balance: float, base_balance: float = 0.0,
                 trade_log=None, token: str = "SOL/USDC") -> None:
        self.quote_balance = quote_balance
        self.base_balance = base_balance
        self.position: Optional[Position] = None
        self.realized_pnl_total = 0.0
        self._log = trade_log
        self._token = token

    def equity(self, price: float) -> float:
        return self.quote_balance + self.base_balance * max(price, 0.0)

    def exposure_notional(self, price: float) -> float:
        return self.base_balance * max(price, 0.0)

    def has_position(self) -> bool:
        return self.position is not None

    def apply_fill(self, order: OrderRequest, result: ExecutionResult) -> None:
        if not result.success or result.filled_price is None or result.filled_size_base is None:
            raise ValueError("apply_fill called with an unsuccessful execution result")

        size = result.filled_size_base
        price = result.filled_price
        fee = result.fee_quote

        if order.side is Side.BUY:
            cost = size * price + fee
            if cost > self.quote_balance + 1e-9:
                raise ValueError(f"fill cost {cost:.4f} exceeds quote balance "
                                 f"{self.quote_balance:.4f}")
            self.quote_balance -= cost
            self.base_balance += size
            self.position = Position(
                size_base=size,
                entry_price=price,
                stop_price=order.stop_price if order.stop_price else 0.0,
                take_profit=order.take_profit if order.take_profit else 0.0,
                strategy=order.strategy,
                opened_at=time.time(),
            )
            logger.info("OPENED %s %.6f @ %.4f (stop %.4f, tp %.4f)",
                        self._token, size, price,
                        self.position.stop_price, self.position.take_profit)
        else:
            if size > self.base_balance + 1e-9:
                raise ValueError(f"sell size {size:.6f} exceeds base balance "
                                 f"{self.base_balance:.6f}")
            self.base_balance -= size
            self.quote_balance += size * price - fee

            if self.position is not None:
                pnl = (price - self.position.entry_price) * size - fee
                self.realized_pnl_total += pnl
                logger.info("CLOSED %s %.6f @ %.4f, P&L %.4f", self._token, size, price, pnl)
                if self._log is not None:
                    self._log.record_trade(
                        token=self._token,
                        strategy=self.position.strategy,
                        entry=self.position.entry_price,
                        exit_=price,
                        size=size,
                        profit_loss=pnl,
                    )
                remaining = self.position.size_base - size
                self.position = None if remaining <= 1e-9 else Position(
                    size_base=remaining,
                    entry_price=self.position.entry_price,
                    stop_price=self.position.stop_price,
                    take_profit=self.position.take_profit,
                    strategy=self.position.strategy,
                    opened_at=self.position.opened_at,
                    high_water=self.position.high_water,
                )
