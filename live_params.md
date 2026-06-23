# 1h DCA Scalp — Live Trading Parameters

## Universal Settings (all symbols)

| Field | Value |
|-------|-------|
| Timeframe | **1h** |
| Direction | **Long** |
| Entry mode | **Red candle close** (enter on red, arm on green) |
| Position model | **DCA** |
| Price drop step | **0.50%** |
| Step multiplier | **1.0** (linear spacing) |
| Size multiplier | **1.1** (each safety order 1.1x previous) |
| First order ratio | **0.96** (first safety = 0.96x base) |
| Checkpoint exit | **Disabled** (DCA holds for TP; don't exit on first green) |
| Leverage | **10x** |
| Margin per trade | **$10** (adjust to risk tolerance) |
| Taker fee | 0.05% (assumed market orders) |
| Slippage | 2 bps |

> **Stop-loss: Optional.** DCA structure + TP exit already caps downside to ~$1.36 worst trade at 3% stop. Running without stop improves expectancy. Use 3% fixed if you want zero liquidation risk.

## Optimized Per-Symbol Parameters (OOS-validated)

> **Refreshed 2026-06-24 on live data through 2026-06-23 22:00 UTC.**
> Window: 2025-06-01 00:00 → 2026-06-23 22:00 UTC (9,311 1h bars/symbol). June 2026 tail
> fetched live via Binance `fapi/v1/klines` (data.binance.vision monthly dump for 2026-06
> not yet published). Grid: TP% {0.3,0.5,0.8,1.0,1.5} × DCA orders {3,5,7}, 70/30 train/OOS
> split. Rows ranked by OOS expectancy ($/trade).

These are the **best single config** found via grid search per symbol, validated on the
held-out 30% (OOS = 2026-02-27 → 2026-06-23).

```
SYMBOL     TP(%)   DCA Orders   Stop(%)   OOS Exp($/trade)   WR(%)   Net($)
──────     ─────   ──────────   ───────   ────────────────   ─────   ──────
INJUSDT     0.8        3          none          +0.149       79.5   +101.07
NEARUSDT    1.0        5*         none          +0.148       80.3   +101.06
TIAUSDT     0.5        3          none          +0.119       91.1    +82.06
OPUSDT      0.5        3          none          +0.111       90.7    +77.87
APTUSDT     0.5        3          none          +0.110       87.3    +75.53
DOTUSDT     0.5        3          none          +0.100       85.9    +68.11
UNIUSDT     0.5        3          none          +0.100       81.9    +68.38
FILUSDT     0.5        3          none          +0.099       89.7    +68.18
ARBUSDT     0.5        3          none          +0.098       89.0    +66.83
SUIUSDT     0.5        3          none          +0.085       84.5    +58.81
AAVEUSDT    0.5        3          none          +0.065       81.6    +43.93
ETCUSDT     0.5        3          none          +0.063       74.6    +43.33
LINKUSDT    0.5        3          none          +0.061       73.3    +42.33
ADAUSDT     0.5        3          none          +0.057       79.6    +39.35
SOLUSDT     0.5        3          none          +0.056       72.8    +38.42
AVAXUSDT    0.5        3          none          +0.053       73.2    +35.50
DOGEUSDT    0.5        5          none          +0.052       73.9    +36.33
ETHUSDT     0.5        3          none          +0.048       68.3    +33.78
ATOMUSDT    0.5        3          none          +0.048       78.5    +32.65
XRPUSDT     0.3        3          none          +0.033       76.8    +22.46
BCHUSDT     1.0        7          none          +0.019       57.9    +14.41
LTCUSDT     0.3        3          none          +0.018       73.7    +12.67
BTCUSDT     0.3        5          none          +0.012       67.4     +7.94
BNBUSDT     0.3        3          none          +0.011       66.4     +7.45
TRXUSDT     — no edge — best OOS config still negative (-0.009, WR 35.3%)
```

> **\* NEAR DCA=5 is NOT achievable with the current live bot.** `bot_config.yaml` has no
> per-symbol DCA setting — `max_dca_orders` is GLOBAL (currently 2 = 3 total orders). NEAR's
> fresh best config (DCA=5) cannot be applied in isolation. To run NEAR at its optimum you
> would have to raise the global DCA count for ALL symbols, which changes every other symbol's
> behavior. With the live global DCA=3-orders constraint, NEAR's best in-grid config is
> TP=0.8% / DCA=3 (the next-best point) — re-screen at fixed DCA=3 before committing size.

## Bot Configuration Template

### For DCA bots (e.g. 3Commas, Cryptohopper, Bitsgap, custom)

**Base order:** $10 @ 10x = $100 notional
**Safety orders:** 2 more at 0.5% steps
  - Safety 1: $9.60 notional @ -0.5% from base fill
  - Safety 2: $10.56 notional @ -1.0% from base fill
**Take profit:** Per-symbol TP% above weighted-average entry
**Max active DCA baskets:** 3-5 (don't over-concentrate)
**Re-entry:** After TP fills, re-arm immediately (gate=off)

### Important live caveats

1. **Funding costs are real.** 1h hold time is well under the 8h funding window — you'll pay ~0.01% per trade held past 8h. Most DCA trades resolve within 1-4h.

2. **1h candle signal ≠ trade every hour.** The strategy enters ~1.5-2.5x/day per symbol on 1h. With 20+ symbols active expect 30-50 entries/day.

3. **Correlation risk.** Altcoins move together. During a broad selloff many DCA baskets trigger simultaneously. Cap concurrent positions at 5-7 total.

4. **Start small.** Run top 5 symbols (INJUSDT, NEARUSDT, TIAUSDT, OPUSDT, APTUSDT) at $10 margin each for a week, then scale. (Refreshed 2026-06-24 on live data through 2026-06-23; FILUSDT dropped out of the top 5 — now #8 — and APTUSDT moved in. NEAR's listed DCA=5 optimum is not reachable under the global DCA setting — see the per-symbol table footnote.)

5. **Past data ≠ future.** The backtest confirms the mechanism works across 25 symbols for Jun 2025-May 2026. No guarantees going forward.

## Expected metrics (per trade, per symbol)

- Win rate: 65-90% depending on symbol
- Avg win: +$0.04 to +$0.15
- Avg loss: -$0.30 to -$0.50 (capped by DCA structure)
- Drawdown: expect -20-30% on a bad week if alts tank
- Max consecutive losses in backtest: 5-8
