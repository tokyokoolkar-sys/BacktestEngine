"""Pull multi-year 1h klines for a broad symbol set into cache/hist/.

1h not 1m: the whole point of this research direction is LOW turnover. A 1m
bar count over 3 years is ~1.6M rows/symbol and encourages exactly the
high-frequency scalping that just failed on sample-size grounds. 1h gives
~26k rows/symbol, downloads in a fraction of the time, and matches the
holding period we can actually measure.
"""
from __future__ import annotations
import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)
from data import download_klines, download_funding, months_list

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT",
    "AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT","LTCUSDT","ATOMUSDT","UNIUSDT",
    "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT","FILUSDT","INJUSDT","SUIUSDT","TIAUSDT",
]
MONTHS = months_list("2023-01", "2026-06")
OUT = Path(__file__).parent.parent / "cache" / "hist"

if __name__ == "__main__":
    for i, s in enumerate(SYMBOLS, 1):
        try:
            download_klines(s, MONTHS, out_dir=OUT, interval="1h")
            print(f"[{i}/{len(SYMBOLS)}] klines ok {s}", flush=True)
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] klines FAIL {s}: {e}", flush=True)
        try:
            download_funding(s, MONTHS, out_dir=OUT)
            print(f"[{i}/{len(SYMBOLS)}] funding ok {s}", flush=True)
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] funding FAIL {s}: {e}", flush=True)
    print("DONE", flush=True)
