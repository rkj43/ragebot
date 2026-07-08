"""Chaos tests — the six mandated failure scenarios.

1. missing/stale price data  → halt (no approvals)
2. huge spread               → trade rejected
3. flash crash               → circuit protections pause entries
4. duplicate transaction     → ignored
5. RPC failure               → safe shutdown (halt, exits only path)
6. wallet mismatch           → rejected
"""

from __future__ import annotations

import asyncio
import time

import pytest

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import MarketSnapshot, OrderRequest, Side
from solana_trading_bot.execution.order_validator import OrderValidator
from solana_trading_bot.execution.trade_executor import TradeExecutor
from solana_trading_bot.market.liquidity_monitor import LiquidityMonitor
from solana_trading_bot.market.price_feed import PriceFeed
from solana_trading_bot.portfolio.portfolio_manager import PortfolioManager


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def make_entry() -> OrderRequest:
    return OrderRequest(side=Side.BUY, size_base=0.1, entry_price=100.0,
                        strategy="test", reason="test", stop_price=95.0,
                        take_profit=110.0)


# 1 ------------------------------------------------------------------- #
class TestMissingPriceData:
    def test_stale_snapshot_halts_approval(self, risk_manager):
        stale = MarketSnapshot(timestamp=time.time() - 30, price=100.0)
        decision = risk_manager.approve(make_entry(), 1_000.0, 0.0, stale)
        assert not decision.approved and decision.code == "STALE_DATA"

    def test_missing_snapshot_halts_approval(self, risk_manager):
        decision = risk_manager.approve(make_entry(), 1_000.0, 0.0, None)
        assert not decision.approved and decision.code == "STALE_DATA"

    def test_feed_reports_staleness(self):
        feed = PriceFeed(jupiter=None, base_mint="a", quote_mint="b")
        assert feed.is_stale()  # no data at all
        feed.ingest(MarketSnapshot(timestamp=time.time() - 11, price=100.0))
        assert feed.is_stale()
        feed.ingest(MarketSnapshot(timestamp=time.time(), price=100.0))
        assert not feed.is_stale()


# 2 ------------------------------------------------------------------- #
class TestHugeSpread:
    def test_spread_blowout_rejected(self, risk_manager, now):
        monitor: LiquidityMonitor = risk_manager.liquidity
        for _ in range(30):  # establish a 0.05% normal spread
            monitor.record(MarketSnapshot(timestamp=now, price=100.0, spread_pct=0.0005))
        wide = MarketSnapshot(timestamp=now, price=100.0, spread_pct=0.005,
                              liquidity_usd=5_000_000.0)
        decision = risk_manager.approve(make_entry(), 1_000.0, 0.0, wide)
        assert not decision.approved and decision.code == "SPREAD_TOO_WIDE"

    def test_low_liquidity_rejected(self, risk_manager, now):
        thin = MarketSnapshot(timestamp=now, price=100.0, spread_pct=0.0005,
                              liquidity_usd=10_000.0)
        decision = risk_manager.approve(make_entry(), 1_000.0, 0.0, thin)
        assert not decision.approved and decision.code == "LOW_LIQUIDITY"


# 3 ------------------------------------------------------------------- #
class TestFlashCrash:
    def test_flash_crash_pauses_entries(self, risk_manager, now):
        guard = risk_manager.flash_guard
        for i in range(10):
            guard.record_price(100.0, now=now + i)
        guard.record_price(94.0, now=now + 11)  # ~6% below 30s average
        assert not guard.entries_allowed(now=now + 12)

        snap = MarketSnapshot(timestamp=now + 12, price=94.0,
                              liquidity_usd=5_000_000.0)
        decision = risk_manager.approve(make_entry(), 1_000.0, 0.0, snap,
                                        now=now + 12)
        assert not decision.approved and decision.code == "FLASH_CRASH_PAUSE"

    def test_entries_resume_after_pause_window(self, risk_manager, now):
        guard = risk_manager.flash_guard
        for i in range(10):
            guard.record_price(100.0, now=now + i)
        guard.record_price(94.0, now=now + 11)
        resume = now + 11 + cfg.FLASH_CRASH_PAUSE_SECONDS + 1
        assert guard.entries_allowed(now=resume)

    def test_exits_still_allowed_during_flash_crash(self, risk_manager, now):
        guard = risk_manager.flash_guard
        for i in range(10):
            guard.record_price(100.0, now=now + i)
        guard.record_price(94.0, now=now + 11)
        exit_order = OrderRequest(side=Side.SELL, size_base=0.1, entry_price=94.0,
                                  strategy="t", reason="stop", reduce_only=True)
        snap = MarketSnapshot(timestamp=now + 12, price=94.0)
        decision = risk_manager.approve(exit_order, 1_000.0, 9.4, snap, now=now + 12)
        assert decision.approved


# 4 ------------------------------------------------------------------- #
class TestDuplicateTransaction:
    def test_duplicate_order_ignored(self, risk_manager, trade_log, fresh_snapshot):
        from solana_trading_bot.testing.test_execution import RecordingBackend

        portfolio = PortfolioManager(quote_balance=1_000.0, trade_log=trade_log)
        backend = RecordingBackend()
        executor = TradeExecutor(OrderValidator(), risk_manager, backend,
                                 portfolio, trade_log)
        order = make_entry()
        first = run(executor.submit(order, fresh_snapshot))
        second = run(executor.submit(order, fresh_snapshot))  # same client_order_id
        assert first.success
        assert not second.success and "duplicate" in second.error.lower()
        assert len(backend.calls) == 1, "duplicate must never reach the chain"
        assert [o.status for o in trade_log.orders()].count("IGNORED") == 1


# 5 ------------------------------------------------------------------- #
class TestRpcFailure:
    def test_rpc_failure_halts_trading(self, risk_manager, fresh_snapshot):
        risk_manager.heartbeat.report_rpc_failure("connection refused")
        assert risk_manager.heartbeat.halted
        decision = risk_manager.approve(make_entry(), 1_000.0, 0.0, fresh_snapshot)
        assert not decision.approved and decision.code == "TRADING_HALTED"

    def test_websocket_disconnect_halts_trading(self, risk_manager, fresh_snapshot):
        risk_manager.heartbeat.report_ws_disconnect("closed by peer")
        decision = risk_manager.approve(make_entry(), 1_000.0, 0.0, fresh_snapshot)
        assert not decision.approved and decision.code == "TRADING_HALTED"

    def test_halt_is_recorded_as_risk_event(self, risk_manager, trade_log):
        risk_manager.heartbeat.report_rpc_failure("connection refused")
        events = [e.event for e in trade_log.risk_events()]
        assert "TRADING_HALTED" in events

    def test_rpc_client_reports_failure_after_retries(self):
        """The RPC wrapper must escalate to the heartbeat, never swallow."""
        pytest.importorskip("solana")
        from solana_trading_bot.blockchain.rpc_client import RpcClient, RpcError
        from solana_trading_bot.risk.risk_manager import HeartbeatMonitor

        hb = HeartbeatMonitor()
        client = RpcClient("http://invalid", heartbeat=hb, max_retries=2)
        client._client = object()  # pretend connected

        async def failing():
            raise ConnectionError("no route to host")

        with pytest.raises(RpcError):
            run(client._call("test", failing))
        assert hb.halted


# 6 ------------------------------------------------------------------- #
class TestWalletMismatch:
    def test_wallet_mismatch_rejected_by_validator(self, fresh_snapshot):
        validator = OrderValidator(expected_wallet_address="ExpectedWallet111")
        result = validator.validate(make_entry(), fresh_snapshot,
                                    quote_balance=1_000.0, base_balance=0.0,
                                    wallet_address="SomeOtherWallet222")
        assert not result.ok and result.code == "WALLET_MISMATCH"

    def test_wallet_mismatch_never_reaches_backend(self, risk_manager,
                                                   trade_log, fresh_snapshot):
        from solana_trading_bot.testing.test_execution import RecordingBackend

        portfolio = PortfolioManager(quote_balance=1_000.0, trade_log=trade_log)
        backend = RecordingBackend()
        executor = TradeExecutor(
            OrderValidator(expected_wallet_address="ExpectedWallet111"),
            risk_manager, backend, portfolio, trade_log,
            wallet_address="SomeOtherWallet222",
        )
        result = run(executor.submit(make_entry(), fresh_snapshot))
        assert not result.success
        assert backend.calls == []
