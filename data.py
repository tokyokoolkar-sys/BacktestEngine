"""
data.py — download + cache SYNUSDT USD-M futures data from data.binance.vision.

Assumptions / modeling notes:
- No network calls happen outside this module.
- Kline CSV from data.binance.vision has NO header; column order is fixed per Binance spec.
- Funding CSV has a header; column names vary across history dumps:
    time col  : 'calc_time' or 'funding_time' (both are the settle ms-epoch timestamp)
    rate col  : 'last_funding_rate' or 'funding_rate'
- HTTP 404 for a missing month is normal (asset may not have traded yet); logged + skipped.
- Parquet is written only after all months are fetched, so a partial download never poisons cache.
- open_time is stored as datetime64[ns, UTC]; all downstream code must treat it as UTC.
- load_* functions return DataFrames with the index reset (integer RangeIndex); callers sort if needed.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Iterator

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE = "https://data.binance.vision/data/futures/um/monthly"
ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def months_list(start: str, end: str) -> list[str]:
    """Return list of 'YYYY-MM' strings inclusive between start and end."""
    s = pd.Period(start, freq="M")
    e = pd.Period(end, freq="M")
    return [str(p) for p in pd.period_range(s, e, freq="M")]


def _get(url: str) -> bytes | None:
    """GET url; return bytes on 200, None on 404, raise on other errors."""
    r = requests.get(url, timeout=60)
    if r.status_code == 404:
        logger.info("404 (normal) — skipped: %s", url)
        return None
    r.raise_for_status()
    return r.content


def _unzip_single(data: bytes) -> bytes:
    """Extract the sole file from a zip archive returned as bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        name = zf.namelist()[0]
        return zf.read(name)


# ---------------------------------------------------------------------------
# klines
# ---------------------------------------------------------------------------

_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
_KLINE_USE = ["open_time", "open", "high", "low", "close", "volume"]


def _fetch_kline_month(symbol: str, month: str, interval: str = "1m") -> pd.DataFrame | None:
    """Download one month of klines; return DataFrame or None on 404.

    Binance changed the dumps over time: older monthly files are headerless, newer ones
    carry a header row (open_time,open,high,...).  Detect and handle both.
    """
    url = f"{BASE}/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"
    raw = _get(url)
    if raw is None:
        return None
    csv_bytes = _unzip_single(raw)
    has_header = b"open" in csv_bytes[:64].lower()
    float_dtype = {c: "float64" for c in _KLINE_USE if c != "open_time"}
    if has_header:
        df = pd.read_csv(io.BytesIO(csv_bytes), usecols=_KLINE_USE, dtype=float_dtype)
    else:
        df = pd.read_csv(
            io.BytesIO(csv_bytes), header=None, names=_KLINE_COLS,
            usecols=_KLINE_USE, dtype=float_dtype,
        )
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def download_klines(
    symbol: str,
    months: list[str],
    out_dir: str | Path = ROOT / "cache",
    interval: str = "1m",
) -> str:
    """
    Download klines for each month; write combined parquet; return path string.

    Columns written: open_time (datetime64[ns, UTC]), open, high, low, close, volume (float64).
    Idempotent: if the parquet already exists it is returned immediately.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "klines.parquet"

    if path.exists():
        logger.info("klines cache hit: %s", path)
        return str(path)

    frames: list[pd.DataFrame] = []
    for month in months:
        logger.info("Fetching klines %s %s %s …", symbol, interval, month)
        df = _fetch_kline_month(symbol, month, interval)
        if df is not None:
            frames.append(df)

    if not frames:
        raise RuntimeError(f"No kline data fetched for {symbol} months={months}")

    combined = pd.concat(frames, ignore_index=True).sort_values("open_time")
    combined = combined.drop_duplicates(subset=["open_time"]).reset_index(drop=True)
    combined.to_parquet(path, index=False)

    print(
        f"[data] klines written: {path}\n"
        f"       rows={len(combined):,}  "
        f"from={combined['open_time'].iloc[0]}  "
        f"to={combined['open_time'].iloc[-1]}"
    )
    return str(path)


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------

def _parse_funding_df(csv_bytes: bytes) -> pd.DataFrame:
    """Parse one funding CSV; handle headered and headerless dumps."""
    first = csv_bytes[:128].split(b"\n", 1)[0].lower()
    has_header = b"time" in first or b"rate" in first or b"funding" in first
    if not has_header:
        # Headerless: col 0 = settle ms-epoch, last col = funding rate.
        raw = pd.read_csv(io.BytesIO(csv_bytes), header=None)
        return pd.DataFrame({
            "funding_time": pd.to_datetime(raw.iloc[:, 0], unit="ms", utc=True),
            "funding_rate": raw.iloc[:, -1].astype("float64"),
        })

    df = pd.read_csv(io.BytesIO(csv_bytes))
    df.columns = df.columns.str.strip()

    # normalise time column
    time_col = next(
        (c for c in df.columns if c in ("calc_time", "fundingTime", "funding_time")),
        None,
    )
    if time_col is None:
        raise ValueError(f"Cannot find time column in funding CSV; got: {list(df.columns)}")

    # normalise rate column
    rate_col = next(
        (c for c in df.columns if c in ("last_funding_rate", "funding_rate", "fundingRate", "lastFundingRate")),
        None,
    )
    if rate_col is None:
        raise ValueError(f"Cannot find rate column in funding CSV; got: {list(df.columns)}")

    out = pd.DataFrame({
        "funding_time": pd.to_datetime(df[time_col], unit="ms", utc=True),
        "funding_rate": df[rate_col].astype("float64"),
    })
    return out


def _fetch_funding_month(symbol: str, month: str) -> pd.DataFrame | None:
    """Download one month of funding rates; return DataFrame or None on 404."""
    url = f"{BASE}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"
    raw = _get(url)
    if raw is None:
        return None
    csv_bytes = _unzip_single(raw)
    return _parse_funding_df(csv_bytes)


def download_funding(
    symbol: str,
    months: list[str],
    out_dir: str | Path = ROOT / "cache",
) -> str:
    """
    Download funding rates for each month; write combined parquet; return path string.

    Columns written: funding_time (datetime64[ns, UTC]), funding_rate (float64).
    Idempotent: if the parquet already exists it is returned immediately.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "funding.parquet"

    if path.exists():
        logger.info("funding cache hit: %s", path)
        return str(path)

    frames: list[pd.DataFrame] = []
    for month in months:
        logger.info("Fetching funding %s %s …", symbol, month)
        df = _fetch_funding_month(symbol, month)
        if df is not None:
            frames.append(df)

    if not frames:
        raise RuntimeError(f"No funding data fetched for {symbol} months={months}")

    combined = pd.concat(frames, ignore_index=True).sort_values("funding_time")
    combined = combined.drop_duplicates(subset=["funding_time"]).reset_index(drop=True)
    combined.to_parquet(path, index=False)

    print(
        f"[data] funding written: {path}\n"
        f"       rows={len(combined):,}  "
        f"from={combined['funding_time'].iloc[0]}  "
        f"to={combined['funding_time'].iloc[-1]}"
    )
    return str(path)


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------

def load_klines(path: str | Path) -> pd.DataFrame:
    """
    Load klines parquet.

    Returns DataFrame with columns:
        open_time (datetime64[ns, UTC]), open, high, low, close, volume (float64).
    Sorted by open_time, RangeIndex.
    """
    df = pd.read_parquet(path)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return df


def load_funding(path: str | Path) -> pd.DataFrame:
    """
    Load funding parquet.

    Returns DataFrame with columns:
        funding_time (datetime64[ns, UTC]), funding_rate (float64).
    Sorted by funding_time, RangeIndex.
    """
    df = pd.read_parquet(path)
    df["funding_time"] = pd.to_datetime(df["funding_time"], utc=True)
    df = df.sort_values("funding_time").reset_index(drop=True)
    return df
