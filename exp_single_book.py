"""Single combined book: ONE position at a time per symbol. At each lock the greencount
lookback picks the side; a basket runs to completion before the next lock can open.
Honest occupancy — no independent-basket double-count. Uses simulate_basket (faithful).

Compares, OOS(H2) + all:
  Blind_1book  : both-book locks merged, greencount OFF (side = whatever the lock was)... 
                 no — blind has no side choice. Instead: Independent (old +290) vs 1book.
  Indep        : long-book + short-book run independently, greencount-gated (the +290 number)
  1book        : single book, greencount picks side, one-at-a-time occupancy
"""
import sys; sys.path.insert(0,".")
import numpy as np, pandas as pd
from config import DCAConfig, ExecConfig
from data import load_funding, load_klines
from engine import run_backtest
import exp_delayed_fill as X
SYMS=X.SYMS; N=10; STOP=2.0

def gv(o,c,i,N):
    a=i-N+1
    if a<0: return 0
    g=int((c[a:i+1]>o[a:i+1]).sum())
    return 1 if g>N-g else -1 if N-g>g else 0

def lockbars(k1h,f):
    """Union of long+short engine lock bars = candidate entry points (bar index i)."""
    kns=X.ns(k1h["open_time"].values); nbar=len(k1h); locks=set()
    for d in ("long","short"):
        for tr in run_backtest(k1h,f,X.cfg(d)).trades:
            pos=int(np.searchsorted(kns,tr.entry_time.value,side="left")); i=pos-1
            if 0<=i<nbar: locks.add(i)
    return sorted(locks)

def run(split):
    tot_ind=tot_1b=0.0; n_ind=n_1b=n_skip_occ=0
    per=[]
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
        dcfg,ecfg=DCAConfig(),ExecConfig()
        scfgL=X.cfg("long").strategy; scfgS=X.cfg("short").strategy

        # --- Independent (the +290 method): every lock whose verdict has a side, run it, ignore occupancy
        ind=0.0
        # --- 1book: chronological, one position at a time
        one=0.0; busy_until=-1
        for i in lockbars(k1h,f):
            v=gv(o,c,i,N)
            if v==0: continue
            direction="long" if v==1 else "short"
            scfg=scfgL if v==1 else scfgS
            # entry at immediate fill = open of bar i+1 (engine cadence)
            ei=i+1
            if ei>=nbar: continue
            slip=ecfg.slippage_bps/1e4
            entry_px=o[ei]*(1.0+slip) if v==1 else o[ei]*(1.0-slip)
            pnl,_,reason,exj=X.simulate_basket(entry_px,kns[ei],direction,o,h,l,c,kns,ft,fr,dcfg,ecfg,scfg)
            ind+=pnl; n_ind+=1
            if i>busy_until:
                one+=pnl; n_1b+=1; busy_until=exj
            else:
                n_skip_occ+=1
        tot_ind+=ind; tot_1b+=one
        per.append((s,ind,one))
    return tot_ind,tot_1b,n_ind,n_1b,n_skip_occ,per

for split in ("H2","all"):
    ti,t1,ni,n1,nsk,per=run(split)
    print(f"\n=== split={split}  greencount N={N}  stop={STOP}% ===")
    for s,ind,one in per:
        print(f"  {s:>9}  Indep {ind:>+8.1f}   1book {one:>+8.1f}")
    print(f"  TOTAL      Indep {ti:>+8.1f}   1book {t1:>+8.1f}")
    print(f"  baskets: indep {ni}  1book {n1}  skipped(occupied) {nsk}   retention {t1/ti*100 if ti else 0:.0f}%")
