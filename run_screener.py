"""run_screener.py — CLI entry point for the multi-symbol long-market screener.

Usage
-----
    python run_screener.py [--start YYYY-MM] [--end YYYY-MM] [--frac FLOAT]
                           [--fetch] [--symbols SYM1,SYM2,...] [--stop-grid V1,V2,...]

Defaults
--------
    --start      2025-06
    --end        2026-05
    --frac       0.7     (70 % train / 30 % validate)
    --fetch      (flag; if absent, only cached symbols are screened)
    --symbols    all tickers from symbols.SYMBOLS
    --stop-grid  0.5,1.0,1.5,2.0,3.0  (stop_pct % values; "none" is always added)

Legend
------
  train_exp  = expectancy (mean net PnL / trade, USDT) on the IN-SAMPLE training window.
  OOS_exp    = same metric on the HELD-OUT validation window — the only number that matters.
  overfit    = "***YES***" when train_exp > 0 but OOS_exp <= 0 (strategy memo-ised noise).
"""

from __future__ import annotations

import argparse
import logging
import sys

from data import months_list
from screener import format_screen, screen_universe, summarize
from symbols import SYMBOLS

# ---------------------------------------------------------------------------
# Logging — INFO to stderr so stdout is clean for piping
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

_LEGEND = (
    "\nLEGEND  train_exp = in-sample expectancy (USDT/trade) | "
    "OOS_exp = out-of-sample expectancy (USDT/trade) | "
    "OVERFIT = ***YES*** means train looked good but OOS did not.\n"
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Screen a universe of Binance USD-M perpetuals for OOS long-scalp edge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--start",
        default="2025-06",
        metavar="YYYY-MM",
        help="First month to include in the dataset (default: 2025-06).",
    )
    p.add_argument(
        "--end",
        default="2026-05",
        metavar="YYYY-MM",
        help="Last month to include in the dataset (default: 2026-05).",
    )
    p.add_argument(
        "--frac",
        type=float,
        default=0.7,
        metavar="FLOAT",
        help="Fraction of bars used for training (default: 0.7).",
    )
    p.add_argument(
        "--fetch",
        action="store_true",
        default=False,
        help="Download missing symbol data from data.binance.vision.",
    )
    p.add_argument(
        "--symbols",
        default=None,
        metavar="SYM1,SYM2,...",
        help="Comma-separated list of tickers to screen (default: all from symbols.py).",
    )
    p.add_argument(
        "--stop-grid",
        dest="stop_grid",
        default="0.5,1.0,1.5,2.0,3.0",
        metavar="V1,V2,...",
        help="Comma-separated stop_pct values to grid-search (default: 0.5,1.0,1.5,2.0,3.0).",
    )
    p.add_argument(
        "--direction",
        default="long",
        choices=["long", "short"],
        help="Trade direction: long (default) or short mirror.",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # -- Parse stop grid
    try:
        stop_grid: list[float] = [float(v.strip()) for v in args.stop_grid.split(",") if v.strip()]
    except ValueError as exc:
        parser.error(f"--stop-grid must be comma-separated numbers: {exc}")

    if not stop_grid:
        parser.error("--stop-grid produced an empty list; provide at least one value.")

    # -- Parse symbol list
    if args.symbols:
        symbols: list[str] = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            parser.error("--symbols produced an empty list.")
    else:
        symbols = list(SYMBOLS)

    # -- Build month list
    try:
        months = months_list(args.start, args.end)
    except Exception as exc:
        parser.error(f"Could not build month list from {args.start!r} to {args.end!r}: {exc}")

    if not months:
        parser.error(f"No months in range {args.start!r}..{args.end!r}.")

    # -- Guard: if not fetching, warn about symbols that have no cache
    # (screen_universe will skip them with a clear message; we just surface the flag here)
    if not args.fetch:
        print(
            "[run_screener] --fetch not set: only symbols with an existing cache will be screened.\n"
            "               Add --fetch to download missing data from data.binance.vision.\n",
            file=sys.stderr,
        )

    # -- Summary header
    print(
        f"[run_screener] Screening {len(symbols)} symbol(s)  "
        f"months={months[0]}..{months[-1]}  "
        f"frac={args.frac}  "
        f"stop_grid={stop_grid}  "
        f"fetch={args.fetch}",
        file=sys.stderr,
    )

    # -- Run
    rows = screen_universe(
        symbols=symbols,
        months=months,
        stop_grid=stop_grid,
        frac=args.frac,
        fetch=args.fetch,
        base_kwargs={"direction": args.direction},
    )

    if not rows:
        print("\n(No symbols produced results.)\n")
        print(_LEGEND)
        return

    # -- Display
    print()
    print(format_screen(rows))
    print()
    print(summarize(rows))
    print(_LEGEND)


if __name__ == "__main__":
    main()
