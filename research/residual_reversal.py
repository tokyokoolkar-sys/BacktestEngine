"""residual_reversal.py — does an idiosyncratic overshoot mean-revert?

The claim (ranked #1 by two independent fleet agents): when one symbol moves hard
relative to the rest of the universe, that excess is partly forced flow — margin
calls, not opinion — and it partly reverses.

Guards built in from the start, all three earned by the two families this session
already buried (see FINDINGS.md §7):

  * beta removed FIRST. The signal IS the residual; a raw-return version of this
    test would just re-measure the alt market, which is what killed momentum.
  * era split reported before any aggregate.
  * non-overlapping windows; costs on every leg of both sides.

Two legs means 4 fills per round trip. At $200 with $5 min notional that is
affordable; at $20 it is the whole account in one paired bet.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from xs_momentum import load_panel, FEE, SLIP

SPLIT = "2025-01-01"


def run(panel, form_h: int, hold_h: int, k: int, start=None, end=None,
        side: str = "both"):
    """Short the top-k residual gainers, buy the bottom-k. Market-neutral by build."""
    p = panel
    if start is not None:
        p = p[p.index >= start]
    if end is not None:
        p = p[p.index < end]
    if len(p) < form_h + hold_h + 2:
        return {"n": 0}

    rets, dates = [], []
    i = form_h
    while i + hold_h < len(p):
        past, now, fut = p.iloc[i - form_h], p.iloc[i], p.iloc[i + hold_h]
        m = past.notna() & now.notna() & fut.notna()
        if m.sum() >= 8:
            form = (now[m] / past[m] - 1.0)
            resid = form - form.mean()                     # beta out, by construction
            fwd = (fut[m] / now[m] - 1.0)
            fwd_resid = fwd - fwd.mean()                   # score residual too
            order = resid.sort_values()
            lo, hi = order.head(k).index, order.tail(k).index
            if side == "both":
                r = fwd_resid[lo].mean() - fwd_resid[hi].mean()
                cost = 4 * (FEE + SLIP)
            elif side == "long":
                r = fwd_resid[lo].mean(); cost = 2 * (FEE + SLIP)
            else:
                r = -fwd_resid[hi].mean(); cost = 2 * (FEE + SLIP)
            rets.append(r - cost)
            dates.append(p.index[i])
        i += hold_h                                        # no overlap

    a = np.array(rets)
    if len(a) < 5:
        return {"n": len(a)}
    se = a.std(ddof=1) / np.sqrt(len(a))
    s = pd.Series(a, index=pd.to_datetime(dates))
    bymo = s.groupby(s.index.to_period("M")).sum()
    return {"n": len(a), "mean": a.mean(), "t": a.mean() / se if se else np.nan,
            "win": (a > 0).mean(), "total": a.sum(),
            "topmo": bymo.max() / s.sum() if s.sum() else np.nan}


def line(tag, d):
    if d.get("n", 0) < 5:
        return f"  {tag:30s} n<5"
    return (f"  {tag:30s} n={d['n']:4d} mean={d['mean']*100:+7.3f}% t={d['t']:6.2f} "
            f"win={d['win']*100:5.1f}% tot={d['total']*100:+8.1f}% topmo={d['topmo']*100:5.1f}%")


if __name__ == "__main__":
    panel = load_panel()
    print(f"panel {panel.shape[1]} symbols; residual = return minus cross-sectional mean")
    print(f"costs {(FEE+SLIP)*1e4:.0f}bp/side, 4 fills per paired round trip")
    print()
    for f in (6, 12, 24, 48):
        for h in (6, 12, 24, 48):
            for k in (3,):
                e = run(panel, f, h, k, end=SPLIT)
                l = run(panel, f, h, k, start=SPLIT)
                if e.get("n", 0) < 5 or l.get("n", 0) < 5:
                    continue
                print(line(f"form={f}h hold={h}h EARLY", e))
                print(line(f"form={f}h hold={h}h LATE ", l))
                print()
