# PORTFOLIO.md — Endgame Architecture (the standing blueprint)

Vision: one repo, one treasury, many isolated compounders. High-velocity engines
throw off cash; cash migrates down the risk ladder; income skims off the top.
Built incrementally — each tier activates at its milestone, never before.

## The Tiers

Every bot is a separate process, separate config, separate ledger line, and
(from Tier 2 on) a separate Binance sub-account so a blown tier CANNOT touch
another tier's capital. No bot ever reads another bot's balance.

### Tier 0 — Treasury (`treasury.py`, build at $500)
Not a bot. The ledger + router. Runs weekly:
1. Read every tier's equity + week PnL.
2. Owner skim per phase rules (PORTFOLIO phases below).
3. Overflow above each tier's cap → route down to Tier 2.
4. Refill Tier 3 satellite budget (never above its cap).
5. Append one immutable ledger row per movement (CSV, append-only).
All movements are Binance internal transfers — deterministic rules, no AI in
the money path. Claude maintains the rules; the rules move the money.

### Tier 1 — Income Engine: DCA scalp fleet  [LIVE today]
- What: `bot.py` + flip_20_config.yaml — checkpoint DCA, greencount auto-side,
  ATR ladder, 6-symbol fleet, 10x isolated.
- Job: fastest measured compounder. The paycheck printer.
- Cap: ~$10-20k equity (order-book impact on 1h alt scalps beyond that).
- Overflow above cap → Tier 2 weekly.
- Maintenance: monthly re-rank of the 10-symbol pool (first: Aug 1, 2026);
  swap any symbol negative both directions on the fresh month.
- Kill rule: two consecutive weeks of live results outside the backtest
  envelope (cadence or PnL) → halt, re-validate before restart.

### Tier 2 — Wealth Store: long-run spot book  [build at $500]
- What: `longrun/` — spot DCA + quarterly rebalance. BTC/ETH core; Binance
  tokenized stocks / index tokens added at Phase 4 (same API key — confirmed).
- Job: the reservoir. Where surviving money becomes irreplaceable money.
- Rules: NO leverage, NO timing, NO edge-hunting, NO withdrawals except
  owner skim at Phase 3+. Allocation weights are backtested (growth-optimal
  mix is allowed); *activity* is not. It buys on schedule and holds.
- Cap: none. This is the tier that scales to millions.

### Tier 3 — Satellite: moonshot book  [pilot at Phase 4, ~$5k+ total equity]
- What: `satellite/` — positive-skew, high-variance strategies. Candidates:
  prediction markets (research/calibration edge — our strength), SOL sniper
  (infra edge — requires the pilot to prove it).
- Hard cap: 5% of TOTAL portfolio equity. Breach → treasury sweeps excess to
  Tier 2 immediately. Wins do not raise the cap; they fund Tier 2.
- Funding: ONLY from Tier 1 profits. Never from Tier 2. A zeroed satellite
  never touches the reservoir.
- Every candidate strategy gets the same trial Tier 1 got:
  fixed pilot bankroll → 200+ logged attempts → measured EV after all costs
  (rugs, fees, slippage, infra) → positive scales within cap, negative dies.
  No strategy enters on narrative. Sniper included: pilot $1-2k, private-RPC
  infra costed in, deployer-rug rate measured, verdict from data.
- Sniper runs TWO stages:
  Stage 1 "observatory" (free, can start pre-$5k): log every new pair for
  2-3 months, NO trades. Per launch: deployer wallet age/funding source,
  LP lock/burn, mint+freeze authority, holder concentration, whale wallets
  organic vs deployer-funded (funding-graph clustering — rug crews recycle
  wallets), and labeled outcome (rug type / survivor peak). Output = training
  dataset for the entry filter.
  Stage 2 pilot ($1-2k at ~$5k equity): trade only what the trained filter
  passes; 200 logged attempts; EV verdict after all costs.
  The observatory never shuts down: it keeps running under any live sniper
  as its kill-switch — tell-pattern drift means the filter is stale, pause.
- Kill rule: satellite loses its full budget → tier goes dark until next
  quarter's refill. No mid-quarter top-ups. Ever.

### Owner income (the point of all this)
- Phase 0  (<$250 total):        skim nothing.
- Phase 1  ($250):               withdraw initial $87 — house money forever.
- Phase 2  ($500-2.5k):          25% of green-week profit.
- Phase 3  ($2.5k+):             50% of green-week profit.
- Phase 4  (Tier 1 at cap):      100% of Tier 1 profit above cap, minus
                                  satellite refill. Tier 2 untouched — it only
                                  pays out via its own Phase-5 dividend rule
                                  (define when we get there).
- Never skim in a red week. Percentages, never fixed dollars.

## Build order (trigger-gated, not calendar-gated)
1. NOW        — Tier 1 runs. Wait. Discipline is the current job.
2. $500       — treasury.py + Tier 2 spot book (BTC/ETH, Binance-only).
3. Tier 1 cap — Tier 2 adds tokenized stocks/index; Phase 4 skims begin.
4. ~$5k+      — Tier 3 pilot #1 (prediction markets first: measurable, no
                infra spend). Sniper pilot second, infra costs included.
5. Millions   — same machine, bigger ledger numbers. Structure doesn't change;
                that's the entire point of the structure.

## Invariants (survive every rewrite)
- One tier's failure can never cascade: sub-account isolation + no cross-reads.
- Money flows DOWN the risk ladder only (T1→T2, T3 refill from T1). Never up.
- Every strategy earns its allocation with measured live/backtest EV. No
  exceptions for exciting ideas — excitement is anti-correlated with edge.
- AI (Claude) writes rules, re-ranks fleets, backtests allocations, reviews
  monthly. AI never sits in the per-trade or per-transfer hot path.
- The backtest engine (`engine.py`) stays the single source of truth for any
  strategy claiming an edge on candles. New venues get adapters, not forks.
