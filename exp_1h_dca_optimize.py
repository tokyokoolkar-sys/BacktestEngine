"""
exp_1h_dca_optimize.py — Per-symbol DCA param optimization on 1h.
Grid: TP x DCA_orders x stop_pct
Goal: find best params for each symbol, then validate with OOS.
"""
import sys, math
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pandas as pd
from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_klines
from engine import run_backtest
from metrics import scorecard

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT",
    "AVAXUSDT","LINKUSDT","TRXUSDT","DOTUSDT","LTCUSDT","BCHUSDT","NEARUSDT",
    "APTUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","ATOMUSDT","FILUSDT",
    "ETCUSDT","UNIUSDT","AAVEUSDT","TIAUSDT",
]

def _empty_funding():
    return pd.DataFrame({"funding_time": pd.to_datetime([], utc=True),
                         "funding_rate": pd.Series([], dtype="float64")})

def _split(klines, frac=0.7):
    n = len(klines)
    cutoff = int(math.floor(n * frac))
    return klines.iloc[:cutoff].reset_index(drop=True), klines.iloc[cutoff:].reset_index(drop=True)

def _cfg(tp, dca, stop):
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode="fixed" if stop else "none",
            stop_pct=stop, checkpoint_mode="first_green",
            position_model="dca", direction="long",
        ),
        dca=DCAConfig(take_profit_pct=tp, max_dca_orders=dca,
                      price_drop_step_pct=0.5, step_multiplier=1.0,
                      size_multiplier=1.1, first_order_ratio=0.96),
        exec=ExecConfig(),
    )

TPS = [0.3, 0.5, 0.75, 1.0]
DCAS = [3, 5, 7, 10]
STOPS = [None, 3.0]

funding = _empty_funding()
MIN_EDGE = 0.005

results = []

print(f"{'SYMBOL':<9} {'TP':>4} {'DCA':>3} {'STOP':>4} {'trades':>6} {'WR':>6} {'exp':>9} {'net':>9} {'payoff':>6} {'BE_WR':>6} {'gap':>6} {'liq':>3}", flush=True)
print("-" * 80, flush=True)

for sym in SYMBOLS:
    kp = _HERE / "cache_1h" / sym / "klines.parquet"
    if not kp.exists():
        print(f"{sym:<9} — NO CACHE —", flush=True)
        continue
    klines = load_klines(kp)
    train, validate = _split(klines, 0.7)

    for tp in TPS:
        for d in DCAS:
            for stop in STOPS:
                cfg = _cfg(tp, d, stop)
                cfg.symbol = sym
                try:
                    tr = scorecard(run_backtest(train, funding, cfg))
                    vr = scorecard(run_backtest(validate, funding, cfg))
                except:
                    continue
                stop_label = f"{stop:.0f}" if stop else "none"
                gap = vr['actual_winrate'] - vr['breakeven_winrate']
                overfit = tr['expectancy'] > MIN_EDGE and vr['expectancy'] <= MIN_EDGE
                verdict = "edge" if vr['expectancy'] > MIN_EDGE and not overfit else ("overfit" if overfit else "no-edge")
                print(f"{sym:<9} {tp:>4.1f} {d:>3} {stop_label:>4} {vr['n_trades']:>6d} {vr['win_rate']*100:>5.1f}% {vr['expectancy']:>+9.4f} {vr['net_pnl']:>+9.2f} {vr['payoff_ratio']:>6.2f} {vr['breakeven_winrate']*100:>5.1f}% {gap*100:>+5.1f}% {vr['n_liquidations']:>3d}", flush=True)
                results.append({"symbol": sym, "tp": tp, "dca": d, "stop": stop, "sc": vr, "verdict": verdict, "overfit": overfit})

# Per-symbol best by OOS expectancy
print("\n\n=== BEST PER SYMBOL (OOS expectancy) ===", flush=True)
print(f"{'SYMBOL':<9} {'TP':>4} {'DCA':>3} {'STOP':>4} {'exp':>9} {'WR':>6} {'net':>9} {'gap':>6} {'verdict':<9}", flush=True)
print("-" * 65, flush=True)
for sym in sorted(set(r["symbol"] for r in results)):
    syms = [r for r in results if r["symbol"] == sym]
    best = max(syms, key=lambda r: r["sc"]["expectancy"])
    s = best["sc"]
    stop_label = f"{best['stop']:.0f}" if best['stop'] else "none"
    gap = (s['actual_winrate'] - s['breakeven_winrate']) * 100
    print(f"{sym:<9} {best['tp']:>4.1f} {best['dca']:>3} {stop_label:>4} {s['expectancy']:>+9.4f} {s['win_rate']*100:>5.1f}% {s['net_pnl']:>+9.2f} {gap:>+5.1f}% {best['verdict']:<9}", flush=True)

# Global TOP 20
print("\n\n=== GLOBAL TOP 20 BY OOS EXPECTANCY ===", flush=True)
all_sorted = sorted(results, key=lambda r: r["sc"]["expectancy"], reverse=True)
print(f"{'RANK':<5} {'SYMBOL':<9} {'TP':>4} {'DCA':>3} {'STOP':>4} {'exp':>9} {'WR':>6} {'net':>9} {'gap':>6} {'verdict':<9}", flush=True)
print("-" * 65, flush=True)
for i, r in enumerate(all_sorted[:20]):
    s = r["sc"]
    stop_label = f"{r['stop']:.0f}" if r['stop'] else "none"
    gap = (s['actual_winrate'] - s['breakeven_winrate']) * 100
    print(f"{i+1:<5} {r['symbol']:<9} {r['tp']:>4.1f} {r['dca']:>3} {stop_label:>4} {s['expectancy']:>+9.4f} {s['win_rate']*100:>5.1f}% {s['net_pnl']:>+9.2f} {gap:>+5.1f}% {r['verdict']:<9}", flush=True)

# Summary: which param values work best across all symbols
print("\n\n=== PARAM EFFECTIVENESS ===", flush=True)
for tp in TPS:
    for d in DCAS:
        grp = [r for r in results if r["tp"] == tp and r["dca"] == d and r['stop'] is None]
        avg_exp = sum(r["sc"]["expectancy"] for r in grp) / len(grp) if grp else 0
        n_edge = sum(1 for r in grp if r["verdict"] == "edge")
        n_ovf = sum(1 for r in grp if r["verdict"] == "overfit")
        print(f"  TP={tp:.1f} DCA={d} stop=none: avg_exp={avg_exp:+.4f}  edge={n_edge}  overfit={n_ovf}  n={len(grp)}", flush=True)

# Count how many symbols have ANY positive-expectancy combo
pos_symbols = set()
for r in results:
    if r["sc"]["expectancy"] > 0:
        pos_symbols.add(r["symbol"])
print(f"\n\nSymbols with ANY positive OOS expectancy: {len(pos_symbols)}/{len(SYMBOLS)}", flush=True)

# Count how many have a clear edge
edge_symbols = set(r["symbol"] for r in results if r["verdict"] == "edge")
overfit_symbols = set(r["symbol"] for r in results if r["verdict"] == "overfit")
print(f"Symbols with EDGE: {len(edge_symbols)}  overfit only: {len(overfit_symbols)}", flush=True)
