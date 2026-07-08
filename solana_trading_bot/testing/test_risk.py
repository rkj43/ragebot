"""Risk engine tests: the layers that must never fail open."""

from __future__ import annotations

import time

import pytest

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import Action, MarketSnapshot, OrderRequest, Position, Side
from solana_trading_bot.risk.circuit_breaker import BreakerState, CircuitBreaker
from solana_trading_bot.risk.exposure_manager import ExposureManager
from solana_trading_bot.risk.position_sizer import PositionSizer


def make_entry(size_base: float, price: float = 100.0, stop: float = 95.0) -> OrderRequest:
    return OrderRequest(side=Side.BUY, size_base=size_base, entry_price=price,
                        strategy="test", reason="test", stop_price=stop,
                        take_profit=price * 1.1)


# --------------------------------------------------------------------- #
# Layer 1 — hard position limit                                         #
# --------------------------------------------------------------------- #
class TestPositionLimit:
    def test_oversized_order_rejected(self, risk_manager, fresh_snapshot):
        equity = 1_000.0
        order = make_entry(size_base=0.6)  # 60 notional = 6% > 5%
        decision = risk_manager.approve(order, equity, 0.0, fresh_snapshot)
        assert not decision.approved
        assert decision.code == "POSITION_LIMIT_EXCEEDED"

    def test_position_limit_logged_as_risk_event(self, risk_manager, trade_log, fresh_snapshot):
        risk_manager.approve(make_entry(size_base=0.6), 1_000.0, 0.0, fresh_snapshot)
        events = [e.event for e in trade_log.risk_events()]
        assert "POSITION_LIMIT_EXCEEDED" in events

    def test_order_at_limit_approved(self, risk_manager, fresh_snapshot):
        order = make_entry(size_base=0.5)  # exactly 5%
        decision = risk_manager.approve(order, 1_000.0, 0.0, fresh_snapshot)
        assert decision.approved, decision.reason


# --------------------------------------------------------------------- #
# Layer 2 — risk-based sizing                                           #
# --------------------------------------------------------------------- #
class TestPositionSizer:
    def test_sizing_formula(self):
        result = PositionSizer().size_position(equity=10_000.0, entry_price=100.0,
                                               stop_price=98.0)
        # risk 1% = 100; stop distance 2 → 50 base units... but that is 5000
        # notional = 50% of equity, so the 5% cap must bite instead.
        assert result.ok
        assert result.capped
        assert result.notional == pytest.approx(10_000.0 * cfg.MAX_POSITION_PCT)

    def test_uncapped_sizing_matches_formula(self):
        # wide stop: risk 1% = 100 over distance 25 → 4 units = 400 notional (4%)
        result = PositionSizer().size_position(10_000.0, 100.0, 75.0)
        assert result.ok and not result.capped
        assert result.size_base == pytest.approx(100.0 / 25.0)

    def test_higher_volatility_means_smaller_position(self):
        sizer = PositionSizer()
        tight = sizer.size_position(10_000.0, 100.0, 75.0)   # 25 wide stop
        wide = sizer.size_position(10_000.0, 100.0, 50.0)    # 50 wide stop
        assert not tight.capped and not wide.capped
        assert wide.size_base < tight.size_base

    def test_missing_stop_rejected(self):
        assert not PositionSizer().size_position(10_000.0, 100.0, None).ok

    def test_stop_above_entry_rejected(self):
        assert not PositionSizer().size_position(10_000.0, 100.0, 101.0).ok


# --------------------------------------------------------------------- #
# Layer 3 — stops are mandatory                                         #
# --------------------------------------------------------------------- #
class TestStops:
    def test_entry_without_stop_rejected(self, risk_manager, fresh_snapshot):
        order = OrderRequest(side=Side.BUY, size_base=0.1, entry_price=100.0,
                             strategy="test", reason="test")
        decision = risk_manager.approve(order, 1_000.0, 0.0, fresh_snapshot)
        assert not decision.approved
        assert decision.code == "MISSING_STOP"

    def test_trailing_stop_ratchets_up(self, risk_manager):
        from solana_trading_bot.domain import IndicatorSet
        pos = Position(size_base=1.0, entry_price=100.0, stop_price=96.0,
                       take_profit=200.0, strategy="test")
        ind = IndicatorSet(ready=True, atr=2.0)
        snap = MarketSnapshot(timestamp=time.time(), price=110.0)
        signal = risk_manager.manage_position(pos, snap, ind)
        assert signal is None  # nothing hit yet
        assert pos.stop_price == pytest.approx(110.0 - cfg.STOP_ATR_MULT * 2.0)

    def test_stop_hit_forces_exit(self, risk_manager):
        pos = Position(size_base=1.0, entry_price=100.0, stop_price=96.0,
                       take_profit=110.0, strategy="test")
        snap = MarketSnapshot(timestamp=time.time(), price=95.0)
        signal = risk_manager.manage_position(pos, snap)
        assert signal is not None and signal.action is Action.EXIT

    def test_take_profit_forces_exit(self, risk_manager):
        pos = Position(size_base=1.0, entry_price=100.0, stop_price=96.0,
                       take_profit=110.0, strategy="test")
        snap = MarketSnapshot(timestamp=time.time(), price=111.0)
        signal = risk_manager.manage_position(pos, snap)
        assert signal is not None and signal.action is Action.EXIT


# --------------------------------------------------------------------- #
# Layer 4 — daily loss circuit breaker                                  #
# --------------------------------------------------------------------- #
class TestCircuitBreaker:
    def test_trips_at_daily_loss_limit(self):
        breaker = CircuitBreaker()
        t0 = time.time()
        breaker.check(1_000.0, now=t0)
        breaker.check(969.0, now=t0 + 60)  # -3.1%
        assert breaker.state is BreakerState.SLEEP_MODE
        assert not breaker.allows_new_trades
        assert breaker.must_close_positions

    def test_does_not_trip_below_limit(self):
        breaker = CircuitBreaker()
        t0 = time.time()
        breaker.check(1_000.0, now=t0)
        breaker.check(975.0, now=t0 + 60)  # -2.5%
        assert breaker.state is BreakerState.ACTIVE

    def test_new_day_does_not_clear_sleep_mode(self):
        breaker = CircuitBreaker()
        t0 = time.time()
        breaker.check(1_000.0, now=t0)
        breaker.check(900.0, now=t0 + 60)
        assert breaker.state is BreakerState.SLEEP_MODE
        breaker.check(900.0, now=t0 + 3 * 86_400)  # days later
        assert breaker.state is BreakerState.SLEEP_MODE, "must require manual restart"

    def test_manual_restart_restores_trading(self):
        breaker = CircuitBreaker()
        t0 = time.time()
        breaker.check(1_000.0, now=t0)
        breaker.check(900.0, now=t0 + 60)
        breaker.manual_restart(900.0, now=t0 + 120)
        assert breaker.state is BreakerState.ACTIVE

    def test_breaker_blocks_new_entries(self, risk_manager, fresh_snapshot):
        t0 = time.time()
        risk_manager.circuit_breaker.check(1_000.0, now=t0)
        risk_manager.circuit_breaker.check(900.0, now=t0 + 1)
        decision = risk_manager.approve(make_entry(0.1), 900.0, 0.0, fresh_snapshot)
        assert not decision.approved
        assert decision.code == "CIRCUIT_BREAKER_ACTIVE"

    def test_breaker_still_allows_exits(self, risk_manager, fresh_snapshot):
        t0 = time.time()
        risk_manager.circuit_breaker.check(1_000.0, now=t0)
        risk_manager.circuit_breaker.check(900.0, now=t0 + 1)
        exit_order = OrderRequest(side=Side.SELL, size_base=0.1, entry_price=100.0,
                                  strategy="test", reason="close", reduce_only=True)
        decision = risk_manager.approve(exit_order, 900.0, 10.0, fresh_snapshot)
        assert decision.approved, "reducing risk must always be possible"


# --------------------------------------------------------------------- #
# Exposure                                                              #
# --------------------------------------------------------------------- #
class TestExposure:
    def test_total_exposure_capped(self):
        check = ExposureManager().check(make_entry(0.5), equity=1_000.0,
                                        current_exposure_notional=60.0)
        assert not check.ok  # 60 + 50 = 110 > 10% of 1000
        assert check.code == "EXPOSURE_LIMIT_EXCEEDED"

    def test_exit_never_blocked_by_exposure(self):
        exit_order = OrderRequest(side=Side.SELL, size_base=5.0, entry_price=100.0,
                                  strategy="t", reason="r", reduce_only=True)
        assert ExposureManager().check(exit_order, 1_000.0, 500.0).ok
