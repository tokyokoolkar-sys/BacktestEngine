"""
sweep_drown_flip.py — does flipping a drowning long to short beat long-only?

Rule under test (config.StrategyConfig.drown_flip): while in a DCA long, if price
falls drown_flip_pct % below avg_entry, force-close and open a short. Symmetric —
the short flips back to long if IT drowns. Guarded by drown_flip_cooldown_bars.

This runs the SAME engine.run_backtest as run.py (DCA model), so fills/fees/funding/
liq are identical to every other backtest — no hand-rolled math. Compares long-only
vs drown-flip side by side; --grid sweeps drown_flip_pct.

Usage:
    python sweep_drown_flip.py --symbol SYNUSDT --start 2024-12 --end 2025-05
    python sweep_drown_flip.py --symbol SYNUSDT --start 2024-12 --end 2025-05 --grid
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Windows consoles default to cp1252 and choke on non-ASCII; force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from data import load_funding, load_klines, months_list
from engine import run_backtest
from metrics import format_scorecard, scorecard

CACHE_DIR = _HERE / "cache"
DEFAULT_FLIP_PCT = 2.5
GRID_FLIP_PCTS = [1.5, 2.0, 2.5, 3.0]


def resample_1h(k):
    """1m klines -> 1h OHLCV so the strategy fires on the live 1h cadence."""
    import pandas as pd
    df = k.set_index("open_time")
    out = pd.DataFrame({
        "open_time": df["open"].resample("1h").first().index,
        "open": df["open"].resample("1h").first().values,
        "high": df["high"].resample("1h").max().values,
        "low": df["low"].resample("1h").min().values,
        "close": df["close"].resample("1h").last().values,
    })
    if "volume" in df.columns:
        out["volume"] = df["volume"].resample("1h").sum().values
    return out.dropna().reset_index(drop=True)


def _cfg(symbol: str, drown_flip: bool, flip_pct: float | None = None) -> BacktestConfig:
    return BacktestConfig(
        symbol=symbol,
        strategy=StrategyConfig(
            entry_mode="red_dip",
            green_gate="off",
            stop_mode="none",
            checkpoint_mode="first_green",
            position_model="dca",
            direction="long",
            drown_flip=drown_flip,
            drown_flip_pct=flip_pct,
        ),
        dca=DCAConfig(),
        exec=ExecConfig(),
    )


def _liq_distance_pct(ex: ExecConfig) -> float:
    """Adverse price move % that hits liquidation — a flip_pct >= this never fires."""
    return 100.0 * (1.0 / ex.leverage - ex.maint_margin_rate)


def main() -> None:
    ap = argparse.ArgumentParser(description="drown-flip vs long-only DCA comparison")
    ap.add_argument("--symbol", default="SYNUSDT")
    ap.add_argument("--start", default="2024-12", metavar="YYYY-MM")
    ap.add_argument("--end", default="2025-05", metavar="YYYY-MM")
    ap.add_argument("--grid", action="store_true",
                    help="sweep drown_flip_pct instead of a single value")
    ap.add_argument("--tf", default="1h", choices=["1m", "1h"],
                    help="strategy timeframe: 1h (live cadence, default) or 1m (raw)")
    args = ap.parse_args()

    months = months_list(args.start, args.end)
    # Cache layout is per-symbol: cache/<SYM>/klines.parquet + cache/<SYM>-funding.parquet.
    # Fall back to the flat run.py layout (cache/klines.parquet) if that's what exists.
    kp = CACHE_DIR / args.symbol / "klines.parquet"
    fp = CACHE_DIR / f"{args.symbol}-funding.parquet"
    if not kp.exists():
        kp = CACHE_DIR / "klines.parquet"
        fp = CACHE_DIR / "funding.parquet"
    if not kp.exists():
        print(f"[ERROR] no klines cache for {args.symbol} "
              f"(looked in cache/{args.symbol}/ and cache/). Run run.py --fetch first.")
        sys.exit(1)
    if not fp.exists():
        print(f"[warn] no funding cache for {args.symbol} — running with zero funding")
        fp = None

    klines = load_klines(str(kp))
    if fp is not None:
        funding = load_funding(str(fp))
    else:
        import pandas as pd
        funding = pd.DataFrame({"funding_time": pd.Series([], dtype="int64"),
                                "funding_rate": pd.Series([], dtype=float)})

    raw_bars = len(klines)
    if args.tf == "1h":
        klines = resample_1h(klines)
    print(f"[sweep] {args.symbol}  months={months[0]}..{months[-1]}  "
          f"tf={args.tf}  {raw_bars:,} raw -> {len(klines):,} bars")

    liq_dist = _liq_distance_pct(ExecConfig())
    print(f"[sweep] liq distance ~ {liq_dist:.2f}% - a drown_flip_pct at/above this "
          f"can't fire (liquidation hits first)\n")

    named: list[tuple[str, dict]] = []
    named.append(("long-only", scorecard(run_backtest(klines, funding, _cfg(args.symbol, False)))))

    flip_pcts = GRID_FLIP_PCTS if args.grid else [DEFAULT_FLIP_PCT]
    for pct in flip_pcts:
        if pct >= liq_dist:
            print(f"[warn] drown_flip_pct={pct} >= liq distance {liq_dist:.2f}% — skipping (can't fire)")
            continue
        sc = scorecard(run_backtest(klines, funding, _cfg(args.symbol, True, pct)))
        named.append((f"flip@{pct}%", sc))

    print("\n=== drown-flip vs long-only (DCA) ===\n")
    print(format_scorecard(named))


if __name__ == "__main__":
    main()
