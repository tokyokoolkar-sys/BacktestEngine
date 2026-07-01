"""
sweep_compound.py — capital-dependent account simulator.

The core backtest engine reports pnl_pct = net / margin (return-on-margin), which
is INVARIANT to capital: scaling margin just scales absolute PnL by a constant.
So a plain backtest can never show "what does $30 vs $5000 do differently".

Two things actually make capital matter, and this script models both:

  1. minNotional floor ($5 on Binance USDⓈ-M). A tiny account is FORCED to
     over-size orders to meet the exchange minimum -> it risks a larger fraction
     of equity than intended. Big accounts never feel this.
  2. Sizing mode:
       fixed   — live-actual today (base_margin=$2.5 flat, risk_per_trade_pct=0).
                 On $30 that's a huge fraction; on $5000 it's a rounding error.
       frac    — constant-fraction compounding (margin = risk_pct * equity).
                 Geometric growth; capital matters via the minNotional floor only.

Method:
  * Run the shared engine once per active live symbol (DCA model, live params).
  * pnl_pct per trade is return-on-deployed-margin.
  * Merge all trades chronologically into ONE shared account.
  * Walk entry/exit events: lock margin on entry (respect max_concurrent_baskets
    and free equity), realize pnl_pct * margin on exit, compound.
  * Report final equity, return %, CAGR, max drawdown, ruin for each capital.

No lookahead beyond what the engine already guarantees. Offline; uses cache/.
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines
from engine import run_backtest

CACHE = _HERE / "cache"

# --- Live risk surface (from bot_config.yaml global: + symbols:) ---
LEVERAGE = 10
BASE_MARGIN = 2.5          # fixed-dollar base margin per trade (risk_per_trade_pct=0)
MAX_DCA = 5
DROP_STEP_PCT = 0.3
SIZE_MULT = 1.1
FIRST_ORDER_RATIO = 0.96
STOP_PCT = 3.0
MAINT = 0.05
TAKER, MAKER = 0.0005, 0.0002
MIN_NOTIONAL = 5.0
MAX_CONCURRENT = 6

# Active symbols in bot_config.yaml (active: true) with their tp_pct
ACTIVE = {
    "NEARUSDT": 0.5, "INJUSDT": 0.5, "TIAUSDT": 0.8,
    "FILUSDT": 0.5, "OPUSDT": 0.5, "ARBUSDT": 0.5, "APTUSDT": 0.5,
}

CAPITALS = [30, 50, 100, 1000, 5000]


def _cfg(symbol: str, tp_pct: float) -> BacktestConfig:
    return BacktestConfig(
        symbol=symbol,
        strategy=StrategyConfig(
            entry_mode="red_dip", green_gate="off",
            stop_mode="fixed", stop_pct=STOP_PCT,
            checkpoint_mode="first_green", position_model="dca", direction="long",
        ),
        dca=DCAConfig(
            price_drop_step_pct=DROP_STEP_PCT, step_multiplier=1.0,
            size_multiplier=SIZE_MULT, max_dca_orders=MAX_DCA,
            first_order_ratio=FIRST_ORDER_RATIO, take_profit_pct=tp_pct,
        ),
        exec=ExecConfig(
            leverage=LEVERAGE, margin_usdt=BASE_MARGIN,
            taker_fee=TAKER, maker_fee=MAKER, slippage_bps=2.0, maint_margin_rate=MAINT,
        ),
    )


def collect_trades():
    """Run engine per active symbol; return merged, time-sorted event list.

    Each event: (entry_ns, exit_ns, pnl_pct)  where pnl_pct is return-on-margin.
    """
    events = []
    missing = []
    for sym, tp in ACTIVE.items():
        kp = CACHE / sym / "klines.parquet"
        fp = CACHE / f"{sym}-funding.parquet"
        if not kp.exists() or not fp.exists():
            missing.append(sym)
            continue
        kl, fu = load_klines(str(kp)), load_funding(str(fp))
        res = run_backtest(kl, fu, _cfg(sym, tp))
        for t in res.trades:
            events.append((t.entry_time.value, t.exit_time.value, t.pnl_pct, sym))
    events.sort(key=lambda e: e[0])
    return events, missing


def simulate(events, capital, mode, risk_pct):
    """Walk events on a shared compounding account.

    mode="fixed": margin = BASE_MARGIN flat (live-actual, risk_per_trade_pct=0).
    mode="frac" : margin = risk_pct * equity (constant-fraction compounding).
    Both floored so notional >= MIN_NOTIONAL (the capital-sensitive constraint).
    """
    # Build chronological entry/exit event stream.
    stream = []
    for idx, (en, ex, pnl_pct, sym) in enumerate(events):
        stream.append((en, "open", idx))
        stream.append((ex, "close", idx))
    # Tiebreak at equal ns: a trade's own open MUST precede its own close, else
    # zero-duration trades (entry_ns == exit_ns) leak a slot permanently. Order
    # by (ns, open-before-close, idx) so each trade opens then closes in sequence.
    stream.sort(key=lambda s: (s[0], 0 if s[1] == "open" else 1, s[2]))

    equity = float(capital)
    peak = equity
    max_dd = 0.0
    locked = 0.0          # margin currently tied up in open baskets
    open_ct = 0
    margins = {}          # idx -> deployed margin (0 if skipped)
    pnls = {idx: e[2] for idx, e in enumerate(events)}
    n_taken = n_skip_cap = n_skip_funds = n_forced = 0
    ruined = False

    for _ns, kind, idx in stream:
        if kind == "open":
            if ruined:
                margins[idx] = 0.0
                continue
            want = BASE_MARGIN if mode == "fixed" else risk_pct * equity
            notional = want * LEVERAGE
            forced = False
            if notional < MIN_NOTIONAL:          # exchange floor -> over-size
                notional = MIN_NOTIONAL
                want = notional / LEVERAGE
                forced = True
            free = equity - locked
            if open_ct >= MAX_CONCURRENT:
                margins[idx] = 0.0; n_skip_cap += 1; continue
            if want > free:                      # can't afford it
                margins[idx] = 0.0; n_skip_funds += 1; continue
            margins[idx] = want
            locked += want
            open_ct += 1
            n_taken += 1
            if forced:
                n_forced += 1
        else:  # close
            m = margins.get(idx, 0.0)
            if m <= 0.0:
                continue
            equity += pnls[idx] * m
            locked -= m
            open_ct -= 1
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            # ruin: can't even meet minNotional margin any more
            if equity < MIN_NOTIONAL / LEVERAGE:
                ruined = True

    span_days = (events[-1][1] - events[0][0]) / 1e9 / 86400 if events else 0.0
    years = span_days / 365.25 if span_days else 0.0
    ret = (equity / capital - 1.0) * 100.0
    cagr = ((equity / capital) ** (1 / years) - 1.0) * 100.0 if years > 0 and equity > 0 else float("nan")
    return dict(capital=capital, final=equity, ret=ret, cagr=cagr, max_dd=max_dd * 100,
                taken=n_taken, forced=n_forced, skip_cap=n_skip_cap,
                skip_funds=n_skip_funds, ruined=ruined, years=years)


def main():
    ap = argparse.ArgumentParser(description="Capital-dependent compounding account sim")
    ap.add_argument("--mode", choices=["fixed", "frac", "both"], default="both",
                    help="fixed=live-actual $2.5 flat; frac=constant-fraction compounding")
    ap.add_argument("--risk-pct", type=float, default=0.05,
                    help="frac mode: margin = risk_pct * equity (default 0.05 = 5%%)")
    args = ap.parse_args()

    # Local self-generated cache of engine output (own machine/data — trusted).
    cache_f = _HERE / "cache" / "_compound_events.pkl"
    if cache_f.exists():
        print(f"[compound] loading cached events {cache_f.name}...")
        events, missing = pickle.load(open(cache_f, "rb"))
    else:
        print(f"[compound] collecting trades for {len(ACTIVE)} active symbols...")
        events, missing = collect_trades()
        pickle.dump((events, missing), open(cache_f, "wb"))
    if missing:
        print(f"[compound] WARNING no cache for: {', '.join(missing)}")
    if not events:
        print("[compound] no trades collected; nothing to sim."); return
    span_d = (events[-1][1] - events[0][0]) / 1e9 / 86400
    print(f"[compound] {len(events)} trades merged across {len(ACTIVE)-len(missing)} symbols, "
          f"span {span_d:.0f}d\n")

    modes = ["fixed", "frac"] if args.mode == "both" else [args.mode]
    for mode in modes:
        tag = ("FIXED-DOLLAR ($%.2f flat margin, = live risk_per_trade_pct=0)" % BASE_MARGIN
               if mode == "fixed"
               else "CONSTANT-FRACTION (margin = %.0f%% of equity, compounding)" % (args.risk_pct * 100))
        print(f"=== {tag} ===")
        print(f"{'capital':>8} {'final$':>12} {'return%':>10} {'CAGR%':>9} "
              f"{'maxDD%':>7} {'taken':>6} {'forced':>7} {'skipCap':>8} {'skipFund':>9} {'ruin':>5}")
        for cap in CAPITALS:
            r = simulate(events, cap, mode, args.risk_pct)
            cagr = f"{r['cagr']:>9.1f}" if r["cagr"] == r["cagr"] else f"{'n/a':>9}"
            print(f"{r['capital']:>8} {r['final']:>12.2f} {r['ret']:>10.1f} {cagr} "
                  f"{r['max_dd']:>7.1f} {r['taken']:>6} {r['forced']:>7} "
                  f"{r['skip_cap']:>8} {r['skip_funds']:>9} {str(r['ruined']):>5}")
        print()
    print("forced  = trades bumped UP to minNotional (small-capital over-risk)")
    print("skipCap = trades dropped, >6 baskets already open")
    print("skipFund= trades dropped, not enough free equity to open")


if __name__ == "__main__":
    main()
