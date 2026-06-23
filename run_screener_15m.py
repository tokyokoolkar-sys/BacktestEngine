"""
run_screener_15m.py — Multi-symbol 15m OOS screener.

Usage: python run_screener_15m.py [--fetch]
"""
import sys, logging
from pathlib import Path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pandas as pd
from data import months_list, download_klines, load_klines
from screener import screen_symbol, format_screen, summarize
from symbols import SYMBOLS

_ROOT = _HERE
_INTERVAL = "15m"
_MONTHS = months_list("2025-06", "2026-05")
_STOP_GRID = [0.5, 1.0, 1.5, 2.0, 3.0]

def _empty_funding():
    return pd.DataFrame({"funding_time": pd.to_datetime([], utc=True),
                         "funding_rate": pd.Series([], dtype="float64")})

fetch = "--fetch" in sys.argv or "-f" in sys.argv

funding = _empty_funding()
rows = []

print(f"[run_screener_15m] Screening {len(SYMBOLS)} symbols  interval={_INTERVAL}  months={_MONTHS[0]}..{_MONTHS[-1]}  fetch={fetch}\n")

for sym in SYMBOLS:
    cache_dir = _ROOT / "cache_15m" / sym
    parquet_path = cache_dir / "klines.parquet"

    if not parquet_path.exists():
        if not fetch:
            print(f"[screener] SKIP {sym}: no cache and no --fetch. Use --fetch to download.")
            continue
        print(f"[screener] Downloading {sym} {_INTERVAL} …", flush=True)
        try:
            download_klines(sym, _MONTHS, out_dir=str(cache_dir), interval=_INTERVAL)
        except RuntimeError as e:
            print(f"[screener] SKIP {sym}: {e}")
            continue

    try:
        klines = load_klines(parquet_path)
    except Exception as e:
        print(f"[screener] SKIP {sym}: load error: {e}")
        continue

    if klines is None or len(klines) == 0:
        print(f"[screener] SKIP {sym}: empty klines.")
        continue

    print(f"[screener] Screening {sym} ({len(klines):,} rows, {_INTERVAL}) …", flush=True)
    try:
        row = screen_symbol(sym, klines, funding, _STOP_GRID, 0.7, {"direction": "long"})
    except Exception as e:
        print(f"[screener] SKIP {sym}: screen error: {e}")
        continue
    rows.append(row)

print()
print(format_screen(rows))
print()
print(summarize(rows))
print()
