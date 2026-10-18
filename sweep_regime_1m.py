"""
REGIME SPLIT: is the short edge real, or just 'alts went down'?

Full-history data now covers 2021 bull / 2022 bear / 2023-24 recovery+bull /
2025-26 down. We label each hour by BTC's 30-day trend and bucket every trade's
PnL by the regime it OPENED in:

  bull  : BTC 30d return > +10%
  bear  : BTC 30d return < -10%
  chop  : in between

Verdict logic:
  - short profits in BEAR only, bleeds in BULL  -> directional beta, NOT edge.
    (you're just short in a downtrend; live-forward in a rally it dies.)
  - short profits in BULL and BEAR both         -> real structural edge
    (maker rebate + entry timing survive regardless of direction).
  Same read applied to long.

Honest 1m fills, maker_paid fee (realistic base tier), 3x leverage.
Per-symbol SYM-funding.parquet (merged funding.parquet collapses symbols).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import DCAConfig, StrategyConfig
from data import load_klines, load_funding
from engine import (_funding_in_window, _dca_cumulative_drops,
                    _dca_safety_notionals, _dca_base_notional)
from strategy import CheckpointStrategy

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
           "NEARUSDT", "INJUSDT", "TIAUSDT", "OPUSDT", "APTUSDT", "ARBUSDT",
           "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT",
           "BCHUSDT", "ETCUSDT", "ATOMUSDT", "UNIUSDT", "FILUSDT",
           "AAVEUSDT", "TRXUSDT", "SUIUSDT"]

LEV = 3
MAKER = 0.0002       # base-tier maker fee PAID
TAKER = 0.0005       # for forced liq exit
MAINT = 0.05
MARGIN = 2.5
SLIP = 2.0 / 10_000.0
TPS = [0.3, 0.5, 0.8, 1.0]
_H_NS = 3_600_000_000_000

BULL_THR = 0.10
BEAR_THR = -0.10


def resample_1h(k):
    df = k.set_index("open_time")
    return pd.DataFrame({
        "open_time": df["open"].resample("1h").first().index,
        "open": df["open"].resample("1h").first().values,
        "high": df["high"].resample("1h").max().values,
        "low": df["low"].resample("1h").min().values,
        "close": df["close"].resample("1h").last().values,
    }).dropna()


def btc_regime_map(btc_1h):
    """Return dict: hour_ns -> regime label, from BTC 30d (720h) return."""
    c = btc_1h["close"].to_numpy(float)
    t = btc_1h["open_time"].to_numpy("int64")
    reg = np.empty(len(c), dtype=object)
    win = 720
    for i in range(len(c)):
        if i < win:
            reg[i] = "chop"
            continue
        ret = c[i] / c[i - win] - 1.0
        reg[i] = "bull" if ret > BULL_THR else ("bear" if ret < BEAR_THR else "chop")
    return dict(zip(t, reg))


def sim(symbol, k1, k1h, funding, tp, short, regime_of):
    dca = DCAConfig(price_drop_step_pct=0.3, step_multiplier=1.0,
                    size_multiplier=1.1, max_dca_orders=3,
                    first_order_ratio=0.96, take_profit_pct=tp)
    cum_drops = _dca_cumulative_drops(dca)
    liq_mult = (1.0 + 1.0 / LEV - MAINT) if short else (1.0 - 1.0 / LEV + MAINT)

    m_high = k1["high"].to_numpy(float); m_low = k1["low"].to_numpy(float)
    m_t = k1["open_time"].to_numpy("int64")
    ho = k1h["open"].to_numpy(float); hh = k1h["high"].to_numpy(float)
    hl = k1h["low"].to_numpy(float); hc = k1h["close"].to_numpy(float)
    ht = k1h["open_time"].to_numpy("int64")
    ft = funding["funding_time"].to_numpy("int64")
    fr = funding["funding_rate"].to_numpy(float)

    strat = CheckpointStrategy(StrategyConfig(
        entry_mode="red_dip", green_gate="off", checkpoint_mode="first_green",
        position_model="dca", direction=("short" if short else "long"), stop_mode="none"))

    in_pos = False
    avg = size = notional = fees = 0.0
    entry_ns = 0; entry_regime = "chop"
    saf_notionals = []; saf_trigs = []; next_saf = 0
    tp_price = liq = 0.0
    pending = False
    # per-regime accumulators: [net, ntrades, nwins, nliq]
    acc = {r: [0.0, 0, 0, 0] for r in ("bull", "bear", "chop")}

    def levels():
        nonlocal tp_price, liq
        tp_price = avg * (1.0 - tp / 100.0) if short else avg * (1.0 + tp / 100.0)
        liq = avg * liq_mult

    def walk(t0):
        nonlocal in_pos, avg, size, notional, fees, next_saf
        a = np.searchsorted(m_t, t0, "left"); b = np.searchsorted(m_t, t0 + _H_NS, "left")
        for j in range(a, b):
            bl, bh = m_low[j], m_high[j]
            while next_saf < len(saf_trigs):
                trig = saf_trigs[next_saf]
                if not (bh >= trig if short else bl <= trig):
                    break
                sn = saf_notionals[next_saf]; ss = sn / trig
                avg = (avg * size + trig * ss) / (size + ss)
                size += ss; notional += sn; fees += sn * MAKER
                next_saf += 1; levels()
            if (bh >= liq if short else bl <= liq):
                fill = liq * (1 + SLIP) if short else liq * (1 - SLIP)
                gross = (avg - fill if short else fill - avg) * size
                ef = notional * TAKER
                rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                fund = (-rate if short else rate) * notional
                net = max(-notional / LEV, gross - (fees + ef) - fund)
                a2 = acc[entry_regime]; a2[0] += net; a2[1] += 1; a2[3] += 1
                in_pos = False; return True
            if (bl <= tp_price if short else bh >= tp_price):
                gross = (avg - tp_price if short else tp_price - avg) * size
                ef = notional * MAKER
                rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                fund = (-rate if short else rate) * notional
                net = gross - (fees + ef) - fund
                a2 = acc[entry_regime]; a2[0] += net; a2[1] += 1
                if net > 0: a2[2] += 1
                in_pos = False; return True
        return False

    for i in range(1, len(k1h)):
        if in_pos:
            walk(ht[i])
        if pending and not in_pos:
            bn = _dca_base_notional(MARGIN, LEV, dca)
            fill0 = ho[i]
            avg = fill0; size = bn / fill0; notional = bn; fees = bn * MAKER
            saf_notionals = _dca_safety_notionals(dca, bn)
            saf_trigs = ([fill0 * (1.0 + d / 100.0) for d in cum_drops] if short
                         else [fill0 * (1.0 - d / 100.0) for d in cum_drops])
            next_saf = 0; entry_ns = ht[i]
            entry_regime = regime_of.get(ht[i], "chop")
            in_pos = True; pending = False
            levels(); walk(ht[i])
        prev = {"high": hh[i - 1]}
        row = {"open": ho[i], "close": hc[i], "high": hh[i], "low": hl[i], "t": ht[i]}
        if strat.on_closed_bar(prev, row, in_pos) == "enter" and not in_pos:
            pending = True

    return acc


def main():
    # BTC regime map first
    print("Loading BTC for regime map...")
    btc = resample_1h(load_klines(str(_HERE / "cache" / "BTCUSDT" / "klines.parquet")))
    regime_of = btc_regime_map(btc)

    rows = []
    for s in SYMBOLS:
        kp = _HERE / "cache" / s / "klines.parquet"
        if not kp.exists():
            print(f"SKIP {s}"); continue
        fp = _HERE / "cache" / f"{s}-funding.parquet"
        try:
            funding = load_funding(str(fp)) if fp.exists() else pd.DataFrame(columns=["funding_time", "funding_rate"])
        except Exception:
            funding = pd.DataFrame(columns=["funding_time", "funding_rate"])
        print(f"Running {s}...")
        k1 = load_klines(str(kp)); k1h = resample_1h(k1)
        for short in (False, True):
            # pick best TP per (symbol,dir) by total net across regimes
            best = None
            for tp in TPS:
                acc = sim(s, k1, k1h, funding, tp, short, regime_of)
                tot = sum(v[0] for v in acc.values())
                if best is None or tot > best[0]:
                    best = (tot, tp, acc)
            _, tp, acc = best
            for reg, (net, nt, nw, nl) in acc.items():
                rows.append({"symbol": s, "dir": "short" if short else "long",
                             "tp_pct": tp, "regime": reg, "net": net,
                             "trades": nt, "wins": nw, "liqs": nl})

    df = pd.DataFrame(rows)
    df.to_parquet(str(_HERE / "sweep_regime_1m_results.parquet"))

    print("\n" + "=" * 78)
    print("REGIME SPLIT -- honest 1m, maker_paid, 3x. net $ by direction x regime")
    print("=" * 78)
    piv = df.groupby(["dir", "regime"]).agg(
        net=("net", "sum"), trades=("trades", "sum"),
        wins=("wins", "sum"), liqs=("liqs", "sum")).reset_index()
    piv["wr%"] = (piv["wins"] / piv["trades"] * 100).round(1)
    piv["liq%"] = (piv["liqs"] / piv["trades"] * 100).round(2)
    piv["exp"] = (piv["net"] / piv["trades"]).round(4)
    order = {"bull": 0, "chop": 1, "bear": 2}
    piv = piv.sort_values(["dir", "regime"], key=lambda c: c.map(order) if c.name == "regime" else c)
    print(piv.to_string(index=False))

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    for d in ("long", "short"):
        sub = piv[piv["dir"] == d].set_index("regime")
        bull = sub.loc["bull", "net"] if "bull" in sub.index else 0
        bear = sub.loc["bear", "net"] if "bear" in sub.index else 0
        chop = sub.loc["chop", "net"] if "chop" in sub.index else 0
        pos_regimes = sum(1 for x in (bull, bear, chop) if x > 0)
        if pos_regimes == 3:
            tag = "REAL EDGE (positive in ALL regimes)"
        elif pos_regimes == 0:
            tag = "DEAD (negative everywhere)"
        elif (d == "short" and bear > 0 and bull <= 0) or (d == "long" and bull > 0 and bear <= 0):
            tag = "DIRECTIONAL BETA (only wins with the trend) -- NOT an edge"
        else:
            tag = f"MIXED ({pos_regimes}/3 regimes positive)"
        print(f"  {d:6s}: bull={bull:+.1f} chop={chop:+.1f} bear={bear:+.1f}  -> {tag}")


if __name__ == "__main__":
    main()
