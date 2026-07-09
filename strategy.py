"""
strategy.py — 3-state checkpoint strategy for the SYNUSDT scalp backtest.

Public API
----------
CheckpointStrategy(cfg: StrategyConfig)
    .on_closed_bar(prev_row, row, in_position: bool) -> str
    .reset() -> None

Modeling assumptions
--------------------
- Candle color: green if close > open; red otherwise (close == open treated as red).
- on_closed_bar receives the PREVIOUS closed bar (prev_row) and the CURRENT closed bar
  (row). The engine calls this after each bar closes; fills happen at the NEXT bar open.
- The method signature includes in_position so the engine can override strategy state
  after a stop/liquidation fill that the strategy itself did not emit.
- State machine is fully internal; callers must not mutate state directly.
- green_gate="above_high" tracks the high of the last closed position's entry candle as
  the bar that triggered the prior-high requirement (prev_row.high at entry time).
- For entry_mode="green_confirm": the strategy arms on a red bar, then enters on the
  NEXT green bar. The red bar is remembered in _saw_red; state stays FLAT_ARMED while
  _saw_red is True; a green close while _saw_red=True emits "enter".
- No PnL, sizing, fees, or order management here — pure signal logic.
"""

from __future__ import annotations

from config import StrategyConfig


class CheckpointStrategy:
    """Stateful 3-state machine: FLAT_ARMED | IN_POSITION | FLAT_LOCKED."""

    # Internal state names
    _ARMED  = "FLAT_ARMED"
    _POS    = "IN_POSITION"
    _LOCKED = "FLAT_LOCKED"

    _VOL_WINDOW = 24  # bars, matches engine.py's entry_volume_filter rolling MA

    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg
        self._state: str = self._ARMED
        # green_gate bookkeeping
        self._green_count: int = 0       # greens seen since entering FLAT_LOCKED
        self._prior_high: float = 0.0    # high of bar at entry (for above_high gate)
        # green_confirm bookkeeping
        self._saw_red: bool = False      # red bar observed while FLAT_ARMED
        # Running market-state trackers for ema_filter / require_volume_spike.
        # Market state, not position state — survives reset()/flip_direction().
        self._ema: float | None = None
        self._vol_window: list[float] = []
        self._color_window: list[bool] = []   # recent RAW candle colors (True=green) for greencount_filter

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset all state to start-of-session defaults."""
        self._state = self._ARMED
        self._green_count = 0
        self._prior_high = 0.0
        self._saw_red = False

    def flip_direction(self) -> None:
        """Toggle between long and short (for dual_mode)."""
        self.cfg.direction = "short" if self.cfg.direction == "long" else "long"
        self.reset()

    def force_position(self, prior_high: float) -> None:
        """Put the machine into IN_POSITION for an engine-forced entry (no signal).

        Used by the drown-flip: the engine opens the opposite side directly, so the
        strategy must reflect the held position — otherwise the next bar it would
        re-enter _handle_armed while actually in a position.
        """
        self._state = self._POS
        self._saw_red = False
        self._green_count = 0
        self._prior_high = prior_high

    def on_closed_bar(
        self,
        prev_row,   # pd.Series — the bar before `row`; may be None on the very first bar
        row,        # pd.Series — the just-closed bar
        in_position: bool,
    ) -> str:
        """Process one closed bar and return the action for the engine.

        Returns one of:
            "enter"            — open a position at next bar open
            "exit_checkpoint"  — close position at next bar open (checkpoint exit)
            "hold"             — stay in position, no action
            "none"             — not in position, no action

        No-lookahead guarantee: only `row` (the closed candle) and `prev_row`
        (the previous closed candle) are used. Fills execute at the NEXT bar open.
        """
        self._update_running_state(row)

        is_green = row["close"] > row["open"]   # close == open -> red (see module docstring)
        # For short: flip the signal so the same state machine enters on green and exits on red
        if self.cfg.direction == "short":
            is_green = not is_green

        # Sync state if engine force-closed (stop/liquidation) without strategy seeing "exit"
        if not in_position and self._state == self._POS:
            self._on_forced_exit(row, is_green)
            return "none"

        if self._state == self._ARMED:
            return self._handle_armed(prev_row, row, is_green)
        elif self._state == self._POS:
            return self._handle_in_position(row, is_green)
        else:  # FLAT_LOCKED
            return self._handle_locked(row, is_green)

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _handle_armed(self, prev_row, row, is_green: bool) -> str:
        """FLAT_ARMED: waiting for entry signal."""
        cfg = self.cfg

        if cfg.entry_mode == "red_dip":
            if not is_green:
                return self._try_enter(prev_row, row)
            # Green while armed -> stay armed, reset confirm flag
            self._saw_red = False
            return "none"

        else:  # green_confirm
            if not is_green:
                # Red bar -> remember we saw it; stay armed
                self._saw_red = True
                return "none"
            else:
                # Green bar
                if self._saw_red:
                    return self._try_enter(prev_row, row)
                # Green without prior red -> stay armed
                return "none"

    def _update_running_state(self, row) -> None:
        """Update EMA / rolling-volume trackers. Called once per bar, before any gate."""
        cfg = self.cfg
        close = float(row["close"])
        if cfg.ema_filter:
            if self._ema is None:
                self._ema = close
            else:
                k = 2.0 / (cfg.ema_period + 1)
                self._ema = close * k + self._ema * (1 - k)
        if cfg.require_volume_spike:
            self._vol_window.append(float(row.get("volume", 0.0)))
            if len(self._vol_window) > self._VOL_WINDOW:
                self._vol_window.pop(0)
        if cfg.greencount_filter:
            # RAW candle color (direction-independent): green = close > open. The majority
            # over the window is a market fact; the side check happens in _try_enter.
            self._color_window.append(float(row["close"]) > float(row["open"]))
            if len(self._color_window) > cfg.greencount_period:
                self._color_window.pop(0)

    def _try_enter(self, prev_row, row) -> str:
        """Gate entry on body_pct / ema_filter / red_candle_avoid_pct / require_volume_spike."""
        cfg = self.cfg
        long_side = cfg.direction != "short"
        if cfg.body_pct > 0.0:
            o = float(row["open"]); c = float(row["close"])
            body = abs(o - c) / o * 100.0 if o > 0 else 0.0
            if body < cfg.body_pct:
                # Body too small -> stay armed, no entry
                self._saw_red = False
                return "none"
        if cfg.ema_filter:
            # Fail-closed during warmup: no confirmed trend yet, don't enter blind.
            if self._ema is None:
                self._saw_red = False
                return "none"
            close = float(row["close"])
            trend_ok = close > self._ema if long_side else close < self._ema
            if not trend_ok:
                self._saw_red = False
                return "none"
        if cfg.greencount_filter:
            # Fail-closed during warmup: need a full window of closed candles first.
            if len(self._color_window) < cfg.greencount_period:
                self._saw_red = False
                return "none"
            greens = sum(self._color_window)
            reds = len(self._color_window) - greens
            # long enters only when greens dominate, short only when reds dominate; tie skips.
            trend_ok = greens > reds if long_side else reds > greens
            if not trend_ok:
                self._saw_red = False
                return "none"
        if cfg.red_candle_avoid_pct > 0.0 and prev_row is not None:
            po = prev_row.get("open")
            pc = prev_row.get("close")
            if po is not None and pc is not None and po > 0:
                po = float(po); pc = float(pc)
                adverse_move = (po - pc) / po * 100.0 if long_side else (pc - po) / po * 100.0
                if adverse_move >= cfg.red_candle_avoid_pct:
                    self._saw_red = False
                    return "none"
        if cfg.require_volume_spike:
            # Fail-closed during warmup: can't confirm a spike without a full window.
            if len(self._vol_window) < self._VOL_WINDOW:
                self._saw_red = False
                return "none"
            avg_vol = sum(self._vol_window) / len(self._vol_window)
            cur_vol = self._vol_window[-1]
            ratio = cur_vol / avg_vol if avg_vol > 0 else 0.0
            if ratio < cfg.volume_spike_min_ratio:
                self._saw_red = False
                return "none"
        self._state = self._POS
        self._saw_red = False
        self._prior_high = prev_row["high"] if prev_row is not None else row["high"]
        return "enter"

    def _handle_in_position(self, row, is_green: bool) -> str:
        """IN_POSITION: check for checkpoint exit; engine handles stop/liq/TP."""
        cfg = self.cfg

        if cfg.checkpoint_mode == "first_green":
            if is_green:
                # Checkpoint: exit on first green close; route through FLAT_LOCKED
                # so the green_gate applies before re-arming (same as forced-exit path).
                self._green_count = 0
                self._saw_red = False
                if cfg.green_gate == "off":
                    self._state = self._ARMED
                else:
                    self._state = self._LOCKED
                return "exit_checkpoint"
            # Red -> hold; stop/liq/TP handled by engine
            return "hold"

        else:  # target mode — engine manages exit via target_pct
            # Strategy never calls the exit; always return hold
            return "hold"

    def _handle_locked(self, row, is_green: bool) -> str:
        """FLAT_LOCKED: waiting for re-arm condition (green_gate)."""
        cfg = self.cfg

        if not is_green:
            # Red while locked -> stay locked, reset green counter
            self._green_count = 0
            return "none"

        # Green candle — check gate
        self._green_count += 1

        if cfg.green_gate == "off":
            # Should not normally reach locked with gate=off; re-arm anyway
            self._to_armed()
            return "none"

        elif cfg.green_gate == "one":
            if self._green_count >= 1:
                self._to_armed()
            return "none"

        elif cfg.green_gate == "two":
            if self._green_count >= 2:
                self._to_armed()
            return "none"

        elif cfg.green_gate == "above_high":
            # Re-arm when close > prior_high captured at entry time
            if row["close"] > self._prior_high:
                self._to_armed()
            return "none"

        # Unknown gate value -> re-arm immediately (fail-open)
        self._to_armed()
        return "none"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_armed(self) -> None:
        self._state = self._ARMED
        self._green_count = 0
        self._saw_red = False

    def _on_forced_exit(self, row, is_green: bool) -> None:
        """Called when engine has already closed the position (stop/liq/TP/end).

        Determine whether to go FLAT_ARMED or FLAT_LOCKED based on green_gate.
        For gate=off: re-arm immediately.
        For all other gates: go FLAT_LOCKED and let _handle_locked advance it.
        """
        cfg = self.cfg
        self._green_count = 0
        self._saw_red = False

        if cfg.green_gate == "off":
            self._state = self._ARMED
        else:
            self._state = self._LOCKED
