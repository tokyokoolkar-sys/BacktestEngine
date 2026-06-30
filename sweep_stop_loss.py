"""
Sweep stop-loss params across 5 live symbols on 1h data.
Resamples 1m cache -> 1h on load.

Grid: 17 stops × 5 TP × 3 DCA × 2 drop = 510 variants/symbol × 5 symbols = 2550.
~10 min runtime on 1h data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import numpy as np

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import run_backtest
from metrics import scorecard

SYMBOLS = ["NEARUSDT", "INJUSDT", "TIAUSDT", "OPUSDT", "APTUSDT"]

BASE_EXEC = ExecConfig(
    leverage=10, margin_usdt=2.5, taker_fee=0.0005,
    maker_fee=0.0002, slippage_bps=2.0, maint_margin_rate=0.05,
)

STOPS = [(m, sp, atr) for m, sp, atr in [
    ("none", None, None),
] + [("fixed", sp, None) for sp in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
] + [("atr", None, atr) for atr in [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]]]

TPS = [0.3, 0.5, 0.8, 1.0, 1.5]
DCAS = [2, 3, 5]
DROPS = [0.3, 0.5]


def resample_1h(klines: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m OHLCV -> 1h. open=first, high=max, low=min, close=last, volume=sum."""
    df = klines.set_index("open_time")
    rule = "1h"
    ohlc = df["open"].resample(rule).first()
    high = df["high"].resample(rule).max()
    low = df["low"].resample(rule).min()
    close = df["close"].resample(rule).last()
    vol = df["volume"].resample(rule).sum()
    out = pd.DataFrame({
        "open_time": ohlc.index,
        "open": ohlc.values,
        "high": high.values,
        "low": low.values,
        "close": close.values,
        "volume": vol.values,
    }).dropna()
    return out


def run_sweep(symbol: str, klines: pd.DataFrame, funding: pd.DataFrame) -> list[dict]:
    results = []
    total = len(STOPS) * len(TPS) * len(DCAS) * len(DROPS)
    idx = 0

    for stop_mode, stop_pct, stop_atr_mult in STOPS:
        for tp in TPS:
            for dca_n in DCAS:
                for drop in DROPS:
                    idx += 1
                    if idx % 100 == 0 or idx == 1:
                        print(f"  [{symbol}] {idx}/{total}")

                    cfg = BacktestConfig(
                        symbol=symbol,
                        strategy=StrategyConfig(
                            entry_mode="red_dip",
                            green_gate="off",
                            checkpoint_mode="first_green",
                            position_model="dca",
                            direction="long",
                            stop_mode=stop_mode,
                            stop_pct=stop_pct,
                            stop_atr_mult=stop_atr_mult,
                        ),
                        dca=DCAConfig(
                            price_drop_step_pct=drop,
                            step_multiplier=1.0,
                            size_multiplier=1.1,
                            max_dca_orders=dca_n,
                            first_order_ratio=0.96,
                            take_profit_pct=tp,
                        ),
                        exec=BASE_EXEC,
                    )
                    try:
                        result = run_backtest(klines, funding, cfg)
                        sc = scorecard(result)
                        sc.update({
                            "stop_mode": stop_mode,
                            "stop_pct": stop_pct or 0,
                            "stop_atr_mult": stop_atr_mult or 0,
                            "tp_pct": tp,
                            "dca_orders": dca_n,
                            "drop_step_pct": drop,
                        })
                        results.append(sc)
                    except Exception as e:
                        print(f"  [{symbol}] ERR {stop_mode}/{stop_pct}/{tp}/{dca_n}/{drop}: {e}")
    return results


def main():
    funding_path = _HERE / "cache" / "funding.parquet"
    try:
        funding = load_funding(str(funding_path))
    except Exception:
        funding = pd.DataFrame(columns=["funding_time", "funding_rate"])

    all_symbol_results = {}

    for symbol in SYMBOLS:
        print(f"\n{'='*60}")
        print(f"Loading {symbol}…")
        kp = _HERE / "cache" / symbol / "klines.parquet"
        if not kp.exists():
            print(f"  SKIP")
            continue
        klines_raw = load_klines(str(kp))
        klines = resample_1h(klines_raw)
        print(f"  1m: {len(klines_raw):,} -> 1h: {len(klines):,} bars  ({klines['open_time'].iloc[0].date()} -> {klines['open_time'].iloc[-1].date()})")

        sym_results = run_sweep(symbol, klines, funding)
        all_symbol_results[symbol] = sym_results
        print(f"  Done: {len(sym_results)} configs")

    # Assemble
    rows = []
    for sym, res in all_symbol_results.items():
        for r in res:
            r["symbol"] = sym
            rows.append(r)
    df = pd.DataFrame(rows)

    if df.empty:
        print("No results!")
        return

    df = df[df["n_trades"] >= 20].copy()
    df["score"] = (
        df["expectancy"] * df["n_trades"] ** 0.5
        - df["max_drawdown"].clip(upper=0).abs() * 0.3
    )
    df = df.sort_values("score", ascending=False)

    cols = ["symbol", "stop_mode", "stop_pct", "stop_atr_mult", "tp_pct",
            "dca_orders", "drop_step_pct", "n_trades", "win_rate", "net_pnl",
            "expectancy", "avg_loss", "max_drawdown", "worst_trade", "score"]

    # Overall top
    print(f"\n{'='*60}")
    print(f"GLOBAL TOP 30 (all symbols, {len(df)} configs)")
    print(f"{'='*60}")
    pdf = df[cols].head(30)
    for _, r in pdf.iterrows():
        print(f"  {r['symbol']:8s}  {r['stop_mode']:6s} {r['stop_pct']:4.1f}%  TP={r['tp_pct']:.1f}%  DCA={r['dca_orders']}  "
              f"drop={r['drop_step_pct']:.1f}%  exp={r['expectancy']:+.4f}  wr={r['win_rate']*100:.1f}%  "
              f"worst={r['worst_trade']:+.4f}  loss={r['avg_loss']:.4f}  score={r['score']:.4f}")

    # Per-symbol top 5
    print(f"\n{'='*60}")
    print("TOP 5 PER SYMBOL")
    for sym in SYMBOLS:
        sub = df[df["symbol"] == sym].head(5)
        print(f"\n--- {sym} ---")
        for _, r in sub.iterrows():
            print(f"  {r['stop_mode']:6s} {r['stop_pct']:4.1f}%  TP={r['tp_pct']:.1f}%  DCA={r['dca_orders']}  "
                  f"exp={r['expectancy']:+.4f}  wr={r['win_rate']*100:.1f}%  worst={r['worst_trade']:+.4f}  loss={r['avg_loss']:.4f}")

    # Baseline (live config)
    print(f"\n{'='*60}")
    print("LIVE BASELINE (no stop, TP=0.5, DCA=3, drop=0.3)")
    print(f"{'='*60}")
    live_mask = (df["stop_mode"] == "none") & (df["tp_pct"] == 0.5) & (df["dca_orders"] == 3) & (df["drop_step_pct"] == 0.3)
    live = df[live_mask]
    if not live.empty:
        for _, r in live.iterrows():
            print(f"  {r['symbol']:8s}  exp={r['expectancy']:+.4f}  wr={r['win_rate']*100:.1f}%  "
                  f"worst={r['worst_trade']:+.4f}  loss={r['avg_loss']:.4f}  score={r['score']:.4f}")

    # Game changers
    if not live.empty:
        avg_live_exp = live["expectancy"].mean()
        avg_live_worst = live["worst_trade"].mean()
        print(f"\n{'='*60}")
        print("GAME CHANGERS: expectancy >20% better AND worst_trade >30% better than baseline")
        print(f"  Baseline: avg_exp={avg_live_exp:.4f}  avg_worst={avg_live_worst:.4f}")
        print(f"{'='*60}")
        gc = df[(df["expectancy"] > avg_live_exp * 1.2) & (df["worst_trade"] > avg_live_worst * 0.7)]
        if not gc.empty:
            gc = gc.sort_values("score", ascending=False)
            for _, r in gc.head(20).iterrows():
                exp_chg = (r["expectancy"] - avg_live_exp) / abs(avg_live_exp) * 100 if avg_live_exp != 0 else 0
                worst_chg = (r["worst_trade"] - avg_live_worst) / abs(avg_live_worst) * 100 if avg_live_worst < 0 else 0
                print(f"  {r['symbol']:8s}  {r['stop_mode']:6s} {r['stop_pct']:4.1f}%  TP={r['tp_pct']:.1f}%  DCA={r['dca_orders']}  "
                      f"exp={r['expectancy']:+.4f} ({exp_chg:+.0f}%)  worst={r['worst_trade']:+.4f} ({worst_chg:+.0f}%)  "
                      f"loss={r['avg_loss']:.4f}")
        else:
            print("(none)")
            gc = df[(df["expectancy"] > avg_live_exp * 1.2)]
            if not gc.empty:
                print("Configs beating baseline expectancy by 20%+:")
                for _, r in gc.head(20).iterrows():
                    exp_chg = (r["expectancy"] - avg_live_exp) / abs(avg_live_exp) * 100 if avg_live_exp != 0 else 0
                    print(f"  {r['symbol']:8s}  {r['stop_mode']:6s} {r['stop_pct']:4.1f}%  TP={r['tp_pct']:.1f}%  "
                          f"exp={r['expectancy']:+.4f} ({exp_chg:+.0f}%)  worst={r['worst_trade']:+.4f}")

    # No-stop vs best-stop comparison per symbol
    print(f"\n{'='*60}")
    print("NO-STOP vs BEST FIXED-STOP (sorted by expectancy)")
    print(f"{'='*60}")
    for sym in SYMBOLS:
        sub = df[df["symbol"] == sym]
        ns = sub[sub["stop_mode"] == "none"].sort_values("score", ascending=False)
        fx = sub[(sub["stop_mode"] == "fixed")].sort_values("score", ascending=False)
        if not ns.empty and not fx.empty:
            best_ns = ns.iloc[0]
            best_fx = fx.iloc[0]
            print(f"\n{sym}:")
            print(f"  NO-STOP:    exp={best_ns['expectancy']:+.4f}  wr={best_ns['win_rate']*100:.1f}%  "
                  f"worst={best_ns['worst_trade']:+.4f}  loss={best_ns['avg_loss']:.4f}")
            print(f"  BEST-STOP:  exp={best_fx['expectancy']:+.4f}  wr={best_fx['win_rate']*100:.1f}%  "
                  f"worst={best_fx['worst_trade']:+.4f}  loss={best_fx['avg_loss']:.4f}  "
                  f"stop={best_fx['stop_pct']}%  TP={best_fx['tp_pct']}%  DCA={best_fx['dca_orders']}")
            exp_chg = (best_fx['expectancy'] - best_ns['expectancy']) / abs(best_ns['expectancy']) * 100 if best_ns['expectancy'] != 0 else 0
            worst_chg = (best_fx['worst_trade'] - best_ns['worst_trade']) / abs(best_ns['worst_trade']) * 100 if best_ns['worst_trade'] < 0 else 0
            print(f"  CHANGE:     exp={exp_chg:+.1f}%  worst={worst_chg:+.1f}%")

    # Save
    out_path = _HERE / "sweep_stop_loss_results.parquet"
    df.to_parquet(str(out_path))
    print(f"\nSaved {len(df)} results to {out_path}")


if __name__ == "__main__":
    main()
