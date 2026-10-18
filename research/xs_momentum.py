"""xs_momentum.py — cross-sectional momentum on the perp universe.

WHY THIS SHAPE
--------------
Carry died because it lived in one regime (2024 mania) and I only found out by
splitting eras AFTER seeing a good number. So this test bakes in every guard up
front, before any result is visible:

  * era split is the FIRST output, not a post-hoc check
  * windows are NON-OVERLAPPING by construction (rebalance == hold)
  * costs charged on both legs of every rebalance, at taker rates
  * the top month's share of P&L is reported always, pass or fail

The mechanism claimed: slow capital rotates between alts over days, so a name
that outperformed its peers over the last N days keeps outperforming for the
next H days. Payer is whoever rebalances late.

KNOWN LIMITATION, stated rather than hidden: this universe is the 22 symbols that
are ALIVE TODAY. Dead and delisted contracts are absent, so results are
survivorship-biased UPWARD. A positive result here is a necessary condition, not
a sufficient one. A negative result is decisive — survivorship bias only helps,
so failing with the bias still in means it fails harder without it.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path(__file__).parent.parent / "cache" / "hist"
FEE = 0.0005          # taker per side
SLIP = 0.0005         # modeled slippage per side on a small-cap perp


def load_panel() -> pd.DataFrame:
    """Wide close-price panel, hourly, one column per symbol."""
    cols = {}
    for f in sorted(glob.glob(str(CACHE / "*-1h-klines.parquet"))):
        sym = os.path.basename(f).replace("-1h-klines.parquet", "")
        df = pd.read_parquet(f)[["open_time", "close"]].set_index("open_time")
        cols[sym] = df["close"]
    panel = pd.DataFrame(cols).sort_index()
    return panel[~panel.index.duplicated()]


def run(panel: pd.DataFrame, lookback_d: int, hold_d: int, n_long: int,
        start=None, end=None) -> dict:
    """Rank by trailing return, hold the top n_long, rebalance every hold_d days.

    Rebalance period == hold period, so consecutive observations never share a
    bar. This is what makes n honest.
    """
    p = panel
    if start is not None:
        p = p[p.index >= start]
    if end is not None:
        p = p[p.index < end]

    lb, h = lookback_d * 24, hold_d * 24
    if len(p) < lb + h:
        return {"n": 0}

    rets, dates = [], []
    i = lb
    while i + h < len(p):
        past = p.iloc[i - lb] ; now = p.iloc[i] ; fut = p.iloc[i + h]
        sig = (now / past - 1.0).dropna()
        # a name must be priced at all three points to be tradable
        sig = sig[now.notna() & fut.notna()]
        if len(sig) >= n_long * 2:
            picks = sig.sort_values(ascending=False).head(n_long).index
            fwd = (fut[picks] / now[picks] - 1.0).mean()
            rets.append(fwd - 2 * (FEE + SLIP))     # in and out
            dates.append(p.index[i])
        i += h                                       # <-- no overlap

    r = np.array(rets)
    if len(r) == 0:
        return {"n": 0}
    se = r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else np.nan
    s = pd.Series(r, index=pd.to_datetime(dates))
    bymo = s.groupby(s.index.to_period("M")).sum()
    top_share = bymo.max() / s.sum() if s.sum() != 0 else np.nan
    return {"n": len(r), "mean": r.mean(), "sd": r.std(), "se": se,
            "t": r.mean() / se if se else np.nan, "win": (r > 0).mean(),
            "total": r.sum(), "top_month_share": top_share}


def fmt(tag: str, d: dict) -> str:
    if d.get("n", 0) == 0:
        return f"  {tag:34s} NO DATA"
    return (f"  {tag:34s} n={d['n']:4d} mean={d['mean']*100:+7.3f}% "
            f"t={d['t']:6.2f} win={d['win']*100:5.1f}% "
            f"tot={d['total']*100:+8.2f}% topmo={d['top_month_share']*100:5.1f}%")


if __name__ == "__main__":
    panel = load_panel()
    print(f"panel: {panel.shape[1]} symbols  {panel.index.min()} .. {panel.index.max()}")
    print("costs: 10bp/side round trip both legs  |  windows non-overlapping")
    print()
    SPLIT = "2025-01-01"
    for lb in (3, 7, 14, 30):
        for h in (3, 7, 14):
            for n in (3, 5):
                a = run(panel, lb, h, n, end=SPLIT)
                b = run(panel, lb, h, n, start=SPLIT)
                if a.get("n", 0) < 5 or b.get("n", 0) < 5:
                    continue
                print(fmt(f"lb={lb}d h={h}d top{n} EARLY", a))
                print(fmt(f"lb={lb}d h={h}d top{n} LATE ", b))
                print()
