"""
run.py — CLI entry point for the SYNUSDT checkpoint-scalp backtest.

Usage examples
--------------
    # Download cache then run default variants (last 6 months):
    python run.py --start 2024-12 --end 2025-05 --fetch

    # Run from existing cache:
    python run.py --start 2024-12 --end 2025-05

    # Also run a stop_pct grid sweep:
    python run.py --start 2024-12 --end 2025-05 --sweep

Three named variants always printed side-by-side:
    "bare"        — stop_mode=none, green_gate=off
    "+stop"       — stop_mode=fixed stop_pct=2.0, green_gate=off
    "+stop+gate"  — stop_mode=fixed stop_pct=2.0, green_gate=one

No network calls happen here; all IO is delegated to data.py.
No lookahead: all decisions use only closed candle data (enforced by engine.py).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure backtest/ is on the path so sibling modules resolve regardless of cwd
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import (
    download_funding,
    download_klines,
    load_funding,
    load_klines,
    months_list,
)
from engine import run_backtest
from metrics import format_scorecard, scorecard


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SYMBOL = "SYNUSDT"
DEFAULT_STOP_PCT = 2.0          # fixed stop used in "+stop" variant
DEFAULT_SWEEP_STOP_PCTS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
CACHE_DIR = _HERE / "cache"


# ---------------------------------------------------------------------------
# Variant builders
# ---------------------------------------------------------------------------

def _make_cfg(
    symbol: str,
    stop_mode: str = "none",
    stop_pct: float | None = None,
    green_gate: str = "off",
    position_model: str = "single",
    direction: str = "long",
) -> BacktestConfig:
    """Construct a BacktestConfig for one named variant."""
    return BacktestConfig(
        symbol=symbol,
        strategy=StrategyConfig(
            entry_mode="red_dip",
            green_gate=green_gate,
            stop_mode=stop_mode,
            stop_pct=stop_pct,
            checkpoint_mode="first_green",
            position_model=position_model,
            direction=direction,
        ),
        dca=DCAConfig(),
        exec=ExecConfig(),
    )


def _three_variants(symbol: str, direction: str = "long") -> list[tuple[str, BacktestConfig]]:
    """Return the three canonical named variants from PLAN.md sec 5."""
    return [
        ("bare",        _make_cfg(symbol, stop_mode="none", direction=direction)),
        ("+stop",       _make_cfg(symbol, stop_mode="fixed", stop_pct=DEFAULT_STOP_PCT, direction=direction)),
        ("+stop+gate",  _make_cfg(symbol, stop_mode="fixed", stop_pct=DEFAULT_STOP_PCT, green_gate="one", direction=direction)),
    ]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _ensure_cache(symbol: str, months: list[str], fetch: bool) -> tuple[str, str]:
    """Return (klines_path, funding_path); download if missing and fetch=True."""
    klines_path = CACHE_DIR / "klines.parquet"
    funding_path = CACHE_DIR / "funding.parquet"

    missing = not klines_path.exists() or not funding_path.exists()

    if missing and not fetch:
        print(
            "\n[ERROR] Cache files not found.\n"
            f"  klines : {klines_path}\n"
            f"  funding: {funding_path}\n"
            "Re-run with --fetch to download them first.\n"
            f"  Example: python run.py --start {months[0]} --end {months[-1]} --fetch\n"
        )
        sys.exit(1)

    if not klines_path.exists():
        download_klines(symbol, months, out_dir=CACHE_DIR)
    else:
        print(f"[run] klines cache exists: {klines_path}")

    if not funding_path.exists():
        download_funding(symbol, months, out_dir=CACHE_DIR)
    else:
        print(f"[run] funding cache exists: {funding_path}")

    return str(klines_path), str(funding_path)


# ---------------------------------------------------------------------------
# Sweep table
# ---------------------------------------------------------------------------

def _run_sweep(
    klines,
    funding,
    symbol: str,
    stop_pcts: list[float],
) -> None:
    """Run a grid of stop_pct values on the single model and print results."""
    print("\n=== Stop-% Sweep (single model, red_dip, gate=off) ===\n")
    named = []
    for sp in stop_pcts:
        cfg = _make_cfg(symbol, stop_mode="fixed", stop_pct=sp)
        result = run_backtest(klines, funding, cfg)
        sc = scorecard(result)
        label = f"stop={sp:.1f}%"
        named.append((label, sc))
    print(format_scorecard(named))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SYNUSDT checkpoint-scalp backtest CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run.py --start 2024-12 --end 2025-05 --fetch\n"
            "  python run.py --start 2024-12 --end 2025-05 --sweep\n"
        ),
    )
    parser.add_argument("--start",  default="2024-12", metavar="YYYY-MM",
                        help="First month of data (default: 2024-12)")
    parser.add_argument("--end",    default="2025-05", metavar="YYYY-MM",
                        help="Last month of data  (default: 2025-05)")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL,
                        help=f"Futures symbol (default: {DEFAULT_SYMBOL})")
    parser.add_argument("--fetch",  action="store_true",
                        help="Download cache from data.binance.vision if missing")
    parser.add_argument("--sweep",  action="store_true",
                        help="Also run a stop_pct grid sweep after the main scorecard")
    parser.add_argument("--direction", default="long", choices=["long", "short"],
                        help="Trade direction: long (default) or short mirror")

    args = parser.parse_args()

    months = months_list(args.start, args.end)
    print(f"[run] months={months[0]}..{months[-1]}  n={len(months)}  symbol={args.symbol}")

    # --- Ensure data ---
    klines_path, funding_path = _ensure_cache(args.symbol, months, args.fetch)

    klines  = load_klines(klines_path)
    funding = load_funding(funding_path)

    print(
        f"[run] klines loaded  : {len(klines):,} rows  "
        f"{klines['open_time'].iloc[0]} -> {klines['open_time'].iloc[-1]}"
    )
    print(
        f"[run] funding loaded : {len(funding):,} rows  "
        f"{funding['funding_time'].iloc[0]} -> {funding['funding_time'].iloc[-1]}"
    )

    # --- Three canonical variants ---
    variants = _three_variants(args.symbol, direction=args.direction)
    named_scores: list[tuple[str, dict]] = []
    for name, cfg in variants:
        result = run_backtest(klines, funding, cfg)
        sc = scorecard(result)
        named_scores.append((name, sc))
        print(f"[run] {name}: {sc['n_trades']} trades, net_pnl={sc['net_pnl']:+.4f}")

    print("\n=== Scorecard ===\n")
    print(format_scorecard(named_scores))

    # --- Optional sweep ---
    if args.sweep:
        _run_sweep(klines, funding, args.symbol, DEFAULT_SWEEP_STOP_PCTS)


if __name__ == "__main__":
    main()
