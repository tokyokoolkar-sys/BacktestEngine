"""
exp_delayed_fill.py — the EXACT delayed-fill sim (honest net PnL of delayed-decide).

The upper bound (exp_delayed_decide.py) kept real-engine DCA trades whose entry-bar
verdict matched direction but used the IMMEDIATE entry price — ignoring the ~75min delay
tax. This runner pays the tax: it enters at the END of the 5x15m confirm wait (worse,
later price) and runs the real DCA ladder / stop / liq / take-profit / first-green
checkpoint from there. That is the tradeable number.

Machine (per lock, no lookahead in the signal):
  1h "first red after green" lock at bar i (close of a red 1h whose prev 1h was green).
  -> wait N=5 15m klines strictly after bar i's OPEN (same window as the upper bound's
     verdict_series; those klines have all CLOSED by the lock decision at bar i's close).
  -> netmove verdict: LONG / SHORT / SKIP.
  -> DELAYED ENTRY at the open of the 15m kline immediately after the wait
     (entry instant = lock_open + 75min; ~15min into bar i+1).
  -> run one independent DCA basket on the 1h bars with open_time >= entry_time
     (the partial entry bar is skipped = conservative, no intrabar lookahead).

Independent-basket assumption matches the upper bound exactly (it kept individual trades),
so Blind / Imm / Delay sit on the same footing. The honest delay tax is Imm - Delay.

  python exp_delayed_fill.py --split H2          # OOS (default)
  python exp_delayed_fill.py --split all
  python exp_delayed_fill.py --split H2 --self-check   # prove basket math == engine

ponytail: management runs on 1h bars only (checkpoint = 1h green, like the whole project);
the 15m klines are used solely for the delayed fill price/timing. Sub-1h moves inside the
skipped entry bar are not modeled -> add 15m-granular management only if the delay number
is close enough to matter. Independent baskets ignore long/short occupancy conflicts (same
simplification as the upper bound) -> model a single combined book only if Delay clears C.
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

import numpy as np
import pandas as pd

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import (
    run_backtest,
    _compute_stop,
    _dca_base_notional,
    _dca_cumulative_drops,
    _dca_safety_notionals,
    _funding_in_window,
)

CACHE = next(p / "cache" for p in _HERE.parents if (p / "cache").is_dir())
SYMS = ["NEARUSDT", "INJUSDT", "TIAUSDT", "FILUSDT", "OPUSDT", "ARBUSDT", "APTUSDT"]
STOP = 2.0
N_CONFIRM = 5
NET_DEADBAND = 0.001
_HOUR_NS = 3_600_000_000_000
_Q_NS = 900_000_000_000  # 15min


# --------------------------------------------------------------------------- #
# resample / time helpers (same as the upper-bound harness)
# --------------------------------------------------------------------------- #

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


def ns(idx):
    idx = pd.to_datetime(idx)
    idx = idx.tz_localize(None) if idx.tz is not None else idx
    return idx.values.astype("datetime64[ns]").astype("int64")


def cfg(direction):
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode="fixed", stop_pct=STOP,
            checkpoint_mode="first_green", position_model="dca",
            direction=direction),
        dca=DCAConfig(), exec=ExecConfig())


# --------------------------------------------------------------------------- #
# verdict per 1h bar (IDENTICAL definition to exp_delayed_decide.verdict_series)
# --------------------------------------------------------------------------- #

def verdicts_and_fills(k1h, k15):
    """Return (verdict[], fill_ns[], fill_px[]) per 1h bar.
    verdict: +1 long / -1 short / 0 skip. fill = open of the 15m kline right after the
    N-confirm wait (the delayed entry). -1 in fill_ns where the wait runs off the data.
    """
    ht = ns(k1h["open_time"].values)          # 1h open times
    ct = ns(k15["open_time"].values)          # 15m open times
    o15 = k15["open"].values
    c15 = k15["close"].values
    start = np.searchsorted(ct, ht, side="right")   # first 15m kline after each 1h OPEN
    end = start + N_CONFIRM                          # index of the delayed-fill kline
    m = len(k1h)
    verdict = np.zeros(m, dtype=int)
    fill_ns = np.full(m, -1, dtype="int64")
    fill_px = np.full(m, np.nan)
    ok = end < len(k15)
    o0 = o15[start[ok]]
    cN = c15[end[ok] - 1]
    net = (cN - o0) / o0
    verdict[ok] = np.where(net > NET_DEADBAND, 1, np.where(net < -NET_DEADBAND, -1, 0))
    fill_ns[ok] = ct[end[ok]]
    fill_px[ok] = o15[end[ok]]
    return verdict, fill_ns, fill_px


# --------------------------------------------------------------------------- #
# ONE independent basket — exact engine math (fees / funding / DCA / stop / liq /
# TP / first-green checkpoint). Management on 1h bars with open_ns >= entry_ns.
# --------------------------------------------------------------------------- #

def simulate_basket(entry_px, entry_ns, direction, o, h, l, c, kns, ft, fr, dcfg, ecfg, scfg):
    short = direction == "short"
    slip = ecfg.slippage_bps / 10_000.0
    tp_pct = dcfg.take_profit_pct / 100.0
    lev, mmr, tf = ecfg.leverage, ecfg.maint_margin_rate, ecfg.taker_fee

    base_notional = _dca_base_notional(ecfg.margin_usdt, lev, dcfg)
    safety_notionals = _dca_safety_notionals(dcfg, base_notional)
    cum_drops = _dca_cumulative_drops(dcfg)

    avg = entry_px
    size = base_notional / entry_px
    tot_notional = base_notional
    tot_margin = base_notional / lev
    tot_fees = base_notional * tf
    if short:
        trigs = [entry_px * (1.0 + d / 100.0) for d in cum_drops]
        liq = avg * (1.0 + 1.0 / lev - mmr)
        tp = avg * (1.0 - tp_pct)
    else:
        trigs = [entry_px * (1.0 - d / 100.0) for d in cum_drops]
        liq = avg * (1.0 - 1.0 / lev + mmr)
        tp = avg * (1.0 + tp_pct)
    stop = _compute_stop(entry_px, np.nan, scfg)
    idx = 0
    n_saf = 0

    def _close(fill, exit_ns, cap):
        gross = (avg - fill if short else fill - avg) * size
        exit_fee = tot_notional * tf
        rate = _funding_in_window(ft, fr, entry_ns, exit_ns)
        fund = (-rate if short else rate) * tot_notional
        net = gross - (tot_fees + exit_fee) - fund
        return max(-tot_margin, net) if cap else net

    j0 = int(np.searchsorted(kns, entry_ns, side="left"))
    nbars = len(kns)
    for j in range(j0, nbars):
        bh, bl = h[j], l[j]
        # a. DCA safety fills (change avg -> recompute liq/tp/stop)
        while idx < dcfg.max_dca_orders:
            trig = trigs[idx]
            if not (bh >= trig if short else bl <= trig):
                break
            saf_fill = trig * (1.0 - slip) if short else trig * (1.0 + slip)
            saf_notional = safety_notionals[idx]
            saf_size = saf_notional / saf_fill
            avg = (avg * size + saf_fill * saf_size) / (size + saf_size)
            size += saf_size
            tot_notional += saf_notional
            tot_margin += saf_notional / lev
            tot_fees += saf_notional * tf
            n_saf += 1
            idx += 1
            if short:
                liq = avg * (1.0 + 1.0 / lev - mmr); tp = avg * (1.0 - tp_pct)
            else:
                liq = avg * (1.0 - 1.0 / lev + mmr); tp = avg * (1.0 + tp_pct)
            stop = _compute_stop(avg, np.nan, scfg)

        # b. exit priority: liq (unless protected) -> stop -> TP  (assumption 9)
        stop_active = scfg.stop_mode != "none" and stop is not None
        if short:
            stop_hit = stop_active and bh >= stop
            liq_hit = bh >= liq
            protects = stop_hit and stop <= liq
        else:
            stop_hit = stop_active and bl <= stop
            liq_hit = bl <= liq
            protects = stop_hit and stop >= liq

        if liq_hit and not protects:
            fill = liq * (1.0 + slip) if short else liq * (1.0 - slip)
            return _close(fill, kns[j], cap=True), n_saf, "liquidation"
        if stop_hit:
            fill = stop * (1.0 + slip) if short else stop * (1.0 - slip)
            return _close(fill, kns[j], cap=False), n_saf, "stop"
        if (bl <= tp if short else bh >= tp):
            fill = tp * (1.0 + slip) if short else tp * (1.0 - slip)
            return _close(fill, kns[j], cap=False), n_saf, "take_profit"

        # c. first-green checkpoint -> fill at NEXT 1h open
        is_green = c[j] > o[j]
        if short:
            is_green = not is_green
        if is_green:
            if j + 1 < nbars:
                raw, ex_ns = o[j + 1], kns[j + 1]
            else:
                raw, ex_ns = c[j], kns[j]
            fill = raw * (1.0 + slip) if short else raw * (1.0 - slip)
            return _close(fill, ex_ns, cap=False), n_saf, "checkpoint"

    # end of data
    fill = c[nbars - 1] * (1.0 + slip) if short else c[nbars - 1] * (1.0 - slip)
    return _close(fill, kns[nbars - 1], cap=False), n_saf, "end"


# --------------------------------------------------------------------------- #
# self-check: replay engine long DCA trades through simulate_basket
# --------------------------------------------------------------------------- #

def self_check(k1h, f):
    o = k1h["open"].values; h = k1h["high"].values
    l = k1h["low"].values; c = k1h["close"].values
    kns = ns(k1h["open_time"].values)
    ft = f["funding_time"].to_numpy("int64"); fr = f["funding_rate"].to_numpy(float)
    dcfg, ecfg, scfg = DCAConfig(), ExecConfig(), cfg("long").strategy
    worst = 0.0
    res = run_backtest(k1h, f, cfg("long"))
    for tr in res.trades:
        my, _, _ = simulate_basket(tr.entry_price, tr.entry_time.value, "long",
                                   o, h, l, c, kns, ft, fr, dcfg, ecfg, scfg)
        worst = max(worst, abs(my - tr.net_pnl))
    return worst, len(res.trades)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="H2", choices=["all", "H1", "H2"])
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    dcfg, ecfg = DCAConfig(), ExecConfig()
    Btot = Itot = Dtot = 0.0
    n_lock = n_imm = n_del = 0
    worst_sc = 0.0

    hdr = f"{'sym':>9}  {'Blind':>9}  {'Imm(eng)':>9}  {'Delay':>9}  {'locks':>6}  {'d.n':>5}"
    if args.self_check:
        hdr += f"  {'sc.err':>9}"
    print(hdr)

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

        o = k1h["open"].values; h = k1h["high"].values
        l = k1h["low"].values; c = k1h["close"].values
        kns = ns(k1h["open_time"].values)
        ft = f["funding_time"].to_numpy("int64"); fr = f["funding_rate"].to_numpy(float)
        scfg_l = cfg("long").strategy
        scfg_s = cfg("short").strategy
        nbar = len(k1h)

        # Real engine trades = proper strategy cadence (one basket at a time,
        # FLAT_ARMED/LOCKED cycle). Blind = both books unfiltered.
        long_tr = run_backtest(k1h, f, cfg("long")).trades
        short_tr = run_backtest(k1h, f, cfg("short")).trades
        blind = sum(t.net_pnl for t in long_tr) + sum(t.net_pnl for t in short_tr)

        verdict, fill_ns, fill_px = verdicts_and_fills(k1h, k15)

        imm = dl = 0.0
        nl = ni = nd = 0

        # Keep each engine trade whose lock-bar verdict matches its side; recompute
        # PnL from the delayed fill. Imm = engine's own net_pnl (== upper bound).
        for trades, want_v, direction, scfg in (
                (long_tr, 1, "long", scfg_l), (short_tr, -1, "short", scfg_s)):
            for tr in trades:
                # lock bar i: engine filled at bar i+1 open == entry_time.
                pos = int(np.searchsorted(kns, tr.entry_time.value, side="left"))
                i = pos - 1
                if i < 0 or i >= nbar:
                    continue
                if int(verdict[i]) != want_v:
                    continue          # verdict disagrees with this book -> skip
                nl += 1
                imm += tr.net_pnl; ni += 1
                if fill_ns[i] < 0:
                    continue          # delayed wait ran off the 15m data
                entry_ns = int(fill_ns[i])
                slip = ecfg.slippage_bps / 10_000.0
                entry_px = fill_px[i] * (1.0 + slip) if want_v == 1 else fill_px[i] * (1.0 - slip)
                if int(np.searchsorted(kns, entry_ns, side="left")) >= nbar:
                    continue
                pnl_d, _, _ = simulate_basket(entry_px, entry_ns, direction,
                                              o, h, l, c, kns, ft, fr, dcfg, ecfg, scfg)
                dl += pnl_d; nd += 1

        Btot += blind; Itot += imm; Dtot += dl
        n_lock += nl; n_imm += ni; n_del += nd
        line = f"{s:>9}  {blind:>+9.2f}  {imm:>+9.2f}  {dl:>+9.2f}  {nl:>6}  {nd:>5}"
        if args.self_check:
            err, _ = self_check(k1h, f)
            worst_sc = max(worst_sc, err)
            line += f"  {err:>9.2e}"
        print(line)

    print(f"\n=== stop={STOP}%  N_confirm={N_CONFIRM}  split={args.split}  (independent baskets) ===")
    print(f"  Blind  both books (engine)   : {Btot:>+9.2f}")
    print(f"  Imm    immediate + verdict   : {Itot:>+9.2f}   n={n_imm}")
    print(f"  Delay  HONEST delayed fill   : {Dtot:>+9.2f}   n={n_del}")
    print(f"  delay tax (Imm - Delay)      : {Itot - Dtot:>+9.2f}")
    if Btot:
        print(f"  Delay/Blind = {Dtot / Btot:.2f}   Imm/Blind = {Itot / Btot:.2f}")
    if args.self_check:
        verdict_txt = "FAITHFUL" if worst_sc < 1e-6 else "MISMATCH — basket math drifts from engine"
        print(f"  self-check worst |basket-engine| : {worst_sc:.2e}   -> {verdict_txt}")


if __name__ == "__main__":
    main()
