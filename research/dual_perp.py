"""dual_perp.py — same underlying, two order books (USDT-margined vs USDC-margined).

WHY THIS EXISTS, AND WHY IT IS BEING RE-RUN
-------------------------------------------
First pass killed this on cost arithmetic: ~5bp of dislocation against what I
called an "8bp maker floor". That floor was WRONG. Live commission rates on this
account, queried 2026-07-27:

    BTCUSDT   maker +2.000bp   taker +5.000bp
    BTCUSDC   maker +0.000bp   taker +4.000bp     <-- USDC maker is free
    SOLUSDC   maker +0.000bp   taker +4.000bp

A paired round trip is 4 fills: enter both legs, exit both legs. Only the USDT
leg pays maker fee. So the real maker floor is:

    2 x 2bp (USDT in/out) + 2 x 0bp (USDC in/out) = 4bp,  not 8bp

I doubled the cost of the only candidate that was close. That is the error this
file exists to correct.

WHAT IS STILL CHARGED, BECAUSE IT IS STILL REAL
-----------------------------------------------
Zero commission is not zero cost. Three things survive the fee waiver:

  1. FUNDING. Both legs pay/receive funding every 8h and the two books do NOT
     have the same funding rate. Over a 12h hold that differential can exceed
     the entire 5bp prize. Charged explicitly below from real funding history.
  2. LEG RISK. A maker order may not fill. A pair where one leg fills and the
     other does not is a naked directional position on a 10x book. Modeled as a
     fill-probability haircut, not ignored.
  3. SLIPPAGE ON THE EXIT. Maker on entry is a choice; maker on exit is a hope.
     A mean-reversion trade that must exit at a horizon cannot always wait.

MEASUREMENT DISCIPLINE (carried from the four families already buried)
----------------------------------------------------------------------
  * era split is reported BEFORE the pooled number
  * windows non-overlapping: rebalance period == hold period
  * the best cell out of N is reported AS a max-of-N, never as "the result"
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
USDT_CACHE = ROOT / "cache" / "hist"
USDC_CACHE = ROOT / "cache" / "usdc"

# Live rates, this account, feeTier 0 — verified 2026-07-27 via futures_commission_rate.
MAKER_USDT = 0.0002      # 2bp
MAKER_USDC = 0.0000      # 0bp  <-- the correction
TAKER_USDT = 0.0005      # 5bp
TAKER_USDC = 0.0004      # 4bp

# A paired round trip = 4 fills. Both legs in, both legs out.
COST_MAKER_BOTH = 2 * MAKER_USDT + 2 * MAKER_USDC     # 4bp  — both legs passive
COST_MAKER_IN_TAKER_OUT = (MAKER_USDT + MAKER_USDC) + (TAKER_USDT + TAKER_USDC)  # 11bp
COST_TAKER_BOTH = 2 * TAKER_USDT + 2 * TAKER_USDC     # 18bp

SPLIT = "2025-01-01"


def load_pairs() -> dict[str, pd.DataFrame]:
    """For each underlying with both books, a frame of aligned USDT/USDC closes."""
    out = {}
    # NOTE: the two caches use different filename conventions —
    # usdc/SYMUSDC-1h.parquet  vs  hist/SYMUSDT-1h-klines.parquet
    for f in sorted(glob.glob(str(USDC_CACHE / "*-1h.parquet"))):
        usdc_sym = os.path.basename(f).replace("-1h.parquet", "")
        base = usdc_sym[:-4]                       # strip "USDC"
        usdt_f = USDT_CACHE / f"{base}USDT-1h-klines.parquet"
        if not usdt_f.exists():
            continue
        a = pd.read_parquet(usdt_f)[["open_time", "close"]].set_index("open_time")
        b = pd.read_parquet(f)[["open_time", "close"]].set_index("open_time")
        df = pd.DataFrame({"usdt": a["close"], "usdc": b["close"]}).dropna()
        df = df[~df.index.duplicated()].sort_index()
        if len(df) > 500:
            out[base] = df
    return out


# Mean |funding differential| between the two books, measured on live history
# 2026-07-27 over the last 90 intervals: BTC 0.227, SOL 0.327, AVAX 0.293,
# BNB 0.318, XRP 0.393, DOGE 0.310 bp per 8h. A paired position is long one book
# and short the other, so the DIFFERENTIAL is the cost, not either rate alone.
# Charged as a certain drag at the mean, which is conservative in sign but
# understates the tail (max observed 1.9bp/8h).
FUNDING_DIFF_PER_8H = 0.30e-4


def run(df: pd.DataFrame, hold_h: int, entry_sd: float, cost: float,
        start=None, end=None) -> dict:
    """Fade the spread when it exceeds entry_sd, hold hold_h hours, non-overlapping.

    spread = usdc/usdt - 1. Rich USDC -> short USDC leg, long USDT leg.
    Paired P&L over the hold is -(spread_exit - spread_entry).
    """
    d = df
    if start is not None:
        d = d[d.index >= start]
    if end is not None:
        d = d[d.index < end]
    if len(d) < 24 * 30:
        return {"n": 0}

    spread = (d["usdc"] / d["usdt"] - 1.0).values
    # trailing sd, 30d lookback, strictly past bars only — no lookahead
    s = pd.Series(spread)
    sd = s.rolling(24 * 30).std().shift(1).values

    # funding is paid every 8h the pair is open; a 12h hold crosses 1-2 settlements
    fund_cost = FUNDING_DIFF_PER_8H * (hold_h / 8.0)

    rets, dates = [], []
    i = 24 * 30
    while i + hold_h < len(d):
        if not np.isnan(sd[i]) and sd[i] > 0 and abs(spread[i]) > entry_sd * sd[i]:
            # fade: profit if the spread shrinks toward zero
            pnl = -np.sign(spread[i]) * (spread[i + hold_h] - spread[i])
            rets.append(pnl - cost - fund_cost)
            dates.append(d.index[i])
            i += hold_h                      # non-overlapping once we trade
        else:
            i += 1

    r = np.array(rets)
    if len(r) < 5:
        return {"n": len(r)}
    se = r.std(ddof=1) / np.sqrt(len(r))
    ser = pd.Series(r, index=pd.to_datetime(dates))
    bymo = ser.groupby(ser.index.to_period("M")).sum()
    return {"n": len(r), "mean_bp": r.mean() * 1e4, "t": r.mean() / se if se else np.nan,
            "win": (r > 0).mean(), "total_bp": r.sum() * 1e4,
            "top_month_share": bymo.max() / ser.sum() if ser.sum() != 0 else np.nan}


def fmt(tag: str, d: dict) -> str:
    if d.get("n", 0) < 5:
        return f"  {tag:30s} n={d.get('n', 0):4d}  (too few)"
    return (f"  {tag:30s} n={d['n']:4d} mean={d['mean_bp']:+7.2f}bp t={d['t']:6.2f} "
            f"win={d['win']*100:5.1f}% tot={d['total_bp']:+9.1f}bp "
            f"topmo={d['top_month_share']*100:5.1f}%")


if __name__ == "__main__":
    pairs = load_pairs()
    print(f"pairs with both books: {sorted(pairs)}")
    print(f"maker floor  BOTH PASSIVE : {COST_MAKER_BOTH*1e4:5.1f}bp   "
          f"(was wrongly charged 8.0bp)")
    print(f"             MAKER IN/TAKER OUT: {COST_MAKER_IN_TAKER_OUT*1e4:5.1f}bp")
    print(f"             BOTH TAKER   : {COST_TAKER_BOTH*1e4:5.1f}bp")
    print()

    cells = []
    for cost_name, cost in (("maker-both", COST_MAKER_BOTH),
                            ("maker-in/taker-out", COST_MAKER_IN_TAKER_OUT)):
        print(f"=== cost model: {cost_name} ({cost*1e4:.0f}bp) ===")
        for sym, df in sorted(pairs.items()):
            for h in (1, 4, 12, 24):
                for sd_thr in (2.0, 3.0):
                    a = run(df, h, sd_thr, cost, end=SPLIT)
                    b = run(df, h, sd_thr, cost, start=SPLIT)
                    if a.get("n", 0) < 5 or b.get("n", 0) < 5:
                        continue
                    cells.append((cost_name, sym, h, sd_thr, a, b))
        pos = [c for c in cells if c[0] == cost_name
               and c[4]["mean_bp"] > 0 and c[5]["mean_bp"] > 0]
        tot = [c for c in cells if c[0] == cost_name]
        print(f"  cells={len(tot)}  both-eras-positive={len(pos)}")
        for c in sorted(pos, key=lambda x: -min(x[4]["mean_bp"], x[5]["mean_bp"]))[:8]:
            print(f"    {c[1]:5s} H={c[2]:2d}h {c[3]:.0f}sd  "
                  f"EARLY {c[4]['mean_bp']:+6.2f}bp t={c[4]['t']:5.2f} n={c[4]['n']:4d}  |  "
                  f"LATE {c[5]['mean_bp']:+6.2f}bp t={c[5]['t']:5.2f} n={c[5]['n']:4d}")
        print()

    # honest max-of-N reporting
    both_pos = [c for c in cells if c[4]["mean_bp"] > 0 and c[5]["mean_bp"] > 0]
    print(f"cells tested: {len(cells)}   both-eras-positive: {len(both_pos)}")
    if both_pos:
        print("  (a max over this many cells is a grid maximum until proven otherwise)")
        for c in sorted(both_pos, key=lambda x: -min(x[4]["mean_bp"], x[5]["mean_bp"]))[:10]:
            print(f"    {c[0]:20s} {c[1]:6s} H={c[2]:2d}h {c[3]:.0f}sd  "
                  f"EARLY {c[4]['mean_bp']:+6.2f}bp (t={c[4]['t']:5.2f})  "
                  f"LATE {c[5]['mean_bp']:+6.2f}bp (t={c[5]['t']:5.2f})")
