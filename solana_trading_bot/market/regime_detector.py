"""Deterministic market regime classifier.

Pure function of the indicator set — no randomness, no learned parameters —
so every classification is reproducible and explainable. Checks run in a
fixed priority order:

1. insufficient data            → UNKNOWN (never trade blind)
2. abnormal volatility          → HIGH_VOLATILITY (safety first)
3. trend alignment              → BULL_TREND / BEAR_TREND
4. quiet, range-bound tape      → SIDEWAYS
5. anything unconvincing        → UNKNOWN
"""

from __future__ import annotations

from solana_trading_bot.domain import IndicatorSet, Regime, RegimeState

# Thresholds — deliberately hard-coded and auditable.
HIGH_VOL_ATR_RATIO = 2.0        # ATR > 2x its own 50-period average
HIGH_VOL_PERCENTILE = 0.95      # volatility in the top 5% of the lookback
RAPID_MOVE_PCT = 0.03           # >3% move over 5 candles
ACCEPTABLE_VOL_PERCENTILE = 0.90
SIDEWAYS_EMA_DISTANCE = 0.01    # price within 1% of EMA50
SIDEWAYS_MAX_ATR_PCT = 0.005    # ATR below 0.5% of price
MIN_TREND_CONFIDENCE = 0.30


class RegimeDetector:
    def classify(self, ind: IndicatorSet) -> RegimeState:
        if not ind.ready:
            return RegimeState(Regime.UNKNOWN, 0.0, "insufficient indicator history")

        # --- HIGH_VOLATILITY: checked first; safety outranks opportunity ---
        atr_ratio = ind.atr / ind.atr_sma if ind.atr_sma > 0 else 1.0
        rapid_move = abs(ind.return_5) > RAPID_MOVE_PCT
        if atr_ratio > HIGH_VOL_ATR_RATIO or ind.volatility_percentile > HIGH_VOL_PERCENTILE or rapid_move:
            reasons = []
            if atr_ratio > HIGH_VOL_ATR_RATIO:
                reasons.append(f"ATR {atr_ratio:.1f}x its baseline")
            if ind.volatility_percentile > HIGH_VOL_PERCENTILE:
                reasons.append(f"volatility percentile {ind.volatility_percentile:.2f}")
            if rapid_move:
                reasons.append(f"{ind.return_5:+.2%} move over 5 candles")
            confidence = min(1.0, 0.6 + 0.2 * len(reasons))
            return RegimeState(Regime.HIGH_VOLATILITY, confidence, "; ".join(reasons))

        # --- Trends: price/EMA alignment with acceptable volatility ---
        ema_sep = (ind.ema50 - ind.ema200) / ind.ema200 if ind.ema200 > 0 else 0.0
        vol_ok = ind.volatility_percentile <= ACCEPTABLE_VOL_PERCENTILE

        if ind.price > ind.ema200 and ind.ema50 > ind.ema200 and vol_ok:
            confidence = min(1.0, abs(ema_sep) / 0.02)  # full confidence at 2% separation
            if confidence >= MIN_TREND_CONFIDENCE:
                return RegimeState(
                    Regime.BULL_TREND, confidence,
                    f"price above EMA200, EMA50 {ema_sep:+.2%} above EMA200, volatility acceptable",
                )

        if ind.price < ind.ema200 and ind.ema50 < ind.ema200:
            confidence = min(1.0, abs(ema_sep) / 0.02)
            if confidence >= MIN_TREND_CONFIDENCE:
                return RegimeState(
                    Regime.BEAR_TREND, confidence,
                    f"price below EMA200, EMA50 {ema_sep:+.2%} below EMA200",
                )

        # --- SIDEWAYS: near the mean with a quiet ATR ---
        ema_distance = abs(ind.price - ind.ema50) / ind.price if ind.price > 0 else 1.0
        if ema_distance < SIDEWAYS_EMA_DISTANCE and ind.atr_pct < SIDEWAYS_MAX_ATR_PCT:
            confidence = min(1.0, 0.5 + (SIDEWAYS_EMA_DISTANCE - ema_distance) / SIDEWAYS_EMA_DISTANCE * 0.5)
            return RegimeState(
                Regime.SIDEWAYS, confidence,
                f"price within {ema_distance:.2%} of EMA50, ATR {ind.atr_pct:.2%} of price",
            )

        return RegimeState(Regime.UNKNOWN, 0.0, "no regime rule matched with sufficient confidence")
