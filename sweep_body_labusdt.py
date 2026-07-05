"""OOS-validate body_pct threshold for LABUSDT long, DCA model.

70/30 train/test split. Grid body_pct × tp_pct. Survive if test exp > 0
AND >= 30 test trades. Reports ranked table.

Usage: python sweep_body_labusdt.py
"""
from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import run_backtest

CACHE = Path(__file__).parent / "cache" / "LABUSDT"


def split_70_30(kl: pd.DataFrame, fu: pd.DataFrame):
    n = len(kl)
    cut = int(n * 0.7)
    return (kl.iloc[:cut], fu[fu["funding_time"] <= kl.iloc[cut]["open_time"]]), \
           (kl.iloc[cut:], fu[fu["funding_time"] > kl.iloc[cut]["open_time"]])


def build_cfg(body_pct: float, tp_pct: float) -> BacktestConfig:
    return BacktestConfig(
        symbol="LABUSDT",
        strategy=StrategyConfig(
            entry_mode="red_dip",
            green_gate="off",
            stop_mode="none",
            checkpoint_mode="first_green",
            position_model="dca",
            direction="long",
            body_pct=body_pct,
            target_pct=tp_pct,
        ),
        dca=DCAConfig(
            price_drop_step_pct=0.65,
            step_multiplier=1.0,
            size_multiplier=1.1,
            max_dca_orders=4,
            first_order_ratio=0.96,
            take_profit_pct=tp_pct,
        ),
        exec=ExecConfig(
            leverage=10,
            margin_usdt=2.5,
            taker_fee=0.0005,
            maker_fee=0.0002,
            slippage_bps=2.0,
            maint_margin_rate=0.004,
        ),
    )


def exp_per_trade(trades) -> float:
    return sum(t.net_pnl for t in trades) / len(trades) if trades else 0.0


def main():
    if not CACHE.exists():
        print(f"NO DATA: {CACHE} — run data download for LABUSDT first")
        sys.exit(1)
    kl = load_klines(CACHE / "klines.parquet")
    fu = load_funding(CACHE / "funding.parquet")
    (kl_tr, fu_tr), (kl_te, fu_te) = split_70_30(kl, fu)
    print(f"LABUSDT bars: train={len(kl_tr)} test={len(kl_te)}")

    bodies = [0.0, 0.5, 1.0, 1.5, 2.0]
    tps = [0.5, 0.8, 1.05, 1.2, 1.5]
    rows = []
    for b in bodies:
        for tp in tps:
            cfg = build_cfg(b, tp)
            tr = run_backtest(kl_tr, fu_tr, cfg)
            te = run_backtest(kl_te, fu_te, cfg)
            tr_n, te_n = len(tr.trades), len(te.trades)
            tr_exp = exp_per_trade(tr.trades)
            te_exp = exp_per_trade(te.trades)
            survive = te_n >= 30 and te_exp > 0
            rows.append({
                "body_pct": b, "tp_pct": tp,
                "tr_n": tr_n, "tr_exp": round(tr_exp, 5),
                "te_n": te_n, "te_exp": round(te_exp, 5),
                "survive": "Y" if survive else "",
            })

    df = pd.DataFrame(rows).sort_values("te_exp", ascending=False)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False))
    surv = df[df.survive == "Y"]
    print(f"\nSURVIVORS (te_exp>0, te_n>=30): {len(surv)}/{len(df)}")
    if not surv.empty:
        top = surv.iloc[0]
        print(f"TOP: body={top.body_pct} tp={top.tp_pct} exp={top.te_exp} n={top.te_n}")


if __name__ == "__main__":
    main()
