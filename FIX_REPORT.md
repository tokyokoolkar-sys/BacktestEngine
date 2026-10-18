# GreenCount limitation-fix report

## Fixed

- Added real side-aware live execution: short entries/safety orders use SELL; short TP/stop/trailing exits use BUY.
- Added side-aware DCA price ladders: long ladders below entry, short ladders above entry.
- Added side-aware TP, stop, trailing-stop, realized-PnL, startup reconciliation, order classification, and safety-ladder recovery.
- Basket direction is frozen at entry so later strategy changes cannot corrupt open-position management.
- Added `greencount_auto_side`: on each closed 1h candle, the last N closed candles select long when green-majority or short when red-majority; ties and warmup fail closed.
- Removed the need for duplicate long/short rows. One row per symbol now supports sequential GreenCount-selected sides.
- Changed Binance minimum-notional handling to skip an entry when it would inflate configured margin by more than 5%, rather than silently exceeding the risk budget.

## Validation

- Python compilation and AST validation passed.
- Static audit confirms there are no remaining hard-coded BUY/SELL order sides in `bot.py`.
- Existing backtest engine synthetic long/short sanity checks had passed previously.

## Remaining limitations

- Uploaded caches are Apache Parquet, but this sandbox lacks pyarrow/fastparquet and cannot install packages because outbound package access is blocked. I therefore did not fabricate a fresh sweep result.
- Maintenance margin remains a coarse constant rather than Binance symbol/notional brackets.
- Candle bars cannot model spread, latency, queue position, partial fills, or exact same-bar sequencing.
- The patch assumes Binance one-way mode: GreenCount selects one side per symbol and never holds both simultaneously. Hedge-mode simultaneous long+short is intentionally not implemented.

## Safety

Use dry-run and Binance testnet first. Short execution is financially sensitive; verify emitted sides and trigger prices before real orders.
