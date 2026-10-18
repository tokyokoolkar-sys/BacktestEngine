"""USDC-margined perp klines — the paired leg for the dual-perp dislocation test.

Same underlying, two quote currencies, two independent order books. If BTCUSDT and
BTCUSDC diverge, the spread is mechanically pinned by arbitrage: both settle against
essentially the same index. That makes it beta-neutral by construction, which is
what the three families buried today were NOT.
"""
from __future__ import annotations
import io, sys, zipfile
from pathlib import Path
import pandas as pd, requests
sys.path.insert(0, str(Path(__file__).parent.parent))
from data import months_list

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
OUT = Path(__file__).parent.parent / "cache" / "usdc"
SYMBOLS = ["BTCUSDC","ETHUSDC","SOLUSDC","XRPUSDC","DOGEUSDC","BNBUSDC","ADAUSDC","LINKUSDC","AVAXUSDC"]
COLS = ["open_time","open","high","low","close","volume","close_time","qav","trades","tbb","tbq","ignore"]

def fetch(sym, month, interval="1h"):
    r = requests.get(f"{BASE}/{sym}/{interval}/{sym}-{interval}-{month}.zip", timeout=60)
    if r.status_code == 404: return None
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        raw = zf.read(zf.namelist()[0])
    head = raw[:200].decode("utf-8", "ignore")
    df = pd.read_csv(io.BytesIO(raw), header=0 if "open_time" in head else None, names=COLS)
    df = df[["open_time","open","high","low","close","volume","trades"]].copy()
    ot = pd.to_numeric(df["open_time"], errors="coerce")
    df = df[ot.notna()]; ot = ot[ot.notna()]
    df["open_time"] = pd.to_datetime(ot, unit="us" if ot.max() > 1e14 else "ms", utc=True)
    for c in ["open","high","low","close","volume","trades"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna()

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    months = months_list("2023-01", "2026-06")
    for i, s in enumerate(SYMBOLS, 1):
        p = OUT / f"{s}-1h.parquet"
        if p.exists():
            print(f"[{i}/{len(SYMBOLS)}] cached {s}", flush=True); continue
        frames = []
        for m in months:
            try:
                d = fetch(s, m)
                if d is not None: frames.append(d)
            except Exception as e:
                print(f"   {s} {m}: {e}", flush=True)
        if frames:
            out = pd.concat(frames, ignore_index=True).sort_values("open_time")
            out = out.drop_duplicates("open_time").reset_index(drop=True)
            out.to_parquet(p, index=False)
            print(f"[{i}/{len(SYMBOLS)}] ok {s} rows={len(out)} from {out.open_time.min()}", flush=True)
        else:
            print(f"[{i}/{len(SYMBOLS)}] NO DATA {s}", flush=True)
    print("DONE", flush=True)
