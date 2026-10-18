# Findings — 2026-07-27

## 1. The live strategy family is dead. Not mistuned — dead.

`research/wf_checkpoint_dca.py` runs the only test that was ever decision-relevant:
select parameters on one month, trade them the next, never look back.

```
       train         test    IS net   OOS net  OOSgross     fees  trades      WR  liq  params
    MAY 2026    JUNE 2026     +0.22    -23.46    -16.16     7.19     404  64.11%  119  tp=3.0,stop=5.0
   JUNE 2026    JULY 2026    -23.46     +5.74     +8.08     2.35     130  71.54%   30  tp=3.0,stop=5.0

  FAIL  pooled OOS net -17.72
  FAIL  1/2 folds OOS-positive
  FAIL  pooled OOS gross (pre-fee) -8.08
  WARN  149 liquidations in OOS
  VERDICT: NOT DEPLOYABLE
```

The pre-fee failure is the one that ends the argument. June's gross was **-$25.81 before
a cent of commission**. "Cut fees" / "widen the TP" / "trade less" cannot repair a signal
that is wrong at entry. Fees made a losing edge worse; they did not create the loss.

### Why July looked promising and wasn't

`exp_payoff_structure.py` on July alone reports 8 of 17 configs profitable with win rate
above break-even. Across all three months the same grid is negative in 49 of 51 cells,
and the per-config ranking does not persist:

| config | MAY | JUNE | JULY |
|---|---|---|---|
| tp=2.0 stop=3.0 | -10.44 | -41.37 | +5.79 |
| tp=3.0 stop=5.0 | +0.22 | -23.46 | +5.74 |

July was a regime, not an edge. The live account traded July's shape and still lost
money, because its parameters came from May.

### The win rate was never the problem

In **51 of 51** month-config cells the realized win rate beat the break-even win rate.
The 90%+ win rate is real and it is worthless: it is bought with a payoff ratio of about
1:10 (avg win +$0.11 vs avg loss -$1.18 live). This is the structural point — a filter
that raises win rate by shrinking the target buys nothing.

### Stop distance decides whether you get a stop at all

At `stop=5.0` on July, exits were **39 liquidations against 4 stops**. The 5% stop sits
behind the liquidation price of a 10x DCA ladder, so the exchange closes the position
before the stop can. At `stop=3.0`, liquidations are zero. Any future ladder design must
verify the stop is reachable before liquidation, not merely configured.

## 2. `greencount_auto_side` was never backtested. Now it is.

It existed **only in `bot.py`** — zero references in `config.py`, `strategy.py`, or
`engine.py`. It has been selecting the side on live money with no offline evidence.

Ported to the shared strategy (`config.py`, `strategy.py`) so backtest and live agree.
Honest result at tp=2.0/stop=3.0:

| window | fixed long | auto_side |
|---|---|---|
| MAY  | -10.44 | -23.32 |
| JUNE | -41.16 | -17.92 |
| JULY |  +5.79 |  +0.18 |

A wash, and negative everywhere. It does not rescue the family.

### Engine bug found and fixed during the port

The first auto-side run reported **+$476 gross on June**. False. `engine.py` caches
`_short = scfg.direction == "short"` once and only `dual_mode` refreshed it, so an
auto-selected short was priced with long math — producing **stop-outs that booked
profits** (`exit_reason="stop"` with `net_pnl=+0.1378`, +6.89% on margin).

Fixed by latching `_short` at the entry bar, before any price is computed
(`engine.py`, entry block). Latching there also freezes the side for the position's
life: `cfg.direction` may move again while flat, but the open trade is already priced.
Verified: zero profitable stops.

**Guard for the future:** a stop exit with positive PnL is proof of a side-mismatch bug.
Worth a permanent assertion.

## 3. Live account state

Three positions open, all correctly protected — stop triggers sit in front of the
liquidation price on every one:

| symbol | side | entry | liq | stop | TP |
|---|---|---|---|---|---|
| NEARUSDT | SHORT | 1.8064 | 1.9674 (+8.91%) | 1.8960 (+4.96%) | 1.7910 (-0.85%) |
| SUIUSDT | LONG | 0.7126 | 0.6480 (-9.06%) | 0.6806 (-4.49%) | 0.7200 (+1.04%) |
| OPUSDT | SHORT | 0.0940 | 0.1028 (+9.35%) | 0.0986 (+4.91%) | 0.0935 (-0.51%) |

Balance $71.50, available $29.88, unrealized -$2.71. Let them resolve on their existing
orders. The entry engine stays off.

### Environment gotcha

`load_dotenv()` does not override an existing environment variable. A stale 60-char
ed25519 `BINANCE_API_KEY` in `HKCU\Environment` shadows the correct HMAC key in `.env`
and yields `APIError(code=-2014): API-key format invalid`. Use `load_dotenv(override=True)`,
or clear the registry value:

```bash
reg delete "HKCU\Environment" /v BINANCE_API_KEY /f
```

Also: `futures_get_open_algo_orders()` returns a **list** on this client version, not a
dict with an `orders` key. Handle both.

## 4. The standing rule

No capital is deployed until a strategy passes `research/walkforward.py` —
selection on window N, scored on N+1, pooled OOS net **and pre-fee gross** positive, a
majority of folds positive. Every previously reported "winner" in this repo was an
in-sample grid maximum and none of them would have passed.

---

# Findings — 2026-07-27 (session 2): two more families buried

Both died the same way, and neither died from bad parameters. They died from a
question no parameter search asks: **does the mechanism still exist?**

## 5. Funding-rate cash-and-carry — expired, not overfit

Ranked #1 by three independent model-fleet agents. Mechanism is real: levered perp
longs pay funding, and a delta-neutral spot-long/perp-short collects it without
predicting direction.

First pass looked extraordinary — 98-100% win rates, +2.8% per 30-day hold. Three
traps had to be checked before that number meant anything.

**Trap 1, overlapping windows.** Sampling a 30-day hold every 8h reuses the same
data ~90x. Re-run with rebalance period == hold period (windows cannot share a bar):

```
  hold=  3.0d  INDEP n= 232  net=+0.210%  win 92.2%
  hold=  7.0d  INDEP n= 149  net=+0.546%  win 98.0%
  hold= 30.0d  INDEP n=  85  net=+1.614%  win 96.5%
```

Survived. n was inflated 10-90x but the sign held.

**Trap 2, event concentration.** 195 events across 22 symbols looked like 195 bets.
It was ~3:

| top month share of total P&L | 40.9% |
| top 3 months | **85.5%** |
| months with any event | 15 of 34 |

**Trap 3 — the one that killed it. Era split.**

| period | n | net/trade | total |
|---|---|---|---|
| 2023-01 → 2024-12 | 193 | **+0.561%** | +108% |
| 2025-01 → 2026-06 | **2** | **-0.177%** | -0.35% |

The base rate is the whole story, with no strategy attached — % of 8h intervals
with funding above 5bp:

| 2024Q1 | 2024Q4 | 2025Q1 | 2025Q2 | 2025Q3 | 2026Q1 | 2026Q2 |
|---|---|---|---|---|---|---|
| **15.29%** | 0.95% | **0.000%** | **0.000%** | **0.000%** | **0.000%** | **0.000%** |

Eighteen months, zero triggers. Mean funding is now *negative* (2025Q4: -0.27bp).
The leveraged-long crowd that paid for this is gone and the market repriced.
Lowering the threshold does not rescue it — thr=0.0002 gives 14 events in 18
months at a 21% win rate, all negative.

**The 99% win rate was true and useless.** It described a market that no longer
exists. This is a distinct failure mode from the checkpoint family: that one was
never real; this one was real and ended.

## 6. Cross-sectional momentum — market beta wearing a costume

`research/xs_momentum.py`. Rank the 22-symbol universe by trailing return, hold the
top N, rebalance = hold (non-overlapping by construction), 10bp/side costs.

20 of 20 configs showed EARLY positive and LATE negative. Never once the reverse —
which is itself the tell. A real edge does not flip sign on every single setting at
the same date.

It wasn't the strategy switching off. It was the market:

| horizon | equal-weight universe EARLY | LATE |
|---|---|---|
| 3d | +0.779% | -0.532% |
| 7d | +1.894% | -1.218% |
| 14d | +3.880% | -2.340% |

Long top-3 minus short the equal-weight universe — the actual momentum claim, with
beta removed:

```
  lb=14 h= 7 EARLY  n= 102  resid=+1.219%  t= 1.68  win=47.1%
  lb=14 h=14 EARLY  n=  51  resid=+1.651%  t= 1.27  win=56.9%
  lb= 7 h= 3 EARLY  n= 241  resid=-0.212%  t=-0.74  win=39.0%
```

**Twelve configs, not one reaches t=2.** Win rates 39-48%, i.e. below half. The
picker adds nothing over a coin-weighted basket of the same coins.

Standing caveat, stated because it cuts one way only: this universe is the 22
symbols alive TODAY, so it is survivorship-biased **upward**. A positive result
here would have been necessary but not sufficient. A negative result is decisive —
removing the bias can only make it worse.

## 7. What these two teach that walk-forward alone does not

`research/walkforward.py` selects on window N and scores on N+1. Both families
above would have produced *beautiful* walk-forward results inside 2023-2024 and
nothing but losses after. Walk-forward catches parameter overfitting. It does not
catch a mechanism that expired.

Three additions to the standing rule, each earned by a specific failure here:

1. **Split eras BEFORE looking at the aggregate.** I found carry's death only by
   splitting after a good number appeared. That ordering is luck, not method.
2. **Report the top month's share of P&L every time, pass or fail.** 85.5% in 3
   months is not 195 bets.
3. **Subtract the benchmark the strategy is secretly long.** A long-only alt
   strategy in 2023-2024 has a +200% tailwind. Show the residual or show nothing.

## 8. The 2025-2026 regime, stated plainly

Equal-weight across these 22 perps is down roughly 90% cumulative from 2025-01 to
2026-06. Funding is flat-to-negative. Any long-biased alt strategy validated on
2023-2024 data inherits a tailwind that has since reversed hard.

## 9. Residual reversal (and its mirror) — no signal in either direction

`research/residual_reversal.py`. Ranked #1 by two independent fleet agents. The claim:
a symbol that moves hard against its peers is partly forced flow, and that excess
reverses.

Market-neutral by construction — the signal IS the cross-sectional residual, so
there is no beta to strip out afterward. Non-overlapping, 4 fills costed per pair.

**32 configs. Every single one negative, both eras.** Mean -0.29% to -0.56% per
trade, which is roughly the 40bp cost plus adverse drift.

```
  form= 6h hold= 6h EARLY  n=2922  mean=-0.440%  t=-13.44  win=37.4%
  form= 6h hold= 6h LATE   n=2182  mean=-0.401%  t=-12.92  win=35.5%
  form=48h hold=24h EARLY  n= 728  mean=-0.335%  t= -2.69  win=45.1%
```

The obvious follow-up: if reversal loses, does continuation win? **No.** The mirror
trade (buy residual winners, short residual losers) is negative in all 32 cells too.

**Both directions losing is the informative result.** A sign error would show up as
one direction winning. Both losing means there is no exploitable structure at 6-48h
horizons on this universe — only the cost of four fills.

## 10. Scoreboard, three families buried in one session

| family | ranked by fleet | how it died |
|---|---|---|
| funding cash-and-carry | #1 (3 agents) | mechanism **expired** — 0 triggers in 18mo |
| cross-sectional momentum | #2 (2 agents) | **beta** — no config reaches t=2 residually |
| residual reversal | #1 (2 agents) | **no signal** — both directions negative |

Note what this says about the fleet: six frontier models, high agreement, all three
top-ranked candidates dead on this data. Consensus among reasoners is not evidence.
The mechanisms they described were real and correctly reasoned; the question of
whether the mechanism still pays is empirical and none of them could answer it.

None of these died from bad parameters. Parameter search would have found a
"winner" in every one.

## 11. USDT-vs-USDC dual-perp dislocation — killed by arithmetic, not by backtest

Ranked #1 by a fourth independent agent. Genuinely the best-designed candidate yet:
same underlying, two order books, beta-neutral by construction — immune to the
regime-dependence that killed §5 and §6.

It dies before any strategy is written, on the size of the thing being captured.
Spread = (USDC close / USDT close − 1), 1h bars, 9 pairs, 2023-01 → 2026-06:

| symbol | n | mean | sd | p99 | \|spread\|>20bp |
|---|---|---|---|---|---|
| BTC | 21,804 | −0.31bp | 5.26 | +10.68 | 0.13% |
| ETH | 21,804 | −0.17bp | 5.15 | +11.08 | 0.10% |
| SOL | 21,804 | −0.11bp | 5.50 | +14.01 | 0.39% |
| XRP | 21,804 | −0.02bp | 5.30 | +13.06 | 0.11% |

**The whole dislocation is ~5bp sd. Four fills cost 40bp taker, 8bp maker.** The
99th-percentile event is a third of the taker cost of capturing it.

Generous test — maker on all four legs, trade only the >2sd tail:

```
  H= 1h  n=8949  gross= 1.67bp
  H= 4h  n=8931  gross= 2.33bp
  H=12h  n=8875  gross= 3.29bp
  best single symbol/horizon: 5.19bp   vs 8bp maker floor
```

Fails at maker. Fails 8x at taker. Mean spread is ~0.0bp on all nine pairs, so
there is no persistent lean either.

**This is the healthy version of an efficient market:** the arbitrage that pins the
two books works, and that is exactly why nothing is left. A mechanism being real is
not the same as it being *large enough*. Measure the size of the prize before
building anything to chase it.

### Correction to an earlier assumption in this repo

`research/legibility.py` uses `MIN_NOTIONAL = 5.0` universally. **That is wrong.**
Live exchange filters:

| BTCUSDT / BTCUSDC | ETHUSDT / ETHUSDC | SOL, XRP, DOGE |
|---|---|---|
| **$50** | **$20** | $5 |

At $72 balance the paired-trade universe is 3 symbols, not 9 — and any BTC pair
needs $100 of notional across two legs before leverage. The `affordable_trades()`
count in `legibility.py` is optimistic for majors by 4-10x.

## 12. Correction to §11 — the USDC maker fee is zero, and I charged 8bp anyway

§11 killed the dual-perp trade against an "8bp maker floor". **That floor was wrong.**
Live commission rates, this account, feeTier 0, queried via `futures_commission_rate`
2026-07-27:

| symbol | maker | taker |
|---|---|---|
| BTCUSDT / SOLUSDT | +2.000bp | +5.000bp |
| BTCUSDC / ETHUSDC / SOLUSDC / XRPUSDC / DOGEUSDC | **+0.000bp** | +4.000bp |

A paired round trip is 4 fills — both legs in, both legs out — and only the USDT leg
pays maker. Real floor is `2*2bp + 2*0bp = **4bp**`, not 8bp. I doubled the cost of the
only candidate that was close to the line.

**This was not harmless.** `research/dual_perp.py`, 64 cells, non-overlapping, trailing-sd
entry with the sd shifted one bar (no lookahead):

| cost model | EARLY positive | LATE positive | both |
|---|---|---|---|
| maker-both **4bp**, no funding | **29 / 64** | 2 / 64 | 2 |
| maker-both 4bp **+ funding differential** | 25 / 64 | **0 / 64** | **0** |

At the corrected fee, nearly half the pre-2025 cells go positive. The original §11
verdict was right by accident, on arithmetic that was wrong by 2x.

### What actually kills it

**1. Zero commission is not zero cost — funding survives the waiver.** A pair is long one
book and short the other, so the *differential* is the carry. Measured on live history,
last 90 intervals:

| BTC | SOL | AVAX | BNB | XRP | DOGE |
|---|---|---|---|---|---|
| 0.227 | 0.327 | 0.293 | 0.318 | 0.393 | 0.310 |

mean |diff| bp per 8h, max observed 1.889. Over a 24h hold that is ~0.9bp against a ~1bp
prize. It takes the last 2 surviving cells negative on its own.

**2. The era signature is the momentum signature again.** 29 EARLY positive against 2 LATE
positive is not a strategy switching off — it is a dislocation that got arbitraged out as
the USDC books gained liquidity. Best LATE cell is AVAX H=24h 3sd at **+0.58bp, t=0.64,
n=110** before funding: indistinguishable from zero.

Verdict on the family is unchanged. The reasoning that produced it is now correct.

### The generalized error

"Fee-free" is a claim about *commission*, not about *cost*. On a paired trade the surviving
costs are funding differential, leg risk (a maker order that does not fill leaves a naked
10x position), and exit slippage — maker on entry is a choice, maker on exit is a hope.
Before crediting any promotional zero-fee venue, enumerate what the waiver does **not**
cover.
