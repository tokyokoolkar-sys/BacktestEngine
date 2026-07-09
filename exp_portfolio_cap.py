"""Portfolio cap: max_concurrent_baskets is GLOBAL across all symbols (live reality).
Merge every symbol's greencount-gated locks into ONE chronological timeline; at most K
baskets open at once across the whole portfolio. Basket PnL via faithful simulate_basket.

Sweeps K. K=1/2 = live capital-constrained; larger K approaches per-symbol single-book."""
import sys; sys.path.insert(0,".")
import numpy as np, pandas as pd
from config import DCAConfig, ExecConfig
from data import load_funding, load_klines
from engine import run_backtest
import exp_delayed_fill as X
SYMS=X.SYMS; N=10

def gv(o,c,i,N):
    a=i-N+1
    if a<0: return 0
    g=int((c[a:i+1]>o[a:i+1]).sum())
    return 1 if g>N-g else -1 if N-g>g else 0

def build_events(split):
    """All greencount locks across all symbols as (entry_ns, symkey, pnl, exit_ns).
    Precompute each basket's pnl+exit once; the K-cap just decides which we KEEP."""
    ev=[]
    dcfg,ecfg=DCAConfig(),ExecConfig(); slip=ecfg.slippage_bps/1e4
    for s in SYMS:
        kraw=load_klines(str(X.CACHE/s/"klines.parquet"))
        f=load_funding(str(X.CACHE/f"{s}-funding.parquet"))
        k1h=X.resample(kraw,"1h")
        if split!="all":
            mid=len(k1h)//2
            k1h=(k1h.iloc[:mid] if split=="H1" else k1h.iloc[mid:]).reset_index(drop=True)
        o=k1h["open"].values;h=k1h["high"].values;l=k1h["low"].values;c=k1h["close"].values
        kns=X.ns(k1h["open_time"].values); nbar=len(k1h)
        ft=f["funding_time"].to_numpy("int64");fr=f["funding_rate"].to_numpy(float)
        scfgL=X.cfg("long").strategy; scfgS=X.cfg("short").strategy
        # candidate locks = union of engine long/short lock bars
        locks=set()
        for d in ("long","short"):
            for tr in run_backtest(k1h,f,X.cfg(d)).trades:
                pos=int(np.searchsorted(kns,tr.entry_time.value,side="left")); i=pos-1
                if 0<=i<nbar: locks.add(i)
        for i in sorted(locks):
            v=gv(o,c,i,N)
            if v==0: continue
            ei=i+1
            if ei>=nbar: continue
            direction="long" if v==1 else "short"; scfg=scfgL if v==1 else scfgS
            entry_px=o[ei]*(1.0+slip) if v==1 else o[ei]*(1.0-slip)
            pnl,_,reason,exj=X.simulate_basket(entry_px,kns[ei],direction,o,h,l,c,kns,ft,fr,dcfg,ecfg,scfg)
            ev.append((int(kns[ei]), s, pnl, int(kns[exj])))
    ev.sort(key=lambda e:e[0])
    return ev

def apply_cap(ev,K):
    """Walk events in time order; keep at most K concurrent (by exit_ns). Returns (pnl,n,skipped)."""
    active=[]  # list of exit_ns
    tot=0.0; n=0; sk=0
    for entry_ns,s,pnl,exit_ns in ev:
        active=[x for x in active if x>entry_ns]  # free finished baskets
        if len(active)<K:
            tot+=pnl; n+=1; active.append(exit_ns)
        else:
            sk+=1
    return tot,n,sk

for split in ("H2","all"):
    ev=build_events(split)
    full=sum(e[2] for e in ev)  # K=inf
    print(f"\n=== split={split}  greencount N={N}  portfolio cap sweep ===")
    print(f"  {'K':>4} | {'PnL':>9} {'baskets':>8} {'skipped':>8}  {'vs K=inf':>8}")
    for K in (1,2,3,5,8,10**9):
        tot,n,sk=apply_cap(ev,K)
        klabel="inf" if K>10**6 else str(K)
        print(f"  {klabel:>4} | {tot:>+9.1f} {n:>8} {sk:>8}  {tot/full*100 if full else 0:>7.0f}%")
