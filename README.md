# SYNUSDT Futures Checkpoint-Scalp Backtest

Does the candle-color "checkpoint" strategy have positive expectancy on SYNUSDT 1m? This backtest answers that question by simulating realistic futures trading: bar-by-bar decisions on closed candles only, fees, funding, leverage, and liquidation risk. The goal is to settle whether small wins + unbounded losses (the live -57% runaway) can be tamed with disciplined stops and entry gates.

The strategy spec and the live loss that motivated it are in [`PLAN.md`](PLAN.md). Tuned live parameters are in [`live_params.md`](live_params.md).

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python run.py --fetch --start 2025-12 --end 2026-06
```

This fetches SYNUSDT 1m klines and funding rates for every month from `--start` to `--end` inclusive (here 7 months) from data.binance.vision and caches them in `cache/`. It then runs the three canonical variants side by side and prints the scorecard:

| Variant | What it is |
|---|---|
| `bare` | the rule alone: enter the first red after a green, exit on the next green |
| `+stop` | the rule plus a fixed 2% stop |
| `+stop+gate` | the rule plus the stop plus a one-green entry gate |

All three use the single-entry position model. Other options:

```bash
python run.py --start 2025-12 --end 2026-06            # run from the existing cache
python run.py --start 2025-12 --end 2026-06 --sweep    # add a stop-% grid sweep
python run.py --direction short                        # mirror the strategy
```

Without `--start` / `--end`, `run.py` uses 2024-12 to 2025-05.

**Note on the cache:** the files are named by symbol only (`cache/SYNUSDT-1m-klines.parquet`, `cache/SYNUSDT-funding.parquet`), not by date range. A second run with a different `--start` / `--end` reuses whatever range is already cached. Delete `cache/` to fetch a new range.

## Output

The scorecard goes to stdout, one column per variant: trades, win rate, average win and loss, payoff ratio, net PnL, expectancy per trade, max drawdown, worst trade, stop hits, liquidations, and the break-even win rate next to the actual win rate. Per-trade PnL is a fraction of margin, not notional, and net PnL is the sum of those fractions.

### Latest result (run on 2026-11-04)

SYNUSDT, 2025-12 to 2026-06: 305,280 one-minute candles and 1,271 funding rows.

| | bare | +stop | +stop+gate |
|---|---:|---:|---:|
| Trades | 77,018 | 77,141 | 49,240 |
| Win rate | 17.5% | 17.5% | 17.8% |
| Stops | 0 | 816 | 546 |
| Liquidations | 73 | 0 | 0 |
| Break-even win rate | 48.5% | 48.7% | 48.6% |

The stop removes every liquidation, but no variant comes close to its break-even win rate. On this data the rule has negative expectancy.

## What else is in this repo

- **Screener and experiments** (offline, same engine): `run_screener.py` ranks symbols by expectancy, `run_all_experiments.py` runs the `exp_*.py` battery, and `sweep_*.py` scripts sweep single parameters.
- **Live trading stack** (real money): `bot.py` (autonomous DCA bot), `trade.py` (manual-oracle variant), `oracle.py` (LLM go/no-go gate) and `dashboard.py` (Flask control plane). These need a `.env` built from [`.env.example`](.env.example). Start with `python bot.py --config bot_config.yaml --dry-run`, which places no real orders.

[`CLAUDE.md`](CLAUDE.md) describes the architecture of both halves.

## Tests

```bash
pip install pytest
pytest
```
