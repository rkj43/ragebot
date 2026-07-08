"""Shared fixtures: fresh snapshots, an in-memory audit log, and a risk
manager wired the same way the live bot wires it."""

from __future__ import annotations

import time

import pytest

from solana_trading_bot.database.database import TradeLog
from solana_trading_bot.domain import MarketSnapshot
from solana_trading_bot.market.liquidity_monitor import LiquidityMonitor
from solana_trading_bot.risk.risk_manager import RiskManager


@pytest.fixture
def now() -> float:
    return time.time()


@pytest.fixture
def fresh_snapshot(now) -> MarketSnapshot:
    return MarketSnapshot(timestamp=now, price=100.0, spread_pct=0.0005,
                          liquidity_usd=5_000_000.0, source="test")


@pytest.fixture
def trade_log() -> TradeLog:
    return TradeLog("sqlite:///:memory:")


@pytest.fixture
def risk_manager(trade_log) -> RiskManager:
    rm = RiskManager(liquidity_monitor=LiquidityMonitor(),
                     event_recorder=trade_log.record_risk_event)
    rm.heartbeat.beat_data()  # healthy by default; tests break things explicitly
    return rm
