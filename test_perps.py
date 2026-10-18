"""
test_perps.py — Tests for the hardened USDT-perp symbol source.

Verifies the layered fallback chain (live API → in-memory cache → on-disk cache
→ bundled list) and the core invariant: fetch_usdt_perps() never raises and
never returns []. All network calls are mocked; no real Binance traffic.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import perps


def _fake_exchange_info(symbols):
    """Build a minimal exchangeInfo payload that _parse_exchange_info accepts."""
    return {
        "symbols": [
            {
                "symbol": s,
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
            }
            for s in symbols
        ]
    }


class PerpsTestBase(unittest.TestCase):
    def setUp(self):
        # Isolate every test: reset in-memory cache and redirect disk cache to a
        # tmp path so we never touch the real perps_cache.json.
        perps._cache = None
        self._tmpdir = __import__("tempfile").TemporaryDirectory()
        self._patch_disk = patch.object(
            perps, "_DISK_CACHE_PATH",
            __import__("pathlib").Path(self._tmpdir.name) / "perps_cache.json",
        )
        self._patch_disk.start()

    def tearDown(self):
        self._patch_disk.stop()
        self._tmpdir.cleanup()
        perps._cache = None


class TestLiveFetch(PerpsTestBase):
    def test_live_fetch_filters_and_sorts(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = _fake_exchange_info(["ETHUSDT", "BTCUSDT"])
        with patch("perps.requests.get", return_value=resp) as g:
            out = perps.fetch_usdt_perps()
        self.assertEqual(out, ["BTCUSDT", "ETHUSDT"])  # sorted
        g.assert_called_once()  # first host hit, no mirror needed

    def test_live_fetch_writes_disk_cache(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = _fake_exchange_info(["BTCUSDT"])
        with patch("perps.requests.get", return_value=resp):
            perps.fetch_usdt_perps()
        self.assertTrue(perps._DISK_CACHE_PATH.exists())
        with open(perps._DISK_CACHE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["symbols"], ["BTCUSDT"])

    def test_host_mirror_failover(self):
        """First host raises; second host succeeds."""
        ok = MagicMock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = _fake_exchange_info(["BTCUSDT"])

        calls = {"n": 0}

        def side(url, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise perps.requests.ConnectionError("blocked")
            return ok

        with patch("perps.requests.get", side_effect=side):
            out = perps.fetch_usdt_perps()
        self.assertEqual(out, ["BTCUSDT"])
        self.assertEqual(calls["n"], 2)


class TestFallbacks(PerpsTestBase):
    def test_disk_cache_used_when_api_down(self):
        # Seed disk cache, then make every host fail.
        perps._save_disk_cache(["FOOUSDT", "BARUSDT"], 0.0)
        with patch("perps.requests.get", side_effect=perps.requests.ConnectionError("x")):
            out = perps.fetch_usdt_perps()
        self.assertEqual(sorted(out), ["BARUSDT", "FOOUSDT"])

    def test_bundled_floor_when_no_network_no_cache(self):
        with patch("perps.requests.get", side_effect=perps.requests.ConnectionError("x")):
            out = perps.fetch_usdt_perps()
        self.assertEqual(out, sorted(perps._BUNDLED_SYMBOLS))
        self.assertGreater(len(out), 0)

    def test_never_empty_never_raises(self):
        # Empty filtered API result must NOT be served as []; falls to bundled.
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"symbols": []}
        with patch("perps.requests.get", return_value=resp):
            out = perps.fetch_usdt_perps()
        self.assertGreater(len(out), 0)

    def test_in_memory_cache_short_circuits(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = _fake_exchange_info(["BTCUSDT"])
        with patch("perps.requests.get", return_value=resp) as g:
            perps.fetch_usdt_perps()
            perps.fetch_usdt_perps()  # second call should hit memory, not network
        g.assert_called_once()


if __name__ == "__main__":
    unittest.main()
