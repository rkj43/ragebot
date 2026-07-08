"""Central configuration.

Two kinds of values live here, and the distinction is deliberate:

1. HARD RISK LIMITS — module-level constants. They are intentionally NOT
   loadable from the environment, so a typo or a malicious ``.env`` file can
   never weaken risk controls. Changing them requires a code change and a
   code review.

2. ``Settings`` — operational values (RPC URL, trading mode, pair, database
   location) loaded from environment variables / ``.env``.

Secrets policy: this module never reads PRIVATE_KEY. Only
``wallet.wallet_manager`` touches it, and only to construct a keypair.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# HARD RISK LIMITS — never configurable via environment.
# ---------------------------------------------------------------------------

#: Layer 1 — maximum single position as a fraction of portfolio equity.
MAX_POSITION_PCT = 0.05

#: Layer 2 — capital at risk per trade (distance to stop), fraction of equity.
RISK_PER_TRADE_PCT = 0.01

#: Layer 4 — daily loss that trips the circuit breaker, fraction of equity.
DAILY_LOSS_LIMIT_PCT = 0.03

#: Maximum total non-quote exposure across all positions, fraction of equity.
MAX_TOTAL_EXPOSURE_PCT = 0.10

#: Layer 3 — default stop distance and take-profit distance, in ATRs.
STOP_ATR_MULT = 2.0
TAKE_PROFIT_ATR_MULT = 3.0
#: Start trailing the stop once unrealized profit exceeds this many ATRs.
TRAIL_TRIGGER_ATR = 1.0

#: Layer 7 — price data older than this halts trading (seconds).
MAX_DATA_AGE_SECONDS = 10.0

#: Layer 5 — flash-crash protection.
FLASH_CRASH_WINDOW_SECONDS = 30.0
FLASH_CRASH_DEVIATION_PCT = 0.04
FLASH_CRASH_PAUSE_SECONDS = 120.0

#: Layer 6 — liquidity protection.
MAX_SPREAD_MULTIPLE = 3.0          # reject if spread > 3x rolling normal
MIN_LIQUIDITY_USD = 250_000.0
MAX_PRICE_IMPACT_PCT = 0.01        # reject quotes with >1% price impact

#: Execution — reject if execution price deviates >0.5% from expected mid.
MAX_EXECUTION_DEVIATION_PCT = 0.005

#: Circuit breaker check cadence (seconds).
CIRCUIT_CHECK_INTERVAL_SECONDS = 300.0

#: Optional ML confidence gate. Signals scoring below this are vetoed when a
#: confidence estimator is attached; with no estimator the gate is inactive.
MIN_ML_CONFIDENCE = 0.55

# ---------------------------------------------------------------------------
# Well-known mints (mainnet).
# ---------------------------------------------------------------------------

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@dataclass(frozen=True)
class Settings:
    """Operational (non-risk) configuration loaded from the environment."""

    rpc_url: str
    trading_mode: str                  # "paper" (default) or "live"
    pair_base_mint: str = SOL_MINT
    pair_quote_mint: str = USDC_MINT
    base_decimals: int = 9             # SOL
    quote_decimals: int = 6            # USDC
    db_url: str = "sqlite:///solana_trading_bot.db"
    slippage_bps: int = 50             # slippage tolerance sent to Jupiter
    poll_interval_seconds: float = 2.0
    candle_interval_seconds: int = 60
    jupiter_base_url: str = "https://quote-api.jup.ag/v6"
    jupiter_price_url: str = "https://lite-api.jup.ag/price/v3"
    expected_wallet_address: Optional[str] = None
    log_dir: str = "logs"
    paper_starting_usdc: float = 1_000.0
    birdeye_api_key: Optional[str] = None
    birdeye_base_url: str = "https://public-api.birdeye.so"


class ConfigError(Exception):
    pass


def load_settings() -> Settings:
    """Build ``Settings`` from environment variables, failing loudly on bad
    values instead of silently falling back."""

    mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ConfigError(f"TRADING_MODE must be 'paper' or 'live', got {mode!r}")

    rpc_url = os.environ.get("SOLANA_RPC_URL", "").strip()
    if mode == "live" and not rpc_url:
        raise ConfigError("SOLANA_RPC_URL is required for live trading")
    if mode == "live" and not os.environ.get("PRIVATE_KEY"):
        raise ConfigError("PRIVATE_KEY is required for live trading")

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from exc

    slippage_bps = int(_float("SLIPPAGE_BPS", 50))
    if not 1 <= slippage_bps <= 200:
        raise ConfigError("SLIPPAGE_BPS must be between 1 and 200")

    return Settings(
        rpc_url=rpc_url or "https://api.mainnet-beta.solana.com",
        trading_mode=mode,
        db_url=os.environ.get("DATABASE_URL", "sqlite:///solana_trading_bot.db"),
        slippage_bps=slippage_bps,
        poll_interval_seconds=_float("POLL_INTERVAL_SECONDS", 2.0),
        candle_interval_seconds=int(_float("CANDLE_INTERVAL_SECONDS", 60)),
        jupiter_base_url=os.environ.get("JUPITER_BASE_URL", "https://quote-api.jup.ag/v6"),
        jupiter_price_url=os.environ.get("JUPITER_PRICE_URL", "https://lite-api.jup.ag/price/v3"),
        expected_wallet_address=os.environ.get("EXPECTED_WALLET_ADDRESS") or None,
        log_dir=os.environ.get("LOG_DIR", "logs"),
        paper_starting_usdc=_float("PAPER_STARTING_USDC", 1_000.0),
        birdeye_api_key=os.environ.get("BIRDEYE_API_KEY") or None,
        birdeye_base_url=os.environ.get("BIRDEYE_BASE_URL", "https://public-api.birdeye.so"),
    )
