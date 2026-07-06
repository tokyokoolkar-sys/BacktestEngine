"""run_screener_dca.py — DCA-faithful screener that loads params from a bot config YAML.

Runs a 1-month backtest per symbol using the exact DCA ladder, body filter, leverage,
and tp_pct from the live config. Ranks by (exp_per_trade × weekly_roi) descending.
Prints a ranked table + yaml-ready symbols block for copy-paste.

Usage
-----
    python run_screener_dca.py --config DCA_Longrun_bot_config.yaml [--fetch] [--top N]
    python run_screener_dca.py --config DCA_Longrun_bot_config.yaml --fetch --top 20

Outputs
-------
  Ranked table  — exp/trade (USDT), weekly_roi (%), n_trades, win_rate, worst_trade
  YAML block    — top-N symbols ready to paste into config's symbols: list
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pathlib import Path

import yaml

import pandas as pd

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import download_klines, load_klines, months_list
from engine import run_backtest
from perps import fetch_usdt_perps

_ROOT = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader — reads live YAML, returns flat param dict
# ---------------------------------------------------------------------------

@dataclass
class LiveParams:
    leverage: int
    base_margin: float
    max_dca_orders: int
    drop_step_pct: float
    step_multiplier: float
    size_multiplier: float
    first_order_ratio: float
    fee_taker: float
    fee_maker: float
    entry_volume_max_ratio: float
    # per first symbol in symbols list (used as template for screener)
    tp_pct: float
    stop_pct: float
    body_pct: float
    direction: str
    trail_activate_pct: float = 0.75
    trail_buffer_pct: float = 0.45


def _load_live_params(path: str) -> LiveParams:
    with open(path) as f:
        raw = yaml.safe_load(f)
    g = raw.get("global", {})
    syms = raw.get("symbols", [{}])
    s0 = syms[0] if syms else {}
    return LiveParams(
        leverage=int(g.get("leverage", 10)),
        base_margin=float(g.get("base_margin", 2.5)),
        max_dca_orders=int(g.get("max_dca_orders", 3)),
        drop_step_pct=float(g.get("drop_step_pct", 0.5)),
        step_multiplier=float(g.get("step_multiplier", 1.0)),
        size_multiplier=float(g.get("size_multiplier", 1.1)),
        first_order_ratio=float(g.get("first_order_ratio", 0.96)),
        fee_taker=float(g.get("fee_taker", 0.0005)),
        fee_maker=float(g.get("fee_maker", 0.0002)),
        entry_volume_max_ratio=float(g.get("entry_volume_max_ratio", 2.5)),
        tp_pct=float(s0.get("tp_pct", 1.06)),
        stop_pct=float(s0.get("stop_pct", 9999)),
        body_pct=float(s0.get("body_pct", 0.0)),
        direction=str(s0.get("direction", "long")),
        trail_activate_pct=float(g.get("trail_activate_pct", 0.75)),
        trail_buffer_pct=float(g.get("trail_buffer_pct", 0.45)),
    )


# ---------------------------------------------------------------------------
# Per-symbol backtest
# ---------------------------------------------------------------------------

def _run_symbol(symbol: str, months: list[str], p: LiveParams, fetch: bool = False) -> dict[str, Any] | None:
    cache_dir = _ROOT / "cache" / symbol
    parquet_path = cache_dir / "klines.parquet"

    if not parquet_path.exists():
        if not fetch:
            logger.warning("%s  skip — no cache, use --fetch", symbol)
            return None
        try:
            # download_klines writes cache_dir/{symbol}-1m-klines.parquet;
            # rename to klines.parquet to match screener convention
            dl_path = Path(download_klines(symbol, months, out_dir=str(cache_dir)))
            if dl_path != parquet_path:
                dl_path.rename(parquet_path)
        except Exception as exc:
            logger.warning("%s  skip — download failed: %s", symbol, exc)
            return None

    try:
        klines = load_klines(parquet_path)
    except Exception as exc:
        logger.warning("%s  skip — load failed: %s", symbol, exc)
        return None

    if klines is None or len(klines) < 50:
        logger.warning("%s  skip — insufficient data (%s bars)", symbol, len(klines) if klines is not None else 0)
        return None

    use_fixed_stop = p.stop_pct < 9000
    strat = StrategyConfig(
        entry_mode="red_dip",
        green_gate="off",
        stop_mode="fixed" if use_fixed_stop else "trail",
        stop_pct=p.stop_pct if use_fixed_stop else None,
        trail_activate_pct=p.trail_activate_pct,
        trail_buffer_pct=p.trail_buffer_pct,
        checkpoint_mode="first_green",
        position_model="dca",
        direction=p.direction,
        body_pct=p.body_pct,
        entry_volume_max_ratio=p.entry_volume_max_ratio,
    )
    dca = DCAConfig(
        price_drop_step_pct=p.drop_step_pct,
        max_dca_orders=p.max_dca_orders,
        step_multiplier=p.step_multiplier,
        size_multiplier=p.size_multiplier,
        first_order_ratio=p.first_order_ratio,
        take_profit_pct=p.tp_pct,
    )
    exc_cfg = ExecConfig(
        leverage=p.leverage,
        margin_usdt=p.base_margin,
        taker_fee=p.fee_taker,
        maker_fee=p.fee_maker,
    )
    cfg = BacktestConfig(symbol=symbol, strategy=strat, dca=dca, exec=exc_cfg)

    # empty funding — screener doesn't need funding precision
    empty_funding = pd.DataFrame(columns=["funding_time", "funding_rate"])

    try:
        result = run_backtest(klines, empty_funding, cfg)
    except Exception as exc:
        logger.warning("%s  skip — engine error: %s", symbol, exc)
        return None

    trades = result.trades
    n = len(trades)
    if n < 3:  # ponytail: raise threshold if noise persists
        return None

    net_pnls = [t.net_pnl for t in trades]
    total_pnl = sum(net_pnls)
    exp_per_trade = total_pnl / n
    win_pnls = [x for x in net_pnls if x > 0]
    loss_pnls = [x for x in net_pnls if x <= 0]
    wins = len(win_pnls)
    win_rate = wins / n
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    # BE win rate = 1 / (1 + RR)
    be_win_rate = 1 / (1 + rr) if rr != float("inf") else 0.0
    worst = min(net_pnls)

    # Weekly ROI: total_pnl / base_margin / n_weeks
    n_bars = len(klines)
    # klines are 1h — approximate weeks
    n_weeks = max(n_bars / (24 * 7), 1 / 7)
    weekly_roi_pct = (total_pnl / p.base_margin) * 100 / n_weeks

    score = exp_per_trade * weekly_roi_pct  # rank metric

    return {
        "symbol": symbol,
        "n_trades": n,
        "exp_per_trade": round(exp_per_trade, 4),
        "weekly_roi_pct": round(weekly_roi_pct, 3),
        "win_rate": round(win_rate, 3),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "rr": round(rr, 2) if rr != float("inf") else 999.0,
        "be_win_rate": round(be_win_rate, 3),
        "worst_trade": round(worst, 4),
        "total_pnl": round(total_pnl, 4),
        "score": round(score, 4),
    }


# ---------------------------------------------------------------------------
# Fetch symbol universe
# ---------------------------------------------------------------------------

def _get_symbols() -> list[str]:
    try:
        syms = fetch_usdt_perps()
        if syms:
            return syms
    except Exception:
        pass
    try:
        from symbols import SYMBOLS
        return list(SYMBOLS)
    except ImportError:
        pass
    raise RuntimeError("Could not fetch symbol universe — run with perps.py available or symbols.py present")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

_HDR = (f"{'#':>3}  {'SYMBOL':<14} {'EXP/TRD':>8} {'WK_ROI%':>8} {'N':>5}"
        f" {'WIN%':>6} {'AVG_WIN':>8} {'AVG_LOSS':>9} {'RR':>5} {'BE%':>6} {'WORST':>8}  {'SCORE':>8}")
_SEP = "-" * len(_HDR)


def _fmt_row(rank: int, r: dict) -> str:
    return (
        f"{rank:>3}  {r['symbol']:<14} "
        f"{r['exp_per_trade']:>8.4f} "
        f"{r['weekly_roi_pct']:>8.3f} "
        f"{r['n_trades']:>5} "
        f"{r['win_rate']*100:>5.1f}% "
        f"{r['avg_win']:>8.4f} "
        f"{r['avg_loss']:>9.4f} "
        f"{r['rr']:>5.2f} "
        f"{r['be_win_rate']*100:>5.1f}% "
        f"{r['worst_trade']:>8.4f}  "
        f"{r['score']:>8.4f}"
    )


def _yaml_block(rows: list[dict], p: LiveParams) -> str:
    lines = ["symbols:"]
    for r in rows:
        lines.append(f"- name: {r['symbol']}")
        lines.append(f"  direction: {p.direction}")
        lines.append(f"  tp_pct: {p.tp_pct}")
        lines.append(f"  stop_pct: 9999")
        lines.append(f"  max_dca_orders: {p.max_dca_orders}")
        lines.append(f"  body_pct: {p.body_pct}")
        lines.append(f"  active: true")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _screen(symbols: list[str], months: list[str], p: LiveParams, fetch: bool) -> list[dict]:
    results = []
    for i, sym in enumerate(symbols, 1):
        if i % 20 == 0:
            logger.info("  %d / %d ...", i, len(symbols))
        row = _run_symbol(sym, months, p, fetch=fetch)
        if row:
            results.append(row)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _print_compare(symbols: list[str], months: list[str], p_base: LiveParams,
                   p_alt: LiveParams, fetch: bool) -> None:
    """Side-by-side diff: base config vs alt config for specific symbols."""
    base = {r["symbol"]: r for r in _screen(symbols, months, p_base, fetch)}
    alt  = {r["symbol"]: r for r in _screen(symbols, months, p_alt,  fetch)}

    all_syms = sorted(set(base) | set(alt))

    hdr = (f"{'SYMBOL':<16} {'N_base':>7} {'N_alt':>7} {'dN':>5}"
           f"  {'EXP_base':>9} {'EXP_alt':>9}"
           f"  {'RR_base':>8} {'RR_alt':>8}"
           f"  {'PnL_base':>9} {'PnL_alt':>9}")
    print(f"\nCOMPARE  body_pct={p_base.body_pct} vs {p_alt.body_pct}"
          f"  tp_pct={p_base.tp_pct} vs {p_alt.tp_pct}\n")
    print(hdr)
    print("-" * len(hdr))
    for sym in all_syms:
        b = base.get(sym)
        a = alt.get(sym)
        nb  = b["n_trades"]       if b else 0
        na  = a["n_trades"]       if a else 0
        eb  = b["exp_per_trade"]  if b else 0.0
        ea  = a["exp_per_trade"]  if a else 0.0
        rb  = b["rr"]             if b else 0.0
        ra  = a["rr"]             if a else 0.0
        pb  = b["total_pnl"]      if b else 0.0
        pa  = a["total_pnl"]      if a else 0.0
        print(f"{sym:<16} {nb:>7} {na:>7} {na-nb:>+5}"
              f"  {eb:>9.4f} {ea:>9.4f}"
              f"  {rb:>8.2f} {ra:>8.2f}"
              f"  {pb:>9.4f} {pa:>9.4f}")
    print()
    tot_b = sum((b or {}).get("total_pnl", 0) for b in [base.get(s) for s in all_syms])
    tot_a = sum((a or {}).get("total_pnl", 0) for a in [alt.get(s)  for s in all_syms])
    n_b   = sum((b or {}).get("n_trades",  0) for b in [base.get(s) for s in all_syms])
    n_a   = sum((a or {}).get("n_trades",  0) for a in [alt.get(s)  for s in all_syms])
    print(f"{'TOTAL':<16} {n_b:>7} {n_a:>7} {n_a-n_b:>+5}"
          f"  {'':>9} {'':>9}  {'':>8} {'':>8}"
          f"  {tot_b:>9.4f} {tot_a:>9.4f}  (delta={tot_a-tot_b:+.4f})")


def _worst_case_basket(p: LiveParams) -> float:
    """Max capital deployed per basket if all DCA orders fill."""
    margin = p.base_margin
    total = margin  # base order
    size = margin * p.first_order_ratio
    for _ in range(p.max_dca_orders):
        total += size
        size *= p.size_multiplier
    return total


def _optimize(symbols: list[str], months: list[str], p_base: LiveParams,
              fetch: bool, max_basket_usdt: float, workers: int = 20) -> None:
    """Grid-search body_pct × tp_pct × drop_step_pct × max_dca_orders × size_multiplier × stop_pct
    per symbol, parallelised across combos. Print best combo per symbol + combined yaml block."""
    import itertools
    from concurrent.futures import ThreadPoolExecutor, as_completed

    BODY_PCTS      = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    TP_PCTS        = [1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08]
    DROP_STEPS     = [0.5, 0.6, 0.68, 0.8, 1.0]
    MAX_DCA_ORDERS = [2, 3, 4, 5]
    SIZE_MULTS     = [1.0, 1.1, 1.2, 1.5]
    STOP_PCTS      = [1.5, 2.0, 3.0, 5.0, 9999]  # 9999 = no stop

    all_combos = list(itertools.product(BODY_PCTS, TP_PCTS, DROP_STEPS, MAX_DCA_ORDERS, SIZE_MULTS, STOP_PCTS))
    total_combos = len(all_combos)
    logger.info("Optimize: %d combos x %d symbols = %d backtests",
                total_combos, len(symbols), total_combos * len(symbols))
    logger.info("Capital constraint: max_basket <= $%.2f", max_basket_usdt)

    def _run_combo(symbol: str, combo: tuple) -> dict | None:
        body, tp, drop, ndca, smult, stop = combo
        p = dataclasses.replace(
            p_base,
            body_pct=body,
            tp_pct=tp,
            drop_step_pct=drop,
            max_dca_orders=ndca,
            size_multiplier=smult,
            stop_pct=stop,
            trail_activate_pct=p_base.trail_activate_pct,
            trail_buffer_pct=p_base.trail_buffer_pct,
        )
        if _worst_case_basket(p) > max_basket_usdt:
            return None
        row = _run_symbol(symbol, months, p, fetch=fetch)
        if row is None:
            return None
        row["body_pct"]       = body
        row["tp_pct"]         = tp
        row["drop_step_pct"]  = drop
        row["max_dca_orders"] = ndca
        row["size_multiplier"]= smult
        row["stop_pct"]       = stop
        row["basket_worst"]   = round(_worst_case_basket(p), 2)
        return row

    results_per_symbol: dict[str, list[dict]] = {}

    for sym_i, symbol in enumerate(symbols, 1):
        logger.info("[%d/%d] optimizing %s (%d combos, %d workers) ...",
                    sym_i, len(symbols), symbol, total_combos, workers)
        profitable: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_combo, symbol, c): c for c in all_combos}
            for fut in as_completed(futs):
                row = fut.result()
                if row is None or row["exp_per_trade"] <= 0:
                    continue
                profitable.append(row)
        profitable.sort(key=lambda r: r["weekly_roi_pct"], reverse=True)
        results_per_symbol[symbol] = profitable[:20]

    if not results_per_symbol:
        print("\n(No results — all combos exceeded capital constraint or no data.)\n")
        return

    hdr = (f"{'#':>3} {'EXP':>8} {'WK_ROI%':>8} {'PNL/mo':>8} {'N':>5} {'WIN%':>6}"
           f" {'RR':>5} {'body':>5} {'tp':>5} {'drop':>5} {'ndca':>4} {'smult':>5} {'stop%':>6} {'basket$':>8}")
    sep = "-" * len(hdr)

    for symbol in symbols:
        rows = results_per_symbol.get(symbol, [])
        print(f"\n{'='*60}")
        print(f"  {symbol}  --  {len(rows)} profitable combos (sorted by exp/trade)")
        print(f"{'='*60}")
        if not rows:
            print("  (no profitable combo found)")
            continue
        print(hdr)
        print(sep)
        for i, r in enumerate(rows, 1):
            stop_disp = r.get('stop_pct', 9999)
            print(f"{i:>3}"
                  f" {r['exp_per_trade']:>8.4f}"
                  f" {r['weekly_roi_pct']:>8.3f}"
                  f" {r['total_pnl']:>8.2f}"
                  f" {r['n_trades']:>5}"
                  f" {r['win_rate']*100:>5.1f}%"
                  f" {r['rr']:>5.2f}"
                  f" {r['body_pct']:>5.1f}"
                  f" {r['tp_pct']:>5.2f}"
                  f" {r['drop_step_pct']:>5.2f}"
                  f" {r['max_dca_orders']:>4}"
                  f" {r['size_multiplier']:>5.1f}"
                  f" {stop_disp:>6}"
                  f" {r['basket_worst']:>8.2f}")

    # Global params most common across symbols (drop_step, size_mult)
    drops = [results_per_symbol[s][0]["drop_step_pct"] for s in symbols if results_per_symbol.get(s)]
    smults = [results_per_symbol[s][0]["size_multiplier"] for s in symbols if results_per_symbol.get(s)]
    from collections import Counter
    best_drop = Counter(drops).most_common(1)[0][0]
    best_smult = Counter(smults).most_common(1)[0][0]
    print(f"\n# Suggested global overrides (most common across symbols):")
    print(f"# drop_step_pct: {best_drop}")
    print(f"# size_multiplier: {best_smult}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="DCA-faithful symbol screener.")
    ap.add_argument("--config", default="DCA_Longrun_bot_config.yaml",
                    help="Bot config YAML to load params from.")
    ap.add_argument("--fetch", action="store_true", default=False,
                    help="Download missing kline data.")
    ap.add_argument("--top", type=int, default=20, metavar="N",
                    help="Number of top symbols to show in YAML block (default: 20).")
    ap.add_argument("--symbols", default=None, metavar="SYM1,SYM2,...",
                    help="Comma-separated override (default: full perps universe).")
    ap.add_argument("--body-pct", type=float, default=None, metavar="FLOAT",
                    help="Override body_pct from config.")
    ap.add_argument("--tp-pct", type=float, default=None, metavar="FLOAT",
                    help="Override tp_pct from config.")
    ap.add_argument("--compare", action="store_true", default=False,
                    help="Side-by-side compare config params vs --body-pct/--tp-pct overrides. Requires --symbols.")
    ap.add_argument("--optimize", action="store_true", default=False,
                    help="Grid-search all params per symbol, pick best combo within capital cap. Requires --symbols.")
    ap.add_argument("--max-basket", type=float, default=17.0, metavar="USDT",
                    help="Max worst-case capital per basket for --optimize (default: 17.0 = half of $35).")
    args = ap.parse_args(argv)

    p = _load_live_params(args.config)

    # 1-month window ending yesterday
    now = datetime.now(tz=timezone.utc)
    end_month = f"{now.year}-{now.month:02d}"
    m = now.month - 1 or 12
    y = now.year if now.month > 1 else now.year - 1
    start_month = f"{y}-{m:02d}"
    months = months_list(start_month, end_month)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        logger.info("Fetching symbol universe...")
        symbols = _get_symbols()

    # optimize mode
    if args.optimize:
        if not args.symbols:
            ap.error("--optimize requires --symbols")
        _optimize(symbols, months, p, args.fetch, args.max_basket)
        return

    # compare mode
    if args.compare:
        if not args.symbols:
            ap.error("--compare requires --symbols")
        p_alt = dataclasses.replace(
            p,
            body_pct=args.body_pct if args.body_pct is not None else p.body_pct,
            tp_pct=args.tp_pct   if args.tp_pct   is not None else p.tp_pct,
        )
        _print_compare(symbols, months, p, p_alt, args.fetch)
        return

    # apply overrides for normal run
    if args.body_pct is not None:
        p = dataclasses.replace(p, body_pct=args.body_pct)
    if args.tp_pct is not None:
        p = dataclasses.replace(p, tp_pct=args.tp_pct)

    logger.info("Params loaded from %s", args.config)
    logger.info("  tp_pct=%.4f  body_pct=%.1f  drop=%.2f  max_dca=%d  lev=%dx  margin=$%.2f",
                p.tp_pct, p.body_pct, p.drop_step_pct, p.max_dca_orders, p.leverage, p.base_margin)
    logger.info("  window: %s → %s", months[0], months[-1])

    logger.info("Screening %d symbols...", len(symbols))

    results = _screen(symbols, months, p, fetch=args.fetch)

    if not results:
        print("\n(No symbols produced results — try --fetch or check cache.)\n")
        return

    print(f"\nDCA screener  config={args.config}  window={months[0]}→{months[-1]}"
          f"  n_symbols={len(symbols)}  with_results={len(results)}\n")
    print(_HDR)
    print(_SEP)
    for rank, row in enumerate(results, 1):
        print(_fmt_row(rank, row))
    print(_SEP)

    top = results[: args.top]
    print(f"\n--- YAML block (top {len(top)}) — paste into {args.config} symbols: ---\n")
    print(_yaml_block(top, p))
    print()


if __name__ == "__main__":
    main()
