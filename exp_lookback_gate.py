"""
exp_lookback_gate.py — BACKWARD lookback entry gate. The one honest, tradeable edge.

Idea (user's): don't wait after the lock (that pays a delay tax = the whole predictive
edge; see exp_delayed_fill.py, Delay=-863 OOS). Instead, at the lock bar read the N PRIOR
CLOSED candles -> greencount majority -> pick direction -> enter at the engine's immediate
fill (lock+1 open). No delay tax, no lookahead (past candles only), engine's own net_pnl
(self-check-faithful in exp_delayed_fill.py).

Result (stop=2%, DCA, 7 sym, both books, greencount majority over N prior 1h candles):
  Blind(both books) = -81 OOS/H2.  Lookback N=10 = +290 OOS/H2, +285 H1, +575 all.
  Per-symbol: H2 7/7 positive, H1 6/7. Turns losing-blind symbols (FIL/OP) positive.
  N sweep peaks ~10-12 (N=8 +251, N=10 +290, N=12 +288, N=16 +273, N=24 +159).
  netmove variant is noisier (N=3/5 flip negative); greencount is the robust one.

First robust honest edge in the project. Beats EMA10/25 lagged gate (+126) ~2x.
Still modest: +290 / ~1yr / 7 sym / ~20k baskets. Next: single combined book (no
independent-basket double-count), live occupancy conflicts, N=10 lock into strategy.py.

    python exp_lookback_gate.py
"""
import sys; sys.path.insert(0,".")
import numpy as np, pandas as pd
from data import load_funding, load_klines
from engine import run_backtest
import exp_delayed_fill as X
SYMS=X.SYMS

def greenverdict(o,c,i,N):
    a=i-N+1
    if a<0: return 0,0
    g=int((c[a:i+1]>o[a:i+1]).sum()); r=N-g
    return (1 if g>r else -1 if r>g else 0), g

def run(split,N):
    Blind=0.0; G=0.0; nl=ns=0; total_locks=0; kept=0
    for s in SYMS:
        kraw=load_klines(str(X.CACHE/s/"klines.parquet"))
        f=load_funding(str(X.CACHE/f"{s}-funding.parquet"))
        k1h=X.resample(kraw,"1h")
        if split!="all":
            mid=len(k1h)//2
            k1h=(k1h.iloc[:mid] if split=="H1" else k1h.iloc[mid:]).reset_index(drop=True)
        o=k1h["open"].values;c=k1h["close"].values
        kns=X.ns(k1h["open_time"].values); nbar=len(k1h)
        for direction,want in (("long",1),("short",-1)):
            trs=run_backtest(k1h,f,X.cfg(direction)).trades
            Blind+=sum(t.net_pnl for t in trs)
            for tr in trs:
                pos=int(np.searchsorted(kns,tr.entry_time.value,side="left")); i=pos-1
                if i<0 or i>=nbar: continue
                total_locks+=1
                v,_=greenverdict(o,c,i,N)
                if v==want:
                    G+=tr.net_pnl; kept+=1
                    if want==1: nl+=1
                    else: ns+=1
    skip=(1-kept/total_locks)*100 if total_locks else 0
    return Blind,G,kept,nl,ns,skip

print("greencount lookback — N sweep, OOS(H2) + all")
for split in ("H2","all"):
    print(f"\n=== split={split} ===")
    print(f"{'N':>3} | {'Blind':>9} {'Lookback':>10} {'kept':>6} {'long':>6} {'short':>6} {'skip%':>6}")
    for N in (5,8,10,12,16,20,24):
        Bl,G,k,nl,nsh,skip=run(split,N)
        print(f"{N:>3} | {Bl:>+9.2f} {G:>+10.2f} {k:>6} {nl:>6} {nsh:>6} {skip:>5.1f}")
