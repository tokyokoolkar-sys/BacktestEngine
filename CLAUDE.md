# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two coupled systems around one strategy — the candle-color "checkpoint" DCA scalp on Binance USDⓈ-M futures:

1. **Backtest engine** (offline research): proves/sweeps the strategy on historical klines. Entry point `run.py`.
2. **Live trading stack** (real money): `bot.py` autonomous bot + `trade.py` manual-oracle bot + `oracle.py` AI gate + `dashboard.py` Flask control plane.

The strategy logic (`strategy.py`'s `CheckpointStrategy` + `config.py`'s `StrategyConfig`) is **shared** between backtest and live — the live bot instantiates the same state machine so backtest results map to live behavior. Strategy spec, rationale, and the live loss that motivated it are in `PLAN.md`; tuned live params are in `live_params.md`.

## Commands

```bash
pip install -r requirements.txt

# Backtest (no network in the hot loop; data cached to cache/ as parquet)
python run.py --start 2024-12 --end 2025-05 --fetch   # download then run 3 variants
python run.py --start 2024-12 --end 2025-05            # run from existing cache
python run.py --start 2024-12 --end 2025-05 --sweep    # add a stop_pct grid sweep
python run.py --direction short                        # mirror strategy

# Screener / experiments (offline, over the symbol universe)
python run_screener.py        # rank symbols by expectancy
python run_all_experiments.py # run the exp_*.py battery

# Live bot (needs .env — see below)
python bot.py --config bot_config.yaml --dry-run   # simulate, NO real orders
python bot.py --config bot_config.yaml --once      # one iteration then exit
python bot.py --config bot_config.yaml             # loop forever (default)
python bot.py --config bot_config.yaml --status    # print active positions
python trade.py                                    # manual-oracle variant
python dashboard.py --port 5000                    # control-plane UI at localhost:5000

# Tests (pytest; no config file — run by name)
pytest                              # all
pytest test_engine_bugs.py          # backtest engine regressions
pytest test_bot_regressions.py      # live-bot regressions
pytest test_manual_trade.py
pytest test_engine_bugs.py::test_name   # single test
```

## Backtest architecture

Data flows: `data.py` (download/cache) → `engine.py` (sim) → `metrics.py` (scorecard). All wired by `run.py`.

- **`config.py`** — pure dataclass contracts, no logic. `BacktestConfig` bundles `StrategyConfig` + `DCAConfig` + `ExecConfig`. Every module imports from here. Read this first to understand the parameter surface.
- **`data.py`** — pulls SYNUSDT (and other) 1m klines + 8h funding from **data.binance.vision** bulk dumps (direct `fapi.binance.com` is ISP-blocked in the dev environment). Caches to `cache/` parquet, reused across runs.
- **`engine.py`** — `run_backtest()` is the event loop. Bar-by-bar over **closed candles only — no lookahead** (the core invariant; `test_engine_bugs.py` guards it). Models taker/maker fees, per-8h funding, slippage, leverage, and **liquidation price from maintenance margin** (a forced close = the "runaway" event the whole project exists to measure). Supports both `single` and `dca` position models.
- **`strategy.py`** — `CheckpointStrategy`, the 3-state machine (`FLAT_ARMED` / `IN_POSITION` / `FLAT_LOCKED`) from `PLAN.md` §4. You only ever enter the first red after a green. Entry/exit/gate variants are all `StrategyConfig` flags.
- **`metrics.py`** — `scorecard()` + `format_scorecard()`. Reports break-even win rate vs actual, worst single trade, liquidation count.
- **`screener.py` / `run_screener*.py` / `exp_*.py`** — multi-symbol ranking and parameter experiments built on the same engine.

`Trade.pnl_pct` is a fraction of **margin**, not notional. `exit_reason` ∈ `checkpoint | stop | liquidation | take_profit | end`.

## Live trading architecture

- **`bot.py`** — `DcaBot`, the autonomous bot. `run_loop()` polls every `poll_interval_sec`; each iteration runs `run_once()` → `_check_signal` (reuses `CheckpointStrategy`) → optional oracle gate → `_enter` (base order + DCA safety ladder) → `_manage_baskets` (`_check_safety_fills`, `_replace_tp`, `_check_closure`). `reconcile_on_start()` recovers live positions/orders after a restart. State per position is an in-memory `Basket`.
- **`oracle.py`** — `AiOracle`, an LLM "go/no-go" timing gate (OpenAI-compatible endpoint, configured via `bot_config.yaml`'s `oracle:` block). **Fail-closed**: a `None` return (API failure) MUST block entry, never fall open. Handles reasoning-model quirks (answer left in `reasoning_content`, trailing JSON).
- **`trade.py`** — manual-oracle variant: a human (via the dashboard's manual page) supplies the go signal instead of the LLM. Shares `DcaBot` machinery.
- **`dashboard.py`** — single-file Flask app (HTML/JS inlined as template strings). It does **not** trade directly; it launches `bot.py`/`trade.py` as detached subprocesses and communicates through **JSON files on disk**:
  - writes `bot_control.json` / `manual_control.json` (commands; e.g. manual enter requests)
  - reads `bot_status.json` (positions, balance, oracle history — written by the bot's `_write_status_json`) and `bot_trade_log.csv`
  - all `POST /api/control/*` routes require the `TRADE_CONTROL_TOKEN` env var (else 503)
  - process liveness via PID checks (`_pid_alive`, cross-platform — Windows uses `OpenProcess`, duplicated in `bot.py`/`trade.py` on purpose to avoid a circular import)
- **`perps.py`** — `fetch_usdt_perps()` supplies the symbol-picker universe. Layered fallback so it never returns `[]`: fresh in-memory cache → live API (several `fapi*.binance.com` host mirrors) → persistent on-disk cache (`perps_cache.json`, gitignored) → bundled static list. Refreshes both caches on a successful fetch. Matters because direct `fapi.binance.com` is ISP-blocked in the dev environment.

## Config & secrets flow

`.env` (gitignored; see `.env.example`) → exported as env vars → `bot_config.yaml` references them as `${BINANCE_API_KEY}` etc., expanded by `BotConfig.from_yaml` via `os.path.expandvars`. Required: `BINANCE_API_KEY`, `BINANCE_API_SECRET` (both validated as 64-char alphanumeric HMAC keys before any order), `TRADE_CONTROL_TOKEN` (dashboard control). Optional oracle: `GENFITY_API_KEY` / endpoint / model.

`bot_config.yaml` `global:` block is the live risk surface: `max_dca_orders` (**global, not per-symbol** — a known constraint; see `live_params.md` footnote), `max_concurrent_baskets`, `daily_loss_limit`, leverage, fees. Per-symbol `tp_pct` / `stop_pct` / `active` flags live in the `symbols:` list.

## Conventions worth knowing

- The DCA notional/price ladder math is **deliberately duplicated** in `engine.py` and `bot.py` (`_dca_safety_notionals`, `_dca_safety_prices`, etc.) so the live bot mirrors the backtest exactly. Change both together and check against the regression tests.
- Backtest "always print these three variants side by side": `bare | +stop | +stop+gate` (`run.py::_three_variants`).
- Live discipline assumption baked into the design: **the stop always fires** — the backtest is only honest if live-you never widens it.
