"""
trade.py — Manual trading engine for Binance USDⓈ-M Futures.

Human decides ENTRY (symbol + signal via manual_control.json).
After entry the same DCA strategy auto-manages TP/stop/safety (reuses DcaBot).

Usage:
    python trade.py --config bot_config.yaml           # loop forever
    python trade.py --config bot_config.yaml --once     # single iteration
    python trade.py --config bot_config.yaml --dry-run  # simulate, no orders
    python trade.py --config bot_config.yaml --status   # print active positions

Mutual exclusion: refuses to start if bot.py is alive (bot.pid present + alive).
Writes trade.pid on start, removes on clean exit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Reuse everything from bot.py — do NOT duplicate strategy/order/config logic.
from bot import (
    BotConfig,
    DcaBot,
    GlobalConfig,
    SymbolConfig,
    _HERE,
    _setup_logging,
)
from oracle import OracleDecision

# bot.py already loads .env on import; reload explicitly for robustness.
try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env", override=True)
except ImportError:
    pass

logger = logging.getLogger("trade")


# ---------------------------------------------------------------------------
# PID helpers — duplicated from bot.py to avoid circular-import issues.
# (trade.py imports DcaBot from bot.py; a shared helper module would
#  require bot.py to import from it, creating a cycle.)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is currently running.

    Cross-platform:
    - POSIX: os.kill(pid, 0) raises OSError(ESRCH) when the process is gone.
    - Windows: os.kill(pid, 0) is unsupported; use ctypes OpenProcess instead.
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
# ManualOracle — mimics AiOracle.decide() interface for DcaBot._enter()
# ---------------------------------------------------------------------------

class ManualOracle:
    """Stub oracle that always returns 'go'.

    The HUMAN already decided to enter before the request hits manual_control.json;
    this class satisfies the oracle gate inside DcaBot._enter() without making any
    API calls.
    """

    def decide(
        self,
        symbol: str,
        klines,
        prev_trade=None,
        tp_pct: float = 0.5,
        stop_pct: float = 3.0,
    ) -> OracleDecision:
        return OracleDecision(signal="go", confidence=1.0, reason="manual")

    def reconsider(self, *args, **kwargs) -> OracleDecision:
        return OracleDecision(signal="go", confidence=1.0, reason="manual")


# ---------------------------------------------------------------------------
# Control file helpers
# ---------------------------------------------------------------------------

_MANUAL_CONTROL_PATH = _HERE / "manual_control.json"


def _read_manual_control() -> dict:
    """Read manual_control.json; return {} on missing/parse error (fail closed)."""
    try:
        if _MANUAL_CONTROL_PATH.exists():
            with open(_MANUAL_CONTROL_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("manual_control.json read error (ignored): %s", e)
    return {}


def _clear_enter_request():
    """Write {\"enter\": null} to manual_control.json so the request fires only once."""
    try:
        with open(_MANUAL_CONTROL_PATH, "w") as f:
            json.dump({"enter": None}, f)
    except Exception as e:
        logger.warning("Could not clear manual_control.json: %s", e)


# ---------------------------------------------------------------------------
# Manual loop
# ---------------------------------------------------------------------------

def _manual_loop(bot: DcaBot, poll: int):
    """Persistent manager loop for manual trading.

    - Reads manual_control.json each cycle for a human enter request.
    - If a valid 'go' request arrives for a symbol without an active basket,
      builds/looks up SymbolConfig and calls bot._enter().
    - Calls bot.run_once() every cycle to manage existing baskets (DCA fills,
      TP/stop reconcile, checkpoint exits) — new-entry scan is SKIPPED because
      bot.auto_entry is False.
    - Writes bot_status.json with "mode":"manual" each cycle.
    """
    while True:
        try:
            ctrl = _read_manual_control()
            enter_req = ctrl.get("enter")

            if isinstance(enter_req, dict) and enter_req.get("signal") == "go":
                symbol = str(enter_req.get("symbol", "")).strip().upper()
                if not symbol:
                    logger.warning("manual enter request has empty symbol — clearing.")
                    _clear_enter_request()
                elif symbol in bot.baskets:
                    logger.info("%s: manual enter request ignored — basket already active.", symbol)
                    _clear_enter_request()
                else:
                    # Resolve SymbolConfig: prefer yaml config, else use defaults.
                    sc = _find_or_build_symbol_config(bot, symbol)
                    # Ensure CheckpointStrategy exists for this symbol.
                    if symbol not in bot.strategies:
                        from config import StrategyConfig
                        from strategy import CheckpointStrategy
                        bot.strategies[symbol] = CheckpointStrategy(StrategyConfig(
                            entry_mode="red_dip",
                            green_gate="off",
                            stop_mode="none",
                            checkpoint_mode="first_green",
                            position_model="single",
                            direction="long",
                        ))
                    if symbol not in bot.last_trade:
                        bot.last_trade[symbol] = None
                    logger.info("Manual enter request: symbol=%s", symbol)
                    bot._enter(sc)
                    _clear_enter_request()
            elif enter_req is not None and enter_req is not None:
                # Non-null but signal != "go" — clear silently.
                if isinstance(enter_req, dict) and enter_req.get("signal") != "go":
                    logger.info("manual_control enter signal=%r — clearing (not 'go').",
                                enter_req.get("signal"))
                    _clear_enter_request()

        except Exception as e:
            logger.error("Manual control read/enter error: %s", e)

        try:
            bot.run_once()
        except Exception as e:
            logger.error("run_once error: %s", e)

        try:
            _write_manual_status(bot)
        except Exception as e:
            logger.error("status write error: %s", e)

        time.sleep(poll)


def _find_or_build_symbol_config(bot: DcaBot, symbol: str) -> SymbolConfig:
    """Return SymbolConfig for *symbol* from bot config, or build one with defaults."""
    for sc in bot.cfg.symbols:
        if sc.name == symbol:
            return sc
    # Symbol not in config — use safe defaults matching spec (tp=0.5, stop=3.0).
    logger.info("%s: not in config — using defaults (tp_pct=0.5, stop_pct=3.0)", symbol)
    sc = SymbolConfig(name=symbol, tp_pct=0.5, stop_pct=3.0, active=True)
    bot.cfg.symbols.append(sc)
    return sc


def _write_manual_status(bot: DcaBot):
    """Write bot_status.json with mode='manual' so the dashboard can detect the active engine."""
    try:
        bal = bot._available_balance() if not bot.dry_run else 0.0
        baskets = []
        for symbol, basket in bot.baskets.items():
            baskets.append({
                "symbol": symbol,
                "avg_entry": basket.avg_entry or 0.0,
                "current_price": 0.0,
                "floating_pnl": 0.0,
                "margin": round(basket.total_margin, 2),
                "liquidation": 0.0,
                "state": basket.state,
                "entry_time": basket.entry_time or "",
                "dca_levels": [],
            })
        status = {
            "bot_alive": True,
            "mode": "manual",
            "oracle": False,
            "balance": round(bal, 2),
            "active_baskets": len(bot.baskets),
            "baskets": baskets,
            "symbols": [{"name": s.name, "tp_pct": s.tp_pct} for s in bot.cfg.active_symbols()],
            "oracle_history": [],
        }
        with open(str(_HERE / "bot_status.json"), "w") as f:
            json.dump(status, f)
    except Exception as e:
        logger.warning("_write_manual_status error: %s", e)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Manual Trade Engine — human-entry DCA bot")
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

    # Override symbols if --symbols passed (mirrors bot.py logic).
    if args.symbols:
        overrides = [s.strip().upper() for s in args.symbols.split(",")]
        config.symbols = [s for s in config.symbols if s.name in overrides]
        if not config.symbols:
            for sym in overrides:
                config.symbols.append(SymbolConfig(name=sym, tp_pct=0.5, stop_pct=3.0, active=True))
        logger.info("Symbols overridden: %s", [s.name for s in config.active_symbols()])

    # Validate API creds unless dry-run.
    if not args.dry_run:
        if not config.api_key or not config.api_secret:
            logger.error("API key/secret not set. Use BINANCE_API_KEY / BINANCE_API_SECRET env vars.")
            sys.exit(1)

    # Mutual exclusion: refuse to start if bot.py is alive.
    bot_pid_path = _HERE / "bot.pid"
    if bot_pid_path.exists():
        try:
            pid = int(bot_pid_path.read_text().strip())
            if _pid_alive(pid):
                logger.error(
                    "bot.py is running (pid=%d). Cannot start trade.py. "
                    "Stop bot.py first (or remove bot.pid if stale).", pid)
                sys.exit(1)
            else:
                logger.warning("Stale bot.pid (pid=%d dead) — ignoring.", pid)
        except Exception as e:
            logger.warning("Could not read bot.pid: %s", e)

    # Build bot with auto_entry=False so run_once never auto-scans for signals.
    bot = DcaBot(config, dry_run=args.dry_run)
    bot.oracle = ManualOracle()
    bot.auto_entry = False

    if args.status:
        bot.print_status()
        return

    poll = config.global_.poll_interval_sec

    if args.once:
        # Single iteration: check for a pending enter request and run_once.
        if not bot.dry_run:
            bot.reconcile_on_start()
        ctrl = _read_manual_control()
        enter_req = ctrl.get("enter")
        if isinstance(enter_req, dict) and enter_req.get("signal") == "go":
            symbol = str(enter_req.get("symbol", "")).strip().upper()
            if symbol:
                sc = _find_or_build_symbol_config(bot, symbol)
                logger.info("Manual enter request (once): symbol=%s", symbol)
                bot._enter(sc)
                _clear_enter_request()
        bot.run_once()
        _write_manual_status(bot)
        bot.print_status()
        return

    # Persistent loop — write/remove trade.pid.
    if not bot.dry_run:
        bot.reconcile_on_start()

    trade_pid_path = _HERE / "trade.pid"
    trade_pid_path.write_text(str(os.getpid()))
    logger.info("trade.pid written (pid=%d)", os.getpid())

    try:
        _manual_loop(bot, poll)
    except KeyboardInterrupt:
        logger.info("Manual trade engine stopped by user.")
    finally:
        try:
            trade_pid_path.unlink(missing_ok=True)
            logger.info("trade.pid removed.")
        except Exception as e:
            logger.warning("Could not remove trade.pid: %s", e)


if __name__ == "__main__":
    main()
