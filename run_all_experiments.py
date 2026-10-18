"""
run_all_experiments.py — All SYNUSDT experiments in one shot.

Runs:
  1. DCA model vs Single model (bare / +stop / +stop+gate / stop sweep)
  2. green_confirm vs red_dip (3 variants each)
  3. Short direction (3 variants)
  4. Stop-pct sweep (gate=off, single, long)
"""

import sys
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import run_backtest
from metrics import format_scorecard, scorecard

CACHE_DIR = _HERE / "cache"
DEFAULT_SWEEP_STOP_PCTS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]

def load_data():
    klines = load_klines(CACHE_DIR / "klines.parquet")
    funding = load_funding(CACHE_DIR / "funding.parquet")
    return klines, funding

def make_cfg(symbol="SYNUSDT", stop_mode="none", stop_pct=None,
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

# ── Load ──
print("Loading data...", flush=True)
klines, funding = load_data()
n_bars = len(klines)
print(f"klines: {n_bars:,}  funding: {len(funding):,}\n", flush=True)

# ── 1. DCA vs Single (3 variants each) ──
print("=" * 70)
print("1. DCA vs SINGLE — 3 variants each")
print("=" * 70)

for model in ("single", "dca"):
    named = []
    for stop, gate in [("none", "off"), ("fixed", "off"), ("fixed", "one")]:
        stop_pct = 2.0 if stop == "fixed" else None
        label = f"{model} bare" if stop == "none" else (f"{model}+stop" if gate == "off" else f"{model}+stop+gate")
        cfg = make_cfg(stop_mode=stop, stop_pct=stop_pct, green_gate=gate, position_model=model)
        result = run_backtest(klines, funding, cfg)
        sc = scorecard(result)
        named.append((label, sc))
        print(f"  {label:25s}: {sc['n_trades']:>6d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  "
              f"exp={sc['expectancy']:+.4f}  liq={sc['n_liquidations']}", flush=True)
    print()
    print(format_scorecard(named))
    print()

# ── 2. green_confirm vs red_dip ──
print("=" * 70)
print("2. ENTRY MODE: red_dip vs green_confirm — 3 variants each")
print("=" * 70)

for entry in ("red_dip", "green_confirm"):
    named = []
    for stop, gate in [("none", "off"), ("fixed", "off"), ("fixed", "one")]:
        stop_pct = 2.0 if stop == "fixed" else None
        label = f"{entry} bare" if stop == "none" else (f"{entry}+stop" if gate == "off" else f"{entry}+stop+gate")
        cfg = make_cfg(stop_mode=stop, stop_pct=stop_pct, green_gate=gate, entry_mode=entry)
        result = run_backtest(klines, funding, cfg)
        sc = scorecard(result)
        named.append((label, sc))
        print(f"  {label:30s}: {sc['n_trades']:>6d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  "
              f"exp={sc['expectancy']:+.4f}  liq={sc['n_liquidations']}", flush=True)
    print()
    print(format_scorecard(named))
    print()

# ── 3. Short direction ──
print("=" * 70)
print("3. SHORT direction — 3 variants")
print("=" * 70)

named = []
for stop, gate in [("none", "off"), ("fixed", "off"), ("fixed", "one")]:
    stop_pct = 2.0 if stop == "fixed" else None
    label = "short bare" if stop == "none" else ("short+stop" if gate == "off" else "short+stop+gate")
    cfg = make_cfg(stop_mode=stop, stop_pct=stop_pct, green_gate=gate, direction="short")
    result = run_backtest(klines, funding, cfg)
    sc = scorecard(result)
    named.append((label, sc))
    print(f"  {label:20s}: {sc['n_trades']:>6d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  "
          f"exp={sc['expectancy']:+.4f}  liq={sc['n_liquidations']}", flush=True)
print()
print(format_scorecard(named))
print()

# ── 4. Stop-pct sweep (single, red_dip, gate=off, long) ──
print("=" * 70)
print("4. STOP% SWEEP — single, red_dip, gate=off, long")
print("=" * 70)

named = []
for sp in DEFAULT_SWEEP_STOP_PCTS:
    label = f"stop={sp:.1f}%"
    cfg = make_cfg(stop_mode="fixed", stop_pct=sp)
    result = run_backtest(klines, funding, cfg)
    sc = scorecard(result)
    named.append((label, sc))
    print(f"  {label:12s}: {sc['n_trades']:>6d} trades  net={sc['net_pnl']:+.2f}  WR={sc['win_rate']*100:.1f}%  "
          f"exp={sc['expectancy']:+.4f}  maxDD={sc['max_drawdown']:+.2f}  stops={sc['n_stops']}  liq={sc['n_liquidations']}  "
          f"worst={sc['worst_trade']:+.2f}", flush=True)
print()
print(format_scorecard(named))
print()
