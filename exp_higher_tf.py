"""
Higher timeframe sweeps: 1h and 4h.
"""
import sys
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import download_klines, download_funding, load_klines, load_funding, months_list
from engine import run_backtest
from metrics import format_scorecard, scorecard

SYMBOL = "SYNUSDT"
MONTHS = months_list("2025-12", "2026-06")
INTERVALS = ["1h", "4h"]
BASE_CFG_CACHE = _HERE / "cache"

def make_cfg(stop_mode="none", stop_pct=None, green_gate="off",
             position_model="single", direction="long", entry_mode="red_dip"):
    return BacktestConfig(
        symbol=SYMBOL,
        strategy=StrategyConfig(
            entry_mode=entry_mode, green_gate=green_gate,
            stop_mode=stop_mode, stop_pct=stop_pct,
            checkpoint_mode="first_green",
            position_model=position_model, direction=direction,
        ),
        dca=DCAConfig(),
        exec=ExecConfig(),
    )

for interval in INTERVALS:
    print(f"\n{'='*70}")
    print(f"TIMEFRAME: {interval}")
    print(f"{'='*70}")

    cache_dir = _HERE / f"cache_{interval}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    kp = cache_dir / "klines.parquet"

    if not kp.exists():
        download_klines(SYMBOL, MONTHS, out_dir=str(cache_dir), interval=interval)
        fp = cache_dir / "funding.parquet"
        if not fp.exists():
            download_funding(SYMBOL, MONTHS, out_dir=str(cache_dir))

    klines = load_klines(kp)
    funding = load_funding(cache_dir / "funding.parquet")
    print(f"rows: {len(klines):,}  ({klines['open_time'].iloc[0]} -> {klines['open_time'].iloc[-1]})", flush=True)

    for model in ("single", "dca"):
        print(f"\n--- {model} ---")
        named = []
        for stop, gate in [("none", "off"), ("fixed", "off"), ("fixed", "one")]:
            sp = 2.0 if stop == "fixed" else None
            label = f"{model} bare" if stop == "none" else (f"{model}+stop" if gate == "off" else f"{model}+stop+gate")
            sc = scorecard(run_backtest(klines, funding, make_cfg(stop, sp, gate, model)))
            named.append((label, sc))
            print(f"  {label:20s}: {sc['n_trades']:>4d} t  exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  liq={sc['n_liquidations']}", flush=True)
        print()
        print(format_scorecard(named))
        print()

    # entry mode comparison on single
    print("\n--- entry modes ---")
    for entry in ("red_dip", "green_confirm"):
        sc = scorecard(run_backtest(klines, funding, make_cfg(entry_mode=entry)))
        print(f"  {entry:15s}: {sc['n_trades']:>4d} t  exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}", flush=True)

    # stop sweep on single
    print("\n--- stop sweep ---")
    for sp in [0.5, 1.0, 2.0, 3.0, 5.0]:
        sc = scorecard(run_backtest(klines, funding, make_cfg(stop_mode="fixed", stop_pct=sp)))
        print(f"  stop={sp:.1f}%:  {sc['n_trades']:>4d} t  exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  stops={sc['n_stops']}  liq={sc['n_liquidations']}", flush=True)
