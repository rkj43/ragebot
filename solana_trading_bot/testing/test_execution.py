"""Execution path tests: validation, the risk gate, fills, and audit logging."""

from __future__ import annotations

import asyncio
import time

import pytest

from solana_trading_bot.domain import (
    ExecutionResult, MarketSnapshot, OrderRequest, Quote, Side,
)
from solana_trading_bot.execution.order_validator import OrderValidator
from solana_trading_bot.execution.trade_executor import PaperExecutionBackend, TradeExecutor
from solana_trading_bot.portfolio.portfolio_manager import PortfolioManager


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def make_entry(size_base: float = 0.1, price: float = 100.0) -> OrderRequest:
    return OrderRequest(side=Side.BUY, size_base=size_base, entry_price=price,
                        strategy="test", reason="test", stop_price=95.0,
                        take_profit=110.0)


def make_quote(expected_price: float = 100.0, impact: float = 0.001) -> Quote:
    return Quote(input_mint="USDC", output_mint="SOL", in_amount=10_000_000,
                 out_amount=100_000_000, price_impact_pct=impact,
                 expected_price=expected_price)


class RecordingBackend:
    """Fake backend that records whether it was ever reached."""

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.calls: list[OrderRequest] = []
        self._result = result or ExecutionResult(
            success=True, filled_price=100.0, filled_size_base=0.1, fee_quote=0.01)

    async def execute(self, order: OrderRequest) -> ExecutionResult:
        self.calls.append(order)
        return self._result


@pytest.fixture
def portfolio(trade_log) -> PortfolioManager:
    return PortfolioManager(quote_balance=1_000.0, trade_log=trade_log)


@pytest.fixture
def executor_parts(risk_manager, portfolio, trade_log):
    backend = RecordingBackend()
    executor = TradeExecutor(
        validator=OrderValidator(),
        risk_manager=risk_manager,
        backend=backend,
        portfolio=portfolio,
        trade_log=trade_log,
    )
    return executor, backend


class TestOrderValidator:
    def test_insufficient_balance_rejected(self, fresh_snapshot):
        result = OrderValidator().validate(
            make_entry(size_base=20.0), fresh_snapshot,
            quote_balance=100.0, base_balance=0.0)
        assert not result.ok and result.code == "INSUFFICIENT_BALANCE"

    def test_sell_more_than_held_rejected(self, fresh_snapshot):
        order = OrderRequest(side=Side.SELL, size_base=2.0, entry_price=100.0,
                             strategy="t", reason="r", reduce_only=True)
        result = OrderValidator().validate(order, fresh_snapshot,
                                           quote_balance=0.0, base_balance=1.0)
        assert not result.ok and result.code == "INSUFFICIENT_BALANCE"

    def test_price_impact_rejected(self, fresh_snapshot):
        result = OrderValidator().validate(
            make_entry(), fresh_snapshot, 1_000.0, 0.0,
            quote=make_quote(impact=0.05))
        assert not result.ok and result.code == "PRICE_IMPACT_TOO_HIGH"

    def test_execution_price_deviation_rejected(self, fresh_snapshot):
        # snapshot mid is 100; quote implies 101 → 1% > 0.5% limit
        result = OrderValidator().validate(
            make_entry(), fresh_snapshot, 1_000.0, 0.0,
            quote=make_quote(expected_price=101.0))
        assert not result.ok and result.code == "EXECUTION_PRICE_DEVIATION"

    def test_good_order_passes(self, fresh_snapshot):
        result = OrderValidator().validate(
            make_entry(), fresh_snapshot, 1_000.0, 0.0,
            quote=make_quote(expected_price=100.2))
        assert result.ok, result.reason


class TestTradeExecutor:
    def test_happy_path_fills_and_updates_portfolio(self, executor_parts,
                                                    portfolio, fresh_snapshot):
        executor, backend = executor_parts
        result = run(executor.submit(make_entry(), fresh_snapshot))
        assert result.success
        assert len(backend.calls) == 1
        assert portfolio.has_position()
        assert portfolio.quote_balance < 1_000.0

    def test_risk_rejection_never_reaches_backend(self, executor_parts,
                                                  fresh_snapshot):
        executor, backend = executor_parts
        oversized = make_entry(size_base=5.0)  # 500 notional on 1000 equity
        result = run(executor.submit(oversized, fresh_snapshot))
        assert not result.success
        assert backend.calls == [], "risk-rejected order must never execute"
        assert "POSITION_LIMIT_EXCEEDED" in result.error

    def test_validator_rejection_never_reaches_backend(self, risk_manager,
                                                       trade_log, fresh_snapshot):
        poor = PortfolioManager(quote_balance=1.0, trade_log=trade_log)
        backend = RecordingBackend()
        executor = TradeExecutor(OrderValidator(), risk_manager, backend, poor, trade_log)
        result = run(executor.submit(make_entry(), fresh_snapshot))
        assert not result.success
        assert backend.calls == []

    def test_failed_execution_recorded_not_applied(self, risk_manager,
                                                   portfolio, trade_log,
                                                   fresh_snapshot):
        backend = RecordingBackend(ExecutionResult(success=False, error="boom"))
        executor = TradeExecutor(OrderValidator(), risk_manager, backend,
                                 portfolio, trade_log)
        result = run(executor.submit(make_entry(), fresh_snapshot))
        assert not result.success
        assert not portfolio.has_position()
        statuses = [o.status for o in trade_log.orders()]
        assert "FAILED" in statuses

    def test_every_attempt_is_audited(self, executor_parts, trade_log, fresh_snapshot):
        executor, _ = executor_parts
        run(executor.submit(make_entry(), fresh_snapshot))
        statuses = [o.status for o in trade_log.orders()]
        assert statuses == ["SUBMITTED", "FILLED"]

    def test_round_trip_records_trade_pnl(self, executor_parts, portfolio,
                                          trade_log, fresh_snapshot):
        executor, backend = executor_parts
        run(executor.submit(make_entry(), fresh_snapshot))
        backend._result = ExecutionResult(success=True, filled_price=105.0,
                                          filled_size_base=0.1, fee_quote=0.01)
        exit_order = OrderRequest(side=Side.SELL, size_base=0.1, entry_price=105.0,
                                  strategy="test", reason="tp", reduce_only=True)
        run(executor.submit(exit_order, fresh_snapshot))
        trades = trade_log.trades()
        assert len(trades) == 1
        assert trades[0].profit_loss == pytest.approx((105.0 - 100.0) * 0.1 - 0.01)
        assert not portfolio.has_position()


class TestPaperBackend:
    def test_paper_fill_includes_slippage_and_fee(self, fresh_snapshot):
        class Feed:
            def snapshot(self):
                return fresh_snapshot
            def is_stale(self, now=None):
                return False

        backend = PaperExecutionBackend(Feed(), fee_bps=10, slippage_bps=5)
        result = run(backend.execute(make_entry(size_base=1.0)))
        assert result.success
        assert result.filled_price > fresh_snapshot.price  # buys pay up
        assert result.fee_quote > 0

    def test_paper_fill_refused_on_stale_data(self):
        class StaleFeed:
            def snapshot(self):
                return MarketSnapshot(timestamp=time.time() - 60, price=100.0)
            def is_stale(self, now=None):
                return True

        backend = PaperExecutionBackend(StaleFeed())
        result = run(backend.execute(make_entry()))
        assert not result.success
