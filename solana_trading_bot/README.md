# Solana Adaptive On-Chain Trading Bot

A production-quality, risk-first spot trading bot for Solana (SOL/USDC via the
Jupiter aggregator). Built around the assumption that **markets are not
predictable**: the system is designed to survive changing regimes,
uncertainty, transaction costs, liquidity risk, and infrastructure failure.

Priority order, everywhere in the code:

1. Capital preservation
2. Risk management
3. Reliable execution
4. Strategy performance
5. Profit optimization

> A missed trade is acceptable. A blown account is unacceptable.

Spot only. No leverage, no futures, no borrowing, no margin, no meme coins.

---

## Architecture

Every trade flows through a one-way pipeline; the risk engine is a final,
non-bypassable gate:

```
PriceFeed ──► CandleManager ──► IndicatorEngine ──► RegimeDetector
                                                         │
                                              StrategyRouter (one strategy per regime)
                                                         │  Signal (BUY / EXIT / HOLD)
                                              PositionSizer (1% risk / stop distance)
                                                         │  OrderRequest
                                              OrderValidator (balance, impact, deviation)
                                                         │
                                    ┌────────────────────▼─────────────────────┐
                                    │  RiskManager.approve()  — LAYERS 1..7    │
                                    │  overrides everything; no bypass exists  │
                                    └────────────────────┬─────────────────────┘
                                                         │  RiskDecision(approved)
                                              TradeExecutor (dedupe, audit)
                                                         │
                                    PaperBackend  or  Jupiter + Solana backend
                                                         │
                                    PortfolioManager  +  SQLite audit trail
```

Design rules:

* **Hard risk limits are code constants** (`config/settings.py`), not
  environment variables — a bad `.env` cannot weaken them.
* **Strategies are pure decision functions.** They never size positions,
  never touch execution, never see the wallet.
* **Exits are `reduce_only`** and are allowed even when entries are blocked
  (flash-crash pause, SLEEP_MODE) — the bot can always de-risk.
* **Missing data counts against the trade.** Stale feed, dead RPC, or a
  failing audit log halt trading immediately.
* Shared dataclasses live in `domain.py`; no module imports "sideways",
  which keeps the dependency graph acyclic and testable.

## Risk layers (`risk/`)

| Layer | Rule | Rejection code |
|---|---|---|
| 1 | Max position 5% of equity (hard-coded) | `POSITION_LIMIT_EXCEEDED` |
| 2 | Size = 1% equity risk / stop distance; volatility shrinks size | sizing declines |
| 3 | Every entry needs stop (2 ATR) + take profit (3 ATR); trailing stop after 1 ATR profit | `MISSING_STOP` |
| 4 | Daily loss ≥ 3% → stop entries, close positions, SLEEP_MODE, **manual restart required** | `CIRCUIT_BREAKER_TRIGGERED` |
| 5 | Price >4% off its 30s average → entries paused 2 min | `FLASH_CRASH_DETECTED` |
| 6 | Spread > 3× normal, low liquidity, >1% impact, fragile routes | `SPREAD_TOO_WIDE`, `LOW_LIQUIDITY`, … |
| 7 | Data older than 10s / RPC failure / feed death → halt | `TRADING_HALTED`, `STALE_DATA` |

Additionally: total exposure capped at 10% of equity; execution rejected if
the quoted price deviates >0.5% from the expected mid — re-checked against a
fresh quote immediately before broadcasting.

## Regimes and strategies

`market/regime_detector.py` is deterministic and explains itself:
`{"regime": "BULL_TREND", "confidence": 0.8, "reason": "..."}`.

| Regime | Strategy | Behavior |
|---|---|---|
| `BULL_TREND` | `TrendFollowingStrategy` | Enter on EMA50>EMA200, RSI 40–65, rising volume; exit on EMA cross-down or trailing ATR stop |
| `SIDEWAYS` | `MeanReversionStrategy` | Enter below lower Bollinger Band with RSI<35; exit at the middle band or stop |
| `HIGH_VOLATILITY`, `BEAR_TREND` | `DefensiveStrategy` | Never opens; exits toward USDC |
| `UNKNOWN` | none | HOLD — not trading is a position |

**ML layer:** deliberately just a hook (`ConfidenceEstimator` in `domain.py`,
gated at 0.55 in the risk manager). A future XGBoost classifier can estimate
"is this setup favorable?" — it can only *veto* trades, never create them.
No model ships with the bot.

## File map

```
solana_trading_bot/
  main.py                     orchestration loop; paper / live / backtest modes
  domain.py                   shared dataclasses & protocols (no internal imports)
  config/settings.py          hard risk constants + env-loaded operational settings
  wallet/wallet_manager.py    key from env only; never logged or persisted; mismatch = hard error
  blockchain/rpc_client.py    retries + heartbeat escalation; never trades blind
  blockchain/transaction_builder.py  decode/verify/sign Jupiter transactions
  dex/jupiter_client.py       quotes, swap transactions, prices (normalized)
  dex/swap_executor.py        live backend: fresh-quote re-check before broadcast
  market/price_feed.py        polling feed with staleness detection
  market/candle_manager.py    tick → OHLCV aggregation
  market/indicators.py        EMA50/200, RSI, ATR, Bollinger, volatility percentile
  market/liquidity_monitor.py spread baseline, liquidity floor, impact caps
  market/regime_detector.py   deterministic regime classification
  strategies/                 trend following, mean reversion, defensive + router
  risk/                       position sizer, circuit breaker, exposure, risk manager
  execution/order_validator.py  wallet/balance/impact/deviation checks
  execution/trade_executor.py   dedupe + validator + risk gate + audit; paper backend
  portfolio/portfolio_manager.py  balances, position, equity, realized P&L
  database/                   SQLAlchemy models (trades / orders / risk_events) + TradeLog
  backtesting/                engine (walk-forward), simulator, metrics, Monte Carlo
  testing/                    risk tests, execution tests, chaos tests
```

## Setup

```bash
cd solana_trading_bot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in; PRIVATE_KEY only needed for live mode
```

### Paper trading (default — start here)

```bash
python -m solana_trading_bot.main --mode paper
```

Runs the full pipeline against live Jupiter prices with simulated fills.
No keys required. Let it run long enough to build 200 one-minute candles
(EMA200 warmup) before expecting any signals.

### Backtesting

```bash
python -m solana_trading_bot.main --mode backtest --csv path/to/ohlcv.csv
```

The CSV needs `open,high,low,close,volume` columns with a datetime index.
Prints the full report (return, max drawdown, Sharpe, Sortino, win rate,
profit factor, trade count, simulated failed transactions), a 60/20/20
walk-forward comparison, and a 2,000-path Monte Carlo bootstrap (return
distribution, drawdown distribution, ruin probability). If the validate/test
segments look nothing like train, or the Monte Carlo 5th percentile is ugly —
believe them, not the headline number.

### Live trading — only after everything else

Phases 1–6 (wallet, data, indicators, regimes, paper trading, risk engine,
backtesting, Jupiter integration) must be verified first. Then:

1. Fund a **dedicated wallet with tiny capital you can afford to lose**.
2. Set `TRADING_MODE=live`, `PRIVATE_KEY`, `SOLANA_RPC_URL`, and
   `EXPECTED_WALLET_ADDRESS` (refuses to start on mismatch).
3. `python -m solana_trading_bot.main --mode live`

If the circuit breaker trips, the bot enters SLEEP_MODE and stays there —
restarting requires a deliberate human decision (a fresh process start after
you have understood *why* it tripped).

## Tests

```bash
pytest solana_trading_bot/testing -v
```

Covers the risk layers (position limit, sizing math, stops/trailing, circuit
breaker including "new day must NOT auto-reset"), the execution path (risk
rejections can never reach a backend), and the six mandated chaos scenarios:
missing data → halt, huge spread → reject, flash crash → pause, duplicate
transaction → ignored, RPC failure → halt, wallet mismatch → reject.

## Security notes

* `PRIVATE_KEY` is read from the environment only, parsed once, never
  printed, never stored; `repr(wallet)` shows the public address only.
* `.gitignore` excludes `.env`, databases, and logs.
* The transaction builder refuses to sign any transaction whose fee payer is
  not our wallet.
* Assume the trading wallet holds limited capital. Always.

## Disclaimer

This software executes real financial transactions when run in live mode.
It is provided for educational purposes without warranty of any kind.
Cryptocurrency trading can lose all capital involved. Nothing here is
financial advice.
