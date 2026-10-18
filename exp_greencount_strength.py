"""
exp_greencount_strength.py — does GreenCount *strength* carry signal?

Question (user's): with N=10 auto-side, should a moderate green/red imbalance
trend-follow while an EXTREME imbalance (9:1, 10:0) skip or reverse?

Method — faithful to the LIVE bot (flip_20_config.yaml):
  * 6 live symbols, May+June 2026 1m klines resampled to 1h.
  * Auto-side window EXACTLY as bot.py _check_signal: at fill bar `at`, the signal
    bar is `at-1`; the decision window is the 10 closed bars ending at the signal
    bar => h.iloc[at-10 : at]. greens>reds -> long, reds>greens -> short, tie skip.
  * Every hourly decision is generated directly from klines. After a rule picks
    its side, entry requires the same adverse signal candle as live: red for long,
    green for short. This matters for contrarian rules — reversing the side also
    changes whether the checkpoint allows entry.
  * Each accepted signal enters at the next 1h open and replays one live-shaped
    basket: ATR-0.75 ladder, DCA=3, 10x, symbol TP, 5% stop, taker base/exit,
    maker safety fees, and 2bps market slippage.

Rules compared:
  follow_all      take majority every time (current live behavior)
  skip_ge9        follow majority for strength<=8, skip 9:1 and 10:0
  skip_ge8        follow majority for strength<=7, skip 8:2/9:1/10:0
  reverse_ge9     follow majority for strength<=8, REVERSE 9:1 and 10:0
  reverse_ge8     follow majority for strength<=7, REVERSE 8:2/9:1/10:0
  only_moderate   trade only 6:4 and 7:3 (skip 8:2/9:1/10:0)

Two views:
  A. INDEPENDENT per-candidate return-on-margin (no capital coupling) — pure signal.
  B. PORTFOLIO: $87 start, 30% of free equity per basket, K=3 concurrent, one basket
     per symbol at a time — the live capital reality.

    python exp_greencount_strength.py
"""
from __future__ import annotations
import pickle, math, sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).parent
CACHE = HERE / "cache"
SYMS = ["NEARUSDT", "TIAUSDT", "OPUSDT", "FILUSDT", "UNIUSDT", "SUIUSDT"]
TP = {"NEARUSDT": 0.8, "TIAUSDT": 0.5, "OPUSDT": 0.5, "FILUSDT": 0.5, "UNIUSDT": 0.5, "SUIUSDT": 0.5}
LEV, STOP, TAKER, MAKER, SLIP = 10.0, 5.0, 0.0005, 0.0002, 0.0002
R0, MULT, NDCA = 0.96, 1.1, 3
ATR_MULT, ATR_MIN, ATR_MAX = 0.75, 0.2, 1.0
TR = 1 + sum(R0 * MULT ** k for k in range(NDCA))
BASE_N = LEV / TR
SAF_N = [BASE_N * R0 * MULT ** k for k in range(NDCA)]
START, RISK, K = 87.0, 0.30, 3
HOUR_NS = 3_600_000_000_000


def load():
    D = {}
    for s in SYMS:
        ps = []
        for m in ("may", "june"):
            if m == "may":
                p = CACHE / "oos_may" / f"{s}-1m-klines.parquet"
            elif s in ("NEARUSDT", "TIAUSDT", "SUIUSDT"):
                p = CACHE / s / "klines.parquet"
            else:
                p = CACHE / "oos_june" / f"{s}-1m-klines.parquet"
            ps.append(pd.read_parquet(p))
        d = pd.concat(ps).sort_values("open_time").drop_duplicates("open_time")
        d["open_time"] = pd.to_datetime(d["open_time"], utc=True)
        h = (d.set_index("open_time").resample("1h")
             .agg(open=("open", "first"), high=("high", "max"),
                  low=("low", "min"), close=("close", "last"))
             .dropna().reset_index())
        t = h["open_time"].dt.tz_localize(None).to_numpy("datetime64[ns]").astype("int64")
        D[s] = dict(t=t, o=h["open"].to_numpy(float), h=h["high"].to_numpy(float),
                    l=h["low"].to_numpy(float), c=h["close"].to_numpy(float),
                    month=h["open_time"].dt.month.to_numpy())
    return D


def atr_step(d, di):
    a = max(0, di - 18)
    hi, lo, cl = d["h"][a:di + 1], d["l"][a:di + 1], d["c"][a:di + 1]
    if len(cl) < 15:
        return 0.3
    trs = [hi[0] - lo[0]]
    for j in range(1, len(cl)):
        trs.append(max(hi[j] - lo[j], abs(hi[j] - cl[j - 1]), abs(lo[j] - cl[j - 1])))
    atr = sum(trs[:14]) / 14
    for x in trs[14:]:
        atr = (atr * 13 + x) / 14
    return min(max(ATR_MULT * (atr / cl[-1]) * 100, ATR_MIN), ATR_MAX)


def basket(sym, d, di, ei, side):
    """One faithful basket. di=decision/ATR bar, ei=fill bar. Returns (net_on_margin, exit_ns, reason, n_safety)."""
    t, o, h, l, c = d["t"], d["o"], d["h"], d["l"], d["c"]
    short = side == "short"
    entry = o[ei] * (1 - SLIP if short else 1 + SLIP)
    avg, size, noti, fee = entry, BASE_N / entry, BASE_N, BASE_N * TAKER
    step = atr_step(d, di)
    trig, x = [], 0.0
    for _ in range(NDCA):
        x += step
        trig.append(entry * (1 + x / 100 if short else 1 - x / 100))
    k = 0
    for j in range(ei, len(t)):
        while k < NDCA and (h[j] >= trig[k] if short else l[j] <= trig[k]):
            px = trig[k]
            q = SAF_N[k] / px
            avg = (avg * size + px * q) / (size + q)
            size += q; noti += SAF_N[k]; fee += SAF_N[k] * MAKER; k += 1
        stop = avg * (1 + STOP / 100 if short else 1 - STOP / 100)
        tp = avg * (1 - TP[sym] / 100 if short else 1 + TP[sym] / 100)
        if (h[j] >= stop if short else l[j] <= stop):
            fill = stop * (1 + SLIP if short else 1 - SLIP); reason = "stop"
        elif (l[j] <= tp if short else h[j] >= tp):
            fill = tp * (1 + SLIP if short else 1 - SLIP); reason = "take_profit"
        else:
            continue
        gross = ((avg - fill) if short else (fill - avg)) * size
        net = gross - fee - noti * TAKER
        return net, int(t[j]), reason, k
    fill = c[-1] * (1 + SLIP if short else 1 - SLIP)
    gross = ((avg - fill) if short else (fill - avg)) * size
    return gross - fee - noti * TAKER, int(t[-1]), "end", k


def candidates(D):
    """Every no-lookahead hourly decision. Rule-specific checkpoint gating happens
    after side selection: red signal bar for long, green for short."""
    out = []
    for s in SYMS:
        d = D[s]
        # di = just-closed signal bar; at = next-bar entry open.
        for di in range(9, len(d["t"]) - 1):
            at = di + 1
            w_o = d["o"][di - 9:di + 1]
            w_c = d["c"][di - 9:di + 1]
            g = int((w_c > w_o).sum()); r = 10 - g
            out.append(dict(sym=s, at=at, di=di, g=g, r=r,
                            signal_green=bool(d["c"][di] > d["o"][di]),
                            strength=max(g, r), month=int(d["month"][at])))
    return out


def accepts_checkpoint(cand, side):
    """Live red_dip condition after auto-side picks direction."""
    return (not cand["signal_green"]) if side == "long" else cand["signal_green"]


def side_for(cand, rule):
    g, r, st = cand["g"], cand["r"], cand["strength"]
    if g == r:
        return None
    maj = "long" if g > r else "short"
    opp = "short" if maj == "long" else "long"
    if rule == "follow_all":
        return maj
    if rule == "skip_ge9":
        return None if st >= 9 else maj
    if rule == "skip_ge8":
        return None if st >= 8 else maj
    if rule == "reverse_ge9":
        return opp if st >= 9 else maj
    if rule == "reverse_ge8":
        return opp if st >= 8 else maj
    if rule == "only_moderate":
        return maj if st <= 7 else None
    raise KeyError(rule)


RULES = ["follow_all", "skip_ge9", "skip_ge8", "reverse_ge9", "reverse_ge8", "only_moderate"]


def independent(D, cands):
    print("=" * 78)
    print("VIEW A — INDEPENDENT signal (return-on-margin per candidate, no capital cap)")
    print("=" * 78)
    # bucket table first
    print("\nFOLLOW-MAJORITY BY STRENGTH BUCKET")
    print(f"{'period':<6} {'ratio':<6} {'n':>5} {'WR%':>6} {'stop%':>6} {'sumR':>9} {'avgR':>9} {'PF':>7}")
    cache = {}
    def get(c, side):
        key = (c["sym"], c["at"], side)
        if key not in cache:
            cache[key] = basket(c["sym"], D[c["sym"]], c["di"], c["at"], side)
        return cache[key]
    for period in ("may", "june", "all"):
        for st in range(6, 11):
            xs = [c for c in cands if c["strength"] == st and c["g"] != c["r"] and (period == "all" or c["month"] == (5 if period == "may" else 6))]
            ps = []
            stops = 0
            for c in xs:
                side = "long" if c["g"] > c["r"] else "short"
                if not accepts_checkpoint(c, side):
                    continue
                net, _, reason, _ = get(c, side)
                ps.append(net); stops += reason == "stop"
            n = len(ps); gp = sum(x for x in ps if x > 0); gl = -sum(x for x in ps if x <= 0)
            print(f"{period:<6} {st}:{10-st:<4} {n:>5} {100*sum(x>0 for x in ps)/n if n else 0:>6.1f} "
                  f"{100*stops/n if n else 0:>6.1f} {sum(ps):>9.3f} {np.mean(ps) if ps else 0:>9.4f} "
                  f"{gp/gl if gl else math.inf:>7.2f}")
        print()
    # rule comparison
    print("RULE COMPARISON (independent; skipped candidates simply not taken)")
    print(f"{'period':<6} {'rule':<14} {'n':>5} {'WR%':>6} {'stop%':>6} {'sumR':>9} {'avgR':>9} {'PF':>7}")
    for period in ("may", "june", "all"):
        for rule in RULES:
            ps, stops = [], 0
            for c in cands:
                if period != "all" and c["month"] != (5 if period == "may" else 6):
                    continue
                side = side_for(c, rule)
                if side is None or not accepts_checkpoint(c, side):
                    continue
                net, _, reason, _ = get(c, side)
                ps.append(net); stops += reason == "stop"
            n = len(ps); gp = sum(x for x in ps if x > 0); gl = -sum(x for x in ps if x <= 0)
            print(f"{period:<6} {rule:<14} {n:>5} {100*sum(x>0 for x in ps)/n if n else 0:>6.1f} "
                  f"{100*stops/n if n else 0:>6.1f} {sum(ps):>9.3f} {np.mean(ps) if ps else 0:>9.4f} "
                  f"{gp/gl if gl else math.inf:>7.2f}")
        print()
    return cache


def portfolio(D, cands, cache):
    print("=" * 78)
    print("VIEW B — PORTFOLIO ($87 start, 30% free equity/basket, K=3, 1/symbol)")
    print("=" * 78)
    print(f"{'rule':<14} {'n':>5} {'WR%':>6} {'stop%':>6} {'net$':>9} {'final$':>9} {'PF':>7} {'maxDD%':>7}")
    order = {s: i for i, s in enumerate(SYMS)}
    def get(c, side):
        key = (c["sym"], c["at"], side)
        if key not in cache:
            cache[key] = basket(c["sym"], D[c["sym"]], c["di"], c["at"], side)
        return cache[key]
    for rule in RULES:
        events = []
        for c in cands:
            side = side_for(c, rule)
            if side is None or not accepts_checkpoint(c, side):
                continue
            net, exit_ns, reason, nsaf = get(c, side)
            events.append(dict(entry_ns=int(D[c["sym"]]["t"][c["at"]]), exit_ns=exit_ns,
                               r=net, sym=c["sym"], reason=reason))
        bytime = {}
        for e in events:
            bytime.setdefault(e["entry_ns"], []).append(e)
        equity = free = START; peak = equity; maxdd = 0.0; active = []; closed = []
        def close_due(t):
            nonlocal equity, free, peak, maxdd, active
            due = sorted([x for x in active if x["exit_ns"] <= t], key=lambda x: x["exit_ns"])
            active = [x for x in active if x["exit_ns"] > t]
            for x in due:
                pnl = x["r"] * x["margin"]; equity += pnl; free += x["margin"] + pnl
                closed.append(dict(x, dollar=pnl))
                peak = max(peak, equity); maxdd = max(maxdd, (peak - equity) / peak if peak else 0)
        for tt in sorted(bytime):
            close_due(tt)
            for e in sorted(bytime[tt], key=lambda x: order[x["sym"]]):
                if len(active) >= K or any(a["sym"] == e["sym"] for a in active):
                    continue
                margin = free * RISK
                if margin <= 0:
                    continue
                free -= margin; active.append(dict(e, margin=margin))
        close_due(10 ** 30)
        vals = [x["dollar"] for x in closed]
        gp = sum(x for x in vals if x > 0); gl = -sum(x for x in vals if x <= 0)
        n = len(vals)
        print(f"{rule:<14} {n:>5} {100*sum(x>0 for x in vals)/n if n else 0:>6.1f} "
              f"{100*sum(x['reason']=='stop' for x in closed)/n if n else 0:>6.1f} "
              f"{sum(vals):>9.2f} {equity:>9.2f} {gp/gl if gl else math.inf:>7.2f} {100*maxdd:>7.1f}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    D = load()
    cands = candidates(D)
    dist = {}
    for c in cands:
        dist[c["strength"]] = dist.get(c["strength"], 0) + 1
    print(f"candidates: {len(cands)}  strength dist {dict(sorted(dist.items()))}\n")
    cache = independent(D, cands)
    print()
    portfolio(D, cands, cache)


if __name__ == "__main__":
    main()
