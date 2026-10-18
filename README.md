# SYNUSDT Futures Checkpoint-Scalp Backtest

Does the candle-color "checkpoint" strategy have positive expectancy on SYNUSDT 1m? This backtest answers that question by simulating realistic futures trading: bar-by-bar decisions on closed candles only, fees, funding, leverage, and liquidation risk. The goal is to settle whether small wins + unbounded losses (the live -57% runaway) can be tamed with disciplined stops and entry gates.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python run.py --fetch --start 2025-12 --end 2026-06
```

This fetches 6 months of SYNUSDT 1m klines and funding rates from data.binance.vision, caches them locally, runs the strategy over all variants (bare rule → rule + stop → rule + stop + gate), and prints the scorecard: win rate, net PnL, expectancy per trade, max drawdown, liquidation count, and break-even hurdle.

## Output

- **Scorecard** (stdout): metrics per variant, equity curve
- **Cache** (cache/): parquet files for klines, funding rates, leverage brackets (reused on next run)

Consult `PLAN.md` for strategy rules, parameter sweep details, and what each metric means.
