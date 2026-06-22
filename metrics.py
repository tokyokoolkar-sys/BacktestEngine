"""Scorecard and display utilities for the SYNUSDT checkpoint-scalp backtest.

Imported by run.py as: from metrics import scorecard, format_scorecard
No strategy logic or data IO lives here.
"""

from __future__ import annotations

from config import Result, Trade


def scorecard(result: Result) -> dict:
    """Compute performance statistics from a Result.

    Returns a flat dict with the following keys:
        n_trades         — total closed trades
        win_rate         — fraction of trades with net_pnl > 0
        avg_win          — mean net_pnl of winning trades (positive magnitude)
        avg_loss         — mean |net_pnl| of losing trades (positive magnitude)
        payoff_ratio     — avg_win / avg_loss  (inf if no losses)
        net_pnl          — sum of all net_pnl
        expectancy       — expected net_pnl per trade
        max_drawdown     — largest peak-to-trough drop in running equity (negative or 0)
        worst_trade      — minimum net_pnl across all trades
        n_stops          — trades whose exit_reason == "stop"
        n_liquidations   — trades whose exit_reason == "liquidation"
        breakeven_winrate — avg_loss / (avg_win + avg_loss); 0 if no losses
        actual_winrate   — same as win_rate (convenience alias for display)

    All monetary values are in USDT.  No rounding is applied here.
    """
    trades: list[Trade] = result.trades
    n_trades = len(trades)

    if n_trades == 0:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "payoff_ratio": 0.0,
            "net_pnl": 0.0,
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "worst_trade": 0.0,
            "n_stops": 0,
            "n_liquidations": 0,
            "breakeven_winrate": 0.0,
            "actual_winrate": 0.0,
        }

    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]

    win_rate = len(wins) / n_trades
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0  # positive magnitude

    if avg_loss == 0.0:
        payoff_ratio = float("inf") if avg_win > 0 else 0.0
    else:
        payoff_ratio = avg_win / avg_loss

    net_pnl = sum(t.net_pnl for t in trades)
    expectancy = net_pnl / n_trades
    worst_trade = min(t.net_pnl for t in trades)

    n_stops = sum(1 for t in trades if t.exit_reason == "stop")
    n_liquidations = sum(1 for t in trades if t.exit_reason == "liquidation")

    # Max drawdown from equity curve (peak-to-trough on running equity).
    # equity_curve is list[tuple[Timestamp, float]]; use the float column.
    equity_values = [eq for _, eq in result.equity_curve]
    max_drawdown = 0.0
    if equity_values:
        peak = 0.0  # equity starts at 0 (engine assumption 10); DD measured from there
        for eq in equity_values:
            if eq > peak:
                peak = eq
            dd = eq - peak  # negative or zero
            if dd < max_drawdown:
                max_drawdown = dd

    # breakeven_winrate: the win rate at which expectancy == 0 given these avg sizes.
    # Formula: avg_loss / (avg_win + avg_loss)
    denom = avg_win + avg_loss
    breakeven_winrate = avg_loss / denom if denom > 0 else 0.0

    return {
        "n_trades": n_trades,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio,
        "net_pnl": net_pnl,
        "expectancy": expectancy,
        "max_drawdown": max_drawdown,
        "worst_trade": worst_trade,
        "n_stops": n_stops,
        "n_liquidations": n_liquidations,
        "breakeven_winrate": breakeven_winrate,
        "actual_winrate": win_rate,
    }


def format_scorecard(named: list[tuple[str, dict]]) -> str:
    """Render multiple variant scorecards as an aligned monospaced table.

    Args:
        named: list of (variant_name, scorecard_dict) pairs.

    Returns a string ready to print.  All rounding happens here.
    Row order mirrors PLAN.md sec 5: win rate, trades, avg win/loss, payoff,
    net PnL, expectancy, max DD, worst trade, stops, liquidations,
    break-even win rate, actual win rate.
    """
    if not named:
        return "(no variants)"

    # --- row definitions: (display_label, key, formatter) ---
    def pct(v: float) -> str:
        return f"{v * 100:.1f}%"

    def usd(v: float) -> str:
        return f"{v:+.4f}"

    def ratio(v: float) -> str:
        return "inf" if v == float("inf") else f"{v:.2f}"

    def count(v: float) -> str:
        return str(int(v))

    rows: list[tuple[str, str, object]] = [
        ("Trades",            "n_trades",          count),
        ("Win rate",          "win_rate",           pct),
        ("Avg win",           "avg_win",            usd),
        ("Avg loss",          "avg_loss",           lambda v: f"{v:.4f}"),
        ("Payoff ratio",      "payoff_ratio",       ratio),
        ("Net PnL",           "net_pnl",            usd),
        ("Expectancy/trade",  "expectancy",         usd),
        ("Max drawdown",      "max_drawdown",       usd),
        ("Worst trade",       "worst_trade",        usd),
        ("Stops",             "n_stops",            count),
        ("Liquidations",      "n_liquidations",     count),
        ("BE win rate",       "breakeven_winrate",  pct),
        ("Actual win rate",   "actual_winrate",     pct),
    ]

    variant_names = [name for name, _ in named]
    variant_dicts = [d for _, d in named]

    # Pre-render all cells
    rendered: list[list[str]] = []  # rendered[row_idx][col_idx]
    labels: list[str] = []
    for label, key, fmt in rows:
        labels.append(label)
        rendered.append([fmt(d.get(key, 0)) for d in variant_dicts])

    # Column widths
    label_width = max(len(l) for l in labels)
    col_widths = [
        max(len(name), max(len(rendered[r][c]) for r in range(len(rows))))
        for c, name in enumerate(variant_names)
    ]

    # Header
    header = f"{'':>{label_width}}  " + "  ".join(
        name.rjust(col_widths[c]) for c, name in enumerate(variant_names)
    )
    sep = "-" * len(header)

    lines = [sep, header, sep]
    for r, label in enumerate(labels):
        cells = "  ".join(rendered[r][c].rjust(col_widths[c]) for c in range(len(variant_names)))
        lines.append(f"{label:>{label_width}}  {cells}")
    lines.append(sep)

    return "\n".join(lines)
