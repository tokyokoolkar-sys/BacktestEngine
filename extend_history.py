"""
Extend cached 1m klines + funding back to each symbol's Binance listing.

Existing cache: cache/SYM/klines.parquet covers 2025-06-01 .. 2026-05-31.
This fetches every month from FLOOR (2020-01) up to 2025-05 (pre-listing months
just 404 and are skipped), merges with the existing file, dedupes on open_time,
and rewrites the subdir parquet. Funding same, into cache/SYM-funding.parquet
and the merged cache/funding.parquet.

Idempotent-ish: re-running re-fetches missing months only if the merged file
doesn't already cover them (we skip months already present in the cache).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from data import _fetch_kline_month, _fetch_funding_month, months_list

CACHE = _HERE / "cache"
FLOOR = "2020-01"
CEIL = "2025-05"   # existing cache starts 2025-06

SYMBOLS = sorted([d.name for d in CACHE.iterdir()
                  if d.is_dir() and (d / "klines.parquet").exists()])


def existing_months(df: pd.DataFrame) -> set[str]:
    return set(df["open_time"].dt.strftime("%Y-%m").unique())


def extend_klines(symbol: str) -> None:
    path = CACHE / symbol / "klines.parquet"
    old = pd.read_parquet(path)
    if not pd.api.types.is_datetime64_any_dtype(old["open_time"]):
        old["open_time"] = pd.to_datetime(old["open_time"], utc=True)
    have = existing_months(old)

    frames = []
    fetched = 0
    for month in months_list(FLOOR, CEIL):
        if month in have:
            continue
        df = _fetch_kline_month(symbol, month, "1m")
        if df is not None:
            frames.append(df)
            fetched += 1

    if not frames:
        print(f"  [{symbol}] no new klines (listing after {CEIL} or all present)")
        return

    combined = pd.concat([old] + frames, ignore_index=True)
    combined = (combined.sort_values("open_time")
                        .drop_duplicates(subset=["open_time"])
                        .reset_index(drop=True))
    combined.to_parquet(path, index=False)
    span = (combined["open_time"].iloc[-1] - combined["open_time"].iloc[0]).days
    print(f"  [{symbol}] +{fetched} months -> {len(combined):,} bars, "
          f"{combined['open_time'].iloc[0].date()} .. {combined['open_time'].iloc[-1].date()} ({span}d)")


def extend_funding(symbol: str) -> pd.DataFrame | None:
    path = CACHE / f"{symbol}-funding.parquet"
    if path.exists():
        old = pd.read_parquet(path)
        if not pd.api.types.is_datetime64_any_dtype(old["funding_time"]):
            old["funding_time"] = pd.to_datetime(old["funding_time"], utc=True)
        have = set(old["funding_time"].dt.strftime("%Y-%m").unique())
    else:
        old = pd.DataFrame(columns=["funding_time", "funding_rate"])
        have = set()

    frames = []
    for month in months_list(FLOOR, "2026-05"):  # funding whole span
        if month in have:
            continue
        df = _fetch_funding_month(symbol, month)
        if df is not None:
            frames.append(df)
    if not frames and old.empty:
        return None
    combined = pd.concat(([old] if not old.empty else []) + frames, ignore_index=True)
    combined = (combined.sort_values("funding_time")
                        .drop_duplicates(subset=["funding_time"])
                        .reset_index(drop=True))
    combined.to_parquet(path, index=False)
    combined["symbol"] = symbol
    return combined


def main():
    print(f"Extending {len(SYMBOLS)} symbols back to {FLOOR} (listing-limited)...")
    all_funding = []
    for i, s in enumerate(SYMBOLS, 1):
        print(f"[{i}/{len(SYMBOLS)}] {s}")
        try:
            extend_klines(s)
        except Exception as e:
            print(f"  [{s}] KLINE ERR {e}")
        try:
            f = extend_funding(s)
            if f is not None:
                all_funding.append(f)
        except Exception as e:
            print(f"  [{s}] FUNDING ERR {e}")

    # rebuild merged funding.parquet (all symbols) -- engine uses this
    if all_funding:
        merged = pd.concat(all_funding, ignore_index=True)
        merged = merged[["funding_time", "funding_rate"]].sort_values("funding_time")
        merged = merged.drop_duplicates(subset=["funding_time"]).reset_index(drop=True)
        merged.to_parquet(CACHE / "funding.parquet", index=False)
        print(f"Merged funding.parquet: {len(merged):,} rows")

    print("DONE")


if __name__ == "__main__":
    main()
