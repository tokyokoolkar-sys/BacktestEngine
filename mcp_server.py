"""
mcp_server.py — MCP tool surface for the live DCA trading stack.

Exposes the same start/stop/monitor/enter operations dashboard.py already
provides over HTTP+JSON-file control, as MCP tools instead. Does not talk to
Binance directly and holds no API credentials of its own — it drives
bot.py/trade.py exactly the way dashboard.py does (subprocess + PID file +
control JSON file), so all position sizing, DCA ladder, stop-loss and
take-profit stay governed by bot_config.yaml, not by whatever an MCP caller
passes in. There is deliberately no raw create_order tool here.

Usage:
    python mcp_server.py            # stdio transport, for MCP clients
"""

from __future__ import annotations

from typing import Any

import dashboard
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="dca-trading-bot",
    version="0.1.0",
    instructions="""
    Control plane for the Binance USD-M futures checkpoint-DCA scalp bot.

    This mirrors dashboard.py's control API one-to-one: start/stop the
    autonomous or manual-oracle engine, trigger a manual entry, and read
    status. It does not accept raw order parameters (symbol/side/qty/price) —
    entry sizing, DCA ladder, stop-loss and take-profit all come from
    bot_config.yaml on disk, exactly as they would if a human used the
    dashboard's manual page. Only one of the auto/manual engines can run at
    a time; starting one while the other is running is rejected.
    """,
)


@mcp.tool()
def get_status() -> dict[str, Any]:
    """Get balance, PnL stats, open baskets/positions, and whether the
    auto or manual trading engine is currently running."""
    dashboard._update_cache()
    with dashboard._cache_lock:
        cache = dict(dashboard._cache)
    return {
        "auto_running": dashboard._proc_running(dashboard._BOT_PID),
        "manual_running": dashboard._proc_running(dashboard._TRADE_PID),
        "bot_alive": cache.get("bot_alive"),
        "balance": cache.get("balance"),
        "net_pnl": cache.get("net_pnl"),
        "today_pnl": cache.get("today_pnl"),
        "win_rate": cache.get("win_rate"),
        "total_trades": cache.get("total_trades"),
        "consecutive_wins": cache.get("consecutive_wins"),
        "last_trade_result": cache.get("last_trade_result"),
        "baskets": cache.get("baskets", []),
        "symbols": cache.get("symbols", []),
    }


@mcp.tool()
def get_recent_trades(limit: int = 20) -> list[dict[str, Any]]:
    """Get the most recent closed trades from the trade log."""
    dashboard._update_cache()
    with dashboard._cache_lock:
        trades = list(dashboard._cache.get("recent_trades", []))
    return trades[:limit]


@mcp.tool()
def list_perp_symbols() -> list[str]:
    """List available Binance USDⓈ-M perpetual futures symbols."""
    from perps import fetch_usdt_perps

    return fetch_usdt_perps() or []


@mcp.tool()
def start_auto_bot() -> dict[str, Any]:
    """Start the autonomous bot (bot.py --loop), which trades the
    checkpoint-DCA strategy on its own using bot_config.yaml. Fails if the
    manual engine is currently running."""
    if dashboard._proc_running(dashboard._TRADE_PID):
        return {"ok": False, "error": "manual engine is running; stop it first"}
    if dashboard._proc_running(dashboard._BOT_PID):
        return {"ok": True, "note": "already running"}
    dashboard._start_auto()
    return {"ok": True}


@mcp.tool()
def stop_auto_bot() -> dict[str, Any]:
    """Stop the autonomous bot (bot.py). Any open positions remain on the
    exchange; only the local process managing them is stopped."""
    dashboard._stop_proc(dashboard._BOT_PID, "bot.py")
    return {"ok": True}


@mcp.tool()
def start_manual_bot() -> dict[str, Any]:
    """Start the manual-oracle engine (trade.py --loop). It waits for
    manual_enter() calls instead of deciding entries on its own. Fails if
    the auto engine is currently running."""
    if dashboard._proc_running(dashboard._BOT_PID):
        return {"ok": False, "error": "auto engine is running; stop it first"}
    if dashboard._proc_running(dashboard._TRADE_PID):
        return {"ok": True, "note": "already running"}
    dashboard._start_manual()
    return {"ok": True}


@mcp.tool()
def stop_manual_bot() -> dict[str, Any]:
    """Stop the manual-oracle engine (trade.py). Any open positions remain
    on the exchange; only the local process managing them is stopped."""
    dashboard._stop_proc(dashboard._TRADE_PID, "trade.py")
    return {"ok": True}


@mcp.tool()
def manual_enter(symbol: str) -> dict[str, Any]:
    """Trigger a real entry for `symbol` under the manual-oracle engine —
    equivalent to clicking "Enter" on the dashboard's manual trading page.
    This places a real order sized and risk-managed by bot_config.yaml.
    Requires start_manual_bot() to have been called first."""
    if not dashboard._proc_running(dashboard._TRADE_PID):
        return {"ok": False, "error": "manual engine is not running"}
    symbol = symbol.strip().upper()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}
    import json

    with open(dashboard._MANUAL_CONTROL_PATH, "w") as f:
        json.dump({"enter": {"symbol": symbol, "signal": "go"}}, f)
    return {"ok": True, "symbol": symbol}


if __name__ == "__main__":
    mcp.run(transport="stdio")
