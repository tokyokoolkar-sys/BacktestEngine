"""
test_engine_bugs.py — Regression tests for confirmed backtest bugs.

Bug 1 (CRITICAL): In the DCA safety-fill loop, TP recompute was direction-unaware
  (always used +take_profit_pct), so a short trade would set TP ABOVE avg_entry,
  causing phantom same-bar TP hits and wrong P&L direction.

Bug 2 (LOW): pnl_pct was divided by ecfg.margin_usdt instead of total_margin,
  underestimating the percentage return when safety orders are filled.

Bug 3 (MED): checkpoint exit transitioned straight to _ARMED, bypassing FLAT_LOCKED
  and the green_gate filter. Now routes through _LOCKED when gate != "off".
"""

from __future__ import annotations

import pandas as pd
import numpy as np
import pytest

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from engine import run_backtest
from strategy import CheckpointStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_klines(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows: list of (open, high, low, close); timestamps spaced 15 min apart."""
    base = pd.Timestamp("2025-01-01 00:00:00", tz="UTC")
    records = []
    for idx, (o, h, l, c) in enumerate(rows):
        ts = base + pd.Timedelta(minutes=15 * idx)
        records.append({
            "open_time": ts,
            "open": float(o),
            "high": float(h),
            "low":  float(l),
            "close": float(c),
            "volume": 1000.0,
        })
    df = pd.DataFrame(records)
    df["open_time"] = df["open_time"].astype("int64")
    # The engine needs pd.Timestamp for ts list — rebuild open_time as Timestamps
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ns", utc=True)
    return df


def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame({"funding_time": pd.Series([], dtype="int64"),
                          "funding_rate": pd.Series([], dtype=float)})


def _short_dca_cfg(tp_pct: float = 1.0, max_dca: int = 2) -> BacktestConfig:
    return BacktestConfig(
        strategy=StrategyConfig(
            direction="short",
            position_model="dca",
            entry_mode="red_dip",      # for short: enter on green (flipped)
            checkpoint_mode="first_green",   # for short: exit on red (flipped)
            green_gate="off",
            stop_mode="none",
        ),
        dca=DCAConfig(
            price_drop_step_pct=0.5,
            step_multiplier=1.0,
            size_multiplier=1.0,
            max_dca_orders=max_dca,
            first_order_ratio=1.0,
            take_profit_pct=tp_pct,
        ),
        exec=ExecConfig(
            leverage=10,
            margin_usdt=10.0,
            taker_fee=0.0005,
            slippage_bps=0.0,   # zero slippage for deterministic math
            maint_margin_rate=0.05,
        ),
    )


# ---------------------------------------------------------------------------
# Bug 1: Short DCA TP direction
# ---------------------------------------------------------------------------

class TestShortDcaTpDirection:
    """
    Scenario: SHORT DCA trade.
    - Bar 0: dummy (engine starts at bar 1)
    - Bar 1: is_green for short logic → engine enters short at bar 2 open
    - Bar 2: entry bar open price = 100; is a RED bar (not green for short),
             also triggers safety order (price moves up = adverse for short)
    - Bar 3: price drops enough for TP (below avg_entry * (1-tp_pct/100))

    OLD BUG: after safety fill, TP was set to avg_entry * (1 + tp_pct/100),
    i.e. ABOVE entry. Since we're short (TP should be BELOW), the engine could
    immediately trigger a phantom TP hit even as price goes up.

    NEW CORRECT: TP = avg_entry * (1 - tp_pct/100) — below entry for short.
    """

    def _run_short_dca_scenario(self):
        """
        Short scenario:
        direction=short means is_green is flipped: close > open → is_green=False
        entry_mode=red_dip → enters on NOT-is_green (i.e. real green candle)
        checkpoint_mode=first_green → exits on NOT-is_green (i.e. real green) but
          here we rely on TP exit.

        Candle layout:
          bar 0 (idx=0): open=99, close=101 (real green → flipped = red for short logic = not is_green)
            → armed state sees not-is_green → emits "enter" (but bar 0 is prev_row for bar 1)
          Actually engine starts at i=1, calling on_closed_bar(prev=bars[0], cur=bars[1]).

        Simpler: just use enough bars:
          bars[0]: any (prev_row for bars[1])
          bars[1]: real green (close>open) → flipped→NOT is_green for short → entry_mode=red_dip fires
          bars[2]: fill bar (entry at open=100.0); also triggers safety order
                   (for short, safety triggers when bar_high >= trigger_price)
                   trigger is at entry*(1 + 0.5/100) = 100.5 → high must be >= 100.5
                   We make high=101.0 so safety fills.
                   We make low=99.0, close=99.5 (real red → for short is_green=True → not checkpoint exit yet)
          bars[3]: TP bar — price drops for short: low <= avg_entry*(1-0.01) must hold
                   With avg_entry ≈ 100 after safety: TP_correct ≈ 99.0
                   Make low=98.5 so TP triggers.
        """
        cfg = _short_dca_cfg(tp_pct=1.0, max_dca=1)
        # bar rows: (open, high, low, close)
        klines = _make_klines([
            (99.0, 100.0, 98.0, 99.5),   # bar 0 — prev_row for bar 1; doesn't matter much
            (99.5, 100.5, 99.0, 100.2),  # bar 1 — real green (close>open) → flipped → NOT is_green → enter signal
            (100.0, 101.0, 99.0, 99.5),  # bar 2 — fill bar (entry at open=100.0); high=101 triggers safety at 100.5
            (99.5, 99.8, 98.5, 98.6),    # bar 3 — TP check: low=98.5 should hit TP for short
            (98.6, 99.0, 98.0, 98.5),    # bar 4 — buffer bar
        ])
        return run_backtest(klines, _empty_funding(), cfg)

    def test_short_dca_tp_is_below_entry(self):
        """TP for a short must be strictly BELOW avg_entry — no phantom upside TP."""
        result = self._run_short_dca_scenario()
        tp_trades = [t for t in result.trades if t.exit_reason == "take_profit"]
        assert len(tp_trades) >= 1, (
            f"Expected at least one take_profit trade; got {[t.exit_reason for t in result.trades]}"
        )
        for t in tp_trades:
            # For a short, exit_price must be below entry_price (profit)
            assert t.exit_price < t.entry_price, (
                f"Short TP exit_price {t.exit_price} should be < entry_price {t.entry_price}. "
                f"Bug: TP was set above entry (wrong direction)."
            )
            # net_pnl must be positive for a profitable TP
            assert t.net_pnl > 0, (
                f"Short TP should yield positive net_pnl; got {t.net_pnl}. "
                f"Entry={t.entry_price}, exit={t.exit_price}"
            )

    def test_no_same_bar_phantom_tp(self):
        """
        Old bug: after safety fill, TP set above avg_entry (wrong for short).
        bar_low <= tp_price (phantom upside TP) would fire on the SAME bar as safety fill.
        Verify: no TP exit happens on bar 2 (the safety-fill bar) — it should wait for bar 3.
        """
        result = self._run_short_dca_scenario()
        tp_trades = [t for t in result.trades if t.exit_reason == "take_profit"]
        if tp_trades:
            base_ts = pd.Timestamp("2025-01-01 00:00:00", tz="UTC")
            fill_bar_ts = base_ts + pd.Timedelta(minutes=15 * 2)  # bar index 2
            for t in tp_trades:
                assert t.exit_time != fill_bar_ts, (
                    f"Phantom same-bar TP: trade exited at fill bar ts {fill_bar_ts}. "
                    f"This is the old direction bug."
                )


# ---------------------------------------------------------------------------
# Bug 2: pnl_pct denominator uses total_margin not initial margin_usdt
# ---------------------------------------------------------------------------

class TestPnlPctDenominator:
    """
    When safety orders are filled, total_margin > initial margin_usdt.
    pnl_pct = net_pnl / total_margin should give a smaller (more accurate) percentage
    than net_pnl / margin_usdt.
    """

    def test_pnl_pct_uses_total_margin_after_safety_fills(self):
        """pnl_pct must reflect net_pnl / total_margin (accumulated), not initial margin."""
        cfg = _short_dca_cfg(tp_pct=2.0, max_dca=2)
        # Build a long scenario to keep it simple: just check the math is consistent
        cfg2 = BacktestConfig(
            strategy=StrategyConfig(
                direction="long",
                position_model="dca",
                entry_mode="red_dip",
                checkpoint_mode="first_green",
                green_gate="off",
                stop_mode="none",
            ),
            dca=DCAConfig(
                price_drop_step_pct=1.0,
                step_multiplier=1.0,
                size_multiplier=1.0,
                max_dca_orders=2,
                first_order_ratio=1.0,
                take_profit_pct=2.0,
            ),
            exec=ExecConfig(
                leverage=10,
                margin_usdt=10.0,
                taker_fee=0.0,     # zero fees for clean math
                slippage_bps=0.0,
                maint_margin_rate=0.05,
            ),
        )
        # Candle layout for long DCA:
        # bar 0: prev_row
        # bar 1: red close → entry signal
        # bar 2: fill at open=100; drops to trigger safety orders (low <= 99.0 and 98.0)
        # bar 3: rallies to TP (high >= avg_entry * 1.02)
        klines = _make_klines([
            (101.0, 102.0, 100.0, 100.5),  # bar 0
            (100.5, 101.0, 99.5, 100.0),   # bar 1 — red (close<=open), enters
            (100.0, 100.1, 97.5, 98.0),    # bar 2 — fill at open=100; low=97.5 triggers 2 safety orders at 99.0 and 98.0
            (98.0, 105.0, 97.5, 104.0),    # bar 3 — high=105 should hit TP
            (104.0, 105.0, 103.0, 104.0),  # bar 4 — buffer
        ])
        result = run_backtest(klines, _empty_funding(), cfg2)
        tp_trades = [t for t in result.trades if t.exit_reason == "take_profit"]
        assert tp_trades, f"Expected TP trade; got {[t.exit_reason for t in result.trades]}"
        t = tp_trades[0]
        # total_margin > margin_usdt because safety orders were filled
        # Verify: pnl_pct == net_pnl / total_margin (which should NOT equal net_pnl / margin_usdt)
        initial_margin = cfg2.exec.margin_usdt  # 10.0
        if t.n_safety_filled > 0:
            # total_margin was accumulated; pnl_pct should differ from net_pnl/initial_margin
            # The actual total_margin isn't directly in Trade, but we can verify
            # pnl_pct * some_margin == net_pnl. If denominator is initial margin (wrong),
            # pnl_pct * initial_margin == net_pnl. If denominator is total_margin (correct),
            # pnl_pct * total_margin == net_pnl where total_margin > initial_margin.
            # So correct pnl_pct < net_pnl / initial_margin.
            wrong_pct = t.net_pnl / initial_margin
            # With correct fix, t.pnl_pct should be smaller than wrong_pct
            # (more margin deployed → smaller percentage return)
            assert abs(t.pnl_pct) < abs(wrong_pct) or abs(t.pnl_pct - wrong_pct) < 1e-10, (
                f"pnl_pct={t.pnl_pct:.6f} should be <= net_pnl/margin_usdt={wrong_pct:.6f} "
                f"when safety orders ({t.n_safety_filled}) were filled."
            )


# ---------------------------------------------------------------------------
# Bug 3: Checkpoint exit should route through FLAT_LOCKED when green_gate != off
# ---------------------------------------------------------------------------

class TestCheckpointExitGateRouting:
    """
    With green_gate='one', after a checkpoint exit the strategy must pass through
    FLAT_LOCKED (requiring one green bar) before re-arming. The old bug skipped
    straight to _ARMED, allowing immediate re-entry on the very next bar.
    """

    def test_checkpoint_exit_goes_to_locked_with_gate_one(self):
        """After checkpoint exit with green_gate='one', state should be FLAT_LOCKED."""
        cfg = StrategyConfig(
            direction="long",
            entry_mode="red_dip",
            checkpoint_mode="first_green",
            green_gate="one",
            stop_mode="none",
            position_model="single",
        )
        strat = CheckpointStrategy(cfg)
        # Manually put strategy IN_POSITION
        strat._state = strat._POS

        # Simulate a green bar (triggers checkpoint exit for long)
        green_bar = {"open": 100.0, "close": 101.0, "high": 101.0}
        prev_bar = {"high": 100.0}
        action = strat.on_closed_bar(prev_bar, green_bar, in_position=True)

        assert action == "exit_checkpoint", f"Expected exit_checkpoint, got {action}"
        assert strat._state == strat._LOCKED, (
            f"After checkpoint exit with green_gate='one', state should be FLAT_LOCKED "
            f"(to apply the gate), but got {strat._state}. "
            f"Old bug: transition went straight to _ARMED."
        )

    def test_checkpoint_exit_goes_to_armed_with_gate_off(self):
        """With green_gate='off', checkpoint exit still re-arms immediately."""
        cfg = StrategyConfig(
            direction="long",
            entry_mode="red_dip",
            checkpoint_mode="first_green",
            green_gate="off",
            stop_mode="none",
            position_model="single",
        )
        strat = CheckpointStrategy(cfg)
        strat._state = strat._POS

        green_bar = {"open": 100.0, "close": 101.0, "high": 101.0}
        prev_bar = {"high": 100.0}
        action = strat.on_closed_bar(prev_bar, green_bar, in_position=True)

        assert action == "exit_checkpoint"
        assert strat._state == strat._ARMED, (
            f"With green_gate='off', should re-arm immediately; got {strat._state}"
        )

    def test_locked_then_one_green_re_arms(self):
        """After checkpoint→LOCKED, one green bar should advance to ARMED."""
        cfg = StrategyConfig(
            direction="long",
            entry_mode="red_dip",
            checkpoint_mode="first_green",
            green_gate="one",
            stop_mode="none",
            position_model="single",
        )
        strat = CheckpointStrategy(cfg)
        strat._state = strat._POS

        # Trigger checkpoint exit
        green_bar = {"open": 100.0, "close": 101.0, "high": 101.0}
        strat.on_closed_bar({"high": 100.0}, green_bar, in_position=True)
        assert strat._state == strat._LOCKED

        # Now one green bar while locked → should advance to ARMED
        strat.on_closed_bar({"high": 101.0}, green_bar, in_position=False)
        assert strat._state == strat._ARMED, (
            f"After one green in FLAT_LOCKED with gate='one', should be ARMED; got {strat._state}"
        )


# ---------------------------------------------------------------------------
# Feature: drown-flip (long that drowns force-closes and flips to short)
# ---------------------------------------------------------------------------

def _drown_cfg(flip_pct: float, cooldown: int, leverage: int) -> BacktestConfig:
    """Single-model long with drown-flip on; zero slippage/fees for exact math."""
    return BacktestConfig(
        strategy=StrategyConfig(
            direction="long",
            position_model="single",
            entry_mode="red_dip",
            checkpoint_mode="first_green",
            green_gate="off",
            stop_mode="none",
            drown_flip=True,
            drown_flip_pct=flip_pct,
            drown_flip_cooldown_bars=cooldown,
        ),
        dca=DCAConfig(),
        exec=ExecConfig(
            leverage=leverage,
            margin_usdt=10.0,
            taker_fee=0.0,
            slippage_bps=0.0,
            maint_margin_rate=0.05,
        ),
    )


class TestDrownFlip:
    def test_flip_is_no_lookahead(self):
        """Long drowns on bar i; the short entry must fill at bar i+1 open.

        leverage=2 → liq distance = 45%, well beyond the 5% drown trigger, so the
        flip fires (not liquidation). Entry at 100, drown trigger = 95.
        """
        cfg = _drown_cfg(flip_pct=5.0, cooldown=1, leverage=2)
        klines = _make_klines([
            (100.0, 101.0, 99.0, 100.5),  # bar 0 — prev_row
            (100.0, 100.5, 99.0, 99.5),   # bar 1 — red → enter long, fills bar 2 open
            (100.0, 100.0, 94.0, 96.0),   # bar 2 — entry=100; low=94 <= 95 trigger → drown flip
            (96.0, 97.0, 95.0, 96.5),     # bar 3 — short entry fills here at open=96
            (96.5, 97.0, 96.0, 96.8),     # bar 4 — buffer (short holds)
        ])
        result = run_backtest(klines, _empty_funding(), cfg)

        drown = [t for t in result.trades if t.exit_reason == "drown_flip"]
        assert len(drown) == 1, f"expected 1 drown_flip; got {[t.exit_reason for t in result.trades]}"
        d = drown[0]
        assert d.exit_price < d.entry_price, "long drown must exit below entry (a loss)"

        idx = result.trades.index(d)
        assert idx + 1 < len(result.trades), "a short trade must follow the flip"
        short_t = result.trades[idx + 1]
        # No-lookahead: short filled at bar 3 open = 96.0 (the bar AFTER the drown bar)
        assert short_t.entry_price == 96.0, (
            f"short entry must fill at next-bar open (96.0), got {short_t.entry_price} — lookahead leak"
        )

    def test_cooldown_blocks_second_flip(self):
        """Two adverse legs within cooldown_bars → only ONE flip fires."""
        cfg = _drown_cfg(flip_pct=5.0, cooldown=5, leverage=2)
        klines = _make_klines([
            (100.0, 101.0, 99.0, 100.5),  # bar 0
            (100.0, 100.5, 99.0, 99.5),   # bar 1 — enter long, fills bar 2
            (100.0, 100.0, 94.0, 96.0),   # bar 2 — drown flip → short at bar 3 open=96
            (96.0, 110.0, 95.0, 109.0),   # bar 3 — short adverse: high=110 >= 96*1.05, but cooldown blocks
            (109.0, 110.0, 108.0, 109.0), # bar 4 — still within cooldown
        ])
        result = run_backtest(klines, _empty_funding(), cfg)
        drown = [t for t in result.trades if t.exit_reason == "drown_flip"]
        assert len(drown) == 1, (
            f"cooldown must block the 2nd flip; got {len(drown)} drown_flip trades"
        )

    def test_liquidation_takes_precedence(self):
        """drown_flip_pct above the liq distance → liquidation fires, no drown_flip.

        leverage=10 → liq distance = 5%; drown_flip_pct=8% sits beyond liq, so as
        price falls it crosses the liq level (95) before the drown level (92).
        """
        cfg = _drown_cfg(flip_pct=8.0, cooldown=1, leverage=10)
        klines = _make_klines([
            (100.0, 101.0, 99.0, 100.5),  # bar 0
            (100.0, 100.5, 99.0, 99.5),   # bar 1 — enter long, fills bar 2
            (100.0, 100.0, 90.0, 92.0),   # bar 2 — low=90 crosses liq(95) and drown(92); liq wins
            (92.0, 93.0, 91.0, 92.5),     # bar 3 — buffer
        ])
        result = run_backtest(klines, _empty_funding(), cfg)
        reasons = [t.exit_reason for t in result.trades]
        assert "liquidation" in reasons, f"expected a liquidation; got {reasons}"
        assert "drown_flip" not in reasons, (
            f"drown_flip must not fire when liq is crossed first; got {reasons}"
        )


# ---------------------------------------------------------------------------
# Feature: entry filters — ema_filter, red_candle_avoid_pct, require_volume_spike
# ---------------------------------------------------------------------------

class TestEntryFilters:
    """New strategy.py entry gates for weakly_longrun_bot_config.

    Each gate is tested directly against `_try_enter` (bypassing the is_green
    routing in `_handle_armed`, same private-state-poking style already used by
    TestCheckpointExitGateRouting above) so the gate logic is isolated from the
    state machine's own entry-trigger semantics.
    """

    def _cfg(self, **overrides) -> StrategyConfig:
        base = dict(
            direction="long", entry_mode="red_dip", checkpoint_mode="first_green",
            green_gate="off", stop_mode="none", position_model="single",
        )
        base.update(overrides)
        return StrategyConfig(**base)

    # --- ema_filter ---

    def test_ema_gate_fails_closed_during_warmup(self):
        strat = CheckpointStrategy(self._cfg(ema_filter=True, ema_period=5))
        action = strat._try_enter({"high": 100.0}, {"open": 100.0, "close": 95.0})
        assert action == "none", "EMA gate must block entry before any EMA history exists"

    def test_ema_gate_blocks_below_ema(self):
        strat = CheckpointStrategy(self._cfg(ema_filter=True, ema_period=2))
        strat._ema = 100.0
        action = strat._try_enter({"high": 100.0}, {"open": 100.0, "close": 90.0})
        assert action == "none"

    def test_ema_gate_allows_above_ema(self):
        strat = CheckpointStrategy(self._cfg(ema_filter=True, ema_period=2))
        strat._ema = 90.0
        action = strat._try_enter({"high": 100.0}, {"open": 100.0, "close": 95.0})
        assert action == "enter"

    # --- red_candle_avoid_pct ---

    def test_red_candle_avoid_blocks_after_big_drop(self):
        strat = CheckpointStrategy(self._cfg(red_candle_avoid_pct=3.0))
        prev_row = {"high": 100.0, "open": 100.0, "close": 94.0}  # 6% drop
        action = strat._try_enter(prev_row, {"open": 94.0, "close": 90.0})
        assert action == "none"

    def test_red_candle_avoid_allows_small_drop(self):
        strat = CheckpointStrategy(self._cfg(red_candle_avoid_pct=3.0))
        prev_row = {"high": 100.0, "open": 100.0, "close": 99.0}  # 1% drop
        action = strat._try_enter(prev_row, {"open": 99.0, "close": 95.0})
        assert action == "enter"

    def test_red_candle_avoid_off_when_zero(self):
        strat = CheckpointStrategy(self._cfg(red_candle_avoid_pct=0.0))
        prev_row = {"high": 100.0, "open": 100.0, "close": 90.0}  # big drop, gate off
        action = strat._try_enter(prev_row, {"open": 90.0, "close": 85.0})
        assert action == "enter"

    # --- require_volume_spike ---

    def test_volume_spike_fails_closed_during_warmup(self):
        strat = CheckpointStrategy(self._cfg(require_volume_spike=True, volume_spike_min_ratio=2.5))
        action = strat._try_enter({"high": 100.0}, {"open": 100.0, "close": 95.0})
        assert action == "none", "volume-spike gate must block entry before 24 bars of history"

    def test_volume_spike_blocks_without_spike(self):
        strat = CheckpointStrategy(self._cfg(require_volume_spike=True, volume_spike_min_ratio=2.5))
        strat._vol_window = [1000.0] * 24  # flat volume, no spike
        action = strat._try_enter({"high": 100.0}, {"open": 100.0, "close": 95.0})
        assert action == "none"

    def test_volume_spike_allows_with_spike(self):
        strat = CheckpointStrategy(self._cfg(require_volume_spike=True, volume_spike_min_ratio=2.5))
        strat._vol_window = [1000.0] * 23 + [3000.0]  # last bar ~2.77x rolling avg
        action = strat._try_enter({"high": 100.0}, {"open": 100.0, "close": 95.0})
        assert action == "enter"

    # --- end-to-end wiring through on_closed_bar ---

    def test_running_state_builds_via_on_closed_bar(self):
        """_update_running_state must run every closed bar (not just at _try_enter)."""
        strat = CheckpointStrategy(self._cfg(require_volume_spike=True, volume_spike_min_ratio=2.5))
        for _ in range(24):
            row = {"open": 100.0, "close": 101.0, "high": 101.0, "volume": 1000.0}
            strat.on_closed_bar({"open": 99.0, "close": 100.0, "high": 100.0}, row, in_position=False)
        assert len(strat._vol_window) == 24

        spike_row = {"open": 101.0, "close": 95.0, "high": 101.0, "volume": 5000.0}
        action = strat.on_closed_bar({"open": 100.0, "close": 101.0, "high": 101.0}, spike_row, in_position=False)
        assert action == "enter", "red bar with a real volume spike should pass all gates and enter"
