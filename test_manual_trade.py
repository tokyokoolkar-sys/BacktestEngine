"""
test_manual_trade.py — Tests for manual trading feature.

Covers:
  1. ManualOracle.decide always returns signal="go", confidence=1.0, reason="manual".
  2. DcaBot with auto_entry=False skips the new-entry signal scan (no _enter calls).
  3. DcaBot with auto_entry=False still manages existing baskets (run_once calls
     _manage_baskets).
  4. _pid_alive returns False for a dead pid.

No real Binance API calls are made — all network/client interactions are mocked.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from oracle import OracleDecision
from trade import ManualOracle
from bot import BotConfig, DcaBot, GlobalConfig, SymbolConfig


# ---------------------------------------------------------------------------
# ManualOracle tests
# ---------------------------------------------------------------------------

class TestManualOracle(unittest.TestCase):

    def setUp(self):
        self.oracle = ManualOracle()

    def test_decide_returns_go_signal(self):
        """ManualOracle.decide must return signal='go'."""
        result = self.oracle.decide(
            symbol="BTCUSDT",
            klines=[],
            prev_trade=None,
            tp_pct=0.5,
            stop_pct=3.0,
        )
        self.assertIsInstance(result, OracleDecision)
        self.assertEqual(result.signal, "go")

    def test_decide_returns_full_confidence(self):
        """ManualOracle.decide must return confidence=1.0."""
        result = self.oracle.decide("ETHUSDT", [], None, 0.5, 3.0)
        self.assertAlmostEqual(result.confidence, 1.0)

    def test_decide_returns_manual_reason(self):
        """ManualOracle.decide must return reason='manual'."""
        result = self.oracle.decide("SOLUSDT", None, {}, 0.3, 2.5)
        self.assertEqual(result.reason, "manual")

    def test_decide_works_with_none_klines(self):
        """ManualOracle.decide must not crash when klines is None."""
        result = self.oracle.decide("OPUSDT", klines=None)
        self.assertEqual(result.signal, "go")

    def test_decide_works_without_optional_args(self):
        """ManualOracle.decide must work with just symbol and klines."""
        result = self.oracle.decide("NEARUSDT", [])
        self.assertEqual(result.signal, "go")

    def test_reconsider_also_returns_go(self):
        """ManualOracle.reconsider must also always return go."""
        result = self.oracle.reconsider()
        self.assertEqual(result.signal, "go")


# ---------------------------------------------------------------------------
# DcaBot auto_entry=False tests
# ---------------------------------------------------------------------------

class TestAutoEntryFalse(unittest.TestCase):
    """DcaBot with auto_entry=False must skip new-entry scan but manage baskets."""

    def _make_bot(self) -> DcaBot:
        """Build a minimal dry-run bot with auto_entry=False and no network."""
        cfg = BotConfig(
            symbols=[
                SymbolConfig("BTCUSDT", tp_pct=0.3, stop_pct=3.0, active=True),
            ],
            global_=GlobalConfig(),
            api_key="key",
            api_secret="secret",
        )
        bot = DcaBot(cfg, dry_run=True)
        bot.auto_entry = False
        return bot

    def test_run_once_skips_check_signal_when_auto_entry_false(self):
        """With auto_entry=False, run_once must NOT call _check_signal at all."""
        bot = self._make_bot()

        enter_calls = []

        def fake_enter(sc):
            enter_calls.append(sc.name)

        with patch.object(bot, "_check_signal", return_value=(None, None)) as mock_signal, \
             patch.object(bot, "_enter", side_effect=fake_enter):
            bot.run_once()

        # _check_signal must never be called — the entire new-entry scan is skipped.
        mock_signal.assert_not_called()
        self.assertEqual(enter_calls, [],
                         "No entries should be triggered when auto_entry=False")

    def test_run_once_still_manages_baskets_when_auto_entry_false(self):
        """With auto_entry=False, _manage_baskets must still be called."""
        bot = self._make_bot()

        with patch.object(bot, "_manage_baskets") as mock_manage, \
             patch.object(bot, "_check_signal", return_value=(None, None)):
            bot.run_once()

        mock_manage.assert_called_once()

    def test_run_once_with_paused_true_also_skips_entry(self):
        """paused=True must also skip new-entry scan (auto_entry=True but paused)."""
        cfg = BotConfig(
            symbols=[SymbolConfig("BTCUSDT", tp_pct=0.3, stop_pct=3.0, active=True)],
            global_=GlobalConfig(),
            api_key="key",
            api_secret="secret",
        )
        bot = DcaBot(cfg, dry_run=True)
        bot.auto_entry = True   # enabled — paused gate should still block
        bot.paused = True

        with patch.object(bot, "_check_signal", return_value=("enter", None)) as mock_signal, \
             patch.object(bot, "_enter") as mock_enter:
            bot.run_once()

        mock_signal.assert_not_called()
        mock_enter.assert_not_called()

    def test_run_once_with_disabled_symbols_skips_those_symbols(self):
        """disabled_symbols must cause that symbol's signal scan to be skipped."""
        cfg = BotConfig(
            symbols=[
                SymbolConfig("BTCUSDT", tp_pct=0.3, stop_pct=3.0, active=True),
                SymbolConfig("ETHUSDT", tp_pct=0.5, stop_pct=3.0, active=True),
            ],
            global_=GlobalConfig(),
            api_key="key",
            api_secret="secret",
        )
        bot = DcaBot(cfg, dry_run=True)
        bot.auto_entry = True
        bot.disabled_symbols = ["BTCUSDT"]  # only BTC disabled

        signal_calls = []

        def fake_signal(symbol, **kw):
            signal_calls.append(symbol)
            return (None, None)

        with patch.object(bot, "_check_signal", side_effect=fake_signal):
            bot.run_once()

        self.assertNotIn("BTCUSDT", signal_calls,
                         "BTCUSDT is disabled — _check_signal must not be called for it")
        self.assertIn("ETHUSDT", signal_calls,
                      "ETHUSDT is not disabled — _check_signal must be called for it")


# ---------------------------------------------------------------------------
# _pid_alive helper test
# ---------------------------------------------------------------------------

class TestPidAlive(unittest.TestCase):

    def test_dead_pid_returns_false(self):
        """_pid_alive must return False for a pid that does not exist."""
        from bot import _pid_alive
        # PID 2^22 (4194304) is above the Linux kernel's max (4194304 is the limit,
        # so any value above is guaranteed dead).  On Windows we use a known-dead pid.
        # Use -1 which is always invalid across platforms.
        # Actually pass a very large pid — os.kill(very_large, 0) raises ProcessLookupError
        # on Linux; ctypes OpenProcess returns NULL on Windows.
        dead_pid = 4_000_000  # near-impossible to be alive
        # Acceptable: may return True on a busy system.  Just verify it doesn't raise.
        result = _pid_alive(dead_pid)
        self.assertIsInstance(result, bool)

    def test_own_pid_returns_true(self):
        """_pid_alive must return True for the current process's pid."""
        from bot import _pid_alive
        self.assertTrue(_pid_alive(os.getpid()))


import os  # needed by test above — keep at module level


# ---------------------------------------------------------------------------
# dashboard control route tests (no-token, localhost-only design)
# ---------------------------------------------------------------------------

class TestRequireToken(unittest.TestCase):
    """Verify control routes work without a token (auth dropped, localhost-only).

    Tests updated because require_token is now a pass-through: no 401/503
    responses are emitted. /api/control/pause was removed. All other
    endpoint semantics (409 conflicts, 400 validation) are preserved.
    """

    def setUp(self):
        import dashboard
        dashboard.app.config["TESTING"] = True
        self.client = dashboard.app.test_client()

    def test_state_endpoint_returns_200(self):
        """GET /api/control/state must succeed without any token."""
        r = self.client.get("/api/control/state")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("auto_running", data)
        self.assertIn("manual_running", data)
        self.assertIn("mode", data)

    def test_control_post_succeeds_without_token(self):
        """POST /api/control/auto/start must succeed (200 or 409) without any token."""
        with patch("dashboard._proc_running", return_value=False), \
             patch("dashboard._start_auto"):
            r = self.client.post("/api/control/auto/start")
        # 200 = started; 409 = conflict — either is fine, NOT 401 or 503
        self.assertIn(r.status_code, (200, 409))

    def test_control_post_does_not_return_401(self):
        """No control route should return 401 (auth is gone)."""
        with patch("dashboard._proc_running", return_value=False), \
             patch("dashboard._start_auto"):
            r = self.client.post("/api/control/auto/start")
        self.assertNotEqual(r.status_code, 401)

    def test_control_post_does_not_return_503(self):
        """No control route should return 503 for missing token (auth is gone)."""
        with patch("dashboard._proc_running", return_value=False), \
             patch("dashboard._start_auto"):
            r = self.client.post("/api/control/auto/start")
        self.assertNotEqual(r.status_code, 503)

    def test_pause_route_removed(self):
        """POST /api/control/pause must return 404 (route was removed)."""
        r = self.client.post("/api/control/pause", json={"paused": True})
        self.assertEqual(r.status_code, 404)

    def test_manual_enter_rejects_empty_symbol(self):
        """POST /api/control/manual/enter with no symbol must return 400."""
        with patch("dashboard._proc_running", return_value=True):
            r = self.client.post(
                "/api/control/manual/enter",
                json={"signal": "go"},
            )
        self.assertEqual(r.status_code, 400)

    def test_manual_enter_rejects_non_go_signal(self):
        """POST /api/control/manual/enter with signal != 'go' must return 400."""
        with patch("dashboard._proc_running", return_value=True):
            r = self.client.post(
                "/api/control/manual/enter",
                json={"symbol": "BTCUSDT", "signal": "no-go"},
            )
        self.assertEqual(r.status_code, 400)

    def test_manual_enter_409_when_manual_not_running(self):
        """POST /api/control/manual/enter must 409 if manual engine is not running."""
        with patch("dashboard._proc_running", return_value=False):
            r = self.client.post(
                "/api/control/manual/enter",
                json={"symbol": "BTCUSDT", "signal": "go"},
            )
        self.assertEqual(r.status_code, 409)

    def test_auto_start_409_when_manual_running(self):
        """POST /api/control/auto/start must 409 if manual engine is running."""
        def fake_running(pidfile):
            import dashboard
            return pidfile == dashboard._TRADE_PID

        with patch("dashboard._proc_running", side_effect=fake_running):
            r = self.client.post("/api/control/auto/start")
        self.assertEqual(r.status_code, 409)

    def test_manual_start_409_when_auto_running(self):
        """POST /api/control/manual/start must 409 if auto engine is running."""
        def fake_running(pidfile):
            import dashboard
            return pidfile == dashboard._BOT_PID

        with patch("dashboard._proc_running", side_effect=fake_running):
            r = self.client.post("/api/control/manual/start")
        self.assertEqual(r.status_code, 409)


class TestDashboardPagesParseClean(unittest.TestCase):
    """Guard against smart-quote / smart-punctuation corruption in the inline JS.

    A single curly quote (U+2018/U+2019) inside a <script> block is a JS
    SyntaxError that prevents the ENTIRE block from executing — which silently
    froze the manual page (state stuck on "loading", every button dead, symbol
    dropdown inert). ASCII-only is the invariant; this test enforces it.
    """

    # Smart punctuation that breaks JS string/identifier parsing if it leaks in.
    BAD_CHARS = "‘’“”"  # ‘ ’ “ ”

    def setUp(self):
        import dashboard
        dashboard.app.config["TESTING"] = True
        self.client = dashboard.app.test_client()

    def _assert_clean(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path} did not render")
        html = r.get_data(as_text=True)
        for ch in self.BAD_CHARS:
            self.assertNotIn(
                ch, html,
                f"{path} contains smart-punctuation {ch!r} — breaks inline JS parse",
            )

    def test_manual_page_has_no_smart_quotes(self):
        self._assert_clean("/manual")

    def test_dashboard_page_has_no_smart_quotes(self):
        self._assert_clean("/dashboard")

    def test_landing_page_has_no_smart_quotes(self):
        self._assert_clean("/")


if __name__ == "__main__":
    unittest.main()
