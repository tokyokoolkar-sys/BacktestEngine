"""
test_run_cache.py — run.py must read the cache files that data.py writes.

Regression: run.py looked for cache/klines.parquet and cache/funding.parquet, but
data.download_* write cache/<SYMBOL>-1m-klines.parquet and cache/<SYMBOL>-funding.parquet.
A fresh `python run.py --fetch` downloaded everything and then crashed with
FileNotFoundError. Network is mocked; no Binance traffic.
"""

import pandas as pd

import data
import run


def _fake_months(monkeypatch):
    t = pd.to_datetime(["2026-01-01 00:00", "2026-01-01 00:01"], utc=True)
    klines = pd.DataFrame(
        {"open_time": t, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    )
    funding = pd.DataFrame({"funding_time": t[:1], "funding_rate": [0.0001]})
    monkeypatch.setattr(data, "_fetch_kline_month", lambda symbol, month, interval="1m": klines)
    monkeypatch.setattr(data, "_fetch_funding_month", lambda symbol, month: funding)


def test_fetch_then_load_uses_the_same_files(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "CACHE_DIR", tmp_path)
    _fake_months(monkeypatch)

    klines_path, funding_path = run._ensure_cache("SYNUSDT", ["2026-01"], fetch=True)

    assert len(run.load_klines(klines_path)) == 2
    assert len(run.load_funding(funding_path)) == 1


def test_second_run_hits_the_cache_without_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "CACHE_DIR", tmp_path)
    _fake_months(monkeypatch)
    run._ensure_cache("SYNUSDT", ["2026-01"], fetch=True)

    def no_network(*_args, **_kwargs):
        raise AssertionError("cache should have been reused")

    monkeypatch.setattr(data, "_fetch_kline_month", no_network)
    monkeypatch.setattr(data, "_fetch_funding_month", no_network)

    # fetch=False exits if the cache is "missing"; it must find what the first run wrote.
    klines_path, funding_path = run._ensure_cache("SYNUSDT", ["2026-01"], fetch=False)
    assert klines_path.endswith("SYNUSDT-1m-klines.parquet")
    assert funding_path.endswith("SYNUSDT-funding.parquet")
