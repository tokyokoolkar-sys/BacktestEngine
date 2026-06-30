"""
bot.py — Autonomous 1h DCA scalper for Binance USDⓈ-M Futures.

Usage:
    python bot.py --config bot_config.yaml           # loop forever
    python bot.py --config bot_config.yaml --once     # single iteration
    python bot.py --config bot_config.yaml --dry-run  # simulate, no orders
    python bot.py --config bot_config.yaml --status   # print active positions

Strategy: red_dip entry → DCA safety orders at 0.5% drops → TP at per-symbol % → 3% stop.
Reuses CheckpointStrategy from strategy.py for signal logic.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException, BinanceRequestException
from binance.helpers import round_step_size

# Ensure project root is on path
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Load .env (Binance HMAC keys, oracle, control token) so the dashboard-launched
# subprocess inherits real credentials even when the parent shell didn't export them.
# override=True: .env is the canonical credential source for this project — a stale
# or wrong-type key exported in the parent shell (e.g. an Ed25519 key) must not win.
try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env", override=True)
except ImportError:
    pass

from config import StrategyConfig
from strategy import CheckpointStrategy

logger = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# PID helpers (duplicated in trade.py — do NOT extract to shared module to
# avoid circular imports since trade.py imports DcaBot from bot.py)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is currently running.

    Cross-platform:
    - POSIX: os.kill(pid, 0) raises OSError(ESRCH) when the process does not exist.
    - Windows: os.kill(pid, 0) is NOT supported; use ctypes OpenProcess instead.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle == 0:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------

@dataclass
class SymbolConfig:
    name: str
    tp_pct: float
    stop_pct: float
    active: bool = True
    max_dca_orders: int | None = None       # override global max_dca_orders per symbol


@dataclass
class GlobalConfig:
    leverage: int = 10
    base_margin: float = 2.5
    dca_margin: float = 2.5
    max_dca_orders: int = 2
    drop_step_pct: float = 0.5
    step_multiplier: float = 1.0
    size_multiplier: float = 1.1
    first_order_ratio: float = 0.96
    max_concurrent_baskets: int = 5
    daily_loss_limit: float = 5.0
    fee_taker: float = 0.0005
    fee_maker: float = 0.0002
    poll_interval_sec: int = 60
    risk_per_trade_pct: float = 0.0       # 0 = use fixed base_margin; >0 = % of balance
    trail_activate_pct: float = 1.0       # 0 = disabled, % above avg_entry to activate
    trail_buffer_pct: float = 0.3         # trail stop = highest_price * (1 - buffer/100)
    entry_volume_filter: bool = False     # skip entry when volume > entry_volume_max_ratio × 24h avg
    entry_volume_max_ratio: float = 2.5   # volume threshold
    dual_mode: bool = False                # alternate long/short on each new entry


@dataclass
class BotConfig:
    symbols: list[SymbolConfig]
    global_: GlobalConfig
    api_key: str
    api_secret: str

    @classmethod
    def from_yaml(cls, path: str) -> BotConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)
        syms = [SymbolConfig(**s) for s in raw["symbols"]]
        glob = GlobalConfig(**raw.get("global", {}))
        return cls(
            symbols=syms,
            global_=glob,
            api_key=os.path.expandvars(raw["api"]["key"]),
            api_secret=os.path.expandvars(raw["api"]["secret"]),
        )

    def active_symbols(self) -> list[SymbolConfig]:
        out = []
        seen = set()
        for s in self.symbols:
            if s.active and s.name not in seen:
                out.append(s)
                seen.add(s.name)
        return out


# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------

TRADE_LOG_PATH = _HERE / "bot_trade_log.csv"
_TRADE_LOG_COLS = [
    "symbol", "entry_time", "exit_time", "entry_price", "exit_price",
    "gross_pnl", "fees", "funding", "net_pnl", "pnl_pct",
    "exit_reason", "n_safety_filled", "base_margin", "leverage",
]


def _init_trade_log():
    if not TRADE_LOG_PATH.exists():
        with open(TRADE_LOG_PATH, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(_TRADE_LOG_COLS)
        logger.info("Trade log created: %s", TRADE_LOG_PATH)


def _write_trade(row: dict):
    with open(TRADE_LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_TRADE_LOG_COLS)
        w.writerow(row)
    logger.info("Trade logged: %s", row.get("symbol"))


def _daily_pnl() -> float:
    """Sum of net_pnl for today's trades."""
    if not TRADE_LOG_PATH.exists():
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    with open(TRADE_LOG_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["exit_time"].startswith(today):
                total += float(row["net_pnl"])
    return total


# ---------------------------------------------------------------------------
# Basket state (in-memory, per position)
# ---------------------------------------------------------------------------

@dataclass
class Basket:
    symbol: str
    scfg: SymbolConfig
    gcfg: GlobalConfig
    state: str = "entering"          # entering | active | closing
    entry_price: float | None = None
    avg_entry: float | None = None
    size_base: float = 0.0
    total_notional: float = 0.0
    total_margin: float = 0.0
    total_fees: float = 0.0
    base_order_id: int | None = None
    safety_order_ids: list[int] = field(default_factory=list)
    tp_order_id: int | None = None
    tp_is_algo: bool = False       # True when tp_order_id is an algoId (conditional order)
    stop_order_id: int | None = None
    stop_is_algo: bool = False     # True when stop_order_id is an algoId (conditional order)
    n_safety_filled: int = 0
    entry_time: str | None = None
    exit_reason: str | None = None
    trail_activated: bool = False
    trail_highest: float = 0.0
    trail_stop_price: float | None = None
    trail_active_stop_order_id: int | None = None
    trail_stop_is_algo: bool = False
    logged: bool = False           # True once this basket's close row is written — guards
                                   # against re-logging if closure fires twice (e.g. a
                                   # cancel raises before the basket is deleted)


# ---------------------------------------------------------------------------
# DCA notional ladder (mirrors engine.py helpers)
# ---------------------------------------------------------------------------

def _dca_total_ratio(gcfg: GlobalConfig, dca_n: int | None = None) -> float:
    r0 = gcfg.first_order_ratio
    m = gcfg.size_multiplier
    r = r0
    tr = 1.0
    for _ in range(dca_n if dca_n is not None else gcfg.max_dca_orders):
        tr += r
        r *= m
    return tr


def _dca_safety_notionals(gcfg: GlobalConfig, base_notional: float, dca_n: int | None = None) -> list[float]:
    r0 = gcfg.first_order_ratio
    m = gcfg.size_multiplier
    out = []
    prev = base_notional * r0
    for k in range(dca_n if dca_n is not None else gcfg.max_dca_orders):
        if k == 0:
            out.append(prev)
        else:
            prev = out[-1] * m
            out.append(prev)
    return out


def _dca_safety_prices(entry_price: float, gcfg: GlobalConfig, dca_n: int | None = None) -> list[float]:
    """Cumulative drop % → absolute price levels."""
    cum = 0.0
    step = gcfg.drop_step_pct
    prices = []
    for k in range(dca_n if dca_n is not None else gcfg.max_dca_orders):
        cum += step * (gcfg.step_multiplier ** k)
        prices.append(entry_price * (1.0 - cum / 100.0))
    return prices


# ---------------------------------------------------------------------------
# Binance helpers
# ---------------------------------------------------------------------------

def _symbol_info(client: BinanceClient, symbol: str) -> dict:
    """Get exchange info for one symbol (lot size, tick, etc.)."""
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            return s
    raise ValueError(f"Symbol {symbol} not found in exchange info")


def _lot_filter(sinfo: dict) -> dict:
    """Extract LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL from symbol info."""
    out = {}
    for f in sinfo["filters"]:
        if f["filterType"] == "LOT_SIZE":
            out.update({"minQty": float(f["minQty"]), "maxQty": float(f["maxQty"]), "stepSize": float(f["stepSize"])})
        if f["filterType"] == "PRICE_FILTER":
            out.update({"minPrice": float(f["minPrice"]), "maxPrice": float(f["maxPrice"]), "tickSize": float(f["tickSize"])})
        if f["filterType"] == "MIN_NOTIONAL":
            out["minNotional"] = float(f["notional"])
    if "minNotional" not in out:
        out["minNotional"] = 5.0  # default Binance minimum
    return out


def _round_qty(qty: float, lf: dict) -> float:
    return round_step_size(qty, lf["stepSize"])


def _round_price(price: float, lf: dict) -> float:
    return round_step_size(price, lf["tickSize"])


# ---------------------------------------------------------------------------
# Main bot
# ---------------------------------------------------------------------------

class DcaBot:
    """Autonomous DCA scalper for Binance Futures."""

    def __init__(self, config: BotConfig, dry_run: bool = False):
        self.cfg = config
        self.dry_run = dry_run
        self.g = config.global_

        # Binance client (Futures)
        if not dry_run:
            self.client = BinanceClient(config.api_key, config.api_secret)
            # Sync local clock drift to Binance server time (fixes -1021 timestamp errors)
            server_ms = self.client.futures_time()["serverTime"]
            self.client.timestamp_offset = server_ms - int(time.time() * 1000)
            logger.info("Clock offset to Binance server: %d ms", self.client.timestamp_offset)
            self.client.futures_change_leverage(symbol=config.active_symbols()[0].name, leverage=self.g.leverage) if config.active_symbols() else None
        else:
            self.client = None

        # Per-symbol state
        self.baskets: dict[str, Basket] = {}
        self.strategies: dict[str, CheckpointStrategy] = {}
        self.last_candle_time: dict[str, int] = {}
        self.symbol_info_cache: dict[str, dict] = {}
        self.lot_filter_cache: dict[str, dict] = {}
        self.last_trade: dict[str, dict | None] = {}

        for sc in config.active_symbols():
            self.strategies[sc.name] = CheckpointStrategy(StrategyConfig(
                entry_mode="red_dip",
                green_gate="off",
                stop_mode="none",
                checkpoint_mode="first_green",
                position_model="single",
                direction="long",
                dual_mode=self.g.dual_mode,
            ))
            self.last_trade[sc.name] = None

        if not dry_run:
            _init_trade_log()

        # Engine seam: trade.py sets False so the new-entry signal scan is skipped
        # while basket management (TP/stop/safety) continues normally.
        self.auto_entry: bool = True

        # Control state — updated each cycle from bot_control.json
        self.paused: bool = False
        self.disabled_symbols: list[str] = []

    def _all_symbols_str(self) -> str:
        return ",".join(s.name for s in self.cfg.active_symbols())

    # ------------------------------------------------------------------
    # Fetch klines
    # ------------------------------------------------------------------

    def _fetch_klines(self, symbol: str, limit: int = 3) -> list | None:
        # try limit=3 so we have prev + current for strategy
        if self.dry_run:
            return None
        try:
            return self.client.futures_klines(symbol=symbol, interval="1h", limit=limit)
        except Exception as e:
            logger.warning("%s: klines fetch error: %s", symbol, e)
            return None

    # ------------------------------------------------------------------
    # Cached exchange info
    # ------------------------------------------------------------------

    def _get_lot_filter(self, symbol: str) -> dict:
        if self.dry_run:
            return {"stepSize": 0.001, "tickSize": 0.001, "minQty": 0.001}
        if symbol not in self.lot_filter_cache:
            sinfo = _symbol_info(self.client, symbol)
            self.symbol_info_cache[symbol] = sinfo
            self.lot_filter_cache[symbol] = _lot_filter(sinfo)
        return self.lot_filter_cache[symbol]

    # ------------------------------------------------------------------
    # Position / balance checks
    # ------------------------------------------------------------------

    def _available_balance(self) -> float:
        if self.dry_run:
            return 1000.0
        try:
            bal = self.client.futures_account_balance()
            for b in bal:
                if b["asset"] == "USDT":
                    return float(b["availableBalance"])
            return 0.0
        except Exception as e:
            logger.error("Balance fetch failed: %s", e)
            return 0.0

    def _position_size(self, symbol: str) -> float:
        """Return current position amount (positive = long)."""
        if self.dry_run:
            return 0.0
        try:
            pos = self.client.futures_position_information(symbol=symbol)
            return float(pos[0]["positionAmt"]) if pos else 0.0
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Risk gates
    # ------------------------------------------------------------------

    def _risk_check(self) -> bool:
        """Return True if OK to continue. False = stop everything."""
        # Daily loss limit
        dpl = _daily_pnl()
        if dpl <= -self.g.daily_loss_limit:
            logger.error(
                "DAILY LOSS LIMIT — human intervention required. "
                "PnL %.2f reached limit %.2f. Cancelling all open orders.",
                dpl, -self.g.daily_loss_limit,
            )
            if not self.dry_run:
                for sc in self.cfg.active_symbols():
                    try:
                        self.client.futures_cancel_all_open_orders(symbol=sc.name)
                        self.client.futures_cancel_all_algo_open_orders(symbol=sc.name)
                        logger.info("Cancelled open orders for %s", sc.name)
                    except Exception as e:
                        logger.error("Failed to cancel open orders for %s: %s", sc.name, e)
            return False
        # Active basket count
        active = sum(1 for b in self.baskets.values() if b.state != "closing")
        if active >= self.g.max_concurrent_baskets:
            logger.info("Max concurrent baskets (%d) reached. Waiting.", self.g.max_concurrent_baskets)
            return False
        # Available balance — block new entries if can't cover one base order
        if not self.dry_run:
            bal = self._available_balance()
            if bal < self.g.base_margin:
                logger.info("Balance %.2f < base_margin %.2f. Waiting for recovery.",
                            bal, self.g.base_margin)
                return False
        return True

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def _check_signal(self, symbol: str) -> tuple[str | None, list | None]:
        """
        Fetch klines, detect new candle close, run strategy.
        Returns (action, klines) where action="enter" or None.
        """
        klines = self._fetch_klines(symbol, limit=15)  # 15 for oracle context
        if klines is None:
            if self.dry_run and symbol == "NEARUSDT":
                logger.info("[DRY-RUN] %s: simulating enter signal", symbol)
                return "enter", None
            return None, None

        if len(klines) < 3:
            return None, None
        latest = klines[-2]  # last closed; klines[-1] is still forming
        close_time = latest[6]

        last_seen = self.last_candle_time.get(symbol, 0)
        if close_time <= last_seen:
            return None, None

        self.last_candle_time[symbol] = close_time

        prev = klines[-3]
        prev_row = {"high": float(prev[2]), "close": float(prev[4])}
        row = {"open": float(latest[1]), "close": float(latest[4]), "high": float(latest[2])}

        strategy = self.strategies[symbol]
        action = strategy.on_closed_bar(prev_row, row, in_position=False)
        logger.info("%s: signal=%s  close=%.4f  is_green=%s",
                     symbol, action, float(latest[4]),
                     "yes" if float(latest[4]) > float(latest[1]) else "no")
        if action == "enter":
            # Volume filter: skip entry during high-volume (panic) conditions
            if self.g.entry_volume_filter:
                try:
                    # Mirror engine.py: trailing 24-bar MA *including* the current bar
                    vols = [float(k[5]) for k in klines[-24:] if k[5]]
                    if len(vols) >= 2:
                        avg_vol = sum(vols) / len(vols)
                        cur_vol = float(latest[5])
                        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
                        if vol_ratio >= self.g.entry_volume_max_ratio:
                            logger.info("%s: VOLUME-FILTER — vol_ratio=%.2f ≥ %.1f, skipping entry",
                                        symbol, vol_ratio, self.g.entry_volume_max_ratio)
                            return None, klines
                except Exception as e:
                    logger.warning("%s: volume filter error: %s", symbol, e)
            return "enter", klines
        return None, klines

    # ------------------------------------------------------------------
    # Enter position (place basket)
    # ------------------------------------------------------------------

    def _enter(self, sc: SymbolConfig):
        """Place DCA basket for one symbol."""
        symbol = sc.name
        g = self.g

        # Per-symbol DCA override: use symbol-level if set, fall back to global
        dca_n = sc.max_dca_orders if sc.max_dca_orders is not None else g.max_dca_orders

        # Set leverage per-symbol before entry
        if not self.dry_run:
            try:
                self.client.futures_change_leverage(symbol=symbol, leverage=g.leverage)
            except Exception as e:
                logger.warning("%s: leverage change: %s", symbol, e)

        lf = self._get_lot_filter(symbol)

        # Get current price
        if self.dry_run:
            entry_price = 10.0
        else:
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            entry_price = float(ticker["price"])

        entry_price_r = _round_price(entry_price, lf)

        # Compute DCA ladder
        # Adaptive sizing: use risk_per_trade_pct of available balance if configured
        if g.risk_per_trade_pct > 0 and not self.dry_run:
            bal = self._available_balance()
            effective_base = bal * g.risk_per_trade_pct
        else:
            effective_base = g.base_margin
        tr = _dca_total_ratio(g, dca_n)
        base_notional = (effective_base * g.leverage) / tr
        base_qty = _round_qty(base_notional / entry_price_r, lf)

        # Ensure minQty and minNotional
        if not self.dry_run:
            min_notional = lf.get("minNotional", 5.0)
            if base_notional < min_notional or base_qty * entry_price_r < min_notional:
                # Scale base_notional to meet minNotional
                scaled_notional = max(base_notional, min_notional)
                base_qty = _round_qty(scaled_notional / entry_price_r, lf)
                while base_qty * entry_price_r < min_notional and base_qty > 0:
                    step = lf.get("stepSize", 0.001)
                    base_qty = _round_qty(base_qty + step, lf)
                base_notional = base_qty * entry_price_r
                effective_base = base_notional * tr / g.leverage
                # Scaling up to minNotional pushes total margin (and stop-loss $ risk)
                # ABOVE the configured base_margin. Backtest no longer maps 1:1. Loud
                # on purpose — see the deliberate "never trade above risk budget" rule.
                inflation = effective_base / g.base_margin if g.base_margin > 0 else 0.0
                logger.warning(
                    "%s: RISK-SCALED to meet minNotional — effective_margin=%.2f$ "
                    "= %.2fx configured base_margin %.2f$ (stop-loss $risk inflated)",
                    symbol, effective_base, inflation, g.base_margin)

        if base_qty < lf["minQty"]:
            min_margin = (lf["minQty"] * entry_price_r) * tr / g.leverage
            logger.warning(
                "%s: SKIP — minQty %.6f needs ~%.2f$ margin > configured base_margin %.2f$",
                symbol, lf["minQty"], min_margin, g.base_margin)
            return

        safety_notionals = _dca_safety_notionals(g, base_notional, dca_n)
        safety_prices = _dca_safety_prices(entry_price_r, g, dca_n)

        entry_fee = base_notional * g.fee_taker
        total_margin = base_notional * tr / g.leverage

        # Create basket record
        basket = Basket(symbol=symbol, scfg=sc, gcfg=g)
        basket.entry_time = datetime.now(timezone.utc).isoformat()
        basket.entry_price = entry_price_r
        basket.avg_entry = entry_price_r
        basket.total_notional = base_notional
        basket.total_margin = total_margin
        basket.total_fees = entry_fee

        logger.info("%s: ENTER  base=%.4f$ notional=%.2f qty=%s price=%s",
                     symbol, effective_base, base_notional, base_qty, entry_price_r)

        if self.dry_run:
            basket.state = "active"
            basket.size_base = base_qty
            basket.base_order_id = 0
            for i in range(dca_n):
                sq = _round_qty(safety_notionals[i] / safety_prices[i], lf)
                logger.info("  safety %d: qty=%s @ %.4f", i + 1, sq, safety_prices[i])
            tp_price = entry_price_r * (1.0 + sc.tp_pct / 100.0)
            stop_price = entry_price_r * (1.0 - sc.stop_pct / 100.0)
            logger.info("  TP: %.4f  Stop: %.4f", tp_price, stop_price)
            if g.trail_activate_pct > 0:
                logger.info("  Trail: activate at +%.1f%%, buffer %.1f%%",
                            g.trail_activate_pct, g.trail_buffer_pct)
            self.baskets[symbol] = basket
            self._write_status_json()
            return

        # ── Place orders on Binance ──
        try:
            # 1. Base market order
            base_order = self.client.futures_create_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=base_qty,
            )
            basket.base_order_id = base_order["orderId"]
            basket.size_base = float(base_order.get("executedQty", base_qty))
            logger.info("  base order filled: %s", base_order.get("status"))
        except (BinanceAPIException, Exception) as e:
            logger.error("%s: base order placement error: %s", symbol, e)
            self._cancel_orders(symbol)
            return

        # Register basket immediately after base fill so the live position is
        # tracked by _manage_baskets even if subsequent safety/TP/stop placements fail.
        basket.state = "active"
        self.baskets[symbol] = basket

        # 2. Safety limit orders — per-order try/except so a single failure does
        # not abandon the registered basket or prevent TP/stop from being placed.
        min_notional = lf.get("minNotional", 5.0)
        placed_safety_qty = 0.0
        for i in range(dca_n):
            sp = _round_price(safety_prices[i], lf)
            sq = _round_qty(safety_notionals[i] / sp, lf)
            # round_step_size floors qty, so the smallest rung (base*first_order_ratio,
            # ~0.96x) can land below minNotional even when the base order was scaled up
            # to meet it — Binance then rejects with -4164. Bump qty up to clear it.
            while sq * sp < min_notional and sq > 0:
                sq = _round_qty(sq + lf.get("stepSize", 0.001), lf)
            try:
                so = self.client.futures_create_order(
                    symbol=symbol, side="BUY", type="LIMIT",
                    timeInForce="GTC", price=sp, quantity=sq,
                    # no reduceOnly on entry orders
                )
                basket.safety_order_ids.append(so["orderId"])
                placed_safety_qty += sq
                logger.info("  safety %d placed: qty=%s @ %.4f (orderId=%s)",
                             i + 1, sq, sp, so["orderId"])
            except Exception as e:
                logger.error("%s: safety order %d placement error (basket still active): %s",
                             symbol, i + 1, e)

        # 3. TP (TAKE_PROFIT_MARKET, reduceOnly)
        # Cover the actual max position (base fill + every placed safety qty, which the
        # minNotional bump can push above the notional estimate). reduceOnly caps the
        # close at the real position size, so over-stating is safe; under-stating would
        # leave a residual after TP fires.
        total_qty = _round_qty(basket.size_base + placed_safety_qty, lf)
        tp_price = _round_price(entry_price_r * (1.0 + sc.tp_pct / 100.0), lf)
        try:
            tp_resp = self.client.futures_create_order(
                symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                quantity=total_qty, stopPrice=tp_price,
                timeInForce="GTC", reduceOnly=True,
                workingType="MARK_PRICE",
            )
            logger.info("  TP raw resp: %s", dict(tp_resp))
            # Conditional/algo orders return algoId instead of orderId
            basket.tp_order_id = tp_resp.get("orderId") or tp_resp.get("algoId")
            basket.tp_is_algo = "orderId" not in tp_resp
        except Exception as e:
            logger.warning("%s: TP order failed: %s", symbol, e)
        basket.total_notional = base_notional + sum(safety_notionals)
        logger.info("  TP: %.4f (id=%s)", tp_price, basket.tp_order_id)

        # 4. Stop (STOP_MARKET, reduceOnly)
        stop_price = _round_price(entry_price_r * (1.0 - sc.stop_pct / 100.0), lf)
        try:
            stop_resp = self.client.futures_create_order(
                symbol=symbol, side="SELL", type="STOP_MARKET",
                quantity=total_qty, stopPrice=stop_price,
                timeInForce="GTC", reduceOnly=True,
                workingType="MARK_PRICE",
            )
            # Conditional/algo orders return algoId instead of orderId
            basket.stop_order_id = stop_resp.get("orderId") or stop_resp.get("algoId")
            basket.stop_is_algo = "orderId" not in stop_resp
        except Exception as e:
            logger.warning("%s: Stop order failed: %s", symbol, e)
        logger.info("  Stop: %.4f (id=%s)", stop_price, basket.stop_order_id)
        self._write_status_json()

    # ------------------------------------------------------------------
    # Manage existing baskets
    # ------------------------------------------------------------------

    def _manage_baskets(self):
        """Check safety fills, TP/stop hits for all active baskets."""
        for symbol, basket in list(self.baskets.items()):
            if basket.state == "closing":
                continue
            if self.dry_run:
                continue
            self._check_safety_fills(basket)
            self._check_trail(basket)
            self._check_closure(basket)

    def _check_safety_fills(self, basket: Basket):
        """Check if any unfilled safety order has been filled."""
        symbol = basket.symbol
        for oid in list(basket.safety_order_ids):
            try:
                order = self.client.futures_get_order(symbol=symbol, orderId=oid)
            except Exception as e:
                logger.warning("%s: order %s get error: %s", symbol, oid, e)
                continue
            if order["status"] == "FILLED":
                filled_qty = float(order["executedQty"])
                fill_price = float(order["avgPrice"])
                notional = filled_qty * fill_price
                fee = notional * self.g.fee_maker

                # Update weighted avg
                new_notional = basket.total_notional + notional
                if new_notional > 0:
                    basket.avg_entry = (
                        (basket.avg_entry * basket.size_base + fill_price * filled_qty)
                        / (basket.size_base + filled_qty)
                    )
                basket.size_base += filled_qty
                basket.total_notional = new_notional
                basket.total_margin += notional / self.g.leverage
                basket.total_fees += fee
                basket.n_safety_filled += 1

                # Remove from tracking list
                basket.safety_order_ids.remove(oid)
                logger.info("%s: safety order %s FILLED at %.4f qty=%s  avg_entry=%.4f",
                             symbol, oid, fill_price, filled_qty, basket.avg_entry)

                # Cancel old TP, replace with new TP at updated avg_entry
                self._replace_tp(basket)

    def _replace_tp(self, basket: Basket):
        """Cancel current TP and stop orders by tracked ID, then re-place both at updated avg_entry.

        Cancels only basket.tp_order_id and basket.stop_order_id (algo-aware) so that
        still-unfilled DCA safety LIMIT orders are preserved on the exchange.
        """
        symbol = basket.symbol
        lf = self._get_lot_filter(symbol)
        total_qty = _round_qty(basket.total_notional / basket.avg_entry, lf)
        tp_price = _round_price(basket.avg_entry * (1.0 + basket.scfg.tp_pct / 100.0), lf)
        stop_price = _round_price(basket.avg_entry * (1.0 - basket.scfg.stop_pct / 100.0), lf)

        # Cancel only the current TP and stop by their tracked IDs so that
        # still-unfilled DCA safety LIMIT orders are not wiped.
        if basket.tp_order_id is not None:
            try:
                if basket.tp_is_algo:
                    self.client.futures_cancel_algo_order(symbol=symbol, algoId=basket.tp_order_id)
                else:
                    self.client.futures_cancel_order(symbol=symbol, orderId=basket.tp_order_id)
            except Exception as e:
                logger.warning("%s: cancel TP order %s in _replace_tp: %s", symbol, basket.tp_order_id, e)

        if basket.stop_order_id is not None:
            try:
                if basket.stop_is_algo:
                    self.client.futures_cancel_algo_order(symbol=symbol, algoId=basket.stop_order_id)
                else:
                    self.client.futures_cancel_order(symbol=symbol, orderId=basket.stop_order_id)
            except Exception as e:
                logger.warning("%s: cancel stop order %s in _replace_tp: %s", symbol, basket.stop_order_id, e)

        # Cancel trail stop order too if active
        if basket.trail_active_stop_order_id is not None:
            try:
                if basket.trail_stop_is_algo:
                    self.client.futures_cancel_algo_order(symbol=symbol, algoId=basket.trail_active_stop_order_id)
                else:
                    self.client.futures_cancel_order(symbol=symbol, orderId=basket.trail_active_stop_order_id)
            except Exception as e:
                logger.warning("%s: cancel trail stop in _replace_tp: %s", symbol, e)

        # Reset tracked ids — they are no longer valid
        basket.tp_order_id = None
        basket.tp_is_algo = False
        basket.stop_order_id = None
        basket.stop_is_algo = False
        basket.trail_active_stop_order_id = None
        basket.trail_stop_is_algo = False

        # Place new TP
        try:
            o = self.client.futures_create_order(
                symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                quantity=total_qty, stopPrice=tp_price,
                timeInForce="GTC", reduceOnly=True,
                workingType="MARK_PRICE",
            )
            basket.tp_order_id = o.get("orderId") or o.get("algoId")
            basket.tp_is_algo = "orderId" not in o
            logger.info("%s: TP replaced at %.4f qty=%s (algo=%s)", symbol, tp_price, total_qty, basket.tp_is_algo)
        except Exception as e:
            logger.error("%s: replace TP error: %s", symbol, e)

        # Place new stop
        try:
            o = self.client.futures_create_order(
                symbol=symbol, side="SELL", type="STOP_MARKET",
                quantity=total_qty, stopPrice=stop_price,
                timeInForce="GTC", reduceOnly=True,
                workingType="MARK_PRICE",
            )
            basket.stop_order_id = o.get("orderId") or o.get("algoId")
            basket.stop_is_algo = "orderId" not in o
            logger.info("%s: Stop replaced at %.4f qty=%s (algo=%s)", symbol, stop_price, total_qty, basket.stop_is_algo)
        except Exception as e:
            logger.error("%s: replace stop error: %s", symbol, e)

        # Re-place trail stop if active (cancel above killed the old one)
        if basket.trail_activated and basket.trail_stop_price is not None:
            try:
                ts_price = _round_price(basket.trail_stop_price, lf)
                o = self.client.futures_create_order(
                    symbol=symbol, side="SELL", type="STOP_MARKET",
                    quantity=total_qty, stopPrice=ts_price,
                    timeInForce="GTC", reduceOnly=True,
                    workingType="MARK_PRICE",
                )
                basket.trail_active_stop_order_id = o.get("orderId") or o.get("algoId")
                basket.trail_stop_is_algo = "orderId" not in o
                logger.info("%s: Trail stop re-placed at %.4f qty=%s (algo=%s)",
                            symbol, ts_price, total_qty, basket.trail_stop_is_algo)
            except Exception as e:
                logger.error("%s: replace trail stop error: %s", symbol, e)

    def _check_closure(self, basket: Basket):
        """Check if TP or stop order filled → position closed."""
        symbol = basket.symbol
        pos_size = self._position_size(symbol)

        if abs(pos_size) < 0.0001:
            # Position is flat — one of our exit orders hit
            exit_reason = None
            exit_price = 0.0
            gross_pnl = 0.0

            if basket.tp_order_id is not None:
                try:
                    if basket.tp_is_algo:
                        o = self.client.futures_get_algo_order(symbol=symbol, algoId=basket.tp_order_id)
                    else:
                        o = self.client.futures_get_order(symbol=symbol, orderId=basket.tp_order_id)
                    if o.get("status") == "FILLED" or o.get("algoStatus") == "FILLED":
                        exit_reason = "take_profit"
                        exit_price = float(o.get("avgPrice") or o.get("triggerPrice") or 0)
                        gross_pnl = (exit_price - basket.avg_entry) * basket.size_base
                except Exception:
                    pass

            if exit_reason is None and basket.stop_order_id is not None:
                try:
                    if basket.stop_is_algo:
                        o = self.client.futures_get_algo_order(symbol=symbol, algoId=basket.stop_order_id)
                    else:
                        o = self.client.futures_get_order(symbol=symbol, orderId=basket.stop_order_id)
                    if o.get("status") == "FILLED" or o.get("algoStatus") == "FILLED":
                        exit_reason = "stop"
                        exit_price = float(o.get("avgPrice") or o.get("triggerPrice") or 0)
                        gross_pnl = (exit_price - basket.avg_entry) * basket.size_base
                except Exception:
                    pass

            # Check trail stop (cancels hard stop, so check separately)
            if exit_reason is None and basket.trail_active_stop_order_id is not None:
                try:
                    if basket.trail_stop_is_algo:
                        o = self.client.futures_get_algo_order(symbol=symbol, algoId=basket.trail_active_stop_order_id)
                    else:
                        o = self.client.futures_get_order(symbol=symbol, orderId=basket.trail_active_stop_order_id)
                    if o.get("status") == "FILLED" or o.get("algoStatus") == "FILLED":
                        exit_reason = "trail_stop"
                        exit_price = float(o.get("avgPrice") or o.get("triggerPrice") or 0)
                        gross_pnl = (exit_price - basket.avg_entry) * basket.size_base
                except Exception:
                    pass

            if exit_reason is not None:
                exit_fee = exit_price * basket.size_base * self.g.fee_taker
                net_pnl = gross_pnl - (basket.total_fees + exit_fee)
                pnl_pct = net_pnl / basket.total_margin * 100 if basket.total_margin > 0 else 0.0

                if not basket.logged:
                    basket.logged = True
                    _write_trade({
                        "symbol": symbol,
                        "entry_time": basket.entry_time or "",
                        "exit_time": datetime.now(timezone.utc).isoformat(),
                        "entry_price": basket.entry_price or 0.0,
                        "exit_price": exit_price,
                        "gross_pnl": round(gross_pnl, 6),
                        "fees": round(basket.total_fees + exit_fee, 6),
                        "funding": 0.0,
                        "net_pnl": round(net_pnl, 6),
                        "pnl_pct": round(pnl_pct, 4),
                        "exit_reason": exit_reason,
                        "n_safety_filled": basket.n_safety_filled,
                        "base_margin": basket.total_margin,
                        "leverage": self.g.leverage,
                    })
                    logger.info("%s: CLOSED  reason=%s  gross=%.4f  net=%.4f  pnl=%.2f%%",
                                 symbol, exit_reason, gross_pnl, net_pnl, pnl_pct)

                basket.state = "closing"
                self._cancel_remaining_orders(basket)
                self.strategies[symbol].reset()
                if self.g.dual_mode:
                    self.strategies[symbol].flip_direction()
                del self.baskets[symbol]
                self._on_trade_closed(symbol, {
                    "net_pnl": net_pnl, "exit_reason": exit_reason,
                    "n_safety_filled": basket.n_safety_filled, "pnl_pct": pnl_pct,
                })
            else:
                # Position flat but no tracked exit order matched → closed some
                # other way (manual close on Binance, TP/stop fill the order query
                # missed, algo-status quirk). Reconstruct the trade from realized
                # income so it still lands in the trade log instead of vanishing.
                realized = self._reconstruct_realized_pnl(basket)
                if realized is not None:
                    gross_pnl, fees, funding = realized
                    net_pnl = gross_pnl - fees + funding
                    pnl_pct = net_pnl / basket.total_margin * 100 if basket.total_margin > 0 else 0.0
                    exit_reason = "take_profit" if net_pnl >= 0 else "stop"
                    if not basket.logged:
                        basket.logged = True
                        _write_trade({
                            "symbol": symbol,
                            "entry_time": basket.entry_time or "",
                            "exit_time": datetime.now(timezone.utc).isoformat(),
                            "entry_price": basket.entry_price or 0.0,
                            "exit_price": 0.0,
                            "gross_pnl": round(gross_pnl, 6),
                            "fees": round(fees, 6),
                            "funding": round(funding, 6),
                            "net_pnl": round(net_pnl, 6),
                            "pnl_pct": round(pnl_pct, 4),
                            "exit_reason": exit_reason,
                            "n_safety_filled": basket.n_safety_filled,
                            "base_margin": basket.total_margin,
                            "leverage": self.g.leverage,
                        })
                        logger.info("%s: CLOSED (reconstructed)  reason=%s  net=%.4f  pnl=%.2f%%",
                                     symbol, exit_reason, net_pnl, pnl_pct)
                    self._cancel_remaining_orders(basket)
                    self.strategies[symbol].reset()
                    if self.g.dual_mode:
                        self.strategies[symbol].flip_direction()
                    del self.baskets[symbol]
                    self._on_trade_closed(symbol, {
                        "net_pnl": net_pnl, "exit_reason": exit_reason,
                        "n_safety_filled": basket.n_safety_filled, "pnl_pct": pnl_pct,
                    })
                else:
                    logger.warning("%s: position flat, could not reconstruct realized PnL. "
                                   "Resetting without a trade-log row.", symbol)
                    self._cancel_remaining_orders(basket)
                    self.strategies[symbol].reset()
                    if self.g.dual_mode:
                        self.strategies[symbol].flip_direction()
                    del self.baskets[symbol]

    def _reconstruct_realized_pnl(self, basket: Basket):
        """Sum realized income for *basket* since entry from Binance income history.

        Returns (gross_pnl, fees, funding) where gross_pnl is REALIZED_PNL,
        fees is the (positive) absolute commission total, and funding is the
        signed FUNDING_FEE total. Returns None in dry-run or on API failure so
        the caller can decide how to degrade.
        """
        if self.dry_run or self.client is None:
            return None
        try:
            start_ms = None
            if basket.entry_time:
                try:
                    dt = datetime.fromisoformat(basket.entry_time)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    start_ms = int(dt.timestamp() * 1000) - 1000
                except Exception:
                    start_ms = None
            params = {"symbol": basket.symbol, "limit": 1000}
            if start_ms is not None:
                params["startTime"] = start_ms
            rows = self.client.futures_income_history(**params)
            gross = sum(float(r["income"]) for r in rows if r.get("incomeType") == "REALIZED_PNL")
            commission = sum(float(r["income"]) for r in rows if r.get("incomeType") == "COMMISSION")
            funding = sum(float(r["income"]) for r in rows if r.get("incomeType") == "FUNDING_FEE")
            return gross, abs(commission), funding
        except Exception as e:
            logger.error("%s: reconstruct realized PnL error: %s", basket.symbol, e)
            return None

    def _cancel_remaining_orders(self, basket: Basket):
        """Cancel any unfilled safety/TP/stop orders, algo-aware."""
        symbol = basket.symbol

        # Safety orders are always normal (LIMIT) orders
        normal_ids = list(basket.safety_order_ids)
        # TP/stop may be algo (conditional) orders — track separately
        algo_ids: list[int] = []
        if basket.tp_order_id is not None:
            if basket.tp_is_algo:
                algo_ids.append(basket.tp_order_id)
            else:
                normal_ids.append(basket.tp_order_id)
        if basket.stop_order_id is not None:
            if basket.stop_is_algo:
                algo_ids.append(basket.stop_order_id)
            else:
                normal_ids.append(basket.stop_order_id)
        if basket.trail_active_stop_order_id is not None:
            if basket.trail_stop_is_algo:
                algo_ids.append(basket.trail_active_stop_order_id)
            else:
                normal_ids.append(basket.trail_active_stop_order_id)

        failed_normal = []
        for oid in normal_ids:
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=oid)
            except Exception as e:
                logger.warning("%s: cancel order %s failed (will retry): %s", symbol, oid, e)
                failed_normal.append(oid)

        for oid in algo_ids:
            try:
                self.client.futures_cancel_algo_order(symbol=symbol, algoId=oid)
            except Exception as e:
                logger.warning("%s: cancel algo order %s failed (will retry): %s", symbol, oid, e)
                # algo order cancel failure falls through to backstop sweep below

        # Retry failed normal cancels once
        still_failed = []
        for oid in failed_normal:
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=oid)
            except Exception as e:
                logger.warning("%s: cancel order %s retry failed: %s", symbol, oid, e)
                still_failed.append(oid)

        # Backstop: always sweep ALL open and algo orders for the symbol so nothing is
        # ever orphaned — this is safe because the bot runs at most one basket per symbol.
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception as e:
            logger.warning("%s: backstop cancel_all_open_orders failed: %s", symbol, e)
        try:
            self.client.futures_cancel_all_algo_open_orders(symbol=symbol)
        except Exception as e:
            logger.warning("%s: backstop cancel_all_algo_open_orders failed: %s", symbol, e)

    # ------------------------------------------------------------------
    # Trailing stop
    # ------------------------------------------------------------------

    def _check_trail(self, basket: Basket) -> None:
        """Check and update trailing stop for a basket.

        Called after _check_safety_fills, before _check_closure.
        Trail is disabled when g.trail_activate_pct <= 0.
        """
        g = self.g
        if g.trail_activate_pct <= 0:
            return

        symbol = basket.symbol

        # Get current mark price
        if self.dry_run:
            return  # dry-run trail is logged at entry only; no simulated price feed
        try:
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            mark = float(ticker["price"])
        except Exception as e:
            logger.warning("%s: trail mark price fetch error: %s", symbol, e)
            return

        lf = self._get_lot_filter(symbol)

        if not basket.trail_activated:
            # Check activation threshold
            threshold = basket.avg_entry * (1.0 + g.trail_activate_pct / 100.0)
            if mark < threshold:
                return

            # Activate trail
            basket.trail_activated = True
            basket.trail_highest = mark
            basket.trail_stop_price = round(
                mark * (1.0 - g.trail_buffer_pct / 100.0), 12
            )

            logger.info("%s: TRAIL ACTIVATED  mark=%.4f  trail_stop=%.4f",
                        symbol, mark, basket.trail_stop_price)

            # Cancel existing hard stop order
            if basket.stop_order_id is not None:
                self._cancel_stop_order(basket)

            total_qty = _round_qty(basket.total_notional / basket.avg_entry, lf)
            ts_price = _round_price(basket.trail_stop_price, lf)

            # Place trailing STOP_MARKET reduceOnly order
            try:
                o = self.client.futures_create_order(
                    symbol=symbol, side="SELL", type="STOP_MARKET",
                    quantity=total_qty, stopPrice=ts_price,
                    timeInForce="GTC", reduceOnly=True,
                    workingType="MARK_PRICE",
                )
                basket.trail_active_stop_order_id = (
                    o.get("orderId") or o.get("algoId")
                )
                basket.trail_stop_is_algo = "orderId" not in o
                logger.info("%s: trail stop placed at %.4f (id=%s algo=%s)",
                            symbol, ts_price,
                            basket.trail_active_stop_order_id,
                            basket.trail_stop_is_algo)
            except Exception as e:
                logger.error("%s: trail stop order failed: %s", symbol, e)
            return

        # Trail is already active — update highest and adjust stop
        basket.trail_highest = max(basket.trail_highest, mark)
        new_trail_stop = round(
            basket.trail_highest * (1.0 - g.trail_buffer_pct / 100.0), 12
        )

        if new_trail_stop > (basket.trail_stop_price or 0):
            basket.trail_stop_price = new_trail_stop
            total_qty = _round_qty(basket.total_notional / basket.avg_entry, lf)
            ts_price = _round_price(basket.trail_stop_price, lf)

            # Cancel old trail stop
            if basket.trail_active_stop_order_id is not None:
                try:
                    if basket.trail_stop_is_algo:
                        self.client.futures_cancel_algo_order(
                            symbol=symbol, algoId=basket.trail_active_stop_order_id
                        )
                    else:
                        self.client.futures_cancel_order(
                            symbol=symbol, orderId=basket.trail_active_stop_order_id
                        )
                except Exception as e:
                    logger.warning("%s: cancel old trail stop failed: %s",
                                   symbol, e)

            # Place new trailing STOP_MARKET
            try:
                o = self.client.futures_create_order(
                    symbol=symbol, side="SELL", type="STOP_MARKET",
                    quantity=total_qty, stopPrice=ts_price,
                    timeInForce="GTC", reduceOnly=True,
                    workingType="MARK_PRICE",
                )
                basket.trail_active_stop_order_id = (
                    o.get("orderId") or o.get("algoId")
                )
                basket.trail_stop_is_algo = "orderId" not in o
                logger.info("%s: trail stop raised to %.4f (hi=%.4f, id=%s)",
                            symbol, ts_price, basket.trail_highest,
                            basket.trail_active_stop_order_id)
            except Exception as e:
                logger.error("%s: trail stop update order failed: %s",
                             symbol, e)

    def _cancel_stop_order(self, basket: Basket) -> None:
        """Cancel the current hard stop order (not trail stop) on a basket."""
        symbol = basket.symbol
        if basket.stop_order_id is None:
            return
        try:
            if basket.stop_is_algo:
                self.client.futures_cancel_algo_order(
                    symbol=symbol, algoId=basket.stop_order_id
                )
            else:
                self.client.futures_cancel_order(
                    symbol=symbol, orderId=basket.stop_order_id
                )
            logger.info("%s: stop order %s cancelled", symbol, basket.stop_order_id)
        except Exception as e:
            logger.warning("%s: cancel stop order %s failed: %s",
                          symbol, basket.stop_order_id, e)
            return  # keep old order id so _check_closure/exchange-order cleanup still knows about it
        basket.stop_order_id = None
        basket.stop_is_algo = False

    def _cancel_orders(self, symbol: str):
        """Cancel all open orders for a symbol (cleanup on error)."""
        if self.dry_run:
            return
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception:
            pass
        try:
            self.client.futures_cancel_all_algo_open_orders(symbol=symbol)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def print_status(self):
        """Print current baskets and account summary."""
        print(f"\n=== DCA Bot Status ===")
        print(f"Active baskets: {len(self.baskets)}/{self.g.max_concurrent_baskets}")
        for symbol, b in self.baskets.items():
            print(f"  {symbol}: state={b.state}  avg_entry={b.avg_entry:.4f}  "
                  f"size={b.size_base:.4f}  margin={b.total_margin:.2f}")
        if not self.dry_run:
            bal = self._available_balance()
            dpl = _daily_pnl()
            print(f"Available USDT: {bal:.2f}")
            print(f"Daily PnL: {dpl:+.2f}")

    # ------------------------------------------------------------------
    # Main iteration
    # ------------------------------------------------------------------

    def run_once(self):
        """Single pass: check risk, enter signals, manage baskets."""
        # Always manage open baskets first so TP/stop fills are caught even at
        # max-basket count or after a daily-loss halt.
        self._manage_baskets()

        if not self._risk_check():
            return

        # New-entry scan: skip entirely when auto_entry is False (trade.py manual mode)
        # or when paused via bot_control.json.
        if not self.auto_entry:
            return
        if self.paused:
            logger.info("Bot paused via bot_control.json — skipping new-entry scan.")
            return

        # Check signals for symbols without active baskets
        for sc in self.cfg.active_symbols():
            if sc.name in self.baskets:
                continue
            if sc.name in self.disabled_symbols:
                logger.debug("%s: skipped — in disabled_symbols", sc.name)
                continue
            active = sum(1 for b in self.baskets.values() if b.state != "closing")
            if active >= self.g.max_concurrent_baskets:
                break
            signal, klines = self._check_signal(sc.name)
            if signal != "enter":
                continue

            self._enter(sc)

    def _on_trade_closed(self, symbol: str, trade_row: dict):
        """Callback after a trade is logged. Updates last_trade history."""
        self.last_trade[symbol] = {
            "net_pnl": trade_row.get("net_pnl", 0),
            "exit_reason": trade_row.get("exit_reason", ""),
            "n_safety_filled": trade_row.get("n_safety_filled", 0),
            "pnl_pct": trade_row.get("pnl_pct", 0),
        }
        logger.info("%s: last_trade updated (net=%.2f, reason=%s)",
                     symbol, self.last_trade[symbol]["net_pnl"],
                     self.last_trade[symbol]["exit_reason"])

    def _write_status_json(self):
        """Write bot status to JSON file for dashboard to read."""
        try:
            import json
            bal = self._available_balance() if not self.dry_run else 0

            # Build basket details for dashboard
            baskets = []
            for symbol, basket in self.baskets.items():
                mark = liq = upnl = 0.0
                if not self.dry_run:
                    try:
                        pos = self.client.futures_position_information(symbol=symbol)
                        if pos:
                            mark = float(pos[0].get("markPrice", 0) or 0)
                            liq = float(pos[0].get("liquidationPrice", 0) or 0)
                            upnl = float(pos[0].get("unRealizedProfit", 0) or 0)
                    except Exception:
                        pass  # leave zeros; dashboard falls back to live exchange read
                direction = "long"
                strat = self.strategies.get(symbol)
                if strat is not None:
                    direction = strat.cfg.direction
                baskets.append({
                    "symbol": symbol,
                    "avg_entry": basket.avg_entry or 0.0,
                    "current_price": mark,
                    "floating_pnl": upnl,
                    "margin": round(basket.total_margin, 2),
                    "liquidation": liq,
                    "state": basket.state,
                    "direction": direction,
                    "entry_time": basket.entry_time or "",
                    "dca_levels": [],
                })

            direction = "dual" if self.g.dual_mode else "long"
            status = {
                "bot_alive": True,
                "mode": "auto",
                "direction": direction,
                "oracle": False,
                "balance": round(bal, 2),
                "active_baskets": len(self.baskets),
                "baskets": baskets,
                "symbols": [{"name": s.name, "tp_pct": s.tp_pct} for s in self.cfg.active_symbols()],
                "oracle_history": [],
            }
            with open(str(_HERE / "bot_status.json"), "w") as f:
                json.dump(status, f)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Startup reconciler
    # ------------------------------------------------------------------

    def _place_exit_orders(self, symbol: str, scfg: SymbolConfig, total_qty: float,
                           avg_entry: float, place_tp: bool = True,
                           place_stop: bool = True) -> tuple[int | None, bool, int | None, bool]:
        """Place TP and/or stop for a reconciled basket.

        Only places the orders requested via place_tp / place_stop flags.
        Returns (tp_id, tp_is_algo, stop_id, stop_is_algo).
        """
        lf = self._get_lot_filter(symbol)
        qty = _round_qty(total_qty, lf)
        tp_price = _round_price(avg_entry * (1.0 + scfg.tp_pct / 100.0), lf)
        stop_price = _round_price(avg_entry * (1.0 - scfg.stop_pct / 100.0), lf)

        tp_id: int | None = None
        tp_is_algo = False
        stop_id: int | None = None
        stop_is_algo = False

        if place_tp:
            try:
                o = self.client.futures_create_order(
                    symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                    quantity=qty, stopPrice=tp_price,
                    timeInForce="GTC", reduceOnly=True,
                    workingType="MARK_PRICE",
                )
                tp_id = o.get("orderId") or o.get("algoId")
                tp_is_algo = "orderId" not in o
                logger.info("RECONCILE: %s re-placed TP at %.4f qty=%s (id=%s algo=%s)",
                            symbol, tp_price, qty, tp_id, tp_is_algo)
            except Exception as e:
                logger.error("RECONCILE: %s TP placement failed: %s", symbol, e)

        if place_stop:
            try:
                o = self.client.futures_create_order(
                    symbol=symbol, side="SELL", type="STOP_MARKET",
                    quantity=qty, stopPrice=stop_price,
                    timeInForce="GTC", reduceOnly=True,
                    workingType="MARK_PRICE",
                )
                stop_id = o.get("orderId") or o.get("algoId")
                stop_is_algo = "orderId" not in o
                logger.info("RECONCILE: %s re-placed Stop at %.4f qty=%s (id=%s algo=%s)",
                            symbol, stop_price, qty, stop_id, stop_is_algo)
            except Exception as e:
                logger.error("RECONCILE: %s Stop placement failed: %s", symbol, e)

        return tp_id, tp_is_algo, stop_id, stop_is_algo

    def reconcile_on_start(self):
        """Adopt any live exchange positions into self.baskets at boot.

        Called once after client/leverage init and before the poll loop.
        For each active symbol:
          - Queries live position; skips flat symbols.
          - Reconstructs a Basket from exchange data.
          - Recovers open exit/safety order ids from normal + algo open orders.
          - Re-places any missing TP and/or stop (protection-gap handling).
          - Registers the basket so _manage_baskets picks it up next cycle.

        Dry-run: logs what would be adopted; places nothing.
        Per-symbol errors are caught so one failure never aborts boot.
        """
        _POSITION_EPSILON = 1e-6

        symbols = self.cfg.active_symbols()
        logger.info("RECONCILE: scanning %d active symbol(s) for live positions...", len(symbols))
        before = len(self.baskets)
        for scfg in symbols:
            symbol = scfg.name
            try:
                self._reconcile_symbol(symbol, scfg, _POSITION_EPSILON)
            except Exception as e:
                logger.error("RECONCILE: %s unexpected error, skipping: %s", symbol, e)
        adopted = len(self.baskets) - before
        logger.info("RECONCILE: done — %d position(s) adopted, %d symbol(s) flat.",
                    adopted, len(symbols) - adopted)

    def _reconcile_symbol(self, symbol: str, scfg: SymbolConfig, epsilon: float):
        """Reconcile one symbol.  Called by reconcile_on_start(); isolated for per-symbol try/except."""
        # 1. Query live position
        pos_list = self.client.futures_position_information(symbol=symbol)
        if not pos_list:
            return
        pos = pos_list[0]
        pos_amt = float(pos.get("positionAmt", 0))
        if abs(pos_amt) < epsilon:
            return  # flat — nothing to adopt

        avg_entry = float(pos.get("entryPrice", 0) or pos.get("markPrice", 0))
        size_base = abs(pos_amt)
        total_notional = avg_entry * size_base
        total_margin = total_notional / self.g.leverage

        if self.dry_run:
            logger.info(
                "RECONCILE [dry-run]: %s would adopt position: size=%.6f avg=%.4f "
                "notional=%.2f margin=%.2f — nothing placed.",
                symbol, size_base, avg_entry, total_notional, total_margin,
            )
            return

        # 2. Query open normal and algo orders
        normal_orders: list[dict] = []
        algo_orders: list[dict] = []
        try:
            normal_orders = self.client.futures_get_open_orders(symbol=symbol) or []
        except Exception as e:
            logger.warning("RECONCILE: %s get_open_orders error: %s", symbol, e)
        try:
            # futures_get_open_algo_orders accepts optional symbol kwarg; filter here for safety
            all_algo = self.client.futures_get_open_algo_orders(symbol=symbol) or []
            algo_orders = [o for o in all_algo if o.get("symbol") == symbol]
        except Exception as e:
            logger.warning("RECONCILE: %s get_open_algo_orders error: %s", symbol, e)

        # 3. Classify orders
        tp_order_id: int | None = None
        tp_is_algo = False
        stop_order_id: int | None = None
        stop_is_algo = False
        safety_order_ids: list[int] = []
        existing_safety_prices: list[float] = []

        for o in normal_orders:
            otype = o.get("type", "")
            side = o.get("side", "")
            reduce_only = o.get("reduceOnly", False)
            oid = o.get("orderId")
            if reduce_only and side == "SELL" and otype == "TAKE_PROFIT_MARKET":
                tp_order_id = oid
                tp_is_algo = False
            elif reduce_only and side == "SELL" and otype == "STOP_MARKET":
                stop_order_id = oid
                stop_is_algo = False
            elif not reduce_only and side == "BUY" and otype == "LIMIT":
                if oid is not None:
                    safety_order_ids.append(oid)
                    raw_price = o.get("price")
                    if raw_price is not None:
                        ep = float(raw_price)
                        if ep > 0:
                            existing_safety_prices.append(ep)

        for o in algo_orders:
            otype = o.get("orderType", o.get("type", ""))
            side = o.get("side", "")
            reduce_only = o.get("reduceOnly", False)
            algo_id = o.get("algoId")
            if reduce_only and side == "SELL" and otype == "TAKE_PROFIT_MARKET":
                tp_order_id = algo_id
                tp_is_algo = True
            elif reduce_only and side == "SELL" and otype == "STOP_MARKET":
                stop_order_id = algo_id
                stop_is_algo = True

        # 4. Estimate n_safety_filled conservatively
        # base notional = margin * leverage (single base order at full margin)
        base_notional_est = self.g.base_margin * self.g.leverage
        base_qty_est = base_notional_est / avg_entry if avg_entry > 0 else 0.0
        # Excess size beyond the base order implies safety fills
        dca_max = scfg.max_dca_orders if scfg.max_dca_orders is not None else self.g.max_dca_orders
        excess = size_base - base_qty_est
        if excess > 0 and base_qty_est > 0:
            n_safety_filled = max(0, min(
                dca_max,
                round(excess / (base_qty_est * self.g.first_order_ratio)),
            ))
        else:
            n_safety_filled = 0
        logger.info(
            "RECONCILE: %s n_safety_filled estimated=%d (base_qty_est=%.6f size=%.6f) — "
            "may be inaccurate; used for logging only.",
            symbol, n_safety_filled, base_qty_est, size_base,
        )

        # 5. Build reconstructed basket
        basket = Basket(symbol=symbol, scfg=scfg, gcfg=self.g)
        basket.state = "active"
        basket.entry_time = datetime.now(timezone.utc).isoformat()
        basket.entry_price = avg_entry
        basket.avg_entry = avg_entry
        basket.size_base = size_base
        basket.total_notional = total_notional
        basket.total_margin = total_margin
        basket.total_fees = 0.0  # unknown after restart
        basket.safety_order_ids = safety_order_ids
        basket.tp_order_id = tp_order_id
        basket.tp_is_algo = tp_is_algo
        basket.stop_order_id = stop_order_id
        basket.stop_is_algo = stop_is_algo
        basket.n_safety_filled = n_safety_filled

        # 6. Protection-gap handling: re-place any missing exit orders
        missing_tp = tp_order_id is None
        missing_stop = stop_order_id is None

        if missing_tp or missing_stop:
            logger.warning(
                "RECONCILE: %s position adopted; re-placing missing exit orders "
                "(missing_tp=%s missing_stop=%s) avg_entry=%.4f size=%.6f",
                symbol, missing_tp, missing_stop, avg_entry, size_base,
            )
            lf = self._get_lot_filter(symbol)
            total_qty = _round_qty(size_base, lf)

            new_tp_id, new_tp_is_algo, new_stop_id, new_stop_is_algo = \
                self._place_exit_orders(
                    symbol, scfg, total_qty, avg_entry,
                    place_tp=missing_tp, place_stop=missing_stop,
                )
            if missing_tp:
                basket.tp_order_id = new_tp_id
                basket.tp_is_algo = new_tp_is_algo
            if missing_stop:
                basket.stop_order_id = new_stop_id
                basket.stop_is_algo = new_stop_is_algo
        else:
            logger.info(
                "RECONCILE: %s position adopted with live TP (id=%s algo=%s) "
                "and Stop (id=%s algo=%s); no re-placement needed.",
                symbol, tp_order_id, tp_is_algo, stop_order_id, stop_is_algo,
            )

        # 6b. Top up missing DCA safety rungs (e.g. one rung lost to the old -4164
        # minNotional bug). Only safe when no safety has filled (avg_entry == the
        # original base entry, so the ladder reconstructs exactly) and the ladder is
        # partial (some rungs still open). A fully-empty safety set (0 open) is left
        # alone — could be a filled-and-consumed ladder we can't safely reconstruct.
        if n_safety_filled == 0 and 0 < len(safety_order_ids) < dca_max:
            logger.warning(
                "RECONCILE: %s topping up safety ladder — %d open of %d expected (0 filled)",
                symbol, len(safety_order_ids), dca_max,
            )
            lf = self._get_lot_filter(symbol)
            min_notional = lf.get("minNotional", 5.0)
            tick = lf.get("tickSize", 0.0)
            step = lf.get("stepSize", 0.001)
            base_notional = total_notional  # valid because n_safety_filled == 0
            ladder_notionals = _dca_safety_notionals(self.g, base_notional, dca_max)
            ladder_prices = _dca_safety_prices(avg_entry, self.g, dca_max)
            tolerance = max(tick, 0.4 * avg_entry * (self.g.drop_step_pct / 100.0))
            for i in range(dca_max):
                sp = _round_price(ladder_prices[i], lf)
                already_present = any(abs(sp - ep) <= tolerance for ep in existing_safety_prices)
                if already_present:
                    continue
                sq = _round_qty(ladder_notionals[i] / sp, lf)
                while sq * sp < min_notional and sq > 0:
                    sq = _round_qty(sq + step, lf)
                try:
                    resp = self.client.futures_create_order(
                        symbol=symbol, side="BUY", type="LIMIT",
                        timeInForce="GTC", price=sp, quantity=sq,
                    )
                    oid = resp["orderId"]
                    basket.safety_order_ids.append(oid)
                    existing_safety_prices.append(sp)
                    logger.info(
                        "RECONCILE: %s topped up safety rung %d: qty=%s @ %.6f (id=%s)",
                        symbol, i + 1, sq, sp, oid,
                    )
                except Exception as e:
                    logger.error(
                        "RECONCILE: %s safety top-up rung %d failed: %s",
                        symbol, i + 1, e,
                    )

        # 7. Register into self.baskets
        self.baskets[symbol] = basket
        logger.info(
            "RECONCILE: %s basket registered — avg=%.4f size=%.6f margin=%.2f "
            "tp=%s stop=%s safety_ids=%s",
            symbol, avg_entry, size_base, total_margin,
            basket.tp_order_id, basket.stop_order_id, basket.safety_order_ids,
        )

    def _read_bot_control(self):
        """Read bot_control.json and update self.paused / self.disabled_symbols.

        Missing file or parse errors are silently ignored (fail open for control —
        the bot continues normally rather than crashing the loop).
        """
        path = _HERE / "bot_control.json"
        try:
            if path.exists():
                with open(path) as f:
                    ctrl = json.load(f)
                self.paused = bool(ctrl.get("paused", False))
                self.disabled_symbols = [str(s) for s in ctrl.get("disabled_symbols", [])]
            else:
                self.paused = False
                self.disabled_symbols = []
        except Exception as e:
            logger.warning("bot_control.json read error (ignored): %s", e)

    def run_loop(self):
        """Continuous loop with polling interval."""
        logger.info("Bot starting (poll=%ds, dry_run=%s)", self.g.poll_interval_sec, self.dry_run)

        # Mutual exclusion: refuse to start if trade.py is alive
        trade_pid_path = _HERE / "trade.pid"
        if trade_pid_path.exists():
            try:
                pid = int(trade_pid_path.read_text().strip())
                if _pid_alive(pid):
                    logger.error(
                        "trade.py is running (pid=%d). Cannot start bot.py. "
                        "Stop trade.py first (or remove trade.pid if stale).", pid)
                    return
                else:
                    logger.warning("Stale trade.pid (pid=%d dead) — ignoring.", pid)
            except Exception as e:
                logger.warning("Could not read trade.pid: %s", e)

        if not self.dry_run:
            bal = self._available_balance()
            logger.info("Available USDT: %.2f (need at least %.2f for one entry)",
                         bal, self.g.base_margin)
            if bal < self.g.base_margin:
                logger.warning("Balance %.2f < base_margin %.2f. Starting in recovery "
                               "mode — managing existing positions, no new entries.",
                               bal, self.g.base_margin)

        self.reconcile_on_start()
        self._write_status_json()

        # Write pidfile
        bot_pid_path = _HERE / "bot.pid"
        bot_pid_path.write_text(str(os.getpid()))
        logger.info("bot.pid written (pid=%d)", os.getpid())

        try:
            while True:
                self._read_bot_control()
                self.run_once()
                self._write_status_json()
                logger.info("cycle done — %d active basket(s); next check in %ds.",
                            len(self.baskets), self.g.poll_interval_sec)
                time.sleep(self.g.poll_interval_sec)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
        finally:
            try:
                bot_pid_path.unlink(missing_ok=True)
                logger.info("bot.pid removed.")
            except Exception as e:
                logger.warning("Could not remove bot.pid: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        stream=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(description="DCA Bot — Autonomous Binance Futures scalper")
    parser.add_argument("--config", default="bot_config.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Simulate only, no real orders")
    parser.add_argument("--once", action="store_true", help="Single iteration then exit")
    parser.add_argument("--loop", action="store_true", help="Run continuously (default)")
    parser.add_argument("--status", action="store_true", help="Print active positions and exit")
    parser.add_argument("--symbols", type=str, default=None, metavar="SYM1,SYM2",
                        help="Override: comma-separated symbol list")
    args = parser.parse_args()

    _setup_logging()

    config_path = _HERE / args.config
    if not config_path.exists():
        logger.error("Config not found: %s", config_path)
        sys.exit(1)

    config = BotConfig.from_yaml(str(config_path))

    # Override symbols if --symbols passed
    if args.symbols:
        overrides = [s.strip().upper() for s in args.symbols.split(",")]
        config.symbols = [s for s in config.symbols if s.name in overrides]
        if not config.symbols:
            # Create SymbolConfig for symbols not in config
            for sym in overrides:
                config.symbols.append(SymbolConfig(name=sym, tp_pct=0.5, stop_pct=3.0, active=True))
        logger.info("Symbols overridden: %s", [s.name for s in config.active_symbols()])

    # Validate API creds unless dry-run
    if not args.dry_run:
        if not config.api_key or not config.api_secret:
            logger.error("API key/secret not set. Use BINANCE_API_KEY / BINANCE_API_SECRET env vars.")
            sys.exit(1)
        # HMAC key id and secret are both 64-char alphanumeric. Catch
        # unexpanded ${VARS}, whitespace, or pasted Ed25519/RSA keys here
        # instead of as a cryptic Binance -1022 signature error at runtime.
        for label, val in (("BINANCE_API_KEY", config.api_key), ("BINANCE_API_SECRET", config.api_secret)):
            if len(val) != 64 or not val.isalnum():
                logger.error(
                    "%s looks malformed (len=%d, alnum=%s). Expect a 64-char alphanumeric "
                    "HMAC-SHA256 credential. Check the env var is set and is not an "
                    "Ed25519/RSA key or an unexpanded ${...} placeholder.",
                    label, len(val), val.isalnum(),
                )
                sys.exit(1)

    bot = DcaBot(config, dry_run=args.dry_run)

    if args.status:
        bot.print_status()
        return
    elif args.once:
        if not bot.dry_run:
            bot.reconcile_on_start()
        bot.run_once()
        bot.print_status()
    else:
        bot.run_loop()


if __name__ == "__main__":
    main()
