"""Backtesting engine.

Replays historical OHLCV candles through the *same* components live trading
uses — indicator engine, regime detector, strategy router, position sizer,
and the real ``RiskManager`` (driven with the candle's timestamp, so
staleness and flash-crash windows behave correctly) — with fills provided by
the ``ExecutionSimulator`` (fees, slippage, liquidity impact, failed
transactions).

Also implements walk-forward splitting: train / validate / test segments are
evaluated independently so that any future parameter fitting can only ever
use the training window. The shipped strategies have no fitted parameters,
so today walk-forward serves as an out-of-sample consistency check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from solana_trading_bot.backtesting.metrics import PerformanceReport, compute_report
from solana_trading_bot.backtesting.simulator import ExecutionSimulator, SimulatorConfig
from solana_trading_bot.domain import Action, MarketSnapshot, OrderRequest, Side
from solana_trading_bot.market.indicators import (
    WARMUP_CANDLES, compute_indicator_frame, indicator_set_from_row,
)
from solana_trading_bot.market.regime_detector import RegimeDetector
from solana_trading_bot.portfolio.portfolio_manager import PortfolioManager
from solana_trading_bot.risk.position_sizer import PositionSizer
from solana_trading_bot.risk.risk_manager import RiskManager
from solana_trading_bot.strategies.base_strategy import StrategyRouter
from solana_trading_bot.strategies.defensive_strategy import DefensiveStrategy
from solana_trading_bot.strategies.mean_reversion import MeanReversionStrategy
from solana_trading_bot.strategies.trend_following import TrendFollowingStrategy

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    report: PerformanceReport
    equity_curve: List[float] = field(default_factory=list)
    trade_pnls: List[float] = field(default_factory=list)
    rejected_orders: int = 0
    regime_counts: Dict[str, int] = field(default_factory=dict)


def default_router() -> StrategyRouter:
    return StrategyRouter([
        TrendFollowingStrategy(),
        MeanReversionStrategy(),
        DefensiveStrategy(),
    ])


class BacktestEngine:
    def __init__(
        self,
        starting_equity: float = 1_000.0,
        candle_interval_seconds: int = 60,
        simulator: Optional[ExecutionSimulator] = None,
        router: Optional[StrategyRouter] = None,
    ) -> None:
        self.starting_equity = starting_equity
        self.candle_interval_seconds = candle_interval_seconds
        self.simulator = simulator or ExecutionSimulator(SimulatorConfig())
        self.router = router or default_router()

    def run(self, candles: pd.DataFrame) -> BacktestResult:
        """``candles``: OHLCV DataFrame indexed by datetime."""
        if len(candles) <= WARMUP_CANDLES:
            raise ValueError(
                f"need more than {WARMUP_CANDLES} candles, got {len(candles)}")

        frame = compute_indicator_frame(candles)
        detector = RegimeDetector()
        sizer = PositionSizer()
        risk = RiskManager()  # real risk engine, driven by candle time
        portfolio = PortfolioManager(quote_balance=self.starting_equity)

        equity_curve: List[float] = []
        trade_pnls: List[float] = []
        rejected = 0
        regime_counts: Dict[str, int] = {}

        for i in range(WARMUP_CANDLES, len(frame)):
            row = frame.iloc[i]
            ts = frame.index[i].timestamp()
            price = float(row["close"])
            snapshot = MarketSnapshot(timestamp=ts, price=price, source="backtest")
            ind = indicator_set_from_row(row, ready=not row.isna().any(), timestamp=ts)

            risk.heartbeat.beat_data(ts)
            risk.flash_guard.record_price(price, now=ts)
            risk.circuit_breaker.check(portfolio.equity(price), now=ts)

            regime = detector.classify(ind)
            regime_counts[regime.regime.value] = regime_counts.get(regime.regime.value, 0) + 1

            # 1. Protective exits first (stop / take profit / trailing / breaker).
            forced_exit = None
            if portfolio.position is not None:
                forced_exit = risk.manage_position(portfolio.position, snapshot, ind)

            signal = forced_exit or self.router.decide(regime, ind, snapshot, portfolio.position)

            if signal.action is Action.EXIT and portfolio.position is not None:
                order = OrderRequest(
                    side=Side.SELL,
                    size_base=portfolio.position.size_base,
                    entry_price=price,
                    strategy=signal.strategy,
                    reason=signal.reason,
                    reduce_only=True,
                )
                decision = risk.approve(order, portfolio.equity(price),
                                        portfolio.exposure_notional(price),
                                        snapshot, ind, now=ts)
                if decision.approved:
                    result = self.simulator.execute(order, price)
                    if result.success:
                        before = portfolio.realized_pnl_total
                        portfolio.apply_fill(order, result)
                        trade_pnls.append(portfolio.realized_pnl_total - before)
                else:
                    rejected += 1

            elif signal.action is Action.BUY and portfolio.position is None:
                equity = portfolio.equity(price)
                sizing = sizer.size_position(equity, price, signal.stop_price)
                if sizing.ok and sizing.size_base > 0:
                    order = OrderRequest(
                        side=Side.BUY,
                        size_base=sizing.size_base,
                        entry_price=price,
                        strategy=signal.strategy,
                        reason=signal.reason,
                        stop_price=signal.stop_price,
                        take_profit=signal.take_profit,
                    )
                    decision = risk.approve(order, equity,
                                            portfolio.exposure_notional(price),
                                            snapshot, ind, now=ts)
                    if decision.approved:
                        result = self.simulator.execute(order, price)
                        if result.success and result.filled_size_base * result.filled_price + result.fee_quote <= portfolio.quote_balance:
                            portfolio.apply_fill(order, result)
                    else:
                        rejected += 1

            equity_curve.append(portfolio.equity(price))

        report = compute_report(
            equity_curve, trade_pnls,
            candle_interval_seconds=self.candle_interval_seconds,
            failed_transactions=self.simulator.failed_transactions,
        )
        return BacktestResult(
            report=report,
            equity_curve=equity_curve,
            trade_pnls=trade_pnls,
            rejected_orders=rejected,
            regime_counts=regime_counts,
        )


@dataclass(frozen=True)
class WalkForwardResult:
    train: BacktestResult
    validate: BacktestResult
    test: BacktestResult


def walk_forward(
    candles: pd.DataFrame,
    train_frac: float = 0.6,
    validate_frac: float = 0.2,
    starting_equity: float = 1_000.0,
    candle_interval_seconds: int = 60,
) -> WalkForwardResult:
    """Split history chronologically and evaluate each segment independently.

    Never optimize on all history: fit (if anything) on train, tune on
    validate, and only ever *report* on test.
    """
    if not 0 < train_frac < 1 or not 0 < validate_frac < 1 or train_frac + validate_frac >= 1:
        raise ValueError("fractions must be positive and sum to < 1")

    n = len(candles)
    t_end = int(n * train_frac)
    v_end = int(n * (train_frac + validate_frac))
    segments = {
        "train": candles.iloc[:t_end],
        "validate": candles.iloc[t_end - WARMUP_CANDLES if t_end > WARMUP_CANDLES else 0:v_end],
        "test": candles.iloc[v_end - WARMUP_CANDLES if v_end > WARMUP_CANDLES else 0:],
    }
    results = {}
    for name, segment in segments.items():
        engine = BacktestEngine(starting_equity=starting_equity,
                                candle_interval_seconds=candle_interval_seconds)
        results[name] = engine.run(segment)
        logger.info("walk-forward %s: %s", name, results[name].report.to_dict())
    return WalkForwardResult(results["train"], results["validate"], results["test"])
