"""
exp_1h_dca_screener.py — 1h DCA OOS screener on all 26 symbols.
"""
import sys, math
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pandas as pd
from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import download_klines, load_klines
from engine import run_backtest
from metrics import scorecard
from screener import regime_stats

SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT",
           "AVAXUSDT","LINKUSDT","TRXUSDT","DOTUSDT","LTCUSDT","BCHUSDT","NEARUSDT",
           "APTUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","ATOMUSDT","FILUSDT",
           "ETCUSDT","UNIUSDT","AAVEUSDT","TIAUSDT"]

MONTHS = [str(p) for p in pd.period_range("2025-06", "2026-05", freq="M")]

def _empty_funding():
    return pd.DataFrame({"funding_time": pd.to_datetime([], utc=True),
                         "funding_rate": pd.Series([], dtype="float64")})

def _split(klines, frac=0.7):
    n = len(klines)
    cutoff = int(math.floor(n * frac))
    return klines.iloc[:cutoff].reset_index(drop=True), klines.iloc[cutoff:].reset_index(drop=True)

def _make_cfg(tp=0.5, dca_orders=5, step=0.5, stop_pct=None, single=False):
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode="fixed" if stop_pct else "none",
            stop_pct=stop_pct, checkpoint_mode="first_green",
            position_model="single" if single else "dca",
            direction="long",
        ),
        dca=DCAConfig(take_profit_pct=tp, max_dca_orders=dca_orders,
                      price_drop_step_pct=step, step_multiplier=1.0,
                      size_multiplier=1.1, first_order_ratio=0.96),
        exec=ExecConfig(),
    )

funding = _empty_funding()
MIN_EDGE = 0.005

CONFIGS = [
    ("DCA TP=0.5 DCA=5", _make_cfg(0.5, 5, 0.5)),
    ("DCA TP=0.5 DCA=5+stop3", _make_cfg(0.5, 5, 0.5, 3.0)),
    ("single bare", _make_cfg(single=True)),
]

rows = []
print(f"{'SYMBOL':<10} {'variant':<22} {'train_exp':>10} {'OOS_exp':>10} {'OOS_WR':>7} {'OOS_net':>10} {'overfit':<8} {'verdict':<10}", flush=True)
print("-" * 90, flush=True)

for sym in SYMBOLS:
    cache_dir = _HERE / "cache_1h" / sym
    kp = cache_dir / "klines.parquet"
    if not kp.exists():
        print(f"[fetch] {sym} 1h …", flush=True)
        try:
            download_klines(sym, MONTHS, out_dir=str(cache_dir), interval="1h")
        except RuntimeError as e:
            print(f"SKIP {sym}: {e}")
            continue

    try:
        klines = load_klines(kp)
    except Exception as e:
        print(f"SKIP {sym}: {e}")
        continue

    if len(klines) < 100:
        print(f"SKIP {sym}: too few rows")
        continue

    train, validate = _split(klines, 0.7)

    for label, cfg in CONFIGS:
        cfg.symbol = sym
        try:
            tr = scorecard(run_backtest(train, funding, cfg))
            vr = scorecard(run_backtest(validate, funding, cfg))
        except Exception as e:
            print(f"{sym:<10} {label:<22} ERROR: {e}", flush=True)
            continue

        te = tr["expectancy"]
        ve = vr["expectancy"]
        overfit = bool(te > MIN_EDGE and ve <= MIN_EDGE)
        if ve > MIN_EDGE and not overfit:
            verdict = "edge"
        elif overfit:
            verdict = "overfit"
        else:
            verdict = "no-edge"

        print(f"{sym:<10} {label:<22} {te:>+10.4f} {ve:>+10.4f} {vr['win_rate']*100:>6.1f}% {vr['net_pnl']:>+10.2f} {'***YES***' if overfit else 'no':<8} {verdict:<10}", flush=True)
        rows.append({"symbol": sym, "variant": label, "train_exp": te, "oos_exp": ve,
                     "oos_wr": vr["win_rate"], "oos_net": vr["net_pnl"],
                     "overfit": overfit, "verdict": verdict})

print("\n\n=== SUMMARY ===", flush=True)
for v in set(r["variant"] for r in rows):
    vrows = [r for r in rows if r["variant"] == v]
    n_edge = sum(1 for r in vrows if r["verdict"] == "edge")
    n_ovf = sum(1 for r in vrows if r["verdict"] == "overfit")
    print(f"  {v:<22}: {n_edge} edge / {n_ovf} overfit / {len(vrows)} total", flush=True)

edges = [r for r in rows if r["verdict"] == "edge"]
if edges:
    print("\nEdge survivors:", flush=True)
    for r in sorted(edges, key=lambda x: x["oos_exp"], reverse=True):
        print(f"  {r['symbol']:<10} {r['variant']:<22} OOS_exp={r['oos_exp']:+.4f} WR={r['oos_wr']*100:.1f}% net={r['oos_net']:+.2f}", flush=True)
else:
    print("\nNo edge found.", flush=True)
