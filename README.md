# ragebot 🤖

A risk-first, adaptive spot trading bot for Solana (SOL/USDC via the Jupiter
aggregator). Built to survive bad markets: capital preservation and risk
management come before profit. Spot only — no leverage, no margin, no meme
coins.

**Key safety features**

- Every trade passes a non-bypassable 7-layer risk engine: 5% hard position
  cap, 1% risk-per-trade sizing, mandatory ATR stops, a 3% daily-loss circuit
  breaker (manual restart required), flash-crash protection, liquidity/spread
  checks, and an automatic halt on stale data or RPC failure.
- Deterministic market regime detection (bull / bear / sideways / high
  volatility) picks the strategy — or holds. Not trading is a valid decision.
- Paper trading by default; live mode requires explicit opt-in and is meant
  for tiny capital only.

Full architecture, risk-layer table, and module map: [`solana_trading_bot/README.md`](solana_trading_bot/README.md)

## How to use

**1. Install** (Python 3.12+)

```bash
git clone https://github.com/rkj43/ragebot.git
cd ragebot/solana_trading_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cd ..
```

**2. Paper trade (start here — no keys needed)**

```bash
python -m solana_trading_bot.main --mode paper
```

Trades against live Jupiter prices with simulated fills. Give it time to
build ~200 one-minute candles (EMA200 warmup) before expecting signals.
Everything is logged to `logs/bot.log` and a SQLite database
(`solana_trading_bot.db`: trades, orders, risk events).

**3. Backtest a strategy on historical data**

```bash
python -m solana_trading_bot.main --mode backtest --csv path/to/ohlcv.csv
```

The CSV needs a datetime index plus `open,high,low,close,volume` columns.
Prints performance metrics, a walk-forward split, and a Monte Carlo
robustness report.

**4. Run tests**

```bash
pytest solana_trading_bot/testing -v
```

**5. Go live — only after paper trading and backtests look sane**

Fund a dedicated wallet with a small amount you can afford to lose, then in
`.env` set:

```
TRADING_MODE=live
PRIVATE_KEY=<your base58 secret key>
SOLANA_RPC_URL=<your RPC endpoint>
EXPECTED_WALLET_ADDRESS=<your wallet address>   # refuses to start on mismatch
```

```bash
python -m solana_trading_bot.main --mode live
```

If the daily circuit breaker trips, the bot stops and stays stopped until you
restart it deliberately.

## Disclaimer

This software can execute real financial transactions. Educational use only,
no warranty, and crypto trading can lose all capital involved. Not financial
advice.
