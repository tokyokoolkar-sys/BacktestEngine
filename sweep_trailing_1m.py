"""
HONEST verification of the green-after-red long + trailing-stop idea.

Difference vs sweep_trailing.py: exits are walked on REAL 1m bars, not 1h.
The 1h version assumes the favorable intrabar order (price makes the high, THEN
pulls back to the trail) -- that manufactures fake profit on a tight trail. Here
we walk the actual 1m path inside each hour, so if the 1m low hits the trail
BEFORE the hour's high is made, we exit there. This is the config that killed
every prior 'edge'.

  - signal: CheckpointStrategy green_confirm on 1h bars (entry fills next 1h open)
  - exit:   trailing stop simulated bar-by-bar on 1m bars
  - stop starts init_stop_pct below entry; trails trail_pct below running 1m peak
  - no lookahead: ratchet stop from running peak, test current 1m low, update
    peak AFTER the test
  - full costs: taker fee both sides, slippage, funding, liq floor
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
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

INIT_STOPS = [0.5, 1.0, 2.0, 3.0]
TRAILS = [0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0]

_H_NS = 3_600_000_000_000  # 1h in ns


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


def sim(symbol, k1, k1h, funding, init_stop_pct, trail_pct):
    e = EXEC
    slip = e.slippage_bps / 10_000.0

    # 1m arrays
    m_open = k1["open"].to_numpy(float); m_high = k1["high"].to_numpy(float)
    m_low = k1["low"].to_numpy(float); m_t = k1["open_time"].to_numpy("int64")
    # 1h arrays (signal)
    ho = k1h["open"].to_numpy(float); hh = k1h["high"].to_numpy(float)
    hl = k1h["low"].to_numpy(float); hc = k1h["close"].to_numpy(float)
    ht = k1h["open_time"].to_numpy("int64")
    ft = funding["funding_time"].to_numpy("int64")
    fr = funding["funding_rate"].to_numpy(float)

    strat = CheckpointStrategy(StrategyConfig(
        entry_mode="green_confirm", green_gate="off",
        checkpoint_mode="target", direction="long"))

    in_pos = False
    avg = notional = size = fees = 0.0
    entry_ns = 0
    stop = 0.0; peak = 0.0
    pending = False
    net_sum = 0.0; wins = 0; n = 0; worst = 0.0

    liq_mult = (1.0 - 1.0 / e.leverage + e.maint_margin_rate)

    for i in range(1, len(k1h)):
        # ---- 1. if in position, walk the 1m bars of THIS 1h interval for exit ----
        if in_pos:
            t0 = ht[i]
            lo_idx = np.searchsorted(m_t, t0, side="left")
            hi_idx = np.searchsorted(m_t, t0 + _H_NS, side="left")
            liq = avg * liq_mult
            for j in range(lo_idx, hi_idx):
                stop = max(stop, peak * (1.0 - trail_pct / 100.0))
                exit_px = reason = None
                if m_low[j] <= liq and liq >= stop:
                    exit_px = _slip_exit(liq, slip); reason = "liq"
                elif m_low[j] <= stop:
                    exit_px = _slip_exit(stop, slip); reason = "trail"
                if exit_px is not None:
                    gross = (exit_px - avg) * size
                    exit_fee = notional * e.taker_fee
                    rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                    fund = rate * notional
                    net = gross - (fees + exit_fee) - fund
                    if reason == "liq":
                        net = max(-notional / e.leverage, net)
                    net_sum += net; n += 1
                    if net > 0: wins += 1
                    worst = min(worst, net)
                    in_pos = False
                    break
                peak = max(peak, m_high[j])

        # ---- 2. entry fill from prior signal, at this 1h bar open (= first 1m open) ----
        if pending and not in_pos:
            entry_px = _slip_entry(ho[i], slip)
            avg = entry_px; size = e.margin_usdt * e.leverage / entry_px
            notional = size * entry_px; fees = notional * e.taker_fee
            stop = entry_px * (1.0 - init_stop_pct / 100.0)
            peak = entry_px; entry_ns = ht[i]
            in_pos = True; pending = False
            # walk the remaining 1m bars of THIS interval too (entry hour)
            t0 = ht[i]
            lo_idx = np.searchsorted(m_t, t0, side="left")
            hi_idx = np.searchsorted(m_t, t0 + _H_NS, side="left")
            liq = avg * liq_mult
            for j in range(lo_idx, hi_idx):
                stop = max(stop, peak * (1.0 - trail_pct / 100.0))
                exit_px = reason = None
                if m_low[j] <= liq and liq >= stop:
                    exit_px = _slip_exit(liq, slip); reason = "liq"
                elif m_low[j] <= stop:
                    exit_px = _slip_exit(stop, slip); reason = "trail"
                if exit_px is not None:
                    gross = (exit_px - avg) * size
                    exit_fee = notional * e.taker_fee
                    rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                    fund = rate * notional
                    net = gross - (fees + exit_fee) - fund
                    net_sum += net; n += 1
                    if net > 0: wins += 1
                    worst = min(worst, net)
                    in_pos = False
                    break
                peak = max(peak, m_high[j])

        # ---- 3. signal on this closed 1h bar -> fill next 1h open ----
        prev = {"high": hh[i - 1]}
        row = {"open": ho[i], "close": hc[i], "high": hh[i], "low": hl[i], "t": ht[i]}
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
        k1 = load_klines(str(kp))
        k1h = resample_1h(k1)
        for isp in INIT_STOPS:
            for tp in TRAILS:
                rows.append(sim(s, k1, k1h, funding, isp, tp))

    df = pd.DataFrame(rows)
    df.to_parquet(str(_HERE / "sweep_trailing_1m_results.parquet"))

    print("\n" + "=" * 74)
    print("PORTFOLIO by (init_stop, trail) -- 1m-fill HONEST -- summed net $")
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

    # side-by-side vs the 1h-fill run, if present
    p1h = _HERE / "sweep_trailing_results.parquet"
    if p1h.exists():
        d1h = pd.read_parquet(p1h)
        a1h = d1h.groupby(["init_stop_pct", "trail_pct"])["net_pnl"].sum().reset_index()
        best1h = a1h.sort_values("net_pnl", ascending=False).iloc[0]
        print(f"\n1h-fill best was: init_stop={best1h['init_stop_pct']}% "
              f"trail={best1h['trail_pct']}% net={best1h['net_pnl']:+.4f}")
        print(f"1m-fill best is:  net={b['total_net']:+.4f}  "
              f"(delta {b['total_net'] - best1h['net_pnl']:+.4f})")


if __name__ == "__main__":
    main()
