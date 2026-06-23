"""
run_15m.py — Download 15m klines + run all experiments on 15m timeframe.

Same structure as run_all_experiments.py but uses interval=15m.
"""
import sys
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import (
    download_klines, download_funding, load_klines, load_funding,
    months_list,
)
from engine import run_backtest
from metrics import format_scorecard, scorecard

CACHE_DIR = _HERE / "cache"
CACHE_15M = _HERE / "cache_15m"
SYMBOL = "SYNUSDT"
INTERVAL = "15m"
DEFAULT_SWEEP_STOP_PCTS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]

def make_cfg(symbol=SYMBOL, stop_mode="none", stop_pct=None,
             green_gate="off", position_model="single",
             direction="long", entry_mode="red_dip") -> BacktestConfig:
    return BacktestConfig(
        symbol=symbol,
        strategy=StrategyConfig(
            entry_mode=entry_mode,
            green_gate=green_gate,
            stop_mode=stop_mode,
            stop_pct=stop_pct,
            checkpoint_mode="first_green",
            position_model=position_model,
            direction=direction,
        ),
        dca=DCAConfig(),
        exec=ExecConfig(),
    )

# ── Fetch data ──
print(f"=== Fetching {INTERVAL} data into {CACHE_15M} ===", flush=True)
months = months_list("2025-12", "2026-06")
CACHE_15M.mkdir(parents=True, exist_ok=True)
klines_path = CACHE_15M / "klines.parquet"
funding_path = CACHE_15M / "funding.parquet"

if not klines_path.exists():
    download_klines(SYMBOL, months, out_dir=str(CACHE_15M), interval=INTERVAL)

if not funding_path.exists():
    download_funding(SYMBOL, months, out_dir=str(CACHE_15M))

klines = load_klines(klines_path)
funding = load_funding(funding_path)
n_bars = len(klines)
print(f"klines: {n_bars:,}  ({klines['open_time'].iloc[0]} -> {klines['open_time'].iloc[-1]})", flush=True)

# ── 1. DCA vs Single ──
print("\n" + "=" * 70, flush=True)
print("1. DCA vs SINGLE", flush=True)
print("=" * 70, flush=True)
for model in ("single", "dca"):
    named = []
    for stop, gate in [("none", "off"), ("fixed", "off"), ("fixed", "one")]:
        sp = 2.0 if stop == "fixed" else None
        label = f"{model} bare" if stop == "none" else (f"{model}+stop" if gate == "off" else f"{model}+stop+gate")
        cfg = make_cfg(stop_mode=stop, stop_pct=sp, green_gate=gate, position_model=model)
        result = run_backtest(klines, funding, cfg)
        sc = scorecard(result)
        named.append((label, sc))
        print(f"  {label:25s}: {sc['n_trades']:>5d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  exp={sc['expectancy']:+.4f}", flush=True)
    print(flush=True)
    print(format_scorecard(named), flush=True)
    print(flush=True)

# ── 2. green_confirm vs red_dip ──
print("=" * 70, flush=True)
print("2. ENTRY MODE: red_dip vs green_confirm", flush=True)
print("=" * 70, flush=True)
for entry in ("red_dip", "green_confirm"):
    named = []
    for stop, gate in [("none", "off"), ("fixed", "off"), ("fixed", "one")]:
        sp = 2.0 if stop == "fixed" else None
        label = f"{entry} bare" if stop == "none" else (f"{entry}+stop" if gate == "off" else f"{entry}+stop+gate")
        cfg = make_cfg(stop_mode=stop, stop_pct=sp, green_gate=gate, entry_mode=entry)
        result = run_backtest(klines, funding, cfg)
        sc = scorecard(result)
        named.append((label, sc))
        print(f"  {label:30s}: {sc['n_trades']:>5d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  exp={sc['expectancy']:+.4f}", flush=True)
    print(flush=True)
    print(format_scorecard(named), flush=True)
    print(flush=True)

# ── 3. Short ──
print("=" * 70, flush=True)
print("3. SHORT", flush=True)
print("=" * 70, flush=True)
named = []
for stop, gate in [("none", "off"), ("fixed", "off"), ("fixed", "one")]:
    sp = 2.0 if stop == "fixed" else None
    label = "short bare" if stop == "none" else ("short+stop" if gate == "off" else "short+stop+gate")
    cfg = make_cfg(stop_mode=stop, stop_pct=sp, green_gate=gate, direction="short")
    result = run_backtest(klines, funding, cfg)
    sc = scorecard(result)
    named.append((label, sc))
    print(f"  {label:20s}: {sc['n_trades']:>5d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  exp={sc['expectancy']:+.4f}", flush=True)
print(flush=True)
print(format_scorecard(named), flush=True)
print(flush=True)

# ── 4. Stop% sweep ──
print("=" * 70, flush=True)
print("4. STOP% SWEEP", flush=True)
print("=" * 70, flush=True)
named = []
for sp in DEFAULT_SWEEP_STOP_PCTS:
    label = f"stop={sp:.1f}%"
    cfg = make_cfg(stop_mode="fixed", stop_pct=sp)
    result = run_backtest(klines, funding, cfg)
    sc = scorecard(result)
    named.append((label, sc))
    print(f"  {label:12s}: {sc['n_trades']:>5d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  exp={sc['expectancy']:+.4f}  stops={sc['n_stops']}  liq={sc['n_liquidations']}  worst={sc['worst_trade']:+.2f}", flush=True)
print(flush=True)
print(format_scorecard(named), flush=True)
print(flush=True)
