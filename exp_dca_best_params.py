"""
exp_dca_best_params.py — DCA param grid on TIAUSDT, NEARUSDT, INJUSDT.
Vary TP and DCA orders on the best 3 symbols.
"""
import sys
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pandas as pd
from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_klines
from engine import run_backtest
from metrics import scorecard

SYMBOLS = ["TIAUSDT", "NEARUSDT", "INJUSDT"]
TPS = [0.3, 0.5, 0.75, 1.0, 1.5]
DCAS = [3, 5, 7, 10]
STOPS = [None, 3.0]

def _empty_funding():
    return pd.DataFrame({"funding_time": pd.to_datetime([], utc=True),
                         "funding_rate": pd.Series([], dtype="float64")})

def _make_cfg(tp=0.5, dca_orders=5, step=0.5, stop_pct=None):
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode="fixed" if stop_pct else "none",
            stop_pct=stop_pct, checkpoint_mode="first_green",
            position_model="dca", direction="long",
        ),
        dca=DCAConfig(take_profit_pct=tp, max_dca_orders=dca_orders,
                      price_drop_step_pct=step, step_multiplier=1.0,
                      size_multiplier=1.1, first_order_ratio=0.96),
        exec=ExecConfig(),
    )

funding = _empty_funding()

print(f"{'SYMBOL':<10} {'TP':>5} {'DCA':>4} {'stop':>5} {'trades':>6} {'WR':>7} {'exp':>9} {'net':>10} {'payoff':>7} {'BE':>7} {'gap':>7} {'liq':>4}", flush=True)
print("-" * 90, flush=True)

results = []
for sym in SYMBOLS:
    kp = _HERE / "cache_15m" / sym / "klines.parquet"
    if not kp.exists():
        print(f"{sym} — no cache")
        continue
    klines = load_klines(kp)

    for tp in TPS:
        for d in DCAS:
            for stop in STOPS:
                cfg = _make_cfg(tp, d, 0.5, stop)
                cfg.symbol = sym
                try:
                    sc = scorecard(run_backtest(klines, funding, cfg))
                    gap = sc['actual_winrate'] - sc['breakeven_winrate']
                    stop_str = f"{stop:.0f}" if stop else "none"
                    results.append((sym, tp, d, stop, sc, gap))
                    print(f"{sym:<10} {tp:>5.1f} {d:>4} {stop_str:>5} {sc['n_trades']:>6d} {sc['win_rate']*100:>6.1f}% {sc['expectancy']:>+9.4f} {sc['net_pnl']:>+10.2f} {sc['payoff_ratio']:>7.2f} {sc['breakeven_winrate']*100:>6.1f}% {gap*100:>+6.1f}% {sc['n_liquidations']:>4d}", flush=True)
                except Exception as e:
                    pass

print("\n\n=== TOP 15 by expectancy ===", flush=True)
results.sort(key=lambda r: r[4]['expectancy'], reverse=True)
for sym, tp, d, stop, sc, gap in results[:15]:
    stop_str = f"{stop:.0f}" if stop else "none"
    print(f"  {sym:<8} TP={tp:.1f} DCA={d} stop={stop_str:>5}: exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  gap={gap*100:+.1f}%", flush=True)

pos = [r for r in results if r[4]['expectancy'] > 0]
print(f"\nPositive expectancy: {len(pos)}/{len(results)} combos", flush=True)
print(f"Range of positive expectancies: {min(r[4]['expectancy'] for r in pos):+.4f} to {max(r[4]['expectancy'] for r in pos):+.4f}" if pos else "None", flush=True)
