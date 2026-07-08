"""Entry point and orchestration loop.

Wires the pipeline together and runs it:

    price feed → candles → indicators → regime → strategy → sizing
        → validation → RISK APPROVAL → execution → portfolio → database

Modes:
    paper    (default) full pipeline, simulated fills, no keys required
    live     real Jupiter swaps — requires PRIVATE_KEY, SOLANA_RPC_URL,
             TRADING_MODE=live, and should only ever run with tiny capital
    backtest replay a CSV of OHLCV candles through the engine

Run:  python -m solana_trading_bot.main [--mode paper|live|backtest] [--csv file]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.config.settings import Settings, load_settings
from solana_trading_bot.domain import Action, OrderRequest, Side
from solana_trading_bot.database.database import TradeLog
from solana_trading_bot.execution.order_validator import OrderValidator
from solana_trading_bot.execution.trade_executor import PaperExecutionBackend, TradeExecutor
from solana_trading_bot.market.candle_manager import CandleManager
from solana_trading_bot.market.indicators import IndicatorEngine
from solana_trading_bot.market.liquidity_monitor import LiquidityMonitor
from solana_trading_bot.market.price_feed import PriceFeed
from solana_trading_bot.market.regime_detector import RegimeDetector
from solana_trading_bot.portfolio.portfolio_manager import PortfolioManager
from solana_trading_bot.risk.position_sizer import PositionSizer
from solana_trading_bot.risk.risk_manager import RiskManager
from solana_trading_bot.backtesting.engine import default_router

logger = logging.getLogger("solana_trading_bot")


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(log_dir, "bot.log")),
        ],
    )


class TradingBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = asyncio.Event()

        self.trade_log = TradeLog(settings.db_url)
        record = self.trade_log.record_risk_event

        self.risk = RiskManager(
            liquidity_monitor=LiquidityMonitor(),
            event_recorder=record,
        )

        from solana_trading_bot.dex.jupiter_client import JupiterClient
        self.jupiter = JupiterClient(settings.jupiter_base_url, settings.jupiter_price_url)
        self.feed = PriceFeed(
            self.jupiter, settings.pair_base_mint, settings.pair_quote_mint,
            heartbeat=self.risk.heartbeat,
            poll_interval_s=settings.poll_interval_seconds,
        )
        self.candles = CandleManager(settings.candle_interval_seconds)
        self.indicators = IndicatorEngine()
        self.detector = RegimeDetector()
        self.router = default_router()
        self.sizer = PositionSizer()

        wallet_address = None
        self.rpc = None
        if settings.trading_mode == "live":
            from solana_trading_bot.wallet.wallet_manager import WalletManager
            from solana_trading_bot.blockchain.rpc_client import RpcClient
            from solana_trading_bot.blockchain.transaction_builder import TransactionBuilder
            from solana_trading_bot.dex.swap_executor import LiveSwapExecutor

            self.wallet = WalletManager(settings.expected_wallet_address)
            self.wallet.validate()
            wallet_address = self.wallet.address
            self.rpc = RpcClient(settings.rpc_url, heartbeat=self.risk.heartbeat)
            builder = TransactionBuilder(self.wallet)
            self.backend = LiveSwapExecutor(self.jupiter, self.rpc, self.wallet,
                                            builder, settings)
            # Live balances are fetched at startup in run().
            self.portfolio = PortfolioManager(quote_balance=0.0, trade_log=self.trade_log)
            logger.warning("LIVE MODE: wallet %s — this wallet must hold "
                           "limited capital only.", wallet_address)
        else:
            self.backend = PaperExecutionBackend(self.feed)
            self.portfolio = PortfolioManager(
                quote_balance=settings.paper_starting_usdc, trade_log=self.trade_log)
            logger.info("PAPER MODE: starting equity %.2f USDC",
                        settings.paper_starting_usdc)

        self.executor = TradeExecutor(
            validator=OrderValidator(settings.expected_wallet_address),
            risk_manager=self.risk,
            backend=self.backend,
            portfolio=self.portfolio,
            trade_log=self.trade_log,
            wallet_address=wallet_address,
        )

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        if self.settings.trading_mode == "live":
            await self.rpc.connect()
            from solders.pubkey import Pubkey
            owner = Pubkey.from_string(self.wallet.address)
            self.portfolio.quote_balance = await self.rpc.get_token_balance(
                owner, Pubkey.from_string(self.settings.pair_quote_mint))
            self.portfolio.base_balance = await self.rpc.get_sol_balance(owner)
            logger.info("Live balances: %.2f USDC, %.6f SOL",
                        self.portfolio.quote_balance, self.portfolio.base_balance)

        feed_task = asyncio.create_task(self.feed.run(self.stop_event))
        last_breaker_check = 0.0
        try:
            while not self.stop_event.is_set():
                await asyncio.sleep(self.settings.poll_interval_seconds)
                now = time.time()
                snap = self.feed.snapshot()

                # Layer 7: no fresh data → no decisions of any kind.
                if snap is None or self.feed.is_stale(now):
                    logger.warning("Market data stale or missing — trading paused")
                    continue
                if self.risk.heartbeat.halted:
                    logger.critical("Trading halted (%s); manual intervention "
                                    "required", self.risk.heartbeat.halt_reason)
                    self.stop_event.set()
                    break
                if self.trade_log.write_failures > 0:
                    logger.critical("Audit log is failing — halting for safety")
                    self.stop_event.set()
                    break

                self.candles.add_tick(snap.timestamp, snap.price)
                self.risk.flash_guard.record_price(snap.price, now=now)
                self.risk.liquidity.record(snap)

                equity = self.portfolio.equity(snap.price)
                if now - last_breaker_check >= cfg.CIRCUIT_CHECK_INTERVAL_SECONDS:
                    self.risk.circuit_breaker.check(equity, now)
                    last_breaker_check = now

                ind = self.indicators.compute(self.candles.dataframe())

                # 1. Protective exits always run first.
                if self.portfolio.position is not None:
                    forced = self.risk.manage_position(self.portfolio.position, snap, ind)
                    if forced is not None:
                        await self._exit_position(forced.strategy, forced.reason, snap, ind)
                        continue

                # 2. Regime → strategy decision.
                regime = self.detector.classify(ind)
                signal = self.router.decide(regime, ind, snap, self.portfolio.position)
                if signal.action is Action.HOLD:
                    continue
                logger.info("Regime %s | signal %s (%s)", regime.to_dict(),
                            signal.action.value, signal.reason)

                if signal.action is Action.EXIT and self.portfolio.position is not None:
                    await self._exit_position(signal.strategy, signal.reason, snap, ind)
                elif signal.action is Action.BUY and self.portfolio.position is None:
                    sizing = self.sizer.size_position(equity, snap.price, signal.stop_price)
                    if not sizing.ok:
                        logger.info("Sizing declined: %s", sizing.reason)
                        continue
                    order = OrderRequest(
                        side=Side.BUY,
                        size_base=sizing.size_base,
                        entry_price=snap.price,
                        strategy=signal.strategy,
                        reason=signal.reason,
                        stop_price=signal.stop_price,
                        take_profit=signal.take_profit,
                    )
                    await self.executor.submit(order, snap, ind)
        finally:
            self.stop_event.set()
            await feed_task
            await self.jupiter.close()
            if self.rpc is not None:
                await self.rpc.close()
            logger.info("Shutdown complete. Final equity: %.2f | realized P&L: %.2f",
                        self.portfolio.equity(snap.price if (snap := self.feed.snapshot()) else 0.0),
                        self.portfolio.realized_pnl_total)

    async def _exit_position(self, strategy: str, reason: str, snap, ind) -> None:
        pos = self.portfolio.position
        order = OrderRequest(
            side=Side.SELL,
            size_base=pos.size_base,
            entry_price=snap.price,
            strategy=strategy,
            reason=reason,
            reduce_only=True,
        )
        await self.executor.submit(order, snap, ind)

    def request_stop(self) -> None:
        logger.info("Stop requested — shutting down gracefully")
        self.stop_event.set()


def run_backtest(csv_path: str) -> None:
    import pandas as pd
    from solana_trading_bot.backtesting.engine import BacktestEngine, walk_forward
    from solana_trading_bot.backtesting.monte_carlo import MonteCarloTester

    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"CSV missing columns: {sorted(missing)}")

    result = BacktestEngine().run(df)
    print("\n=== Backtest ===")
    for k, v in result.report.to_dict().items():
        print(f"  {k:24s} {v}")
    print(f"  {'rejected_orders':24s} {result.rejected_orders}")
    print(f"  {'regimes':24s} {result.regime_counts}")

    print("\n=== Walk-forward (60/20/20) ===")
    wf = walk_forward(df)
    for name, seg in (("train", wf.train), ("validate", wf.validate), ("test", wf.test)):
        print(f"  {name:9s} {seg.report.to_dict()}")

    if len(result.trade_pnls) >= 5:
        print("\n=== Monte Carlo (2000 bootstrapped paths) ===")
        mc = MonteCarloTester().run(result.trade_pnls)
        for k, v in mc.to_dict().items():
            print(f"  {k:28s} {v}")
    else:
        print("\nMonte Carlo skipped: fewer than 5 closed trades.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Solana adaptive trading bot")
    parser.add_argument("--mode", choices=["paper", "live", "backtest"],
                        default=None, help="override TRADING_MODE")
    parser.add_argument("--csv", help="OHLCV csv for backtest mode")
    args = parser.parse_args()

    if args.mode:
        os.environ["TRADING_MODE"] = args.mode if args.mode != "backtest" else "paper"
    settings = load_settings()
    setup_logging(settings.log_dir)

    if args.mode == "backtest":
        if not args.csv:
            raise SystemExit("backtest mode requires --csv path/to/ohlcv.csv")
        run_backtest(args.csv)
        return

    bot = TradingBot(settings)
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.request_stop)
        except NotImplementedError:
            pass  # e.g. Windows
    try:
        loop.run_until_complete(bot.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
