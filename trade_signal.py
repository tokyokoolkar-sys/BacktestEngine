"""
signal.py — Ask DeepSeek: should I enter a DCA bot on this symbol right now?

Usage:
    python signal.py TIAUSDT
    python signal.py NEARUSDT      (asks DeepSeek for timing)
    python signal.py TIAUSDT --no-oracle  (strategy signal only, no AI)

Requires: BINANCE_API_KEY, BINANCE_API_SECRET, OPENCODE_API_KEY env vars.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from oracle import AiOracle, OracleDecision
from config import StrategyConfig
from strategy import CheckpointStrategy


def _fetch_klines(symbol: str) -> list | None:
    """Fetch last 15 1h klines from Binance."""
    try:
        from binance.client import Client
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            return None
        client = Client(api_key, api_secret, requests_params={"timeout": 30})
        return client.futures_klines(symbol=symbol, interval="1h", limit=15)
    except Exception as e:
        print(f"[error] Binance: {e}")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python signal.py SYMBOL [--no-oracle]")
        sys.exit(1)

    symbol = sys.argv[1].upper()
    use_oracle = "--no-oracle" not in sys.argv

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    opencode_key = os.environ.get("OPENCODE_API_KEY", "")

    # ── 1. Fetch klines ──
    print(f"\n{'='*50}")
    print(f"  {symbol}")
    print(f"{'='*50}")

    klines = _fetch_klines(symbol)
    if klines is None:
        print(f"  [warn] Binance not connected (env keys set?)")
        print(f"  [warn] Running without price data\n")
    else:
        # klines[-1] is the currently-FORMING (unclosed) candle; display it as such.
        latest = klines[-1]
        o, h, l, c, v = float(latest[1]), float(latest[2]), float(latest[3]), float(latest[4]), float(latest[5])
        direction = "RED" if c < o else "GREEN"
        change = ((c - o) / o) * 100 if o != 0 else 0.0
        print(f"  Last 1h candle (forming): {direction}  open={o:.4f} close={c:.4f} ({change:+.2f}%)  vol={v:.0f}")

    # ── 2. Strategy signal ──
    strategy = CheckpointStrategy(StrategyConfig(
        entry_mode="red_dip", green_gate="off", stop_mode="none",
        checkpoint_mode="first_green", position_model="single", direction="long",
    ))

    strategy_signal = "none"
    # klines[-1] is the currently-forming (unclosed) candle — must NOT be used as a
    # closed bar, as that creates lookahead.  Use klines[-2] (last CLOSED candle) as
    # `row` and klines[-3] as the prior closed bar.  Guard requires >= 3 klines.
    if klines and len(klines) >= 3:
        prev = klines[-3]
        last = klines[-2]   # last CLOSED candle
        prev_row = {"high": float(prev[2]), "close": float(prev[4])}
        row = {"open": float(last[1]), "close": float(last[4]), "high": float(last[2])}
        strategy_signal = strategy.on_closed_bar(prev_row, row, in_position=False)

    print(f"  Strategy: {strategy_signal}")

    if strategy_signal != "enter":
        print(f"\n  ⏸  No entry signal. Waiting for red candle close.")
        sys.exit(0)

    # ── 3. Oracle gate (optional) ──
    if use_oracle and opencode_key:
        oracle = AiOracle(api_key=opencode_key)
        prev_trade = _last_trade(symbol)
        tp = _symbol_tp(symbol, klines)

        decision = oracle.decide(
            symbol=symbol, klines=klines, prev_trade=prev_trade,
            tp_pct=tp, stop_pct=3.0,
        )

        if decision is None:
            # FAIL CLOSED: oracle API error/timeout → BLOCK entry (do NOT fire).
            # A risk gate that opens on failure is unsafe for live money.
            print(f"  Oracle: unavailable (API error) → BLOCKING entry (fail-closed)")
            sys.exit(0)
        elif decision.signal == "no-go":
            print(f"\n  ✋ Oracle: NO-GO (confidence={decision.confidence:.0%})")
            print(f"     Reason: {decision.reason}")
            sys.exit(0)
        else:
            print(f"\n  ✅ Oracle: GO (confidence={decision.confidence:.0%})")
            print(f"     Reason: {decision.reason}")
    elif use_oracle and not opencode_key:
        print(f"  Oracle: OPENCODE_API_KEY not set, skip AI gate")
    else:
        print(f"  Oracle: disabled by --no-oracle")

    # ── 4. Entry params ──
    tp = _symbol_tp(symbol, klines)
    print(f"\n  ✅ FIRE")
    print(f"  {'─'*40}")
    print(f"  Symbol:     {symbol}")
    print(f"  Direction:  Long")
    print(f"  Leverage:   10x")
    print(f"  Base:       $2.50")
    print(f"  DCA:        $2.50 × 2 orders")
    print(f"  Drop step:  0.50%")
    print(f"  TP:         {tp:.1f}%")
    print(f"  Stop:       3.0%")
    print(f"  {'─'*40}")
    print(f"  → https://www.binance.com/en/trading-bots/futures/dca-bot/{symbol}")
    print()


def _symbol_tp(symbol: str, klines) -> float:
    """Return TP% for symbol, with fallback based on recent volatility."""
    # Static mapping from live_params.md
    tps = {
        "TIAUSDT": 0.5, "NEARUSDT": 0.8, "INJUSDT": 0.8, "FILUSDT": 0.5,
        "OPUSDT": 0.8, "ARBUSDT": 0.8, "APTUSDT": 0.5, "UNIUSDT": 0.5,
        "DOTUSDT": 0.5, "SUIUSDT": 0.5, "ETHUSDT": 0.5, "SOLUSDT": 0.5,
        "DOGEUSDT": 0.5, "LINKUSDT": 0.5, "AAVEUSDT": 0.3, "AVAXUSDT": 0.5,
        "ADAUSDT": 0.5, "XRPUSDT": 0.3, "LTCUSDT": 0.5, "BNBUSDT": 0.3,
        "BTCUSDT": 0.3, "BCHUSDT": 0.3,
    }
    return tps.get(symbol, 0.5)


def _last_trade(symbol: str) -> dict | None:
    """Read last trade for symbol from bot_trade_log.csv."""
    path = _HERE / "bot_trade_log.csv"
    if not path.exists():
        return None
    import csv
    with open(path) as f:
        trades = list(csv.DictReader(f))
    for t in reversed(trades):
        if t.get("symbol") == symbol:
            try:
                return {
                    # Use `or 0` so empty-string CSV fields don't crash float()/int()
                    "net_pnl": float(t.get("net_pnl") or 0),
                    "exit_reason": t.get("exit_reason", ""),
                    "n_safety_filled": int(t.get("n_safety_filled") or 0),
                    "pnl_pct": float(t.get("pnl_pct") or 0),
                }
            except (ValueError, TypeError):
                return None
    return None


if __name__ == "__main__":
    main()
