"""
The real question: 1h/1h says the red-dip DCA edge is real (TP cashflow +),
but honest 1m-fill flips it negative because at 10x leverage liquidation sits
only -5% from avg entry, and the DCA ladder (-0.9% span) can't defend it. A -5%
intrabar wick = dead basket, and 1m sees those wicks that 1h close hides.

Lever to test: LEVERAGE. Lower leverage pushes liq far from the wick zone:
  liq (long) = entry * (1 - 1/lev + maint) ;  maint=0.05
  10x -> -5.0%   |  5x -> -15.0%  |  3x -> -28.3%  |  2x -> -45%
Fewer wicks reach it -> fewer liquidations -> the TP cashflow can survive.

Note: lower leverage also needs MORE margin per basket for the same notional,
OR you keep margin fixed and trade smaller notional (fewer $ per win). This
tests EXPECTANCY PER TRADE (edge quality), holding margin_usdt fixed so wins
shrink proportionally -- so watch expectancy sign, not absolute $.

Honest 1m fills, long only (short mirrors), best-TP per symbol.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import (_slip_entry, _slip_exit, _funding_in_window,
                    _dca_cumulative_drops, _dca_safety_notionals, _dca_base_notional)
from strategy import CheckpointStrategy

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
           "NEARUSDT", "INJUSDT", "TIAUSDT", "OPUSDT", "APTUSDT", "ARBUSDT"]

LEVS = [10, 5, 3, 2]
TPS = [0.3, 0.5, 0.8, 1.0]
_H_NS = 3_600_000_000_000


def resample_1h(k):
    df = k.set_index("open_time")
    return pd.DataFrame({
        "open_time": df["open"].resample("1h").first().index,
        "open": df["open"].resample("1h").first().values,
        "high": df["high"].resample("1h").max().values,
        "low": df["low"].resample("1h").min().values,
        "close": df["close"].resample("1h").last().values,
        "volume": df["volume"].resample("1h").sum().values,
    }).dropna()


def sim(symbol, k1, k1h, funding, lev, tp):
    e = ExecConfig(leverage=lev, margin_usdt=2.5, taker_fee=0.0005,
                   maker_fee=0.0002, slippage_bps=2.0, maint_margin_rate=0.05)
    slip = e.slippage_bps / 10_000.0
    dca = DCAConfig(price_drop_step_pct=0.3, step_multiplier=1.0,
                    size_multiplier=1.1, max_dca_orders=3,
                    first_order_ratio=0.96, take_profit_pct=tp)
    cum_drops = _dca_cumulative_drops(dca)
    liq_mult = 1.0 - 1.0 / lev + e.maint_margin_rate

    m_high = k1["high"].to_numpy(float); m_low = k1["low"].to_numpy(float)
    m_t = k1["open_time"].to_numpy("int64")
    ho = k1h["open"].to_numpy(float); hh = k1h["high"].to_numpy(float)
    hl = k1h["low"].to_numpy(float); hc = k1h["close"].to_numpy(float)
    ht = k1h["open_time"].to_numpy("int64")
    ft = funding["funding_time"].to_numpy("int64")
    fr = funding["funding_rate"].to_numpy(float)

    strat = CheckpointStrategy(StrategyConfig(
        entry_mode="red_dip", green_gate="off", checkpoint_mode="first_green",
        position_model="dca", direction="long", stop_mode="none"))

    in_pos = False
    avg = size = notional = fees = 0.0
    entry_ns = 0
    saf_notionals = []; saf_trigs = []; next_saf = 0
    tp_price = liq = 0.0
    pending = False
    net_sum = 0.0; wins = 0; n = 0; worst = 0.0; nliq = 0

    def levels():
        nonlocal tp_price, liq
        tp_price = avg * (1.0 + tp / 100.0)
        liq = avg * liq_mult

    def walk(t0):
        nonlocal in_pos, avg, size, notional, fees, next_saf
        nonlocal net_sum, wins, n, worst, nliq
        a = np.searchsorted(m_t, t0, "left"); b = np.searchsorted(m_t, t0 + _H_NS, "left")
        for j in range(a, b):
            bl, bh = m_low[j], m_high[j]
            while next_saf < len(saf_trigs) and bl <= saf_trigs[next_saf]:
                fill = _slip_entry(saf_trigs[next_saf], slip)
                sn = saf_notionals[next_saf]; ss = sn / fill
                avg = (avg * size + fill * ss) / (size + ss)
                size += ss; notional += sn; fees += sn * e.taker_fee
                next_saf += 1; levels()
            if bl <= liq:
                fill = _slip_exit(liq, slip)
                gross = (fill - avg) * size; ef = notional * e.taker_fee
                rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                net = max(-notional / lev, gross - (fees + ef) - rate * notional)
                net_sum += net; n += 1; worst = min(worst, net); nliq += 1
                in_pos = False; return True
            if bh >= tp_price:
                fill = _slip_exit(tp_price, slip)
                gross = (fill - avg) * size; ef = notional * e.taker_fee
                rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                net = gross - (fees + ef) - rate * notional
                net_sum += net; n += 1
                if net > 0: wins += 1
                worst = min(worst, net); in_pos = False; return True
        return False

    for i in range(1, len(k1h)):
        if in_pos:
            walk(ht[i])
        if pending and not in_pos:
            bn = _dca_base_notional(e.margin_usdt, lev, dca)
            fill0 = _slip_entry(ho[i], slip)
            avg = fill0; size = bn / fill0; notional = bn; fees = bn * e.taker_fee
            saf_notionals = _dca_safety_notionals(dca, bn)
            saf_trigs = [fill0 * (1.0 - d / 100.0) for d in cum_drops]
            next_saf = 0; entry_ns = ht[i]; in_pos = True; pending = False
            levels(); walk(ht[i])
        prev = {"high": hh[i - 1]}
        row = {"open": ho[i], "close": hc[i], "high": hh[i], "low": hl[i], "t": ht[i]}
        if strat.on_closed_bar(prev, row, in_pos) == "enter" and not in_pos:
            pending = True

    return {"symbol": symbol, "lev": lev, "tp_pct": tp, "net_pnl": net_sum,
            "n_trades": n, "win_rate": (wins / n if n else 0), "worst": worst,
            "n_liq": nliq, "liq_rate": (nliq / n if n else 0)}


def main():
    try:
        funding = load_funding(str(_HERE / "cache" / "funding.parquet"))
    except Exception:
        funding = pd.DataFrame(columns=["funding_time", "funding_rate"])

    rows = []
    for s in SYMBOLS:
        kp = _HERE / "cache" / s / "klines.parquet"
        if not kp.exists():
            continue
        print(f"Loading {s}...")
        k1 = load_klines(str(kp)); k1h = resample_1h(k1)
        for lev in LEVS:
            for tp in TPS:
                rows.append(sim(s, k1, k1h, funding, lev, tp))

    df = pd.DataFrame(rows)
    df.to_parquet(str(_HERE / "sweep_leverage_1m_results.parquet"))

    best = df.sort_values("net_pnl", ascending=False).groupby(["symbol", "lev"], as_index=False).first()
    print("\n" + "=" * 70)
    print("LEVERAGE vs honest 1m expectancy (best TP per symbol, summed)")
    print("=" * 70)
    print(f"{'lev':>4s}{'total_net':>11s}{'exp/trade':>11s}{'avg_wr':>8s}{'liq_rate':>9s}{'pos_syms':>9s}")
    for lev in LEVS:
        m = best[best["lev"] == lev]
        exp = m["net_pnl"].sum() / m["n_trades"].sum() if m["n_trades"].sum() else 0
        print(f"{lev:>4d}{m['net_pnl'].sum():>11.1f}{exp:>11.4f}"
              f"{m['win_rate'].mean()*100:>7.1f}%{m['liq_rate'].mean()*100:>8.2f}%"
              f"{int((m['net_pnl']>0).sum()):>6d}/{len(m)}")
    print("\n(margin fixed at $2.50 -> lower lev = smaller notional = smaller $ wins.")
    print(" Watch exp/trade SIGN and liq_rate, not absolute $.)")


if __name__ == "__main__":
    main()
