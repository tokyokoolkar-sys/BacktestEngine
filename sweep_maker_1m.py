"""
Does MAKER execution rescue red-dip DCA? Honest 1m fills.

Biggest controllable bleed is the TAKER fee: 0.05% x ~5 ladder fills ~= 0.25% of
notional, eaten before price moves, against a 0.5% TP. Posting limit (maker)
orders changes two things:
  1. fee: taker 0.05% -> maker 0.02% PAID at base tier (NOT a rebate), or
     -0.02% EARNED only at VIP tiers. We test both, labelled honestly.
  2. slippage: gone. You set the price; fills at exactly the trigger.

Caveat (stated, not hidden): a maker TP limit only fills if price TRADES THROUGH
it; "price touched" != "you filled" if you're at the back of the queue. This sim
assumes touch = fill, which is mildly OPTIMISTIC for maker. Liquidation stays
taker + slippage (it's a forced market close). So results are an upper-ish bound
on the maker benefit -- if it DOESN'T cross zero here, it won't live.

1h signal / 1m fill. Long + short. Leverage grid (10/5/3).
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
from engine import (_funding_in_window, _dca_cumulative_drops,
                    _dca_safety_notionals, _dca_base_notional)
from strategy import CheckpointStrategy

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
           "NEARUSDT", "INJUSDT", "TIAUSDT", "OPUSDT", "APTUSDT", "ARBUSDT"]

# (label, entry_fee, exit_fee)  -- fraction of notional; negative = rebate earned
FEE_MODES = [
    ("taker_base", 0.0005, 0.0005),      # current live assumption
    ("maker_paid", 0.0002, 0.0002),      # realistic base-tier maker
    ("maker_rebate", -0.0002, -0.0002),  # optimistic VIP rebate
]
LEVS = [10, 5, 3]
TPS = [0.3, 0.5, 0.8, 1.0]
_H_NS = 3_600_000_000_000
SLIP = 2.0 / 10_000.0  # only applied to taker (liq) fills


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


def sim(symbol, k1, k1h, funding, lev, tp, entry_fee, exit_fee, short):
    margin = 2.5
    maint = 0.05
    dca = DCAConfig(price_drop_step_pct=0.3, step_multiplier=1.0,
                    size_multiplier=1.1, max_dca_orders=3,
                    first_order_ratio=0.96, take_profit_pct=tp)
    cum_drops = _dca_cumulative_drops(dca)
    liq_mult = (1.0 + 1.0 / lev - maint) if short else (1.0 - 1.0 / lev + maint)

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
    entry_ns = 0
    saf_notionals = []; saf_trigs = []; next_saf = 0
    tp_price = liq = 0.0
    pending = False
    net_sum = 0.0; wins = 0; n = 0; worst = 0.0; nliq = 0

    def levels():
        nonlocal tp_price, liq
        tp_price = avg * (1.0 - tp / 100.0) if short else avg * (1.0 + tp / 100.0)
        liq = avg * liq_mult

    def walk(t0):
        nonlocal in_pos, avg, size, notional, fees, next_saf
        nonlocal net_sum, wins, n, worst, nliq
        a = np.searchsorted(m_t, t0, "left"); b = np.searchsorted(m_t, t0 + _H_NS, "left")
        for j in range(a, b):
            bl, bh = m_low[j], m_high[j]
            # maker safety fills at exact trigger (no slippage), fee = entry_fee
            while next_saf < len(saf_trigs):
                trig = saf_trigs[next_saf]
                hit = bh >= trig if short else bl <= trig
                if not hit:
                    break
                sn = saf_notionals[next_saf]; ss = sn / trig
                avg = (avg * size + trig * ss) / (size + ss)
                size += ss; notional += sn; fees += sn * entry_fee
                next_saf += 1; levels()
            # liquidation: taker + slippage (forced)
            liq_hit = bh >= liq if short else bl <= liq
            if liq_hit:
                fill = liq * (1 + SLIP) if short else liq * (1 - SLIP)
                gross = (avg - fill if short else fill - avg) * size
                ef = notional * 0.0005  # taker exit on liq
                rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                fund = (-rate if short else rate) * notional
                net = max(-notional / lev, gross - (fees + ef) - fund)
                net_sum += net; n += 1; worst = min(worst, net); nliq += 1
                in_pos = False; return True
            # maker TP at exact price (touch=fill, optimistic), fee = exit_fee
            tp_hit = bl <= tp_price if short else bh >= tp_price
            if tp_hit:
                gross = (avg - tp_price if short else tp_price - avg) * size
                ef = notional * exit_fee
                rate = _funding_in_window(ft, fr, entry_ns, m_t[j])
                fund = (-rate if short else rate) * notional
                net = gross - (fees + ef) - fund
                net_sum += net; n += 1
                if net > 0: wins += 1
                worst = min(worst, net); in_pos = False; return True
        return False

    for i in range(1, len(k1h)):
        if in_pos:
            walk(ht[i])
        if pending and not in_pos:
            bn = _dca_base_notional(margin, lev, dca)
            fill0 = ho[i]  # maker base entry at open, no slippage
            avg = fill0; size = bn / fill0; notional = bn; fees = bn * entry_fee
            saf_notionals = _dca_safety_notionals(dca, bn)
            if short:
                saf_trigs = [fill0 * (1.0 + d / 100.0) for d in cum_drops]
            else:
                saf_trigs = [fill0 * (1.0 - d / 100.0) for d in cum_drops]
            next_saf = 0; entry_ns = ht[i]; in_pos = True; pending = False
            levels(); walk(ht[i])
        prev = {"high": hh[i - 1]}
        row = {"open": ho[i], "close": hc[i], "high": hh[i], "low": hl[i], "t": ht[i]}
        if strat.on_closed_bar(prev, row, in_pos) == "enter" and not in_pos:
            pending = True

    return {"symbol": symbol, "lev": lev, "tp_pct": tp, "net_pnl": net_sum,
            "n_trades": n, "win_rate": (wins / n if n else 0),
            "worst": worst, "n_liq": nliq}


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
        for fee_label, ef_in, ef_out in FEE_MODES:
            for lev in LEVS:
                for tp in TPS:
                    for short in (False, True):
                        r = sim(s, k1, k1h, funding, lev, tp, ef_in, ef_out, short)
                        r["fee_mode"] = fee_label
                        r["dir"] = "short" if short else "long"
                        rows.append(r)

    df = pd.DataFrame(rows)
    df.to_parquet(str(_HERE / "sweep_maker_1m_results.parquet"))

    print("\n" + "=" * 78)
    print("MAKER vs TAKER -- honest 1m exp/trade (best TP per symbol, summed by dir)")
    print("=" * 78)
    best = (df.sort_values("net_pnl", ascending=False)
              .groupby(["symbol", "fee_mode", "lev", "dir"], as_index=False).first())
    print(f"{'fee_mode':14s}{'dir':6s}{'lev':>4s}{'total_net':>11s}{'exp/trade':>11s}{'avg_wr':>8s}{'pos_syms':>9s}")
    for fee_label, _, _ in FEE_MODES:
        for d in ("long", "short"):
            for lev in LEVS:
                m = best[(best["fee_mode"] == fee_label) & (best["dir"] == d) & (best["lev"] == lev)]
                if m.empty:
                    continue
                exp = m["net_pnl"].sum() / m["n_trades"].sum() if m["n_trades"].sum() else 0
                flag = "  <-- POSITIVE" if m["net_pnl"].sum() > 0 else ""
                print(f"{fee_label:14s}{d:6s}{lev:>4d}{m['net_pnl'].sum():>11.1f}{exp:>11.4f}"
                      f"{m['win_rate'].mean()*100:>7.1f}%{int((m['net_pnl']>0).sum()):>6d}/{len(m)}{flag}")


if __name__ == "__main__":
    main()
