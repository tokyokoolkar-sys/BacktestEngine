"""
Test the "green-after-red long, hard exit above entry" idea, honestly.

Setup (per user idea):
  - entry: last candle red, next candle green -> enter LONG at next bar open
  - "hardstop above entry" for a long == a TAKE-PROFIT at +X% (engine.py:408).
    A real stop below entry is a separate downside protection grid.
  - no DCA averaging (max_dca_orders=0) so it's a clean single-shot bet.

Honest methodology (matches prior edge-killing runs):
  - resample 1m cache -> 1h; signal AND fills on 1h bars (fills at next-bar open).
  - full costs: taker/maker fees, funding, slippage, leverage, liquidation.

Grid: TP (the "exit above") x downside stop, across liquid symbols.
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

BASE_EXEC = ExecConfig(
    leverage=10, margin_usdt=2.5, taker_fee=0.0005,
    maker_fee=0.0002, slippage_bps=2.0, maint_margin_rate=0.05,
)

# "hardstop above entry" == take-profit distance above entry (% of avg entry)
TPS = [0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0]
# downside protection: none, or a fixed stop below entry
STOPS = [("none", None)] + [("fixed", sp) for sp in [0.5, 1.0, 2.0, 3.0, 5.0]]


def resample_1h(klines: pd.DataFrame) -> pd.DataFrame:
    df = klines.set_index("open_time")
    out = pd.DataFrame({
        "open_time": df["open"].resample("1h").first().index,
        "open": df["open"].resample("1h").first().values,
        "high": df["high"].resample("1h").max().values,
        "low": df["low"].resample("1h").min().values,
        "close": df["close"].resample("1h").last().values,
        "volume": df["volume"].resample("1h").sum().values,
    }).dropna()
    return out


def run_sweep(symbol: str, klines: pd.DataFrame, funding: pd.DataFrame) -> list[dict]:
    results = []
    for stop_mode, stop_pct in STOPS:
        for tp in TPS:
            cfg = BacktestConfig(
                symbol=symbol,
                strategy=StrategyConfig(
                    entry_mode="green_confirm",   # enter on green after red
                    green_gate="off",
                    checkpoint_mode="target",     # no green-exit; engine runs TP/stop
                    position_model="dca",         # dca model needed for TP path
                    direction="long",
                    stop_mode=stop_mode,
                    stop_pct=stop_pct,
                ),
                dca=DCAConfig(
                    price_drop_step_pct=0.5,
                    step_multiplier=1.0,
                    size_multiplier=1.1,
                    max_dca_orders=0,             # NO averaging -> clean single bet
                    first_order_ratio=0.96,
                    take_profit_pct=tp,           # the "hardstop above entry"
                ),
                exec=BASE_EXEC,
            )
            try:
                res = run_backtest(klines, funding, cfg)
                sc = scorecard(res)
                sc.update({"symbol": symbol, "stop_mode": stop_mode,
                           "stop_pct": stop_pct or 0, "tp_pct": tp,
                           "n_trades": len(res.trades)})
                results.append(sc)
            except Exception as e:
                print(f"  [{symbol}] ERR {stop_mode}/{stop_pct}/{tp}: {e}")
    return results


def main():
    try:
        funding = load_funding(str(_HERE / "cache" / "funding.parquet"))
    except Exception:
        funding = pd.DataFrame(columns=["funding_time", "funding_rate"])

    all_rows = []
    for symbol in SYMBOLS:
        kp = _HERE / "cache" / symbol / "klines.parquet"
        if not kp.exists():
            print(f"SKIP {symbol} (no cache)")
            continue
        print(f"Loading {symbol}...")
        klines = resample_1h(load_klines(str(kp)))
        all_rows.extend(run_sweep(symbol, klines, funding))

    if not all_rows:
        print("No results.")
        return

    df = pd.DataFrame(all_rows)
    df.to_parquet(str(_HERE / "sweep_green_confirm_results.parquet"))

    # Portfolio view: sum net pnl per (stop,tp) across symbols
    print("\n" + "=" * 74)
    print("PORTFOLIO by (stop, TP) -- summed net $ across all symbols")
    print("=" * 74)
    agg = (df.groupby(["stop_mode", "stop_pct", "tp_pct"])
             .agg(total_net=("net_pnl", "sum"),
                  avg_exp=("expectancy", "mean"),
                  avg_wr=("win_rate", "mean"),
                  trades=("n_trades", "sum"))
             .reset_index()
             .sort_values("total_net", ascending=False))
    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(agg.to_string(index=False))

    best = agg.iloc[0]
    print(f"\nBEST: stop={best['stop_mode']}/{best['stop_pct']} TP={best['tp_pct']}% "
          f"-> total_net={best['total_net']:+.4f} avg_wr={best['avg_wr']*100:.1f}%")
    print(f"Rows with positive total_net: {(agg['total_net'] > 0).sum()}/{len(agg)}")


if __name__ == "__main__":
    main()
