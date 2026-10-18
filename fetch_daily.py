"""
fetch_daily.py — download klines from data.binance.vision DAILY dumps.

data.py only handles the monthly dumps, which are published after a month closes.
To evaluate the current month (a true out-of-sample window that no parameter was
tuned on) we need the daily files, which appear ~1 day after each session.

Same parse rules as data.py::_fetch_kline_month — headerless vs headered CSV is
detected, open_time is UTC, output schema is identical so load_klines() and
run_backtest() consume the parquet unchanged.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

DAILY_BASE = "https://data.binance.vision/data/futures/um/daily"
ROOT = Path(__file__).parent

_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
_KLINE_USE = ["open_time", "open", "high", "low", "close", "volume"]


def _get(url: str) -> bytes | None:
    """GET url; return bytes on 200, None on 404 (day not published yet)."""
    r = requests.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def _parse_kline_csv(csv_bytes: bytes) -> pd.DataFrame:
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


def fetch_days(symbol: str, days: list[str], interval: str = "1m") -> pd.DataFrame | None:
    """Fetch a list of 'YYYY-MM-DD' daily dumps; concat into one DataFrame."""
    frames: list[pd.DataFrame] = []
    missing: list[str] = []
    for day in days:
        url = f"{DAILY_BASE}/klines/{symbol}/{interval}/{symbol}-{interval}-{day}.zip"
        raw = _get(url)
        if raw is None:
            missing.append(day)
            continue
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            frames.append(_parse_kline_csv(zf.read(zf.namelist()[0])))
    if missing:
        logger.info("%s: %d day(s) not published: %s", symbol, len(missing), missing[:3])
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True).sort_values("open_time")
    return out.drop_duplicates(subset=["open_time"]).reset_index(drop=True)


def download_days(symbol: str, days: list[str], out_dir: str | Path,
                  interval: str = "1m") -> str | None:
    """Fetch days and write parquet named like data.py's output. Idempotent."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}-{interval}-klines.parquet"
    if path.exists():
        return str(path)
    df = fetch_days(symbol, days, interval)
    if df is None or df.empty:
        return None
    df.to_parquet(path, index=False)
    return str(path)


def day_range(start: str, end: str) -> list[str]:
    """Inclusive list of 'YYYY-MM-DD' strings."""
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="D")]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    syms = sys.argv[1].split(",") if len(sys.argv) > 1 else ["SUIUSDT"]
    start = sys.argv[2] if len(sys.argv) > 2 else "2026-07-01"
    end = sys.argv[3] if len(sys.argv) > 3 else "2026-07-26"
    outd = sys.argv[4] if len(sys.argv) > 4 else "cache/oos_july"
    days = day_range(start, end)
    for s in syms:
        p = download_days(s, days, outd)
        if p:
            d = pd.read_parquet(p)
            t = pd.to_datetime(d["open_time"])
            print(f"{s:10s} {len(d):6d} rows  {t.min()} -> {t.max()}")
        else:
            print(f"{s:10s} NO DATA")
