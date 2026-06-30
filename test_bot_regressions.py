import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import BotConfig, DcaBot, GlobalConfig, SymbolConfig
from oracle import AiOracle
import dashboard


class OracleRegressionTests(unittest.TestCase):
    def test_empty_content_from_provider_is_unavailable_not_json_error(self):
        class Response:
            status_code = 200
            text = json.dumps({
                "choices": [{"message": {"content": "", "reasoning_content": "not json"}}]
            })

            def raise_for_status(self):
                pass

            def json(self):
                return json.loads(self.text)

        oracle = AiOracle("key")
        with patch("requests.post", return_value=Response()), self.assertLogs("oracle", level="WARNING") as logs:
            self.assertIsNone(oracle._call({}))
        self.assertIn("empty oracle content", "\n".join(logs.output))

    def test_call_recovers_valid_object_with_trailing_extra_data(self):
        # The live deepseek endpoint returns a valid JSON object followed by trailing
        # data, which strict r.json() rejects ("Extra data: ..."). _call must recover
        # the leading object via raw_decode — NOT return None (that lost a real no-go
        # decision live and caused an unwanted entry).
        class Response:
            status_code = 200
            text = json.dumps({
                "choices": [{"message": {"content": json.dumps({"signal": "no-go"})}}]
            }) + '\n{"reasoning":"trailing extra data"}'  # → r.json() raises Extra data

            def raise_for_status(self):
                pass

            def json(self):
                return json.loads(self.text)  # raises JSONDecodeError: Extra data

        oracle = AiOracle("key")
        with patch("requests.post", return_value=Response()):
            result = oracle._call({})
        self.assertEqual(result, {"signal": "no-go"})

    def test_call_returns_none_when_body_has_no_valid_json(self):
        # Truly non-JSON body (e.g. proxy returns plain text / HTML) → fail-closed None.
        class Response:
            status_code = 200
            text = "502 Bad Gateway"

            def raise_for_status(self):
                pass

            def json(self):
                return json.loads(self.text)  # raises JSONDecodeError

        oracle = AiOracle("key")
        with patch("requests.post", return_value=Response()), self.assertLogs("oracle", level="ERROR") as logs:
            self.assertIsNone(oracle._call({}))
        self.assertIn("not valid JSON", "\n".join(logs.output))

    def test_payload_uses_configured_model(self):
        oracle = AiOracle("key", model="oc/deepseek-v4-flash-free")
        payload = oracle._build_payload("BTCUSDT", [], None, 0.3, 3.0)
        self.assertEqual(payload["model"], "oc/deepseek-v4-flash-free")
        self.assertGreaterEqual(payload["max_tokens"], 1024)
        self.assertIn('Return exactly {"signal":', payload["messages"][0]["content"])


class SignalRegressionTests(unittest.TestCase):
    def test_check_signal_uses_last_closed_candle_not_live_candle(self):
        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.g = GlobalConfig()
        bot.last_candle_time = {}
        bot.strategies = {"BTCUSDT": __import__("strategy").CheckpointStrategy(__import__("config").StrategyConfig(
            entry_mode="red_dip",
            green_gate="off",
            stop_mode="none",
            checkpoint_mode="first_green",
            position_model="single",
            direction="long",
        ))}
        bot._fetch_klines = lambda symbol, limit=15: [
            [1, "100", "101", "99", "100", "1", 10],
            [2, "100", "101", "90", "95", "1", 20],   # closed red -> enter
            [3, "95", "110", "94", "105", "1", 30],   # live green -> ignore
        ]

        action, _ = bot._check_signal("BTCUSDT")

        self.assertEqual(action, "enter")
        self.assertEqual(bot.last_candle_time["BTCUSDT"], 20)

    def test_active_symbols_dedupes_by_name(self):
        cfg = BotConfig(
            symbols=[
                SymbolConfig("BTCUSDT", 0.3, 3.0, True),
                SymbolConfig("BTCUSDT", 0.3, 3.0, True),
                SymbolConfig("ETHUSDT", 0.3, 3.0, False),
            ],
            global_=GlobalConfig(),
            api_key="key",
            api_secret="secret",
        )

        self.assertEqual([s.name for s in cfg.active_symbols()], ["BTCUSDT"])


class DashboardRegressionTests(unittest.TestCase):
    def test_dashboard_status_comes_from_bot_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bot_status.json").write_text(json.dumps({
                "bot_alive": True,
                "balance": 10.05,
                "symbols": [{"name": "BTCUSDT", "tp_pct": 0.3}],
            }))
            with patch.object(dashboard, "_HERE", Path(tmp)), \
                 patch.object(dashboard, "_read_trades", return_value=[]), \
                 patch.object(dashboard, "_fetch_baskets", return_value=[]), \
                 patch.object(dashboard, "_bot_alive", return_value=False):
                dashboard._update_cache()

        with dashboard._cache_lock:
            self.assertTrue(dashboard._cache["bot_alive"])
            self.assertEqual(dashboard._cache["balance"], 10.05)
            self.assertEqual(dashboard._cache["symbols"], [{"name": "BTCUSDT", "tp_pct": 0.3}])


class SafetyFillRegressionTests(unittest.TestCase):
    """Regression tests for fix #1 (double-fill in one poll) and fix #4 (orderId capture)."""

    def _make_bot_and_basket(self):
        """Return a minimal bot + basket with two safety orders."""
        from bot import Basket
        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.g = GlobalConfig()
        bot.baskets = {}
        bot.lot_filter_cache = {
            "BTCUSDT": {"stepSize": 0.001, "tickSize": 0.1, "minQty": 0.001}
        }

        scfg = SymbolConfig("BTCUSDT", tp_pct=0.3, stop_pct=3.0)
        gcfg = GlobalConfig()
        basket = Basket(
            symbol="BTCUSDT",
            scfg=scfg,
            gcfg=gcfg,
            state="active",
            entry_price=60000.0,
            avg_entry=60000.0,
            size_base=0.01,
            total_notional=600.0,
            total_margin=60.0,
            total_fees=0.0,
            safety_order_ids=[101, 102],
            tp_order_id=201,
            stop_order_id=301,
        )
        bot.baskets["BTCUSDT"] = basket
        return bot, basket

    def test_both_safety_orders_detected_when_two_fill_in_one_poll(self):
        """Fix #1: iterating a copy means both filled IDs are processed, not just one."""
        bot, basket = self._make_bot_and_basket()

        class FakeClient:
            def futures_get_order(self, symbol, orderId):
                return {
                    "status": "FILLED",
                    "executedQty": "0.005",
                    "avgPrice": "59700.0",
                }

            def futures_cancel_order(self, **kwargs):
                pass

            def futures_cancel_all_open_orders(self, **kwargs):
                pass

            def futures_cancel_all_algo_open_orders(self, **kwargs):
                pass

            def futures_create_order(self, **kwargs):
                return {"orderId": 999, "executedQty": "0", "status": "NEW"}

        bot.client = FakeClient()
        bot._check_safety_fills(basket)

        # Both safety orders should have been processed and removed
        self.assertEqual(len(basket.safety_order_ids), 0,
                         "Both safety order IDs must be removed after two fills in one poll")
        self.assertEqual(basket.n_safety_filled, 2,
                         "n_safety_filled must be 2 after both safety orders fill")

    def test_tp_and_stop_order_id_captured_from_orderId_key(self):
        """Fix #4: order ID must be taken from resp['orderId'], not a fragile .get() chain."""

        class Client:
            def __init__(self):
                self.order_calls = []

            def futures_change_leverage(self, **kwargs):
                pass

            def futures_symbol_ticker(self, symbol):
                return {"price": "60000.0"}

            def futures_create_order(self, **kwargs):
                self.order_calls.append(kwargs)
                # Simulate Binance: only 'orderId' in response (no newOrderId etc.)
                oid = 1000 + len(self.order_calls)
                return {"orderId": oid, "executedQty": "0", "status": "NEW"}

            def futures_cancel_all_open_orders(self, **kwargs):
                pass

            def futures_cancel_all_algo_open_orders(self, **kwargs):
                pass

        client = Client()
        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.client = client
        # base_margin large enough that qty clears stepSize/minQty (skip-policy feasible)
        bot.g = GlobalConfig(base_margin=100.0, leverage=10)
        bot.baskets = {}
        bot.lot_filter_cache = {
            "BTCUSDT": {"stepSize": 0.001, "tickSize": 0.1, "minQty": 0.001}
        }
        bot.last_trade = {}
        # oracle=None means the oracle gate is bypassed inside _enter
        bot.oracle = None

        bot._enter(SymbolConfig("BTCUSDT", tp_pct=0.3, stop_pct=3.0))

        self.assertIn("BTCUSDT", bot.baskets, "Basket must be created")
        basket = bot.baskets["BTCUSDT"]

        # tp_order_id and stop_order_id must be integers (from orderId), not None
        self.assertIsNotNone(basket.tp_order_id, "tp_order_id must not be None")
        self.assertIsNotNone(basket.stop_order_id, "stop_order_id must not be None")
        self.assertIsInstance(basket.tp_order_id, int, "tp_order_id must be an int from orderId")
        self.assertIsInstance(basket.stop_order_id, int, "stop_order_id must be an int from orderId")


class AlgoOrderRegressionTests(unittest.TestCase):
    """Regression tests for conditional/algo order id capture and cancel routing."""

    # Exact live response shape for a conditional order (no orderId key)
    _ALGO_RESP = {
        'algoId': 3000001931015058,
        'clientAlgoId': 'x-CbYtestxxx',
        'algoType': 'CONDITIONAL',
        'orderType': 'TAKE_PROFIT_MARKET',
        'symbol': 'BNBUSDT',
        'side': 'SELL',
        'quantity': '0.04',
        'algoStatus': 'NEW',
        'triggerPrice': '577.690',
        'reduceOnly': True,
    }

    def _make_algo_client(self, call_log):
        """Return a fake client that records calls and returns algo shapes for TP/stop."""
        algo_resp = self._ALGO_RESP.copy()

        class FakeAlgoClient:
            def futures_change_leverage(self, **kw):
                pass

            def futures_symbol_ticker(self, symbol):
                return {"price": "500.0"}

            def futures_create_order(self_, **kw):
                call_log.append(("create_order", kw))
                order_type = kw.get("type", "")
                if order_type in ("TAKE_PROFIT_MARKET", "STOP_MARKET"):
                    # Return conditional/algo shape (no orderId)
                    r = algo_resp.copy()
                    r["orderType"] = order_type
                    return r
                # Base MARKET and safety LIMIT orders return normal orderId
                oid = 1000 + len(call_log)
                return {"orderId": oid, "executedQty": str(kw.get("quantity", 0)), "status": "NEW"}

            def futures_cancel_all_open_orders(self_, **kw):
                call_log.append(("cancel_all_open_orders", kw))

            def futures_cancel_all_algo_open_orders(self_, **kw):
                call_log.append(("cancel_all_algo_open_orders", kw))

            def futures_cancel_order(self_, **kw):
                call_log.append(("cancel_order", kw))

            def futures_cancel_algo_order(self_, **kw):
                call_log.append(("cancel_algo_order", kw))

        return FakeAlgoClient()

    def test_algo_order_ids_captured_not_none(self):
        """When TP/stop responses have algoId but no orderId, ids must not be None."""
        call_log = []
        client = self._make_algo_client(call_log)

        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.client = client
        bot.g = GlobalConfig(base_margin=50.0, leverage=10)
        bot.baskets = {}
        bot.lot_filter_cache = {
            "BNBUSDT": {"stepSize": 0.01, "tickSize": 0.01, "minQty": 0.01}
        }
        bot.last_trade = {}
        bot.oracle = None

        bot._enter(SymbolConfig("BNBUSDT", tp_pct=0.5, stop_pct=3.0))

        self.assertIn("BNBUSDT", bot.baskets, "Basket must be created")
        basket = bot.baskets["BNBUSDT"]

        # ids must be captured from algoId
        self.assertIsNotNone(basket.tp_order_id, "tp_order_id must not be None for algo resp")
        self.assertIsNotNone(basket.stop_order_id, "stop_order_id must not be None for algo resp")
        self.assertEqual(basket.tp_order_id, 3000001931015058)
        self.assertEqual(basket.stop_order_id, 3000001931015058)

        # algo flags must be True
        self.assertTrue(basket.tp_is_algo, "tp_is_algo must be True when no orderId in resp")
        self.assertTrue(basket.stop_is_algo, "stop_is_algo must be True when no orderId in resp")

    def test_cancel_remaining_orders_routes_algo_to_algo_method(self):
        """_cancel_remaining_orders must call futures_cancel_algo_order for algo ids."""
        from bot import Basket
        call_log = []
        client = self._make_algo_client(call_log)

        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.client = client
        bot.g = GlobalConfig()

        scfg = SymbolConfig("BNBUSDT", tp_pct=0.5, stop_pct=3.0)
        gcfg = GlobalConfig()
        basket = Basket(
            symbol="BNBUSDT",
            scfg=scfg,
            gcfg=gcfg,
            state="closing",
            entry_price=500.0,
            avg_entry=500.0,
            size_base=0.1,
            total_notional=50.0,
            total_margin=5.0,
            total_fees=0.0,
            safety_order_ids=[101],       # normal safety order
            tp_order_id=3000001931015058,
            tp_is_algo=True,
            stop_order_id=3000001931015059,
            stop_is_algo=True,
        )

        bot._cancel_remaining_orders(basket)

        methods_called = [c[0] for c in call_log]

        # Normal safety order cancelled via futures_cancel_order
        self.assertIn("cancel_order", methods_called,
                      "futures_cancel_order must be called for normal safety order id")

        # Algo TP/stop cancelled via futures_cancel_algo_order
        self.assertIn("cancel_algo_order", methods_called,
                      "futures_cancel_algo_order must be called for algo tp/stop ids")

        # Backstop sweeps must always fire
        self.assertIn("cancel_all_open_orders", methods_called,
                      "cancel_all_open_orders backstop must always be called")
        self.assertIn("cancel_all_algo_open_orders", methods_called,
                      "cancel_all_algo_open_orders backstop must always be called")

        # Verify algo cancel was called with correct algoId values
        algo_cancel_calls = [c for c in call_log if c[0] == "cancel_algo_order"]
        algo_ids_cancelled = [c[1].get("algoId") for c in algo_cancel_calls]
        self.assertIn(3000001931015058, algo_ids_cancelled)
        self.assertIn(3000001931015059, algo_ids_cancelled)

    def test_replace_tp_cancels_per_id_not_sweep(self):
        """_replace_tp must cancel ONLY the tracked TP/stop by id, never a blanket
        cancel-all sweep (which would also kill the still-unfilled DCA safety ladder).
        Replacement TP/stop must carry workingType=MARK_PRICE."""
        from bot import Basket
        call_log = []
        client = self._make_algo_client(call_log)

        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.client = client
        bot.g = GlobalConfig()
        bot.lot_filter_cache = {
            "BNBUSDT": {"stepSize": 0.01, "tickSize": 0.01, "minQty": 0.01}
        }

        scfg = SymbolConfig("BNBUSDT", tp_pct=0.5, stop_pct=3.0)
        gcfg = GlobalConfig()
        basket = Basket(
            symbol="BNBUSDT",
            scfg=scfg,
            gcfg=gcfg,
            state="active",
            entry_price=500.0,
            avg_entry=499.0,
            size_base=0.1,
            total_notional=49.9,
            total_margin=5.0,
            total_fees=0.0,
            safety_order_ids=[],
            tp_order_id=3000001931015058,
            tp_is_algo=True,
            stop_order_id=3000001931015059,
            stop_is_algo=True,
        )

        bot._replace_tp(basket)

        methods_called = [c[0] for c in call_log]

        # Must NOT sweep all orders — that would cancel unfilled DCA safety orders.
        self.assertNotIn("cancel_all_open_orders", methods_called,
                         "_replace_tp must not blanket-cancel (kills the DCA ladder)")
        self.assertNotIn("cancel_all_algo_open_orders", methods_called,
                         "_replace_tp must not blanket-cancel algo orders")

        # Must cancel exactly the tracked TP + stop by their algoIds.
        algo_cancel_calls = [c for c in call_log if c[0] == "cancel_algo_order"]
        algo_ids_cancelled = [c[1].get("algoId") for c in algo_cancel_calls]
        self.assertIn(3000001931015058, algo_ids_cancelled)
        self.assertIn(3000001931015059, algo_ids_cancelled)

        # New TP and stop must be re-placed, both with MARK_PRICE working type.
        create_calls = [c for c in call_log if c[0] == "create_order"]
        order_types = [c[1].get("type") for c in create_calls]
        self.assertIn("TAKE_PROFIT_MARKET", order_types, "New TP order must be placed")
        self.assertIn("STOP_MARKET", order_types, "New stop order must be placed")
        for c in create_calls:
            self.assertEqual(c[1].get("workingType"), "MARK_PRICE",
                             "replacement %s must trigger on MARK_PRICE" % c[1].get("type"))

        # New ids must be captured
        self.assertIsNotNone(basket.tp_order_id, "tp_order_id must be captured after replace")
        self.assertIsNotNone(basket.stop_order_id, "stop_order_id must be captured after replace")


class QuantityRegressionTests(unittest.TestCase):
    def test_base_quantity_below_min_qty_skips_order(self):
        class Client:
            def __init__(self):
                self.order_calls = 0

            def futures_change_leverage(self, **kwargs):
                pass

            def futures_symbol_ticker(self, symbol):
                return {"price": "62125.7"}

            def futures_create_order(self, **kwargs):
                self.order_calls += 1
                return {"orderId": 1, "executedQty": "0", "status": "NEW"}

            def futures_cancel_all_open_orders(self, **kwargs):
                pass

            def futures_cancel_all_algo_open_orders(self, **kwargs):
                pass

        client = Client()
        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.client = client
        bot.g = GlobalConfig(base_margin=2.5, leverage=10)
        bot.baskets = {}
        bot.oracle = None
        bot.lot_filter_cache = {"BTCUSDT": {"stepSize": 0.001, "tickSize": 0.1, "minQty": 0.001}}
        bot._enter(SymbolConfig("BTCUSDT", tp_pct=0.3, stop_pct=3.0))
        self.assertEqual(client.order_calls, 0)
        self.assertNotIn("BTCUSDT", bot.baskets)


class ReconcileOnStartTests(unittest.TestCase):
    """Regression tests for reconcile_on_start() / _reconcile_symbol()."""

    _BASE_SCFG = SymbolConfig("BNBUSDT", tp_pct=0.5, stop_pct=3.0)

    def _make_bot(self, client):
        """Build a minimal live-mode bot pointing at the given fake client."""
        cfg = BotConfig(
            symbols=[self._BASE_SCFG],
            global_=GlobalConfig(leverage=10, base_margin=5.0, max_dca_orders=2),
            api_key="key",
            api_secret="secret",
        )
        bot = object.__new__(DcaBot)
        bot.cfg = cfg
        bot.dry_run = False
        bot.g = cfg.global_
        bot.client = client
        bot.baskets = {}
        bot.strategies = {}
        bot.last_candle_time = {}
        bot.symbol_info_cache = {}
        bot.lot_filter_cache = {
            "BNBUSDT": {"stepSize": 0.01, "tickSize": 0.01, "minQty": 0.01},
        }
        bot.last_trade = {}
        bot.oracle = None
        return bot

    # ------------------------------------------------------------------
    # Case (a): open position + live TP + live stop → adopted, no re-placement
    # ------------------------------------------------------------------
    def test_reconcile_adopts_position_with_tp_and_stop_no_replacement(self):
        call_log = []

        class FakeClient:
            def futures_position_information(self, symbol):
                return [{"positionAmt": "0.5", "entryPrice": "500.0"}]

            def futures_get_open_orders(self, symbol):
                return [
                    # live TP (normal order)
                    {"orderId": 201, "type": "TAKE_PROFIT_MARKET", "side": "SELL",
                     "reduceOnly": True},
                    # live Stop (normal order)
                    {"orderId": 301, "type": "STOP_MARKET", "side": "SELL",
                     "reduceOnly": True},
                ]

            def futures_get_open_algo_orders(self, symbol):
                return []

            def futures_create_order(self_, **kw):
                call_log.append(("create_order", kw))
                return {"orderId": 999, "executedQty": "0", "status": "NEW"}

        bot = self._make_bot(FakeClient())
        with self.assertLogs("bot", level="INFO"):
            bot.reconcile_on_start()

        # Basket registered
        self.assertIn("BNBUSDT", bot.baskets)
        b = bot.baskets["BNBUSDT"]
        self.assertEqual(b.state, "active")
        self.assertAlmostEqual(b.avg_entry, 500.0)
        self.assertAlmostEqual(b.size_base, 0.5)

        # IDs captured from normal orders
        self.assertEqual(b.tp_order_id, 201)
        self.assertFalse(b.tp_is_algo)
        self.assertEqual(b.stop_order_id, 301)
        self.assertFalse(b.stop_is_algo)

        # No new orders placed (both already exist)
        create_calls = [c for c in call_log if c[0] == "create_order"]
        self.assertEqual(len(create_calls), 0,
                         "No new orders should be placed when TP and stop already exist")

    # ------------------------------------------------------------------
    # Case (a2): open position + live algo TP + live algo stop → ids captured
    # ------------------------------------------------------------------
    def test_reconcile_adopts_algo_tp_and_stop_ids(self):
        call_log = []

        class FakeClient:
            def futures_position_information(self, symbol):
                return [{"positionAmt": "0.04", "entryPrice": "577.69"}]

            def futures_get_open_orders(self, symbol):
                return []

            def futures_get_open_algo_orders(self, symbol):
                return [
                    {"algoId": 3000001931015058, "symbol": "BNBUSDT",
                     "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "reduceOnly": True},
                    {"algoId": 3000001931015059, "symbol": "BNBUSDT",
                     "orderType": "STOP_MARKET", "side": "SELL", "reduceOnly": True},
                ]

            def futures_create_order(self_, **kw):
                call_log.append(("create_order", kw))
                return {"orderId": 999}

        bot = self._make_bot(FakeClient())
        with self.assertLogs("bot", level="INFO"):
            bot.reconcile_on_start()

        self.assertIn("BNBUSDT", bot.baskets)
        b = bot.baskets["BNBUSDT"]
        self.assertEqual(b.tp_order_id, 3000001931015058)
        self.assertTrue(b.tp_is_algo)
        self.assertEqual(b.stop_order_id, 3000001931015059)
        self.assertTrue(b.stop_is_algo)

        # No re-placement
        self.assertEqual(len([c for c in call_log if c[0] == "create_order"]), 0)

    # ------------------------------------------------------------------
    # Case (b): open position but MISSING stop → adopted AND new stop placed
    # ------------------------------------------------------------------
    def test_reconcile_replaces_missing_stop(self):
        call_log = []

        class FakeClient:
            def futures_position_information(self, symbol):
                return [{"positionAmt": "0.5", "entryPrice": "500.0"}]

            def futures_get_open_orders(self, symbol):
                return [
                    # live TP present
                    {"orderId": 201, "type": "TAKE_PROFIT_MARKET", "side": "SELL",
                     "reduceOnly": True},
                    # stop is MISSING — not in list
                ]

            def futures_get_open_algo_orders(self, symbol):
                return []

            def futures_create_order(self_, **kw):
                call_log.append(("create_order", kw))
                return {"orderId": 999 + len(call_log), "executedQty": "0", "status": "NEW"}

        bot = self._make_bot(FakeClient())
        with self.assertLogs("bot", level="WARNING") as log_ctx:
            bot.reconcile_on_start()

        # Basket registered
        self.assertIn("BNBUSDT", bot.baskets)
        b = bot.baskets["BNBUSDT"]

        # Existing TP kept
        self.assertEqual(b.tp_order_id, 201)

        # Exactly one new order placed (the stop)
        create_types = [c[1].get("type") for c in call_log if c[0] == "create_order"]
        self.assertIn("STOP_MARKET", create_types,
                      "A new STOP_MARKET order must be placed for missing stop")
        self.assertNotIn("TAKE_PROFIT_MARKET", create_types,
                         "TP must NOT be re-placed when it already exists")

        # New stop id captured
        self.assertIsNotNone(b.stop_order_id,
                             "stop_order_id must be set after re-placement")

        # Warning logged
        self.assertTrue(any("re-placing missing" in m for m in log_ctx.output),
                        "Warning about missing exit orders must be logged")

    # ------------------------------------------------------------------
    # Case (c): flat symbol → no basket created
    # ------------------------------------------------------------------
    def test_reconcile_skips_flat_symbol(self):
        class FakeClient:
            def futures_position_information(self, symbol):
                return [{"positionAmt": "0.0", "entryPrice": "0.0"}]

            def futures_get_open_orders(self, symbol):
                return []

            def futures_get_open_algo_orders(self, symbol):
                return []

        bot = self._make_bot(FakeClient())
        bot.reconcile_on_start()

        self.assertNotIn("BNBUSDT", bot.baskets,
                         "No basket should be created for a flat position")

    # ------------------------------------------------------------------
    # Case (d): dry_run → nothing placed, nothing registered
    # ------------------------------------------------------------------
    def test_reconcile_dry_run_places_nothing(self):
        call_log = []

        class FakeClient:
            def futures_position_information(self, symbol):
                return [{"positionAmt": "0.5", "entryPrice": "500.0"}]

            def futures_create_order(self_, **kw):
                call_log.append(kw)
                return {"orderId": 1}

        cfg = BotConfig(
            symbols=[self._BASE_SCFG],
            global_=GlobalConfig(leverage=10, base_margin=5.0),
            api_key="key",
            api_secret="secret",
        )
        bot = object.__new__(DcaBot)
        bot.cfg = cfg
        bot.dry_run = True
        bot.g = cfg.global_
        bot.client = FakeClient()
        bot.baskets = {}
        bot.strategies = {}
        bot.last_candle_time = {}
        bot.symbol_info_cache = {}
        bot.lot_filter_cache = {}
        bot.last_trade = {}
        bot.oracle = None

        with self.assertLogs("bot", level="INFO") as log_ctx:
            bot.reconcile_on_start()

        # Nothing placed
        self.assertEqual(len(call_log), 0,
                         "dry_run must not place any orders")
        # Nothing registered
        self.assertNotIn("BNBUSDT", bot.baskets,
                         "dry_run must not register a basket")
        # Logged what would happen
        self.assertTrue(any("dry-run" in m.lower() or "would adopt" in m.lower()
                            for m in log_ctx.output),
                        "dry_run must log the would-adopt message")

    # ------------------------------------------------------------------
    # Case (e): per-symbol error does not abort reconciliation of other symbols
    # ------------------------------------------------------------------
    def test_reconcile_error_on_one_symbol_does_not_abort(self):
        class FakeClient:
            def futures_position_information(self, symbol):
                raise RuntimeError("Exchange down")

        bot = self._make_bot(FakeClient())
        # Should not raise; error logged instead
        with self.assertLogs("bot", level="ERROR"):
            bot.reconcile_on_start()

        self.assertNotIn("BNBUSDT", bot.baskets)


    # ------------------------------------------------------------------
    # Case (f): partial safety ladder (4 of 5 open) → top up missing rung
    # ------------------------------------------------------------------
    def test_reconcile_tops_up_missing_safety_rung(self):
        """When 4 of 5 safety rungs are open (rung 1 missing), reconcile places exactly one new BUY LIMIT order.

        Setup:
          base_margin=5, leverage=10, entry=500
          base_notional = 50, base_qty_est = 0.1
          Ladder prices (drop_step_pct=0.5, step_multiplier=1.0):
            rung1=497.5 (MISSING), rung2=495.0, rung3=492.5, rung4=490.0, rung5=487.5
        """
        create_calls = []
        ENTRY = 500.0
        BASE_QTY = 0.1  # = base_margin * leverage / entry = 50 / 500

        class FakeClient:
            def futures_position_information(self, symbol):
                return [{"positionAmt": str(BASE_QTY), "entryPrice": str(ENTRY)}]

            def futures_get_open_orders(self, symbol):
                return [
                    {"orderId": 201, "type": "TAKE_PROFIT_MARKET", "side": "SELL",
                     "reduceOnly": True},
                    {"orderId": 301, "type": "STOP_MARKET", "side": "SELL",
                     "reduceOnly": True},
                    # Rungs 2-5 open; rung 1 (497.5) is MISSING
                    {"orderId": 401, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "495.0"},
                    {"orderId": 402, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "492.5"},
                    {"orderId": 403, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "490.0"},
                    {"orderId": 404, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "487.5"},
                ]

            def futures_get_open_algo_orders(self, symbol):
                return []

            def futures_create_order(self_, **kw):
                create_calls.append(kw)
                return {"orderId": 5000 + len(create_calls), "status": "NEW"}

        gcfg = GlobalConfig(leverage=10, base_margin=5.0, max_dca_orders=5)
        scfg = SymbolConfig("BNBUSDT", tp_pct=0.5, stop_pct=3.0)
        cfg = BotConfig(symbols=[scfg], global_=gcfg, api_key="key", api_secret="secret")

        bot = object.__new__(DcaBot)
        bot.cfg = cfg
        bot.dry_run = False
        bot.g = gcfg
        bot.client = FakeClient()
        bot.baskets = {}
        bot.strategies = {}
        bot.last_candle_time = {}
        bot.symbol_info_cache = {}
        bot.lot_filter_cache = {
            "BNBUSDT": {"stepSize": 0.01, "tickSize": 0.01, "minQty": 0.01, "minNotional": 5.0},
        }
        bot.last_trade = {}
        bot.oracle = None

        with self.assertLogs("bot", level="INFO"):
            bot.reconcile_on_start()

        self.assertIn("BNBUSDT", bot.baskets)
        b = bot.baskets["BNBUSDT"]

        # Exactly one BUY LIMIT top-up order (the missing rung 1)
        limit_topup = [c for c in create_calls
                       if c.get("type") == "LIMIT" and c.get("side") == "BUY"]
        self.assertEqual(len(limit_topup), 1,
                         "Exactly one top-up order must be placed for the missing rung")

        placed = limit_topup[0]
        price = float(placed["price"])
        qty = float(placed["quantity"])

        # Price must be at rung-1 level (~497.5), NOT near rungs 2-5
        self.assertAlmostEqual(price, 497.5, delta=0.1,
                               msg=f"Top-up price should be ~497.5 (rung 1), got {price}")
        for existing_price in (495.0, 492.5, 490.0, 487.5):
            self.assertGreater(abs(price - existing_price), 0.1,
                               f"Top-up price {price} too close to existing rung {existing_price}")

        # Notional must clear minNotional
        self.assertGreaterEqual(qty * price, 5.0 - 1e-9,
                                f"Top-up notional {qty*price:.4f} below minNotional 5.0")

        # Basket must track all 5 safety ids (4 original + 1 new)
        self.assertEqual(len(b.safety_order_ids), 5,
                         "basket must track all 5 safety order ids after top-up")

    def test_reconcile_no_topup_when_all_rungs_present(self):
        """When all 5 safety rungs are already open, no top-up order is placed."""
        create_calls = []
        ENTRY = 500.0
        BASE_QTY = 0.1

        class FakeClient:
            def futures_position_information(self, symbol):
                return [{"positionAmt": str(BASE_QTY), "entryPrice": str(ENTRY)}]

            def futures_get_open_orders(self, symbol):
                return [
                    {"orderId": 201, "type": "TAKE_PROFIT_MARKET", "side": "SELL",
                     "reduceOnly": True},
                    {"orderId": 301, "type": "STOP_MARKET", "side": "SELL",
                     "reduceOnly": True},
                    {"orderId": 401, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "497.5"},
                    {"orderId": 402, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "495.0"},
                    {"orderId": 403, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "492.5"},
                    {"orderId": 404, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "490.0"},
                    {"orderId": 405, "type": "LIMIT", "side": "BUY",
                     "reduceOnly": False, "price": "487.5"},
                ]

            def futures_get_open_algo_orders(self, symbol):
                return []

            def futures_create_order(self_, **kw):
                create_calls.append(kw)
                return {"orderId": 9999, "status": "NEW"}

        gcfg = GlobalConfig(leverage=10, base_margin=5.0, max_dca_orders=5)
        scfg = SymbolConfig("BNBUSDT", tp_pct=0.5, stop_pct=3.0)
        cfg = BotConfig(symbols=[scfg], global_=gcfg, api_key="key", api_secret="secret")

        bot = object.__new__(DcaBot)
        bot.cfg = cfg
        bot.dry_run = False
        bot.g = gcfg
        bot.client = FakeClient()
        bot.baskets = {}
        bot.strategies = {}
        bot.last_candle_time = {}
        bot.symbol_info_cache = {}
        bot.lot_filter_cache = {
            "BNBUSDT": {"stepSize": 0.01, "tickSize": 0.01, "minQty": 0.01, "minNotional": 5.0},
        }
        bot.last_trade = {}
        bot.oracle = None

        with self.assertLogs("bot", level="INFO"):
            bot.reconcile_on_start()

        self.assertIn("BNBUSDT", bot.baskets)
        limit_topup = [c for c in create_calls
                       if c.get("type") == "LIMIT" and c.get("side") == "BUY"]
        self.assertEqual(len(limit_topup), 0,
                         "No top-up when all 5 rungs already open")


class SafetyMinNotionalRegressionTests(unittest.TestCase):
    """Regression: safety orders must never be sent below minNotional (-4164).

    When base_margin is small, the base order is risk-scaled up to minNotional.
    The smallest safety rung (base * first_order_ratio ~0.96) then gets its qty
    floored by round_step_size and can land below minNotional, which Binance
    rejects with APIError(code=-4164). The bot must bump such a rung's qty up to
    clear minNotional before sending it.
    """

    def _make_recording_client(self, price, calls):
        class Client:
            def futures_change_leverage(self, **kw):
                pass

            def futures_symbol_ticker(self, symbol):
                return {"price": str(price)}

            def futures_create_order(self_, **kw):
                calls.append(kw)
                oid = 1000 + len(calls)
                return {"orderId": oid, "executedQty": str(kw.get("quantity", 0)),
                        "status": "NEW"}

            def futures_cancel_all_open_orders(self_, **kw):
                pass

            def futures_cancel_all_algo_open_orders(self_, **kw):
                pass

        return Client()

    def test_safety_orders_never_below_min_notional(self):
        # Reproduces the live log: integer-qty symbol near ~1.78, base_margin 2.5,
        # 5 DCA rungs. Pre-fix, safety order 1 was sent at ~3.55 notional (< 5).
        price = 1.783
        min_notional = 5.0
        calls = []
        client = self._make_recording_client(price, calls)

        bot = object.__new__(DcaBot)
        bot.dry_run = False
        bot.client = client
        bot.g = GlobalConfig(base_margin=2.5, leverage=10, max_dca_orders=5)
        bot.baskets = {}
        bot.lot_filter_cache = {
            "NEARUSDT": {"stepSize": 1.0, "tickSize": 0.001,
                         "minQty": 1.0, "minNotional": min_notional}
        }
        bot.last_trade = {}
        bot.oracle = None

        bot._enter(SymbolConfig("NEARUSDT", tp_pct=0.5, stop_pct=3.0))

        self.assertIn("NEARUSDT", bot.baskets, "Basket must be created")

        # Every entry LIMIT (safety) order sent must clear minNotional.
        safety_calls = [c for c in calls if c.get("type") == "LIMIT"]
        self.assertGreater(len(safety_calls), 0, "expected safety LIMIT orders")
        for c in safety_calls:
            notional = float(c["quantity"]) * float(c["price"])
            self.assertGreaterEqual(
                notional, min_notional - 1e-9,
                f"safety order sent below minNotional: qty={c['quantity']} "
                f"price={c['price']} notional={notional:.4f}")


if __name__ == "__main__":
    unittest.main()
