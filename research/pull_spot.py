"""Spot klines — required to test the carry hedge honestly.

Carry is spot-long + perp-short. Without the spot leg I can only see the funding
income, never the basis risk that could erase it. Downloading spot separately
because data.binance.vision splits spot and futures under different roots.
"""
from __future__ import annotations
import io, sys, zipfile, logging
from pathlib import Path
import pandas as pd, requests
sys.path.insert(0, str(Path(__file__).parent.parent))
from data import months_list

SPOT_BASE = "https://data.binance.vision/data/spot/monthly/klines"
OUT = Path(__file__).parent.parent / "cache" / "spot"
SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT",
           "AVAXUSDT","LINKUSDT","DOTUSDT","LTCUSDT","ATOMUSDT","UNIUSDT",
           "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT","FILUSDT","INJUSDT","SUIUSDT","TIAUSDT"]
COLS = ["open_time","open","high","low","close","volume","close_time","qav","trades","tbb","tbq","ignore"]

def fetch(sym, month, interval="1h"):
    url = f"{SPOT_BASE}/{sym}/{interval}/{sym}-{interval}-{month}.zip"
    r = requests.get(url, timeout=60)
    if r.status_code == 404: return None
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        raw = zf.read(zf.namelist()[0])
    head = raw[:200].decode("utf-8", "ignore")
    df = pd.read_csv(io.BytesIO(raw), header=0 if "open_time" in head else None, names=COLS)
    df = df[["open_time","open","high","low","close","volume"]].copy()
    ot = pd.to_numeric(df["open_time"], errors="coerce")
    df = df[ot.notna()]
    unit = "us" if ot.max() > 1e14 else "ms"
    df["open_time"] = pd.to_datetime(ot[ot.notna()], unit=unit, utc=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna()

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    months = months_list("2023-01", "2026-06")
    for i, s in enumerate(SYMBOLS, 1):
        p = OUT / f"{s}-1h-spot.parquet"
        if p.exists():
            print(f"[{i}/{len(SYMBOLS)}] cached {s}", flush=True); continue
        frames = []
        for m in months:
            try:
                df = fetch(s, m)
                if df is not None: frames.append(df)
            except Exception as e:
                print(f"   {s} {m}: {e}", flush=True)
        if frames:
            out = pd.concat(frames, ignore_index=True).sort_values("open_time")
            out = out.drop_duplicates("open_time").reset_index(drop=True)
            out.to_parquet(p, index=False)
            print(f"[{i}/{len(SYMBOLS)}] ok {s} rows={len(out)}", flush=True)
        else:
            print(f"[{i}/{len(SYMBOLS)}] NO DATA {s}", flush=True)
    print("DONE", flush=True)
