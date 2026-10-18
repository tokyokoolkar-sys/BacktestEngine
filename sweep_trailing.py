"""
Test the "green-after-red long + TRAILING stop" idea, honestly.

User idea: stop starts a tiny bit above entry, rest trails above the first
level as price rises. For a LONG a stop lives BELOW price and ratchets UP:
  - initial stop = entry * (1 - init_stop_pct/100)   (real downside protection)
  - each bar, trail = max(trail, peak * (1 - trail_pct/100))
  - once peak rises enough the trail crosses ABOVE entry -> locks profit,
    which is exactly "stop a bit above entry, rest above the first".

Engine has no trailing exit, and engine.py mirrors the live bot (regression
guarded) -- so this is a SELF-CONTAINED single-shot long sim that reuses the
engine's exact cost helpers (slippage, taker fee both sides, funding, liq floor)
and the real CheckpointStrategy for the green-after-red entry signal.

No lookahead: entry signal from bar close fills at NEXT bar open. The trailing
level is ratcheted from the PREVIOUS bar's high, then tested against the current
bar's low (never uses the current bar's own high to both move and trigger).

Honest 1h/1m hybrid: 1m cache -> 1h; signal + fills on 1h.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import _slip_entry, _slip_exit, _funding_in_window
from strategy import CheckpointStrategy

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "NEARUSDT", "INJUSDT", "TIAUSDT", "OPUSDT", "APTUSDT", "ARBUSDT",
]

EXEC = ExecConfig(leverage=10, margin_usdt=2.5, taker_fee=0.0005,
                  maker_fee=0.0002, slippage_bps=2.0, maint_margin_rate=0.05)

INIT_STOPS = [0.5, 1.0, 2.0, 3.0]   # initial downside stop % below entry
TRAILS = [0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0]   # trail distance % below peak


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


def sim(symbol, k, funding, init_stop_pct, trail_pct):
    e = EXEC
    slip = e.slippage_bps / 10_000.0
    o = k["open"].to_numpy(float); h = k["high"].to_numpy(float)
    lo = k["low"].to_numpy(float); c = k["close"].to_numpy(float)
    t_ns = k["open_time"].to_numpy("int64"); ts = k["open_time"].to_list()
    ft = funding["funding_time"].to_numpy("int64")
    fr = funding["funding_rate"].to_numpy(float)

    strat = CheckpointStrategy(StrategyConfig(
        entry_mode="green_confirm", green_gate="off",
        checkpoint_mode="target", direction="long"))  # engine-less; we own exits

    in_pos = False
    entry_px = avg = notional = size = fees = 0.0
    entry_ns = 0; entry_t = None
    stop = 0.0; peak = 0.0
    pending = False
    net_sum = 0.0; wins = 0; n = 0; worst = 0.0

    for i in range(1, len(k)):
        if in_pos:
            bar_low, bar_high = lo[i], h[i]
            # ratchet trail from PREVIOUS bar peak (no lookahead), keep initial floor
            stop = max(stop, peak * (1.0 - trail_pct / 100.0))
            liq = avg * (1.0 - 1.0 / e.leverage + e.maint_margin_rate)
            exit_px = reason = None
            if bar_low <= liq and liq >= stop:
                exit_px = _slip_exit(liq, slip); reason = "liq"
            elif bar_low <= stop:
                exit_px = _slip_exit(stop, slip); reason = "trail"
            if exit_px is not None:
                gross = (exit_px - avg) * size
                exit_fee = notional * e.taker_fee
                rate = _funding_in_window(ft, fr, entry_ns, t_ns[i])
                fund = rate * notional
                net = gross - (fees + exit_fee) - fund
                if reason == "liq":
                    net = max(-notional / e.leverage, net)
                net_sum += net; n += 1
                if net > 0: wins += 1
                worst = min(worst, net)
                in_pos = False
            else:
                peak = max(peak, bar_high)  # update AFTER test -> next bar uses it

        # entry fill from prior signal, at this bar open
        if pending and not in_pos:
            entry_px = _slip_entry(o[i], slip)
            avg = entry_px; size = e.margin_usdt * e.leverage / entry_px
            notional = size * entry_px; fees = notional * e.taker_fee
            stop = entry_px * (1.0 - init_stop_pct / 100.0)
            peak = h[i]; entry_ns = t_ns[i]; entry_t = ts[i]
            in_pos = True; pending = False

        # signal on this closed bar -> fill next bar open
        prev = {"high": h[i - 1]}
        row = {"open": o[i], "close": c[i], "high": h[i], "low": lo[i], "t": t_ns[i]}
        action = strat.on_closed_bar(prev, row, in_pos)
        if action == "enter" and not in_pos:
            pending = True

    wr = wins / n if n else 0.0
    return {"symbol": symbol, "init_stop_pct": init_stop_pct, "trail_pct": trail_pct,
            "net_pnl": net_sum, "n_trades": n, "win_rate": wr, "worst": worst}


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
        for isp in INIT_STOPS:
            for tp in TRAILS:
                rows.append(sim(s, k, funding, isp, tp))

    df = pd.DataFrame(rows)
    df.to_parquet(str(_HERE / "sweep_trailing_results.parquet"))

    print("\n" + "=" * 74)
    print("PORTFOLIO by (init_stop, trail) -- summed net $ across symbols")
    print("=" * 74)
    agg = (df.groupby(["init_stop_pct", "trail_pct"])
             .agg(total_net=("net_pnl", "sum"), avg_wr=("win_rate", "mean"),
                  trades=("n_trades", "sum"), worst=("worst", "min"))
             .reset_index().sort_values("total_net", ascending=False))
    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(agg.to_string(index=False))
    b = agg.iloc[0]
    print(f"\nBEST: init_stop={b['init_stop_pct']}% trail={b['trail_pct']}% "
          f"-> total_net={b['total_net']:+.4f} avg_wr={b['avg_wr']*100:.1f}%")
    print(f"Positive configs: {(agg['total_net'] > 0).sum()}/{len(agg)}")


if __name__ == "__main__":
    main()
