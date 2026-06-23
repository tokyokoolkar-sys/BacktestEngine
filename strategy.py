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

    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg
        self._state: str = self._ARMED
        # green_gate bookkeeping
        self._green_count: int = 0       # greens seen since entering FLAT_LOCKED
        self._prior_high: float = 0.0    # high of bar at entry (for above_high gate)
        # green_confirm bookkeeping
        self._saw_red: bool = False      # red bar observed while FLAT_ARMED

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset all state to start-of-session defaults."""
        self._state = self._ARMED
        self._green_count = 0
        self._prior_high = 0.0
        self._saw_red = False

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
                # Red close while armed -> enter long
                self._state = self._POS
                self._saw_red = False
                if prev_row is not None:
                    self._prior_high = prev_row["high"]
                else:
                    self._prior_high = row["high"]
                return "enter"
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
                    # Green after red -> enter
                    self._state = self._POS
                    self._saw_red = False
                    if prev_row is not None:
                        self._prior_high = prev_row["high"]
                    else:
                        self._prior_high = row["high"]
                    return "enter"
                # Green without prior red -> stay armed
                return "none"

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
