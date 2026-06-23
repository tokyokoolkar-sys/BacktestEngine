"""
Top-5 symbols on 15m DCA bare — check if any symbol produces positive expectancy.
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

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT",
           "TRXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "LINKUSDT", "NEARUSDT",
           "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT", "ATOMUSDT",
           "FILUSDT", "ETCUSDT", "UNIUSDT", "AAVEUSDT", "TIAUSDT"]

def _empty_funding():
    return pd.DataFrame({"funding_time": pd.to_datetime([], utc=True),
                         "funding_rate": pd.Series([], dtype="float64")})

funding = _empty_funding()

def make_cfg(stop_mode="none", stop_pct=None, position_model="single"):
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode=stop_mode, stop_pct=stop_pct,
            checkpoint_mode="first_green",
            position_model=position_model, direction="long",
        ),
        dca=DCAConfig(take_profit_pct=0.5, max_dca_orders=5, price_drop_step_pct=0.3),
        exec=ExecConfig(),
    )

print(f"{'SYMBOL':<8} {'model':<7} {'trades':>6} {'WR':>7} {'exp':>9} {'net':>10} {'payoff':>7} {'BE':>7} {'liq':>4}")
print("-" * 70)

results = []
for sym in SYMBOLS:
    kp = _HERE / "cache_15m" / sym / "klines.parquet"
    if not kp.exists():
        continue
    klines = load_klines(kp)
    for model in ("single", "dca"):
        cfg = make_cfg(position_model=model)
        cfg.symbol = sym
        try:
            sc = scorecard(run_backtest(klines, funding, cfg))
            gap = sc['actual_winrate'] - sc['breakeven_winrate']
            results.append((sym, model, sc, gap))
            print(f"{sym:<8} {model:<7} {sc['n_trades']:>6d} {sc['win_rate']*100:>6.1f}% {sc['expectancy']:>+9.4f} {sc['net_pnl']:>+10.2f} {sc['payoff_ratio']:>7.2f} {sc['breakeven_winrate']*100:>6.1f}% {sc['n_liquidations']:>4d}", flush=True)
        except Exception as e:
            print(f"{sym:<8} {model:<7} ERROR: {e}")

print("\n--- TOP 10 by expectancy ---")
results.sort(key=lambda r: r[2]['expectancy'], reverse=True)
for sym, model, sc, gap in results[:10]:
    print(f"  {sym:<8} {model:<7} exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  gap={gap*100:+.1f}%")

print("\n--- WORST 5 by expectancy ---")
for sym, model, sc, gap in results[-5:]:
    print(f"  {sym:<8} {model:<7} exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}  gap={gap*100:+.1f}%")

pos = [r for r in results if r[2]['expectancy'] > 0]
print(f"\nPositive expectancy: {len(pos)}/{len(results)}")
if pos:
    for sym, model, sc, gap in pos:
        print(f"  {sym:<8} {model:<7} exp={sc['expectancy']:+.4f}  WR={sc['win_rate']*100:.1f}%  net={sc['net_pnl']:+.2f}")
