"""
Compare LONG-only vs SHORT-only vs DUAL_MODE (alternating) for the WORKING
red-dip DCA strategy -- the live money-maker in live_params.md.

Question: live long works. Does the short mirror also work, and does running
both via dual_mode (one bot, flip direction each trade) beat either alone with
LOWER drawdown (i.e. are the two sides uncorrelated -> free Sharpe)?

Uses the real engine (dual_mode is wired: engine.py:440/492/586 call
flip_direction after each trade). Same live params: red-dip entry, DCA hold,
no stop, per-symbol-ish TP grid. 1h signal + 1h fill.

DCA entries fill at bar open (not intrabar-optimistic), so no trailing-style
mirage. Costs: taker both sides, funding, slippage, liq floor -- all engine.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import run_backtest
from metrics import scorecard

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "NEARUSDT", "INJUSDT", "TIAUSDT", "OPUSDT", "APTUSDT", "ARBUSDT",
]

BASE_EXEC = ExecConfig(leverage=10, margin_usdt=2.5, taker_fee=0.0005,
                       maker_fee=0.0002, slippage_bps=2.0, maint_margin_rate=0.05)

TPS = [0.3, 0.5, 0.8, 1.0]
MODES = ["long", "short", "dual"]


def resample_1h(k: pd.DataFrame) -> pd.DataFrame:
    df = k.set_index("open_time")
    return pd.DataFrame({
        "open_time": df["open"].resample("1h").first().index,
        "open": df["open"].resample("1h").first().values,
        "high": df["high"].resample("1h").max().values,
        "low": df["low"].resample("1h").min().values,
        "close": df["close"].resample("1h").last().values,
        "volume": df["volume"].resample("1h").sum().values,
    }).dropna()


def make_cfg(symbol, mode, tp):
    direction = "short" if mode == "short" else "long"
    dual = (mode == "dual")
    return BacktestConfig(
        symbol=symbol,
        strategy=StrategyConfig(
            entry_mode="red_dip",          # the WORKING live entry
            green_gate="off",
            checkpoint_mode="first_green",  # live: DCA holds for TP (no first-green exit needed w/ TP)
            position_model="dca",
            direction=direction,
            dual_mode=dual,
            stop_mode="none",              # live runs no stop
        ),
        dca=DCAConfig(
            price_drop_step_pct=0.3,
            step_multiplier=1.0,
            size_multiplier=1.1,
            max_dca_orders=3,
            first_order_ratio=0.96,
            take_profit_pct=tp,
        ),
        exec=BASE_EXEC,
    )


def main():
    try:
        funding = load_funding(str(_HERE / "cache" / "funding.parquet"))
    except Exception:
        funding = pd.DataFrame(columns=["funding_time", "funding_rate"])

    rows = []
    for s in SYMBOLS:
        kp = _HERE / "cache" / s / "klines.parquet"
        if not kp.exists():
            print(f"SKIP {s}"); continue
        print(f"Loading {s}...")
        k = resample_1h(load_klines(str(kp)))
        for mode in MODES:
            for tp in TPS:
                cfg = make_cfg(s, mode, tp)
                try:
                    res = run_backtest(k, funding, cfg)
                    sc = scorecard(res)
                    # max drawdown from equity curve
                    eq = [e for _, e in res.equity_curve]
                    peak = 0.0; mdd = 0.0
                    for v in eq:
                        peak = max(peak, v)
                        mdd = min(mdd, v - peak)
                    sc.update({"symbol": s, "mode": mode, "tp_pct": tp,
                               "net_pnl": sc["net_pnl"], "n_trades": len(res.trades),
                               "mdd": mdd})
                    rows.append(sc)
                except Exception as e:
                    print(f"  ERR {s}/{mode}/{tp}: {e}")

    df = pd.DataFrame(rows)
    df.to_parquet(str(_HERE / "sweep_dualmode_results.parquet"))

    # Best TP per (symbol, mode) by net, then aggregate across symbols per mode
    best = (df.sort_values("net_pnl", ascending=False)
              .groupby(["symbol", "mode"], as_index=False).first())

    print("\n" + "=" * 78)
    print("PER-MODE PORTFOLIO (best TP per symbol, summed across symbols)")
    print("=" * 78)
    agg = (best.groupby("mode")
             .agg(total_net=("net_pnl", "sum"),
                  avg_wr=("win_rate", "mean"),
                  total_mdd=("mdd", "sum"),
                  worst=("worst_trade", "min"),
                  trades=("n_trades", "sum"))
             .reset_index())
    print(agg.to_string(index=False))

    # The real question: dual vs (long+short summed)
    piv = best.pivot_table(index="symbol", columns="mode", values="net_pnl", aggfunc="first")
    piv["long_plus_short"] = piv.get("long", 0) + piv.get("short", 0)
    print("\n" + "=" * 78)
    print("dual vs long+short, per symbol (net $)")
    print("=" * 78)
    with pd.option_context("display.width", 200):
        print(piv.to_string())

    tl = piv["long"].sum() if "long" in piv else 0
    tsh = piv["short"].sum() if "short" in piv else 0
    td = piv["dual"].sum() if "dual" in piv else 0
    print(f"\nLONG total:  {tl:+.4f}")
    print(f"SHORT total: {tsh:+.4f}")
    print(f"DUAL total:  {td:+.4f}")
    print(f"long+short:  {tl + tsh:+.4f}  (running two separate bots)")
    print(f"\nVerdict: dual {'BEATS' if td > max(tl, tsh) else 'does NOT beat'} best single side.")


if __name__ == "__main__":
    main()
