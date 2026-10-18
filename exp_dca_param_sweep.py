"""
DCA parameter sweep on 15m SYNUSDT.
Vary take_profit_pct, max_dca_orders, price_drop_step_pct, and stop distance.
"""
import sys, itertools
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import run_backtest
from metrics import scorecard

CACHE = _HERE / "cache_15m"

klines = load_klines(CACHE / "klines.parquet")
funding = load_funding(CACHE / "funding.parquet")
print(f"klines: {len(klines):,}", flush=True)

def make_cfg(tp, max_dca, step, stop_pct=None):
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode="fixed" if stop_pct else "none",
            stop_pct=stop_pct, checkpoint_mode="first_green",
            position_model="dca", direction="long",
        ),
        dca=DCAConfig(
            take_profit_pct=tp,
            max_dca_orders=max_dca,
            price_drop_step_pct=step,
            step_multiplier=1.0, size_multiplier=1.1,
            first_order_ratio=0.96,
        ),
        exec=ExecConfig(),
    )

# Test plan: vary one param at a time from baseline (TP=1, DCA=3, step=0.5, stop=none)
print("BASELINE: TP=1.0  DCA=3  step=0.5%  stop=none")
named = [("baseline", scorecard(run_backtest(klines, funding, make_cfg(1.0, 3, 0.5))))]
print(f"  baseline: exp={named[0][1]['expectancy']:+.4f}  WR={named[0][1]['win_rate']*100:.1f}%  net={named[0][1]['net_pnl']:+.2f}")

print("\n--- SWEEP: take_profit_pct ---")
for tp in [0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]:
    sc = scorecard(run_backtest(klines, funding, make_cfg(tp, 3, 0.5)))
    named.append((f"TP={tp}", sc))
    print(f"  TP={tp:.1f}%: exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  payoff={sc['payoff_ratio']:.2f}")

print("\n--- SWEEP: max_dca_orders ---")
for d in [1, 2, 3, 5, 7, 10]:
    sc = scorecard(run_backtest(klines, funding, make_cfg(1.0, d, 0.5)))
    named.append((f"DCA={d}", sc))
    print(f"  DCA={d}: exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}")

print("\n--- SWEEP: price_drop_step_pct ---")
for s in [0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0]:
    sc = scorecard(run_backtest(klines, funding, make_cfg(1.0, 3, s)))
    named.append((f"step={s}", sc))
    print(f"  step={s:.1f}%: exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}")

print("\n--- SWEEP: stop + best DCA params ---")
# Try the top TP + stop combos
for tp, stop in [(0.5, 3.0), (0.5, 4.0), (0.75, 3.0), (1.0, 3.0), (1.0, 4.0)]:
    sc = scorecard(run_backtest(klines, funding, make_cfg(tp, 3, 0.5, stop)))
    named.append((f"TP={tp}+stop={stop}", sc))
    print(f"  TP={tp:.1f}+stop={stop:.1f}: exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  liq={sc['n_liquidations']}")

# best combos from above
for tp, d, step, stop in [(0.5, 3, 0.5, None), (0.5, 5, 0.5, None), (0.5, 3, 0.3, None),
                           (0.75, 3, 0.5, None), (1.0, 5, 0.5, None)]:
    sc = scorecard(run_backtest(klines, funding, make_cfg(tp, d, step, stop)))
    named.append((f"TP={tp} DCA={d} step={step}", sc))
    print(f"  best TP={tp} DCA={d} step={step}: exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}")

print("\n--- TOP 10 by expectancy ---")
sorted_named = sorted(named, key=lambda x: x[1]['expectancy'], reverse=True)
for label, sc in sorted_named[:10]:
    print(f"  {label:30s}: exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  payoff={sc['payoff_ratio']:.2f}  BE={sc['breakeven_winrate']*100:.1f}%")
