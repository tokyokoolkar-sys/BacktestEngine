"""
HONEST 1m-fill DCA backtest, LONG vs SHORT, for the live red-dip strategy.

Why: engine.py runs DCA on whatever bars you feed it. Feeding 1h bars means
safety fills / TP / liq are decided on 1h OHLC, which can't see intrabar order.
For DCA that's less mirage-prone than a trailing stop (fills at defined trigger
prices, not "assume we caught the high"), but TP-vs-safety ordering inside an
hour still matters. This walks the REAL 1m path so the order is honest.

Signal: CheckpointStrategy red_dip on 1h bars (entry fills next 1h open).
Fills:  base at 1h open; safeties, TP, liq all triggered on 1m bars in true
        chronological order within each hour the basket is open.
Math:   imports the engine's OWN ladder helpers (_dca_* ) so notionals/triggers
        are identical to the live-mirrored engine -- no hand-rolled DCA math.
Costs:  taker fee every fill + exit, slippage, funding, liq floor.

Compares to sweep_dualmode_results.parquet (the 1h-fill run) to expose any gap.
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

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "NEARUSDT", "INJUSDT", "TIAUSDT", "OPUSDT", "APTUSDT", "ARBUSDT",
]

EXEC = ExecConfig(leverage=10, margin_usdt=2.5, taker_fee=0.0005,
                  maker_fee=0.0002, slippage_bps=2.0, maint_margin_rate=0.05)

TPS = [0.3, 0.5, 0.8, 1.0]
MODES = ["long", "short"]
_H_NS = 3_600_000_000_000


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


def sim(symbol, k1, k1h, funding, mode, tp):
    e = EXEC
    slip = e.slippage_bps / 10_000.0
    short = (mode == "short")
    dca = DCAConfig(price_drop_step_pct=0.3, step_multiplier=1.0,
                    size_multiplier=1.1, max_dca_orders=3,
                    first_order_ratio=0.96, take_profit_pct=tp)
    cum_drops = _dca_cumulative_drops(dca)
    liq_mult = ((1.0 + 1.0 / e.leverage - e.maint_margin_rate) if short
                else (1.0 - 1.0 / e.leverage + e.maint_margin_rate))

    m_open = k1["open"].to_numpy(float); m_high = k1["high"].to_numpy(float)
    m_low = k1["low"].to_numpy(float); m_t = k1["open_time"].to_numpy("int64")
    ho = k1h["open"].to_numpy(float); hh = k1h["high"].to_numpy(float)
    hl = k1h["low"].to_numpy(float); hc = k1h["close"].to_numpy(float)
    ht = k1h["open_time"].to_numpy("int64")
    ft = funding["funding_time"].to_numpy("int64")
    fr = funding["funding_rate"].to_numpy(float)

    strat = CheckpointStrategy(StrategyConfig(
        entry_mode="red_dip", green_gate="off", checkpoint_mode="first_green",
        position_model="dca", direction=mode, stop_mode="none"))

    in_pos = False
    avg = size = notional = fees = 0.0
    entry_ns = 0
    saf_notionals = []; saf_trigs = []; next_saf = 0
    tp_price = liq = 0.0
    pending = False
    net_sum = 0.0; wins = 0; n = 0; worst = 0.0

    def recompute_levels():
        nonlocal tp_price, liq
        tp_price = avg * (1.0 - tp / 100.0) if short else avg * (1.0 + tp / 100.0)
        liq = avg * liq_mult

    def close_trade(exit_px, is_liq, exit_ns):
        nonlocal net_sum, wins, n, worst
        gross = (avg - exit_px if short else exit_px - avg) * size
        exit_fee = notional * e.taker_fee
        rate = _funding_in_window(ft, fr, entry_ns, exit_ns)
        fund = (-rate if short else rate) * notional
        net = gross - (fees + exit_fee) - fund
        if is_liq:
            net = max(-notional / e.leverage, net)
        net_sum += net; n += 1
        if net > 0: wins += 1
        worst = min(worst, net)

    def walk_1m(t0):
        """Walk 1m bars in [t0, t0+1h): safety fills, then TP, then liq per bar.
        Returns True if the basket closed."""
        nonlocal in_pos, avg, size, notional, fees, next_saf
        lo_idx = np.searchsorted(m_t, t0, side="left")
        hi_idx = np.searchsorted(m_t, t0 + _H_NS, side="left")
        for j in range(lo_idx, hi_idx):
            bl, bh = m_low[j], m_high[j]
            # 1. safety fills first (adverse: add into the move) -- may be several
            while next_saf < len(saf_trigs):
                trig = saf_trigs[next_saf]
                hit = bh >= trig if short else bl <= trig
                if not hit:
                    break
                fill = _slip_exit(trig, slip) if short else _slip_entry(trig, slip)
                sn = saf_notionals[next_saf]
                ss = sn / fill
                avg = (avg * size + fill * ss) / (size + ss)
                size += ss; notional += sn; fees += sn * e.taker_fee
                next_saf += 1
                recompute_levels()
            # 2. liquidation check (worst case; conservative -- checked before TP
            #    only when liq is on the same side path). Priority: liq is the
            #    forced loss; check it, then TP.
            liq_hit = bh >= liq if short else bl <= liq
            tp_hit = bl <= tp_price if short else bh >= tp_price
            # within a bar we can't know order; take the ADVERSE one (liq) first
            if liq_hit:
                fill = _slip_entry(liq, slip) if short else _slip_exit(liq, slip)
                close_trade(fill, True, m_t[j]); in_pos = False; return True
            if tp_hit:
                fill = _slip_entry(tp_price, slip) if short else _slip_exit(tp_price, slip)
                close_trade(fill, False, m_t[j]); in_pos = False; return True
        return False

    for i in range(1, len(k1h)):
        if in_pos:
            walk_1m(ht[i])

        if pending and not in_pos:
            base_notional = _dca_base_notional(e.margin_usdt, e.leverage, dca)
            fill0 = _slip_exit(ho[i], slip) if short else _slip_entry(ho[i], slip)
            avg = fill0; size = base_notional / fill0
            notional = base_notional; fees = base_notional * e.taker_fee
            saf_notionals = _dca_safety_notionals(dca, base_notional)
            if short:
                saf_trigs = [fill0 * (1.0 + d / 100.0) for d in cum_drops]
            else:
                saf_trigs = [fill0 * (1.0 - d / 100.0) for d in cum_drops]
            next_saf = 0; entry_ns = ht[i]; in_pos = True; pending = False
            recompute_levels()
            walk_1m(ht[i])  # entry hour can also fill/exit

        prev = {"high": hh[i - 1]}
        row = {"open": ho[i], "close": hc[i], "high": hh[i], "low": hl[i], "t": ht[i]}
        action = strat.on_closed_bar(prev, row, in_pos)
        if action == "enter" and not in_pos:
            pending = True

    wr = wins / n if n else 0.0
    return {"symbol": symbol, "mode": mode, "tp_pct": tp,
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
        k1 = load_klines(str(kp)); k1h = resample_1h(k1)
        for mode in MODES:
            for tp in TPS:
                rows.append(sim(s, k1, k1h, funding, mode, tp))

    df = pd.DataFrame(rows)
    df.to_parquet(str(_HERE / "sweep_dca_1m_results.parquet"))

    best = df.sort_values("net_pnl", ascending=False).groupby(["symbol", "mode"], as_index=False).first()
    for mode in MODES:
        m = best[best["mode"] == mode].sort_values("net_pnl", ascending=False)
        print(f"\n=== {mode.upper()} 1m-fill HONEST best-TP per symbol ===")
        print(f"{'SYM':9s}{'TP':>5s}{'net$':>9s}{'WR%':>7s}{'worst':>8s}{'trades':>8s}")
        for _, r in m.iterrows():
            print(f"{r['symbol']:9s}{r['tp_pct']:>5.1f}{r['net_pnl']:>9.2f}"
                  f"{r['win_rate']*100:>7.1f}{r['worst']:>8.3f}{int(r['n_trades']):>8d}")

    print("\n" + "=" * 60)
    print("MODE TOTALS (best TP per symbol, summed)")
    print("=" * 60)
    for mode in MODES:
        m = best[best["mode"] == mode]
        print(f"{mode:6s} 1m-honest total_net={m['net_pnl'].sum():+.2f} "
              f"avg_wr={m['win_rate'].mean()*100:.1f}%  positive_syms={(m['net_pnl']>0).sum()}/{len(m)}")

    # vs 1h-fill run
    p = _HERE / "sweep_dualmode_results.parquet"
    if p.exists():
        d = pd.read_parquet(p)
        b1h = d.sort_values("net_pnl", ascending=False).groupby(["symbol", "mode"], as_index=False).first()
        print("\n1h-fill vs 1m-fill total_net (the mirage check):")
        for mode in MODES:
            t1h = b1h[b1h["mode"] == mode]["net_pnl"].sum()
            t1m = best[best["mode"] == mode]["net_pnl"].sum()
            keep = t1m / t1h * 100 if t1h else 0
            print(f"  {mode:6s} 1h={t1h:+.1f}  1m={t1m:+.1f}  ({keep:.0f}% survives)")


if __name__ == "__main__":
    main()
