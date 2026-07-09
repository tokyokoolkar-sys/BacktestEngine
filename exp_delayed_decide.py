"""
exp_delayed_decide.py — does "delayed-decide" DCA beat blind both-books, net of costs?

Machine: 1h red-dip lock -> wait N 15m confirm klines (strictly after lock close) ->
netmove verdict (LONG / SHORT / SKIP) -> run the DCA basket that direction, stop=2%.

This is the OPTIMISTIC UPPER BOUND: it keeps real-engine DCA trades (full fee/funding/
liq/stop/checkpoint math via run_backtest) whose ENTRY bar's confirm-verdict matches the
trade direction — but uses the IMMEDIATE entry price, NOT the delayed fill. So it ignores
the ~75min delay tax. If DD does not beat C here, the exact delayed-fill sim is pointless.
If it clears C with room, build the precise delayed-fill version next.

No lookahead in the SIGNAL: the confirm window is 15m klines strictly after the lock close,
and the verdict gates a trade that (in the real engine) already filled at/after that bar.

    python exp_delayed_decide.py --split H2
    python exp_delayed_decide.py --split all
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import numpy as np, pandas as pd
from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import run_backtest

CACHE = _HERE.parents[2] / "cache"
SYMS = ["NEARUSDT", "INJUSDT", "TIAUSDT", "FILUSDT", "OPUSDT", "ARBUSDT", "APTUSDT"]
STOP = 2.0
N_CONFIRM = 5          # 15m klines to wait
NET_DEADBAND = 0.001   # 0.1%


def resample(k, rule):
    df = k.set_index("open_time")
    o = pd.DataFrame({
        "open_time": df["open"].resample(rule).first().index,
        "open": df["open"].resample(rule).first().values,
        "high": df["high"].resample(rule).max().values,
        "low": df["low"].resample(rule).min().values,
        "close": df["close"].resample(rule).last().values,
    })
    if "volume" in df.columns:
        o["volume"] = df["volume"].resample(rule).sum().values
    return o.dropna().reset_index(drop=True)


def naive(idx):
    idx = pd.to_datetime(idx)
    return (idx.tz_localize(None) if idx.tz is not None else idx).values.astype("datetime64[ns]")


def cfg(direction):
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode="fixed", stop_pct=STOP,
            checkpoint_mode="first_green", position_model="dca",
            direction=direction),
        dca=DCAConfig(), exec=ExecConfig())


def verdict_series(k1h, k15):
    """For each 1h bar index, the delayed netmove verdict (+1 long / -1 short / 0 skip),
    computed from N_CONFIRM 15m klines strictly AFTER that 1h bar's close time."""
    ht = naive(k1h["open_time"].values)
    ct = naive(k15["open_time"].values)
    o15 = k15["open"].values
    c15 = k15["close"].values
    start = np.searchsorted(ct, ht, side="right")   # first 15m kline after each 1h close
    end = start + N_CONFIRM
    verdict = np.zeros(len(k1h), dtype=int)
    ok = end < len(k15)
    o0 = o15[start[ok]]
    cN = c15[end[ok] - 1]
    net = (cN - o0) / o0
    v = np.where(net > NET_DEADBAND, 1, np.where(net < -NET_DEADBAND, -1, 0))
    verdict[ok] = v
    return pd.Series(verdict, index=pd.to_datetime(ht))


def keep(trades, vser, want_long):
    """Keep trades whose entry-bar verdict matches the wanted side. Returns (pnl, n).
    ponytail: uses IMMEDIATE entry price, not the ~75min-late delayed fill -> this is an
    OPTIMISTIC UPPER BOUND, not tradeable PnL. The honest net-of-delay-tax number needs the
    exact delayed-fill sim (enter at end of the 5x15m wait, worse price). Build that next."""
    tot = 0.0; n = 0
    for tr in trades:
        et = pd.Timestamp(tr.entry_time)
        et = et.tz_localize(None) if et.tz is not None else et
        pos = vser.index.searchsorted(et, side="right") - 1
        if pos < 0:
            continue
        v = int(vser.iloc[pos])
        if (v == 1 and want_long) or (v == -1 and not want_long):
            tot += tr.net_pnl; n += 1
    return tot, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="H2", choices=["all", "H1", "H2"])
    args = ap.parse_args()

    Ctot = DDtot = 0.0
    dd_long = dd_short = 0
    n_locks_total = 0
    print(f"{'sym':>9}  {'C(blind)':>10}  {'DD(upper)':>10}  {'DD n':>6}")
    for s in SYMS:
        kp = CACHE / s / "klines.parquet"
        fp = CACHE / f"{s}-funding.parquet"
        if not kp.exists():
            print(f"{s:>9}  no cache"); continue
        kraw = load_klines(str(kp))
        f = load_funding(str(fp)) if fp.exists() else pd.DataFrame(
            {"funding_time": pd.Series([], dtype="int64"),
             "funding_rate": pd.Series([], dtype=float)})
        k1h = resample(kraw, "1h")
        k15 = resample(kraw, "15min")
        if args.split != "all":
            mid = len(k1h) // 2
            lo = k1h.iloc[:mid] if args.split == "H1" else k1h.iloc[mid:]
            t0, t1 = lo["open_time"].iloc[0], lo["open_time"].iloc[-1]
            k1h = lo.reset_index(drop=True)
            k15 = k15[(k15["open_time"] >= t0) & (k15["open_time"] <= t1)].reset_index(drop=True)

        vser = verdict_series(k1h, k15)
        # C = both books blind
        lt = run_backtest(k1h, f, cfg("long")).trades
        st = run_backtest(k1h, f, cfg("short")).trades
        c = sum(t.net_pnl for t in lt) + sum(t.net_pnl for t in st)
        # DD = keep long-book trades where verdict=long, short-book where verdict=short
        dl, dln = keep(lt, vser, True)
        ds, dsn = keep(st, vser, False)
        d = dl + ds
        Ctot += c; DDtot += d
        dd_long += dln; dd_short += dsn
        n_locks_total += int((vser != 0).sum())
        print(f"{s:>9}  {c:>+10.2f}  {d:>+10.2f}  {dln+dsn:>6}")

    print(f"\n=== stop={STOP}%  split={args.split}  (DD = optimistic upper bound, no delay tax) ===")
    print(f"  C  both books blind        : {Ctot:>+9.2f}")
    print(f"  DD delayed-decide (netmove): {DDtot:>+9.2f}   long={dd_long} short={dd_short}")
    print(f"  DD/C = {DDtot/Ctot:.2f}" if Ctot else "")
    print("\n  TODO(next session): build the exact delayed-fill sim (enter at END of the 5x15m")
    print("        wait, worse price, real DCA ladder/stop/liq/checkpoint) for the honest net PnL.")
    print("        This upper bound (+ the clean +0.68%/61.6% predictive test) says it's worth building.")


if __name__ == "__main__":
    main()
