"""
Fetch tradable USDT perpetual futures symbols from Binance.

Robust, environment-independent symbol source. The dropdown must never be empty,
even when ``fapi.binance.com`` is unreachable (it is ISP-blocked in the dev
environment — see CLAUDE.md). Resolution order, first non-empty wins:

    1. In-memory cache, if fresh (< TTL).
    2. Live API — tries several Binance host mirrors in turn.
       On success, refreshes both the in-memory and the on-disk cache.
    3. On-disk cache (``perps_cache.json``), at ANY age — survives restarts and
       outlives the process, so a single successful fetch keeps the dropdown
       populated indefinitely.
    4. Bundled static list (curated liquid USDT perps) — the floor; guarantees a
       non-empty result with zero network and no prior cache.

Fails closed in spirit but never empty: returns the bundled list rather than [].
Never raises.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("perps")

_HERE = Path(__file__).parent
_DISK_CACHE_PATH = _HERE / "perps_cache.json"  # persistent across runs

# Module-level in-memory cache: (symbols_list, timestamp)
_cache: Optional[tuple[list[str], float]] = None
_cache_ttl_seconds = 3600  # 1 hour

# Binance USDⓈ-M futures host mirrors. fapi1..4 are official load-balanced
# aliases; trying them in turn survives a single host being blocked/down.
_API_HOSTS = (
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
)
_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"

# Bundled fallback — curated liquid USDT perpetuals. Superset of bot_config.yaml
# symbols plus majors. The floor that keeps the dropdown usable with no network
# and no disk cache. May be stale vs the live exchange; treat as a starting set.
_BUNDLED_SYMBOLS = sorted({
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "ATOMUSDT", "NEARUSDT", "INJUSDT", "TIAUSDT", "FILUSDT",
    "OPUSDT", "ARBUSDT", "APTUSDT", "UNIUSDT", "SUIUSDT", "AAVEUSDT",
    "ETCUSDT", "MATICUSDT", "SHIBUSDT", "PEPEUSDT", "WLDUSDT", "SEIUSDT",
    "RUNEUSDT", "FTMUSDT", "ALGOUSDT", "EGLDUSDT", "SANDUSDT", "AXSUSDT",
    "GALAUSDT", "ICPUSDT", "FETUSDT", "RNDRUSDT", "IMXUSDT", "STXUSDT",
    "ORDIUSDT", "JUPUSDT", "PYTHUSDT", "ENAUSDT", "ONDOUSDT", "WIFUSDT",
})


def _parse_exchange_info(data: dict) -> list[str]:
    """Filter exchangeInfo payload to tradable PERPETUAL USDT symbols, sorted."""
    symbols = [
        s["symbol"]
        for s in data.get("symbols", [])
        if (
            s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        )
    ]
    symbols.sort()
    return symbols


def _fetch_from_api() -> list[str]:
    """Try each host mirror; return symbols from the first that responds.

    Raises the last exception if every host fails.
    """
    last_exc: Optional[Exception] = None
    for host in _API_HOSTS:
        try:
            resp = requests.get(host + _EXCHANGE_INFO_PATH, timeout=10)
            resp.raise_for_status()
            symbols = _parse_exchange_info(resp.json())
            if symbols:
                return symbols
            last_exc = ValueError(f"{host}: empty symbol set after filter")
        except Exception as e:  # noqa: BLE001 — try next host
            last_exc = e
            logger.debug("perps: host %s failed: %r", host, e)
    raise last_exc or RuntimeError("no API hosts configured")


def _load_disk_cache() -> Optional[tuple[list[str], float]]:
    """Return (symbols, timestamp) from the on-disk cache, or None if absent/bad."""
    if not _DISK_CACHE_PATH.exists():
        return None
    try:
        with open(_DISK_CACHE_PATH, encoding="utf-8") as f:
            obj = json.load(f)
        syms = obj.get("symbols")
        ts = float(obj.get("timestamp", 0.0))
        if isinstance(syms, list) and syms:
            return [str(s) for s in syms], ts
    except Exception as e:  # noqa: BLE001
        logger.warning("perps: disk cache unreadable: %r", e)
    return None


def _save_disk_cache(symbols: list[str], timestamp: float) -> None:
    """Persist symbols to disk atomically (temp file + replace)."""
    try:
        tmp = _DISK_CACHE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"symbols": symbols, "timestamp": timestamp}, f)
        tmp.replace(_DISK_CACHE_PATH)
    except Exception as e:  # noqa: BLE001 — caching is best-effort
        logger.warning("perps: failed to write disk cache: %r", e)


def fetch_usdt_perps() -> list[str]:
    """
    Return a sorted list of tradable USDT perpetual futures symbols.

    Never raises and never returns []: falls back through on-disk cache and then
    a bundled static list. See module docstring for the full resolution order.
    """
    global _cache

    now = time.time()

    # 1. Fresh in-memory cache.
    if _cache is not None and now - _cache[1] < _cache_ttl_seconds:
        return _cache[0]

    # 2. Live API (host mirrors).
    try:
        symbols = _fetch_from_api()
        _cache = (symbols, now)
        _save_disk_cache(symbols, now)
        return symbols
    except Exception as e:  # noqa: BLE001
        logger.warning("perps: live fetch failed across all hosts: %r", e)

    # 3a. Stale in-memory cache (better than re-reading disk).
    if _cache is not None:
        return _cache[0]

    # 3b. On-disk cache at any age.
    disk = _load_disk_cache()
    if disk is not None:
        logger.info("perps: serving on-disk cache (%d symbols)", len(disk[0]))
        _cache = disk  # promote to memory
        return disk[0]

    # 4. Bundled floor.
    logger.warning("perps: no network and no cache — serving bundled list")
    return list(_BUNDLED_SYMBOLS)


if __name__ == "__main__":
    syms = fetch_usdt_perps()
    print(f"Total USDT perps: {len(syms)}")
    print(f"First 10: {syms[:10]}")
