"""
exp_payoff_structure.py — does the win rate survive a wider take-profit?

WHY THIS EXISTS
---------------
Live results 2026-06-18..07-27 (734 closed trades, from Binance income history):
    win rate 90.32%   avg win +$0.110   avg loss -$1.177
    gross -$10.60, commissions -$14.85, NET -$25.84

Break-even win rate for that payoff structure is 91.45% gross / 93.0% after fees.
So the edge was NEGATIVE BEFORE FEES. The failure is structural, not bad luck:
a 0.5% take-profit against a 5% stop needs a win rate that no filter delivers.

The obvious fix is "widen the TP". The trap is that widening the TP LOWERS the win
rate — the 90% is partly an artifact of a tiny target against a fat stop. So the
real question is not "what TP is break-even at 90%" but:

    does the win rate stay ABOVE the break-even line as the TP widens?

Break-even WR for a given TP against a 5% stop:
    TP 0.5% -> 90.9%    TP 1.0% -> 83.3%    TP 2.0% -> 71.4%    TP 3.0% -> 62.5%

The break-even bar falls fast. If the realized win rate falls SLOWER than the bar,
there is a workable configuration. If it falls faster, the strategy family is dead
and no amount of tuning saves it.

This runs on July 2026 daily dumps (cache/oos_july) — the exact period the live
account traded and lost money, and a window no parameter here was tuned on.
May/June were used for prior tuning, so they are reported separately as a
consistency check, never as the headline.

Costs are modeled by engine.py: taker fee 0.05%, maker 0.02%, 2bp slippage,
8h funding, liquidation from maintenance margin.
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

FLEET = ["NEARUSDT", "TIAUSDT", "OPUSDT", "FILUSDT", "UNIUSDT", "SUIUSDT"]

# Live config (flip_20_config.yaml) — the structure that lost money.
LIVE_STOP = 5.0
LIVE_LEVERAGE = 10
LIVE_BASE_MARGIN = 2.0

# TP ladder to test, in %. Live sits at 0.5-0.8.
TP_GRID = [0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0]
# Stop distances to test against.
STOP_GRID = [5.0, 3.0, 2.0]


def breakeven_wr(tp_pct: float, stop_pct: float) -> float:
    """Win rate needed for zero expectancy at this payoff ratio (pre-fee)."""
    return stop_pct / (tp_pct + stop_pct) * 100.0


def make_cfg(tp: float, stop: float, direction: str = "long") -> BacktestConfig:
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip",
            green_gate="off",
            stop_mode="fixed",
            stop_pct=stop,
            # MUST be "target", not "first_green". The live bot builds its
            # strategy with stop_mode="none" and uses CheckpointStrategy for
            # ENTRY ONLY — exits are exchange-side TP/STOP orders, and there is
            # no green-candle exit. With "first_green" the checkpoint exit fires
            # before price ever reaches the TP, so >99% of trades close as
            # "checkpoint" and the take_profit_pct sweep measures nothing.
            checkpoint_mode="target",
            position_model="dca",
            direction=direction,
            greencount_filter=True,      # live has this ON
            greencount_period=10,
        ),
        dca=DCAConfig(
            take_profit_pct=tp,
            max_dca_orders=3,
            price_drop_step_pct=0.3,
            step_multiplier=1.0,
            size_multiplier=1.1,
            first_order_ratio=0.96,
            atr_ladder_mult=0.75,        # live ATR-adaptive ladder
            atr_step_min_pct=0.2,
            atr_step_max_pct=1.0,
        ),
        exec=ExecConfig(
            leverage=LIVE_LEVERAGE,
            margin_usdt=LIVE_BASE_MARGIN,
            taker_fee=0.0005,
            maker_fee=0.0002,
            slippage_bps=2.0,
            maint_margin_rate=0.05,
        ),
    )


def load_period(period_dir: Path, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Load klines for a symbol from a period dir; funding is optional."""
    for name in (f"{symbol}-1m-klines.parquet", "klines.parquet"):
        kp = period_dir / name
        if kp.exists():
            klines = load_klines(kp)
            break
    else:
        return None

    fp = _HERE / "cache" / f"{symbol}-funding.parquet"
    if fp.exists():
        funding = load_funding(fp)
    else:
        funding = pd.DataFrame({"funding_time": pd.to_datetime([], utc=True),
                                "funding_rate": []})
    return klines, funding


def run_grid(period_dir: Path, label: str) -> pd.DataFrame:
    rows = []
    for stop in STOP_GRID:
        for tp in TP_GRID:
            if tp >= stop:
                continue  # nonsensical: target beyond the stop
            # Pool every symbol's trades, then score once — a fleet-level
            # scorecard, not an average of per-symbol rates.
            pooled: list = []
            agg_liq = 0
            for sym in FLEET:
                loaded = load_period(period_dir, sym)
                if loaded is None:
                    continue
                klines, funding = loaded
                res = run_backtest(klines, funding, make_cfg(tp, stop))
                pooled.extend(res.trades)
                agg_liq += sum(1 for t in res.trades if t.exit_reason == "liquidation")
            if not pooled:
                continue
            from engine import Result as _Result
            sc = scorecard(_Result(trades=pooled, equity_curve=[], cfg=make_cfg(tp, stop)))
            agg_trades = sc["n_trades"]
            agg_net = sc["net_pnl"]
            wr = sc["win_rate"] * 100.0
            avg_w = sc["avg_win"]
            avg_l = -sc["avg_loss"]   # scorecard returns magnitude; show as negative
            be = breakeven_wr(tp, stop)
            rows.append({
                "period": label, "tp": tp, "stop": stop,
                "trades": agg_trades, "wr": wr, "be_wr": be,
                "margin": wr - be,
                "avg_w": avg_w, "avg_l": avg_l,
                "net": agg_net, "liq": agg_liq,
                "exp": agg_net / agg_trades,
            })
    return pd.DataFrame(rows)


def show(df: pd.DataFrame, label: str) -> None:
    print(f"\n{'='*94}")
    print(f"  {label}   (fleet: {', '.join(FLEET)})")
    print(f"{'='*94}")
    print(f"{'TP%':>5} {'stop%':>6} {'trades':>7} {'winrate':>8} {'need':>7} "
          f"{'margin':>7} {'avgW':>8} {'avgL':>8} {'net$':>9} {'exp$':>8} {'liq':>4}")
    print("-" * 94)
    for stop in sorted(df["stop"].unique(), reverse=True):
        sub = df[df["stop"] == stop].sort_values("tp")
        for _, r in sub.iterrows():
            flag = "  <<<" if r["margin"] > 0 and r["net"] > 0 else ""
            print(f"{r['tp']:5.1f} {r['stop']:6.1f} {r['trades']:7.0f} "
                  f"{r['wr']:7.2f}% {r['be_wr']:6.2f}% {r['margin']:+6.2f} "
                  f"{r['avg_w']:+8.3f} {r['avg_l']:+8.3f} {r['net']:+9.2f} "
                  f"{r['exp']:+8.4f} {r['liq']:4.0f}{flag}")
        print()


if __name__ == "__main__":
    periods = [
        (_HERE / "cache" / "oos_july", "JULY 2026 — TRUE HOLDOUT (live period, never tuned on)"),
        (_HERE / "cache" / "oos_may", "MAY 2026 — prior tuning window (consistency check only)"),
    ]
    all_df = []
    for pdir, label in periods:
        if not pdir.exists():
            print(f"skip {label}: {pdir} missing")
            continue
        df = run_grid(pdir, label.split(" —")[0])
        if df.empty:
            print(f"skip {label}: no trades")
            continue
        show(df, label)
        all_df.append(df)

    if all_df:
        combined = pd.concat(all_df, ignore_index=True)
        combined.to_csv(_HERE / "payoff_structure_results.csv", index=False)
        print(f"\nwrote payoff_structure_results.csv ({len(combined)} rows)")
        july = combined[combined["period"] == "JULY 2026"]
        if not july.empty:
            good = july[(july["margin"] > 0) & (july["net"] > 0)]
            print(f"\nJULY holdout: {len(good)}/{len(july)} configs with positive "
                  f"expectancy AND win rate above break-even")
            if not good.empty:
                print(good[["tp", "stop", "trades", "wr", "be_wr", "net", "exp"]]
                      .sort_values("exp", ascending=False).to_string(index=False))
            else:
                print("  NONE. No TP/stop combination in this grid is viable on the "
                      "holdout.\n  The strategy family does not have an edge to tune.")
