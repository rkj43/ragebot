"""The risk engine. THIS MODULE OVERRIDES EVERYTHING.

``RiskManager.approve()`` is the single gate every order must pass; the trade
executor refuses to touch an execution backend without an approved
``RiskDecision``. No strategy, sizer, or human convenience path can bypass it.

Layers enforced here (or by the components this class owns):

1. hard position limit (5% of equity)          → POSITION_LIMIT_EXCEEDED
2. risk-based sizing (done by PositionSizer upstream; stop required here)
3. stop loss / take profit presence + trailing stop management
4. daily loss circuit breaker (3%)             → CIRCUIT_BREAKER_TRIGGERED
5. flash crash guard (30s avg, 4% deviation)   → FLASH_CRASH_DETECTED
6. liquidity protection (spread / impact)      → via LiquidityMonitor
7. heartbeat: stale data / RPC / feed death    → immediate halt

Exit orders (``reduce_only``) skip entry-only checks: the bot may always
reduce risk, even mid flash-crash or in SLEEP_MODE.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from solana_trading_bot.config import settings as cfg
from solana_trading_bot.domain import (
    Action,
    IndicatorSet,
    MarketSnapshot,
    OrderRequest,
    Position,
    Quote,
    RiskDecision,
    Signal,
)
from solana_trading_bot.risk.circuit_breaker import CircuitBreaker
from solana_trading_bot.risk.exposure_manager import ExposureManager

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """Layer 7 — the bot must never trade on information it cannot trust."""

    def __init__(self, event_recorder=None) -> None:
        self._record = event_recorder
        self._last_data_ts: Optional[float] = None
        self._halted = False
        self.halt_reason: Optional[str] = None

    def beat_data(self, data_ts: Optional[float] = None) -> None:
        self._last_data_ts = data_ts if data_ts is not None else time.time()

    def beat_rpc(self) -> None:  # successful RPC round-trip
        pass

    def report_rpc_failure(self, detail: str) -> None:
        self._halt(f"RPC failure: {detail}")

    def report_feed_failure(self, detail: str) -> None:
        self._halt(f"market data feed failure: {detail}")

    def report_ws_disconnect(self, detail: str) -> None:
        self._halt(f"websocket disconnected: {detail}")

    def _halt(self, reason: str) -> None:
        if not self._halted:
            logger.critical("TRADING_HALTED: %s", reason)
            if self._record is not None:
                self._record("TRADING_HALTED", reason)
        self._halted = True
        self.halt_reason = reason

    def data_is_stale(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (self._last_data_ts is None
                or now - self._last_data_ts > cfg.MAX_DATA_AGE_SECONDS)

    def healthy(self, now: Optional[float] = None) -> bool:
        return not self._halted and not self.data_is_stale(now)

    @property
    def halted(self) -> bool:
        return self._halted

    def clear_halt(self) -> None:
        """Manual recovery only, after the operator verified connectivity."""
        self._halted = False
        self.halt_reason = None


class FlashCrashGuard:
    """Layer 5 — 30-second rolling average; >4% deviation pauses entries."""

    def __init__(self, event_recorder=None) -> None:
        self._record = event_recorder
        self._window: deque[tuple[float, float]] = deque()
        self._paused_until: float = 0.0

    def record_price(self, price: float, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self._window.append((now, price))
        cutoff = now - cfg.FLASH_CRASH_WINDOW_SECONDS
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

        if len(self._window) < 3:
            return
        avg = sum(p for _, p in self._window) / len(self._window)
        if avg <= 0:
            return
        deviation = abs(price / avg - 1.0)
        if deviation > cfg.FLASH_CRASH_DEVIATION_PCT and now >= self._paused_until:
            self._paused_until = now + cfg.FLASH_CRASH_PAUSE_SECONDS
            msg = (f"price {price:.4f} deviates {deviation:.2%} from 30s average "
                   f"{avg:.4f}; entries paused {cfg.FLASH_CRASH_PAUSE_SECONDS:.0f}s")
            logger.warning("FLASH_CRASH_DETECTED: %s", msg)
            if self._record is not None:
                self._record("FLASH_CRASH_DETECTED", msg)

    def entries_allowed(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self._paused_until


class RiskManager:
    def __init__(
        self,
        circuit_breaker: Optional[CircuitBreaker] = None,
        heartbeat: Optional[HeartbeatMonitor] = None,
        flash_guard: Optional[FlashCrashGuard] = None,
        exposure_manager: Optional[ExposureManager] = None,
        liquidity_monitor=None,
        confidence_estimator=None,
        event_recorder=None,
    ) -> None:
        self._record = event_recorder or (lambda event, details: None)
        self.circuit_breaker = circuit_breaker or CircuitBreaker(event_recorder)
        self.heartbeat = heartbeat or HeartbeatMonitor(event_recorder)
        self.flash_guard = flash_guard or FlashCrashGuard(event_recorder)
        self.exposure = exposure_manager or ExposureManager()
        self.liquidity = liquidity_monitor
        self.confidence_estimator = confidence_estimator

    # ------------------------------------------------------------------ #
    # The gate.                                                          #
    # ------------------------------------------------------------------ #
    def approve(
        self,
        order: OrderRequest,
        equity: float,
        current_exposure_notional: float,
        snapshot: Optional[MarketSnapshot],
        indicators: Optional[IndicatorSet] = None,
        quote: Optional[Quote] = None,
        now: Optional[float] = None,
    ) -> RiskDecision:
        now = now if now is not None else time.time()

        def reject(code: str, reason: str) -> RiskDecision:
            logger.warning("RISK REJECT [%s] order %s: %s", code, order.client_order_id, reason)
            self._record(code, f"order {order.client_order_id}: {reason}")
            return RiskDecision(False, code, reason)

        # Layer 7 — heartbeat. Applies to entries AND exits: with no trusted
        # data we cannot even price an exit; halting is the safe state.
        if self.heartbeat.halted:
            return reject("TRADING_HALTED", self.heartbeat.halt_reason or "halted")
        if snapshot is None or snapshot.age(now) > cfg.MAX_DATA_AGE_SECONDS:
            age = "missing" if snapshot is None else f"{snapshot.age(now):.1f}s old"
            return reject("STALE_DATA", f"price data {age} (limit {cfg.MAX_DATA_AGE_SECONDS:.0f}s)")

        # Layer 4 — circuit breaker (also refreshed by the 5-minute loop).
        self.circuit_breaker.check(equity, now)
        if order.is_entry and not self.circuit_breaker.allows_new_trades:
            return reject("CIRCUIT_BREAKER_ACTIVE",
                          self.circuit_breaker.trip_reason or "SLEEP_MODE")

        # Exits: past this point only sanity checks; reducing risk is allowed.
        if not order.is_entry:
            if order.size_base <= 0:
                return reject("INVALID_ORDER", "exit with non-positive size")
            return RiskDecision(True, "OK", "exit approved (reduce-only)")

        # Layer 5 — flash crash pause blocks entries.
        if not self.flash_guard.entries_allowed(now):
            return reject("FLASH_CRASH_PAUSE", "entries paused after flash-crash detection")

        # Layer 3 — every entry needs a stop and a coherent target.
        if order.stop_price is None or order.stop_price <= 0:
            return reject("MISSING_STOP", "entry order has no stop loss")
        if order.stop_price >= order.entry_price:
            return reject("INVALID_STOP", "stop must be below entry for a long")
        if order.take_profit is not None and order.take_profit <= order.entry_price:
            return reject("INVALID_TAKE_PROFIT", "take profit must be above entry")

        # Layer 1 — hard position limit.
        if equity <= 0:
            return reject("NO_EQUITY", "equity is zero or negative")
        if order.notional > equity * cfg.MAX_POSITION_PCT * (1 + 1e-9):
            return reject(
                "POSITION_LIMIT_EXCEEDED",
                f"notional {order.notional:.2f} > {cfg.MAX_POSITION_PCT:.0%} "
                f"of equity ({equity * cfg.MAX_POSITION_PCT:.2f})",
            )

        # Total exposure.
        exp = self.exposure.check(order, equity, current_exposure_notional)
        if not exp.ok:
            return reject(exp.code, exp.reason)

        # Layer 6 — liquidity, spread, impact, route quality.
        if self.liquidity is not None:
            liq = self.liquidity.check(snapshot, quote)
            if not liq.ok:
                return reject(liq.code, liq.reason)

        # Optional ML confidence gate — an extra veto, never a green light.
        if self.confidence_estimator is not None and indicators is not None:
            confidence = self.confidence_estimator.estimate(order, indicators)
            if confidence < cfg.MIN_ML_CONFIDENCE:
                return reject(
                    "LOW_CONFIDENCE",
                    f"ML confidence {confidence:.2f} below {cfg.MIN_ML_CONFIDENCE:.2f}",
                )

        return RiskDecision(True, "OK", "all risk layers passed")

    # ------------------------------------------------------------------ #
    # Position management: stops, take profit, trailing.                 #
    # ------------------------------------------------------------------ #
    def manage_position(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        indicators: Optional[IndicatorSet] = None,
    ) -> Optional[Signal]:
        """Return a forced EXIT signal if a protective level is hit, updating
        the trailing stop along the way. Runs before any strategy logic."""
        price = snapshot.price
        position.high_water = max(position.high_water, price)

        # Trailing: once in profit by TRAIL_TRIGGER_ATR, ratchet the stop up.
        if indicators is not None and indicators.ready and indicators.atr > 0:
            if position.high_water - position.entry_price >= cfg.TRAIL_TRIGGER_ATR * indicators.atr:
                trailed = position.high_water - cfg.STOP_ATR_MULT * indicators.atr
                if trailed > position.stop_price:
                    position.stop_price = trailed

        if price <= position.stop_price:
            return Signal(Action.EXIT, "risk_manager",
                          f"stop loss hit: price {price:.4f} <= stop {position.stop_price:.4f}")
        if position.take_profit and price >= position.take_profit:
            return Signal(Action.EXIT, "risk_manager",
                          f"take profit hit: price {price:.4f} >= target {position.take_profit:.4f}")
        if self.circuit_breaker.must_close_positions:
            return Signal(Action.EXIT, "risk_manager",
                          "circuit breaker SLEEP_MODE: closing all positions")
        return None
