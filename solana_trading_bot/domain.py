"""Shared domain types.

Every module communicates through the small, immutable-ish dataclasses in this
file. Nothing here imports from any other module in the package, which keeps
the dependency graph acyclic: strategies never import execution, execution
never imports strategies, and the risk engine can inspect everything.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


class Regime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MarketSnapshot:
    """Latest observed market state for the traded pair."""

    timestamp: float            # unix seconds when the data was observed
    price: float                # mid price, quote units per base unit
    spread_pct: Optional[float] = None   # (ask - bid) / mid, if known
    volume_24h: Optional[float] = None   # quote units
    liquidity_usd: Optional[float] = None
    source: str = "unknown"

    def age(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.timestamp


@dataclass(frozen=True)
class IndicatorSet:
    """Point-in-time indicator values. ``ready`` is False during warmup."""

    ready: bool
    timestamp: float = 0.0
    price: float = float("nan")
    ema50: float = float("nan")
    ema200: float = float("nan")
    rsi: float = float("nan")
    atr: float = float("nan")
    atr_pct: float = float("nan")        # atr / price
    atr_sma: float = float("nan")        # smoothed ATR baseline
    bb_upper: float = float("nan")
    bb_mid: float = float("nan")
    bb_lower: float = float("nan")
    volume: float = float("nan")
    volume_sma: float = float("nan")
    volatility_percentile: float = float("nan")  # 0..1
    return_5: float = float("nan")       # 5-candle return, for rapid-move detection


@dataclass(frozen=True)
class RegimeState:
    regime: Regime
    confidence: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Signal:
    """Strategy output. HOLD is always a valid decision."""

    action: Action
    strategy: str
    reason: str
    confidence: float = 1.0
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None

    @classmethod
    def hold(cls, strategy: str, reason: str) -> "Signal":
        return cls(action=Action.HOLD, strategy=strategy, reason=reason)


@dataclass
class OrderRequest:
    """A fully specified trade request handed to validation + risk approval."""

    side: Side
    size_base: float            # base token units (SOL)
    entry_price: float          # expected execution price (quote per base)
    strategy: str
    reason: str
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    reduce_only: bool = False   # True for exits: reduces risk, never adds it
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return self.size_base * self.entry_price

    @property
    def is_entry(self) -> bool:
        return not self.reduce_only


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    code: str
    reason: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    code: str
    reason: str

    @classmethod
    def passed(cls) -> "ValidationResult":
        return cls(True, "OK", "validation passed")


@dataclass
class Position:
    """A single open spot position (long base token, funded from quote)."""

    size_base: float
    entry_price: float
    stop_price: float
    take_profit: float
    strategy: str
    opened_at: float = field(default_factory=time.time)
    high_water: float = 0.0     # highest price seen since entry, for trailing

    def __post_init__(self) -> None:
        if self.high_water <= 0:
            self.high_water = self.entry_price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.size_base


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    filled_price: Optional[float] = None
    filled_size_base: Optional[float] = None
    fee_quote: float = 0.0
    signature: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Quote:
    """Normalized swap quote (Jupiter or simulated)."""

    input_mint: str
    output_mint: str
    in_amount: int              # raw units of input mint
    out_amount: int             # raw units of output mint
    price_impact_pct: float     # fraction, e.g. 0.004 = 0.4%
    expected_price: float       # quote units per base unit implied by amounts
    route_hops: int = 1
    raw: Optional[dict] = None


class ExecutionBackend(Protocol):
    """Anything that can fill an approved order (paper or live)."""

    async def execute(self, order: OrderRequest) -> ExecutionResult: ...


class ConfidenceEstimator(Protocol):
    """Optional ML layer hook.

    The ML layer never trades. It only answers: "is this setup favorable?"
    with a probability that the risk engine may use as an extra veto.
    """

    def estimate(self, order: OrderRequest, indicators: IndicatorSet) -> float: ...
