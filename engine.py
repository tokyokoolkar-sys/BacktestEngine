"""
engine.py — Bar-by-bar USD-M futures simulator for the SYNUSDT checkpoint-scalp backtest.

Imported by run.py as:
    from engine import run_backtest

Public API
----------
    run_backtest(klines, funding, cfg) -> Result

Modeling assumptions (every one enumerated)
-------------------------------------------
1.  NO LOOKAHEAD.  All strategy decisions use only CLOSED bars.  Fills execute at the
    NEXT bar's open price, adjusted for slippage.

2.  SLIPPAGE.  Adverse slippage = slippage_bps / 10_000 applied to fills:
    - Long entry:  fill = next_open * (1 + slip)   — pays more
    - Long exit:   fill = next_open * (1 - slip)   — receives less
    (Same for stop/liq/TP fills that happen during a bar — see point 9.)

3.  FEES.  Taker fee on every fill (entry and exit), both legs.
    fee = notional_filled * taker_fee
    (Maker fee exists in ExecConfig but is not used: all fills are assumed market orders.)

4.  FUNDING.  Long pays positive funding_rate * total_notional at each 8 h settlement
    timestamp that falls strictly inside the open position window
    (entry_time < funding_time <= exit_time).  Negative rate = long receives.

5.  WILDER ATR.  Computed with a seed (simple mean for first `atr_period` bars) then
    Wilder's smoothing: ATR_t = (ATR_{t-1} * (n-1) + TR_t) / n.
    Required for stop_mode="atr".  Pre-computed over the full klines array.

6.  SINGLE MODEL SIZING.
    notional  = margin_usdt * leverage
    size_base = notional / entry_fill_price
    Total margin deployed = margin_usdt (fixed per trade).

7.  DCA MODEL SIZING.  The complete ladder (base + max_dca_orders safety orders) must fit
    within the account margin:
        B0 * (1 + r0 + r0*m + r0*m^2 + ... ) = margin_usdt * leverage
    where r0 = first_order_ratio, m = size_multiplier.
    Solving for B0 (base notional):
        total_ratio = 1 + sum_{k=1}^{N} r0 * m^{k-1}
        B0 = margin_usdt * leverage / total_ratio
    Safety order k triggers when price <= avg_entry_k_ref * (1 - cum_drop_k / 100)
    where cum_drop_k = price_drop_step_pct * step_multiplier^0 + ... + step_multiplier^{k-1}
    (step_multiplier=1 => linear: 0.5%, 1.0%, 1.5%).
    Trigger price is referenced against the ORIGINAL base-order fill price (not avg entry).
    Safety-order fill: taker fee on each safety fill notional.
    TP exit: exit_reason = "take_profit"; price = avg_entry * (1 + take_profit_pct/100).
    Checkpoint exit also applies if checkpoint_mode="first_green".

8.  LIQUIDATION.  Long liq price:
        liq = avg_entry * (1 - 1/leverage + maint_margin_rate)
    For single model:  avg_entry = entry fill price, leverage = exec.leverage.
    For DCA model:     avg_entry updates after each safety fill; margin_at_risk is the
                       sum of all margin deployed so far (base + filled safety margins).
    If bar.low <= liq_price -> forced close at liq price (slippage applied);
    net_pnl = max(-margin_at_risk, gross_at_liq - fees - funding)  (loss capped at full margin).

9.  CONSERVATIVE INTRABAR FILL PRIORITY.
    Downside levels are crossed HIGHEST-PRICE-FIRST as a bar falls, so a stop placed ABOVE
    the liq price fills before liquidation — i.e. the stop actually protects. Logic per bar:
        - if liq hit AND no protecting stop at/above liq  -> liquidation (at liq_price)
        - elif stop hit                                   -> stop loss   (at stop_price)
        - elif take-profit hit (bar.high >= tp_price)     -> take profit (at tp_price)
    When both downside and upside trigger in one bar we assume the adverse (downside) fill.
    Intrabar fills use the TRIGGER level price (stop/liq/tp), with slippage applied.
    Checkpoint exits (strategy signal on bar close) fill at the NEXT bar open.

10. EQUITY CURVE.  Starting equity = 0.  After each closed trade, equity += net_pnl.
    Equity represents cumulative P&L (not account balance); one data point per trade.
    This makes the equity curve independent of whether margin is re-invested or flat.

11. CANDLE COLOR.  green if close > open; red otherwise (close == open -> red).
    Defined by CheckpointStrategy; the engine does not redefine it.

12. FUNDING LOOKUP.  Funding timestamps are indexed into a sorted numpy array for O(log n)
    range queries.  No funding data for a period = 0 funding cost (logged once).

13. ATR BUFFER.  The first (atr_period) bars have ATR=NaN; stop_mode="atr" on those bars
    falls back to stop_mode="none" (no stop) until ATR is valid.

14. BAR INDEXING.  The loop runs from bar index 1 onwards (bar 0 has no previous bar).
    Strategy.on_closed_bar(prev_row=bars[i-1], row=bars[i]).
    The fill bar is bars[i+1] for checkpoint/entry signals; if i+1 is out of range (last bar),
    positions are closed at bars[i].close ("end" exit reason).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import (
    BacktestConfig,
    DCAConfig,
    ExecConfig,
    Result,
    StrategyConfig,
    Trade,
)
from strategy import CheckpointStrategy


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def _compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Wilder ATR over full arrays; first `period` values are NaN."""
    n = len(high)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    if n < period:
        return atr
    # seed: simple mean of first `period` true ranges
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


# ---------------------------------------------------------------------------
# Funding helper
# ---------------------------------------------------------------------------

def _funding_in_window(
    funding_times: np.ndarray,   # int64 ns timestamps, sorted
    funding_rates: np.ndarray,   # float64
    entry_ns: int,
    exit_ns: int,
) -> float:
    """Sum funding rates for settlements strictly inside (entry_ns, exit_ns]."""
    # searchsorted: lo = first index where funding_time > entry_ns
    lo = np.searchsorted(funding_times, entry_ns, side="right")
    # hi = first index where funding_time > exit_ns
    hi = np.searchsorted(funding_times, exit_ns, side="right")
    if lo >= hi:
        return 0.0
    return float(funding_rates[lo:hi].sum())


# ---------------------------------------------------------------------------
# DCA ladder helpers
# ---------------------------------------------------------------------------

def _dca_cumulative_drops(dca: DCAConfig) -> list[float]:
    """Cumulative price-drop % triggers for each safety order k=1..max_dca_orders."""
    drops = []
    cum = 0.0
    step = dca.price_drop_step_pct
    for k in range(dca.max_dca_orders):
        cum += step * (dca.step_multiplier ** k)
        drops.append(cum)
    return drops


def _dca_safety_notionals(dca: DCAConfig, base_notional: float) -> list[float]:
    """Notional for each safety order k=1..max_dca_orders."""
    notionals = []
    prev = base_notional * dca.first_order_ratio
    for k in range(dca.max_dca_orders):
        if k == 0:
            notionals.append(prev)
        else:
            prev = notionals[-1] * dca.size_multiplier
            notionals.append(prev)
    return notionals


def _dca_base_notional(margin_usdt: float, leverage: int, dca: DCAConfig) -> float:
    """
    Compute base order notional so the full ladder fits within margin_usdt * leverage.

        total_ratio = 1 + r0 + r0*m + r0*m^2 + ...  (N safety orders)
        B0 = (margin_usdt * leverage) / total_ratio
    """
    r0 = dca.first_order_ratio
    m = dca.size_multiplier
    total_ratio = 1.0
    r = r0
    for _ in range(dca.max_dca_orders):
        total_ratio += r
        r *= m
    return (margin_usdt * leverage) / total_ratio


# ---------------------------------------------------------------------------
# Slippage helper
# ---------------------------------------------------------------------------

def _slip_entry(price: float, slip: float) -> float:
    """Long entry: fill higher (adverse)."""
    return price * (1.0 + slip)


def _slip_exit(price: float, slip: float) -> float:
    """Long exit: fill lower (adverse)."""
    return price * (1.0 - slip)


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

def run_backtest(
    klines: pd.DataFrame,
    funding: pd.DataFrame,
    cfg: BacktestConfig,
) -> Result:
    """
    Run the checkpoint-scalp backtest bar-by-bar.

    Parameters
    ----------
    klines  : DataFrame with columns open_time, open, high, low, close, volume.
              Rows sorted by open_time.  No lookahead: bar[i] decisions fill at bar[i+1].
    funding : DataFrame with columns funding_time, funding_rate.
              Sorted by funding_time.
    cfg     : BacktestConfig (see config.py).

    Returns
    -------
    Result with trades list and equity_curve.
    """
    scfg: StrategyConfig = cfg.strategy
    ecfg: ExecConfig = cfg.exec
    dcfg: DCAConfig = cfg.dca

    _short = scfg.direction == "short"
    slip = ecfg.slippage_bps / 10_000.0

    # --- Pre-compute ATR ---
    highs  = klines["high"].to_numpy(dtype=np.float64)
    lows   = klines["low"].to_numpy(dtype=np.float64)
    closes = klines["close"].to_numpy(dtype=np.float64)
    opens  = klines["open"].to_numpy(dtype=np.float64)
    times  = klines["open_time"].to_numpy(dtype="int64")  # ns since epoch
    ts     = klines["open_time"].to_list()                # list[pd.Timestamp] for O(1) access

    atr_arr = _compute_atr(highs, lows, closes, scfg.atr_period)

    # --- Pre-index funding ---
    fund_times = funding["funding_time"].to_numpy(dtype="int64")
    fund_rates = funding["funding_rate"].to_numpy(dtype=np.float64)

    # --- DCA ladder pre-compute ---
    cum_drops = _dca_cumulative_drops(dcfg)  # list[float], len = max_dca_orders

    # --- Strategy state machine ---
    strategy = CheckpointStrategy(scfg)

    # --- Output accumulators ---
    trades: list[Trade] = []
    equity_curve: list[tuple[pd.Timestamp, float]] = []
    running_equity = 0.0

    # --- Position state ---
    in_position = False

    # single & dca shared
    entry_time: pd.Timestamp | None = None
    entry_price: float = 0.0      # fill price of base order
    avg_entry: float = 0.0        # weighted-average entry (updated after each DCA fill)
    size_base: float = 0.0        # total base-asset position size
    total_notional: float = 0.0   # total notional deployed
    total_margin: float = 0.0     # total margin at risk
    total_fees: float = 0.0       # cumulative entry fees
    stop_price: float | None = None
    liq_price: float = 0.0
    tp_price: float | None = None

    # DCA-specific state
    base_notional: float = 0.0
    safety_notionals: list[float] = []
    safety_triggers: list[float] = []   # abs price levels
    next_safety_idx: int = 0            # index into safety orders (0-based)
    n_safety_filled: int = 0

    n = len(klines)

    for i in range(1, n):
        prev_row = {"high": highs[i - 1]}
        row = {"open": opens[i], "close": closes[i], "high": highs[i]}

        # ------------------------------------------------------------------
        # 1. Intrabar checks for OPEN positions (using current bar i)
        # ------------------------------------------------------------------
        if in_position:
            bar_low  = lows[i]
            bar_high = highs[i]
            bar_open = opens[i]

            # a. DCA safety order fills (check before liq/stop — they change avg_entry)
            if scfg.position_model == "dca" and next_safety_idx < dcfg.max_dca_orders:
                while next_safety_idx < dcfg.max_dca_orders:
                    trig = safety_triggers[next_safety_idx]
                    if (bar_high >= trig if _short else bar_low <= trig):
                        # Safety order fills at trigger price (adversely: we add INTO a move)
                        saf_fill = _slip_exit(trig, slip) if _short else _slip_entry(trig, slip)
                        saf_notional = safety_notionals[next_safety_idx]
                        saf_size = saf_notional / saf_fill
                        saf_fee = saf_notional * ecfg.taker_fee
                        saf_margin = saf_notional / ecfg.leverage

                        # Update weighted average entry
                        new_total_notional = total_notional + saf_notional
                        avg_entry = (avg_entry * size_base + saf_fill * saf_size) / (size_base + saf_size)
                        size_base += saf_size
                        total_notional = new_total_notional
                        total_margin += saf_margin
                        total_fees += saf_fee
                        n_safety_filled += 1
                        next_safety_idx += 1

                        # Recompute liq after each safety fill
                        if _short:
                            liq_price = avg_entry * (1.0 + 1.0 / ecfg.leverage - ecfg.maint_margin_rate)
                        else:
                            liq_price = avg_entry * (1.0 - 1.0 / ecfg.leverage + ecfg.maint_margin_rate)

                        # TP price updates with new avg_entry (direction-aware)
                        if scfg.position_model == "dca":
                            tp_price = avg_entry * (1.0 - dcfg.take_profit_pct / 100.0) if _short else avg_entry * (1.0 + dcfg.take_profit_pct / 100.0)

                        # Recompute stop after each safety fill (mirrors live bot _replace_tp)
                        stop_price = _compute_stop(avg_entry, atr_arr[i], scfg)
                    else:
                        break  # triggers are in order; no need to check further

            # Exit priority (assumption 9): downside levels cross highest-price-first, so a stop
            # above the liq price fills before liquidation (the stop protects).
            closed_trade: Trade | None = None
            _stop_active = scfg.stop_mode != "none" and stop_price is not None
            if _short:
                _stop_hit = _stop_active and bar_high >= stop_price
                _liq_hit = bar_high >= liq_price
            else:
                _stop_hit = _stop_active and bar_low <= stop_price
                _liq_hit = bar_low <= liq_price

            # Priority: highest-price-first on downside (long) / lowest-price-first on upside (short)
            _stop_protects = (_stop_hit and (stop_price <= liq_price if _short else stop_price >= liq_price))
            if _liq_hit and not _stop_protects:
                # Liquidation — forced close at liq price (slippage applied).
                # Realistic mark-to-liq loss, floored at full margin loss (can't lose more than margin).
                liq_fill = _slip_entry(liq_price, slip) if _short else _slip_exit(liq_price, slip)
                gross = (avg_entry - liq_fill if _short else liq_fill - avg_entry) * size_base
                exit_fee = total_notional * ecfg.taker_fee
                _rate_sum = _funding_in_window(fund_times, fund_rates, entry_time.value, times[i])
                fund_paid = (-_rate_sum if _short else _rate_sum) * total_notional
                net = max(-total_margin, gross - (total_fees + exit_fee) - fund_paid)
                closed_trade = Trade(
                    entry_time=entry_time,
                    exit_time=ts[i],
                    entry_price=entry_price,
                    exit_price=liq_fill,
                    size_base=size_base,
                    gross_pnl=gross,
                    fees=total_fees + exit_fee,
                    funding=fund_paid,
                    net_pnl=net,
                    pnl_pct=net / ecfg.margin_usdt,
                    exit_reason="liquidation",
                    n_safety_filled=n_safety_filled,
                )

            elif _stop_hit:
                # c. Stop loss
                stop_fill = _slip_entry(stop_price, slip) if _short else _slip_exit(stop_price, slip)
                gross = (avg_entry - stop_fill if _short else stop_fill - avg_entry) * size_base
                exit_fee = total_notional * ecfg.taker_fee
                _rate_sum = _funding_in_window(fund_times, fund_rates, entry_time.value, times[i])
                fund_paid = (-_rate_sum if _short else _rate_sum) * total_notional
                net = gross - (total_fees + exit_fee) - fund_paid
                closed_trade = Trade(
                    entry_time=entry_time,
                    exit_time=ts[i],
                    entry_price=entry_price,
                    exit_price=stop_fill,
                    size_base=size_base,
                    gross_pnl=gross,
                    fees=total_fees + exit_fee,
                    funding=fund_paid,
                    net_pnl=net,
                    pnl_pct=net / ecfg.margin_usdt,
                    exit_reason="stop",
                    n_safety_filled=n_safety_filled,
                )

            elif tp_price is not None and (bar_low <= tp_price if _short else bar_high >= tp_price):
                # d. Take profit (only if liq/stop not hit first)
                tp_fill = _slip_entry(tp_price, slip) if _short else _slip_exit(tp_price, slip)
                gross = (avg_entry - tp_fill if _short else tp_fill - avg_entry) * size_base
                exit_fee = total_notional * ecfg.taker_fee
                _rate_sum = _funding_in_window(fund_times, fund_rates, entry_time.value, times[i])
                fund_paid = (-_rate_sum if _short else _rate_sum) * total_notional
                net = gross - (total_fees + exit_fee) - fund_paid
                closed_trade = Trade(
                    entry_time=entry_time,
                    exit_time=ts[i],
                    entry_price=entry_price,
                    exit_price=tp_fill,
                    size_base=size_base,
                    gross_pnl=gross,
                    fees=total_fees + exit_fee,
                    funding=fund_paid,
                    net_pnl=net,
                    pnl_pct=net / ecfg.margin_usdt,
                    exit_reason="take_profit",
                    n_safety_filled=n_safety_filled,
                )

            if closed_trade is not None:
                trades.append(closed_trade)
                running_equity += closed_trade.net_pnl
                equity_curve.append((closed_trade.exit_time, running_equity))
                in_position = False
                # Strategy syncs its state on the NEXT call to on_closed_bar via the
                # guard: `if not in_position and self._state == self._POS` — no explicit
                # call needed here; we skip the rest of this iteration.
                continue  # skip strategy signal for this bar (forced exit owns the bar)

        # ------------------------------------------------------------------
        # 2. Strategy signal on closed bar (row = bar i)
        # ------------------------------------------------------------------
        action = strategy.on_closed_bar(prev_row, row, in_position=in_position)

        # ------------------------------------------------------------------
        # 3. Act on strategy signal at NEXT bar open (bar i+1)
        # ------------------------------------------------------------------
        if action == "exit_checkpoint" and in_position:
            # Fill at next bar open (bar i+1) if available, else bar i close
            if i + 1 < n:
                _raw = opens[i + 1]
                fill_time  = ts[i + 1]
                fill_time_ns = times[i + 1]
            else:
                _raw = closes[i]
                fill_time  = ts[i]
                fill_time_ns = times[i]
            fill_price = _slip_entry(_raw, slip) if _short else _slip_exit(_raw, slip)

            gross = (avg_entry - fill_price if _short else fill_price - avg_entry) * size_base
            exit_fee = total_notional * ecfg.taker_fee
            _rate_sum = _funding_in_window(fund_times, fund_rates, entry_time.value, fill_time_ns)
            fund_paid = (-_rate_sum if _short else _rate_sum) * total_notional
            net = gross - (total_fees + exit_fee) - fund_paid

            t = Trade(
                entry_time=entry_time,
                exit_time=fill_time,
                entry_price=entry_price,
                exit_price=fill_price,
                size_base=size_base,
                gross_pnl=gross,
                fees=total_fees + exit_fee,
                funding=fund_paid,
                net_pnl=net,
                pnl_pct=net / ecfg.margin_usdt,
                exit_reason="checkpoint",
                n_safety_filled=n_safety_filled,
            )
            trades.append(t)
            running_equity += t.net_pnl
            equity_curve.append((t.exit_time, running_equity))
            in_position = False

        elif action == "enter" and not in_position:
            # Fill at next bar open (bar i+1) if available
            if i + 1 < n:
                fill_price = _slip_exit(opens[i + 1], slip) if _short else _slip_entry(opens[i + 1], slip)
                fill_time  = ts[i + 1]
            else:
                # Can't enter on the very last bar — skip
                continue

            entry_time  = fill_time
            entry_price = fill_price
            avg_entry   = fill_price

            if scfg.position_model == "single":
                notional    = ecfg.margin_usdt * ecfg.leverage
                sz          = notional / fill_price
                entry_fee   = notional * ecfg.taker_fee
                size_base   = sz
                total_notional = notional
                total_margin   = ecfg.margin_usdt
                total_fees     = entry_fee
                tp_price       = None  # single model uses checkpoint/stop only

            else:  # dca
                base_notional = _dca_base_notional(ecfg.margin_usdt, ecfg.leverage, dcfg)
                sz          = base_notional / fill_price
                entry_fee   = base_notional * ecfg.taker_fee
                size_base   = sz
                total_notional  = base_notional
                total_margin    = base_notional / ecfg.leverage
                total_fees      = entry_fee
                safety_notionals = _dca_safety_notionals(dcfg, base_notional)
                # Safety trigger prices relative to base fill price (original entry)
                if _short:
                    safety_triggers = [fill_price * (1.0 + d / 100.0) for d in cum_drops]
                else:
                    safety_triggers = [fill_price * (1.0 - d / 100.0) for d in cum_drops]
                next_safety_idx  = 0
                n_safety_filled  = 0
                tp_price = avg_entry * (1.0 - dcfg.take_profit_pct / 100.0 if _short else 1.0 + dcfg.take_profit_pct / 100.0)

            # Compute stop price
            stop_price = _compute_stop(fill_price, atr_arr[i], scfg)

            # Compute liq price (initial; updated after DCA fills)
            if _short:
                liq_price = avg_entry * (1.0 + 1.0 / ecfg.leverage - ecfg.maint_margin_rate)
            else:
                liq_price = avg_entry * (1.0 - 1.0 / ecfg.leverage + ecfg.maint_margin_rate)

            in_position = True

    # ------------------------------------------------------------------
    # 4. End-of-data: close any open position at last bar close
    # ------------------------------------------------------------------
    if in_position:
        last_i = n - 1
        fill_price = _slip_entry(closes[last_i], slip) if _short else _slip_exit(closes[last_i], slip)
        fill_time  = ts[last_i]
        fill_time_ns = times[last_i]

        gross = (avg_entry - fill_price if _short else fill_price - avg_entry) * size_base
        exit_fee = total_notional * ecfg.taker_fee
        _rate_sum = _funding_in_window(fund_times, fund_rates, entry_time.value, fill_time_ns)
        fund_paid = (-_rate_sum if _short else _rate_sum) * total_notional
        net = gross - (total_fees + exit_fee) - fund_paid

        t = Trade(
            entry_time=entry_time,
            exit_time=fill_time,
            entry_price=entry_price,
            exit_price=fill_price,
            size_base=size_base,
            gross_pnl=gross,
            fees=total_fees + exit_fee,
            funding=fund_paid,
            net_pnl=net,
            pnl_pct=net / ecfg.margin_usdt,
            exit_reason="end",
            n_safety_filled=n_safety_filled,
        )
        trades.append(t)
        running_equity += t.net_pnl
        equity_curve.append((t.exit_time, running_equity))

    return Result(trades=trades, equity_curve=equity_curve, cfg=cfg)


# ---------------------------------------------------------------------------
# Helpers (module-level since closures in hot loops are slower)
# ---------------------------------------------------------------------------

def _compute_stop(fill_price: float, atr: float, scfg: StrategyConfig) -> float | None:
    """Return stop price or None if stop is disabled / ATR not yet valid."""
    if scfg.stop_mode == "none":
        return None
    _short = scfg.direction == "short"
    if scfg.stop_mode == "fixed":
        if scfg.stop_pct is None:
            return None
        return fill_price * (1.0 + scfg.stop_pct / 100.0) if _short else fill_price * (1.0 - scfg.stop_pct / 100.0)
    if scfg.stop_mode == "atr":
        if scfg.stop_atr_mult is None or np.isnan(atr):
            return None  # assumption 13: no stop until ATR valid
        return fill_price + scfg.stop_atr_mult * atr if _short else fill_price - scfg.stop_atr_mult * atr
    return None


