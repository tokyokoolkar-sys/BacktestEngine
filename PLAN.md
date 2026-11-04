# SYNUSDT Futures Checkpoint-Scalp — Backtest Plan & Strategy Spec

Status: built. All six steps in §7 are done, and the modules live at the repo root (`data.py`, `engine.py`, `strategy.py`, `metrics.py`, `run.py`), not in a `backtest/` folder. The latest scorecard is in [README.md](README.md).

## 0. The one question this exists to answer

Does the candle-color "checkpoint" strategy have **positive expectancy** on SYNUSDT 1m —
and what stop / gate settings let it survive? Stop inventing rules from memory; let data
decide. Output the numbers that settle it: win rate, avg win, avg loss, net, and **how often
the runaway (−57%-type) trade actually happens.**

## 1. Why (evidence from live trading, 2026-06-22)

- Live: SYNUSDT Perp, Long 10x, DCA bot, manual checkpointing.
- 7 trades: **5 wins / 2 losses = 71% win rate**, yet **net −$4.49**.
- Cause: negative skew. Wins were tiny (+0.02..+0.74, all `Canceled` = manually booked).
  One unmanaged trade ran to `Expired` at **−5.88 USDT / −57%** — it ate 4× every win combined.
  That trade's margin was 10.30 (the whole stack).
- Root problems:
  1. **Negative skew** — small wins, unbounded losses. Break-even win rate at this skew ≈ **87%**.
  2. **Human-as-stop-loss** — the −57% happened because manual watching lapsed (`Expired`).
     A machine stop never sleeps, hesitates, or hopes.

## 2. Data layer  (`data.py`)

- Source: **data.binance.vision** bulk dumps. (Direct `fapi.binance.com` is ISP-blocked here.)
- Pull, for SYNUSDT USDⓈ-M futures:
  - 1m klines — default last 6 months (configurable range).
  - funding rate history (8h intervals).
  - exchange filters: tick size, step size, **leverage brackets / maintenance-margin tiers**
    (needed for realistic liquidation price). *As built, these are not downloaded or cached;
    the engine computes the liquidation price from the maintenance-margin settings in `config.py`.*
- Cache everything to local parquet. No network in the hot loop.

## 3. Engine  (`engine.py`) — realistic futures sim

- Event loop, bar-by-bar, **decisions only on CLOSED candles** (no lookahead — this is the
  rule we proved is impossible to violate: you can't see the next candle at entry time).
- **Position model — test BOTH (parameter):**
  - **A. single-entry scalp** — one position per trade (the rule we designed).
  - **B. DCA basket** — base order + N safety orders at deviation steps, averaged entry
    (mirrors the live bot). This directly answers: *did averaging-down cause the −57%?*
- Costs: taker + maker fees (default 0.05% / 0.02%), **funding** paid/received per 8h held,
  optional slippage.
- Leverage + liquidation: compute liq price from maintenance margin. A forced close =
  the **runaway / `Expired` event**. Default 10x, margin ~$10 (matches live).
- Stop: hard stop at parameter distance, conservative intrabar fill.

## 4. Strategy  (`strategy.py`) — the 3-state machine

```
FLAT_ARMED      red close   -> ENTER -> IN_POSITION
                green close -> stay armed (wait for a dip)

IN_POSITION     price <= stop -> EXIT loss      -> FLAT_LOCKED
                green close   -> EXIT checkpoint -> FLAT_ARMED
                red close     -> hold

FLAT_LOCKED     green close -> FLAT_ARMED     (re-armed = all-clear)
                red close   -> skip, stay locked   (kills the bleed loop)
```

Because FLAT_ARMED is only reached *through* a green, you only ever enter the **first red
after green** — never the 3rd red of an established slide. (This is the "skip 2 reds" idea,
already baked in.)

Parameters to sweep:
- `entry_mode`: `red_dip` (mean-reversion) vs `green_confirm` (momentum — buy green after red)
- `green_gate`: off / 1 green / 2 greens / green-above-prior-high
- `stop_distance`: none / fixed-% grid / ATR-mult grid   <- the frequency-vs-severity dial
- `checkpoint_exit`: first green / +X% target / trailing
- `position_model`: single vs DCA
- `leverage`, `base_size`, DCA safety-order ladder

## 5. Scorecard  (`metrics.py`) — printed per variant, side by side

- win rate, # trades, avg win, avg loss, payoff ratio
- **net PnL, expectancy/trade**
- max drawdown, **worst single trade**, # stop hits, **# liquidations/expiries (the runaway count)**
- **break-even win rate vs actual win rate** (the gap that decides everything)
- equity curve (*as built, used for max drawdown but not printed*)

First three rows always: **bare rule | rule + stop | rule + stop + gate.**

## 6. Questions it will answer

1. How often does the runaway/liquidation actually happen? (live: 1-in-7 — typical or unlucky?)
2. Does a stop net positive, and at what distance? (where catastrophes-prevented > winners-clipped)
3. `red_dip` vs `green_confirm` — which actually has edge?
4. Does DCA averaging help, or is it what manufactures the −57%?
5. Is the real win rate above the ~87% break-even hurdle?

**Answers so far** (2026-11-04 run, SYNUSDT 2025-12 to 2026-06, single-entry model, 2% stop):
1. 73 liquidations in 77,018 bare trades, about 1 in 1,055.
2. The 2% stop removes all 73 liquidations, but net PnL stays negative.
5. No. The actual win rate is 17.5%, and the break-even win rate at the backtest's own skew is about 48.5%.

Questions 3 and 4 are not answered by the default run; they need the `green_confirm` and DCA variants.

## 7. Build order

1. `data.py` — download + cache real SYN data; verify funding + leverage brackets present.
2. `engine.py` — single-entry sim first (simplest correct core): fees, funding, liq, stop.
3. `strategy.py` — the 3-state machine + entry/exit/gate variants.
4. `metrics.py` + `run.py` — first scorecard: bare vs stop vs gate.
5. Add DCA position model — compare against single-entry.
6. Parameter sweep — locate the stop sweet spot.

## 8. Defaults chosen (correct me if wrong)

- Leverage 10x, margin ~$10 (matches live).
- Data: SYNUSDT 1m, last 6 months. (*As built, `run.py` defaults to the fixed range 2024-12 to 2025-05; pass `--start` / `--end` for another range.*)
- Fees: taker 0.05% / maker 0.02%.
- **Discipline assumption: the stop always fires** (the bot never widens it). The backtest is
  only honest if real-you commits to the same.
- DCA model B settings (from live bot UI, 2026-06-22) — COMPLETE:
  - Direction: Long
  - Price drop step (base deviation) = **0.50%**; step multiplier = **1** (constant 0.50% spacing)
  - Take Profit Per Round = **1.00%** (from weighted-average entry)
  - Max DCA Orders = **3**
  - DCA Order Size Multiplier (volume scale) = **1.1** (each safety order 1.1x the previous)
  - First DCA Order Ratio = **1:0.96** (first safety order = 0.96x base order)

## 9. Known limits

- Past data != future; a fit stop distance can decay if SYN's character changes.
- 1m candle color has near-zero predictive power alone — edge (if any) comes from the
  gate + stop + exit shaping the distribution, not from prediction.
- SYN is a thin alt: liquidity/slippage worse than majors; treat fills conservatively.
