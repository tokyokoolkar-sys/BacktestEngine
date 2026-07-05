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

    stop_pct: Optional[float] = None          # used when stop_mode="fixed"
    stop_atr_mult: Optional[float] = None     # used when stop_mode="atr"
    atr_period: int = 14                      # Wilder ATR look-back

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
