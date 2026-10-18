"""Shared dataclass contracts for the SYNUSDT checkpoint-scalp backtest.

All other modules import from here.  No logic — pure data definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class DCAConfig:
    """DCA safety-order ladder settings (mirrors live bot, 2026-06-22)."""
    price_drop_step_pct: float = 0.5      # base deviation % between safety orders
    step_multiplier: float = 1.0          # multiplies step each order (1.0 = linear)
    size_multiplier: float = 1.1          # each safety order notional * this vs previous
    max_dca_orders: int = 3               # max safety orders after base
    first_order_ratio: float = 0.96       # first safety size = base_notional * this
    take_profit_pct: float = 1.0          # TP % above weighted-avg entry
    # ATR-adaptive ladder spacing. 0.0 = off (fixed price_drop_step_pct).
    # >0 — per-entry step% = clamp(atr_ladder_mult * ATR14/close * 100,
    #      atr_step_min_pct, atr_step_max_pct). ATR from CLOSED bars only.
    # Widens the ladder in volatile tape so it doesn't exhaust and liquidate
    # (May-2026 OOS: liqs 15 -> 4, net +8.5 -> +23.1 at mult=0.75).
    atr_ladder_mult: float = 0.0
    atr_step_min_pct: float = 0.2
    atr_step_max_pct: float = 1.0


@dataclass
class StrategyConfig:
    """Controls the 3-state checkpoint strategy and entry/exit variants."""
    entry_mode: str = "red_dip"
    # "red_dip"      — enter on red candle while FLAT_ARMED
    # "green_confirm"— enter on green that follows a red

    green_gate: str = "off"
    # "off"          — re-arm immediately after exit
    # "one"          — need 1 green before re-arming
    # "two"          — need 2 greens before re-arming
    # "above_high"   — need green that closes above prior-high before re-arming

    stop_mode: str = "none"
    # "none"         — no hard stop
    # "fixed"        — stop at entry * (1 - stop_pct/100)
    # "atr"          — stop at entry - stop_atr_mult * ATR(atr_period)
    # "trail"        — trailing stop: activates at avg_entry*(1+trail_activate_pct/100),
    #                  stop = highest_price*(1-trail_buffer_pct/100)

    stop_pct: Optional[float] = None          # used when stop_mode="fixed"
    stop_atr_mult: Optional[float] = None     # used when stop_mode="atr"
    atr_period: int = 14                      # Wilder ATR look-back
    trail_activate_pct: float = 0.75          # used when stop_mode="trail"
    trail_buffer_pct: float = 0.45            # used when stop_mode="trail"

    checkpoint_mode: str = "first_green"
    # "first_green"  — exit position on first green candle close
    # "target"       — hold until target_pct gain; engine owns the exit

    target_pct: Optional[float] = None        # used when checkpoint_mode="target"

    position_model: str = "single"
    # "single"       — one fixed-size position per trade
    # "dca"          — base order + DCA safety orders (uses DCAConfig)

    direction: str = "long"
    # "long"         — enter on red close, exit on green close
    # "short"        — enter on green close, exit on red close (mirror)

    dual_mode: bool = False
    # False          — trade in `direction` only
    # True           — alternate between long and short on each new entry
    #                 (first entry uses `direction`, then flips each trade)

    drown_flip: bool = False
    # False          — no drown-flip
    # True           — while in position, if price moves drown_flip_pct % against
    #                 avg_entry, force-close and open the opposite side (rescue flip)

    drown_flip_pct: Optional[float] = None
    # Raw price move % from avg_entry that triggers a flip. Long flips when
    # low <= avg_entry*(1 - pct/100); short when high >= avg_entry*(1 + pct/100).
    # Must be < liq distance (100*(1/lev - maint)) or liquidation fires first.

    drown_flip_cooldown_bars: int = 5
    # Min bars between flips; guards against oscillation/fee-bleed in chop.

    entry_volume_filter: bool = False
    # False          — no volume check
    # True           — skip entry if current bar volume > entry_volume_max_ratio × 24h MA

    entry_volume_max_ratio: float = 2.5
    # Volume ratio threshold. Entry skipped when vol / 24h_MA_vol >= this value.

    body_pct: float = 0.0
    # Min candle body size (% of open) to allow entry. 0 = off (no body gate).
    # long:  body = (open - close) / open * 100  (red candle)
    # short: body = (close - open) / open * 100  (green candle)
    # Entry skipped when body < body_pct.

    ema_filter: bool = False
    # False — no EMA trend gate
    # True  — long: require close > EMA(ema_period); short: require close < EMA(ema_period)
    # Fail-closed during warmup (fewer than ema_period bars seen).

    ema_period: int = 20

    greencount_filter: bool = False
    # False — no green-count lookback gate.
    # True  — at the entry bar, read the last `greencount_period` CLOSED candles and take
    #         the majority color as the trend: long enters only if greens > reds, short
    #         only if reds > greens (ties skip). No lookahead — only closed candles up to
    #         and including the current bar. Fail-closed during warmup (fewer than
    #         greencount_period candles seen). Backtested honest edge: N=10, stop=2%,
    #         Blind -81 -> +290 OOS/H2, 7/7 symbols positive (see exp_lookback_gate.py).

    greencount_period: int = 10

    greencount_auto_side: bool = False
    # False — `direction` is fixed for the whole run.
    # True  — while FLAT, the majority color of the last `greencount_period` CLOSED
    #         candles PICKS the side instead of merely gating it: greens > reds -> long,
    #         reds > greens -> short, tie -> skip the bar. Warmup fails closed.
    #
    #         This is a SIDE-SELECTION rule, not a filter, so it supersedes
    #         greencount_filter — with auto_side on, the majority check can never
    #         reject an entry (the side is chosen to agree with it), which is why
    #         bot.py passes greencount_filter=(filter and not auto_side).
    #
    #         Direction only ever changes while flat; flipping mid-position would
    #         desync the state machine from the open trade.
    #
    #         Ported from bot.py (live-only until 2026-07-27) so it can finally be
    #         backtested — it was running on real money with no offline evidence.

    red_candle_avoid_pct: float = 0.0
    # 0 = off. Otherwise skip entry if the previous closed bar moved against the
    # trade direction by >= this %: long checks (prev_open-prev_close)/prev_open*100,
    # short checks the mirrored green-candle move.

    require_volume_spike: bool = False
    # False — no volume-spike requirement
    # True  — require current bar volume >= volume_spike_min_ratio * rolling 24-bar avg.
    # Fail-closed during warmup (fewer than 24 bars of volume history).

    volume_spike_min_ratio: float = 2.5


@dataclass
class ExecConfig:
    """Execution and cost parameters."""
    leverage: int = 10
    margin_usdt: float = 10.0          # per-trade margin stake
    taker_fee: float = 0.0005          # 0.05 %
    maker_fee: float = 0.0002          # 0.02 %
    slippage_bps: float = 2.0          # adverse slippage in basis points on fills
    maint_margin_rate: float = 0.05    # maintenance-margin rate (5 %)


@dataclass
class Trade:
    """Record of one closed position.

    pnl_pct is expressed as a fraction of margin_usdt (not notional).
    n_safety_filled counts safety orders triggered (0 for single model).
    exit_reason is one of: "checkpoint" | "stop" | "liquidation" |
                           "take_profit" | "end"
    """
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    size_base: float        # total base-asset size held at exit
    gross_pnl: float        # PnL before fees and funding
    fees: float             # total fees paid (all entry + exit fills), positive = cost
    funding: float          # net funding paid (positive = paid by long)
    net_pnl: float          # gross_pnl - fees - funding
    pnl_pct: float          # net_pnl / margin_usdt
    exit_reason: str
    n_safety_filled: int = 0


@dataclass
class BacktestConfig:
    """Top-level config passed to engine.run_backtest."""
    symbol: str = "SYNUSDT"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    dca: DCAConfig = field(default_factory=DCAConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)


@dataclass
class Result:
    """Output of engine.run_backtest."""
    trades: list[Trade]
    equity_curve: list[tuple[pd.Timestamp, float]]   # (timestamp, equity_after_trade)
    cfg: BacktestConfig
