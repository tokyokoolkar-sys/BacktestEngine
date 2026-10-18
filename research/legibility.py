"""
legibility.py — the sample-size filter that decides what we are allowed to look for.

WHY THIS EXISTS
---------------
The checkpoint-DCA family did not only fail walk-forward. It failed a second,
independent test that no amount of parameter search can pass:

    to distinguish a 52% edge from a 50% coinflip at 2 sigma takes ~2,500 trades

At $20 of capital and Binance's $5 min notional, the account supports ~40
independent positions before it is gone. Even $1,000 supports only 2,000. So a
coinflip-grade edge is UNMEASURABLE at any capital level reachable here — not
unprofitable, unmeasurable. We would be guessing and calling it research.

The escape is not more capital. It is a larger edge. Big edges announce
themselves in few trades:

    trades needed ~ (2 * sigma / delta)^2

A 65%-win-rate rule at 1:1 payoff resolves in ~60 trades. That fits inside a
$200 account. A 51% rule never resolves at all.

So this module is a GATE, applied BEFORE we get excited about any backtest:
a candidate strategy is only admissible if its claimed edge is large enough to
have been visible in the number of trades we can actually afford to take.

This is what kills the entire scalp family permanently, on grounds that have
nothing to do with its P&L. 734 trades/month can never be validated by an
account that supports 40.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# Binance USD-M minimum notional. Sets the smallest position an account can hold,
# and therefore the maximum number of independent bets a given balance supports.
#
# NOT uniform across symbols — verified against live exchange filters 2026-07-27:
#     BTCUSDT / BTCUSDC   $50
#     ETHUSDT / ETHUSDC   $20
#     SOL, XRP, DOGE      $5
# Using 5.0 for a major overstates the affordable-trade count by 4-10x. Pass the
# symbol's real filter value when the candidate names its universe.
MIN_NOTIONAL = 5.0

MIN_NOTIONAL_BY_SYMBOL = {
    "BTCUSDT": 50.0, "BTCUSDC": 50.0,
    "ETHUSDT": 20.0, "ETHUSDC": 20.0,
}


def min_notional_for(symbol: str) -> float:
    """Real exchange minimum for `symbol`, falling back to the $5 common case."""
    return MIN_NOTIONAL_BY_SYMBOL.get(symbol.upper(), MIN_NOTIONAL)


def trades_to_resolve(win_rate: float, baseline: float = 0.5, sigmas: float = 2.0) -> float:
    """Trades needed to distinguish `win_rate` from `baseline` at `sigmas` confidence.

    Standard two-proportion detectability. sigma of a Bernoulli proportion over n
    trials is sqrt(p(1-p)/n); we need the gap to exceed `sigmas` of it:

        |p - p0| >= sigmas * sqrt(p(1-p)/n)   ->   n >= (sigmas^2 * p(1-p)) / (p-p0)^2

    Returns inf when win_rate == baseline (no gap is ever resolvable).
    """
    delta = abs(win_rate - baseline)
    if delta < 1e-9:
        return math.inf
    return (sigmas ** 2) * win_rate * (1.0 - win_rate) / (delta ** 2)


def breakeven_win_rate(tp_pct: float, stop_pct: float) -> float:
    """Win rate required to break even at a given payoff ratio, before fees.

    A 90% win rate at 1:10 payoff loses money. Never read a win rate without this.
    """
    return stop_pct / (tp_pct + stop_pct)


def affordable_trades(capital: float, margin_per_trade: float | None = None,
                      leverage: int = 10) -> float:
    """How many independent positions this capital supports before ruin.

    Uses the smallest legal position unless a larger margin is specified, since
    that is the most generous possible count.
    """
    if margin_per_trade is None:
        margin_per_trade = MIN_NOTIONAL / leverage
    return capital / margin_per_trade


def fee_drag_pct(n_trades: float, margin_per_trade: float, capital: float,
                 leverage: int = 10, fee: float = 0.0005) -> float:
    """Round-trip fee cost as a percentage of capital.

    Note this is TURNOVER-driven and scale-invariant: capital cancels against
    margin, so a $20 account and a $71 account running the same strategy pay the
    same percentage. The live account's 20.7%/month came from 734 trades, not
    from being small.
    """
    return n_trades * (margin_per_trade / capital) * leverage * fee * 2 * 100


@dataclass(frozen=True)
class Legibility:
    """Verdict on whether a candidate's claimed edge could ever be verified."""
    win_rate: float
    tp_pct: float
    stop_pct: float
    observed_trades: int
    capital: float
    need_trades: float
    can_afford: float
    breakeven: float
    edge_over_breakeven: float
    admissible: bool
    reason: str


def assess(win_rate: float, tp_pct: float, stop_pct: float, observed_trades: int,
           capital: float = 200.0, leverage: int = 10) -> Legibility:
    """Gate a candidate strategy on measurability, not profitability.

    Admissible requires all of:
      1. the win rate actually beats break-even for its payoff ratio
      2. the edge resolves in fewer trades than the account can afford to take
      3. the backtest itself contains at least that many trades (otherwise the
         result is noise regardless of how good it looks)
    """
    be = breakeven_win_rate(tp_pct, stop_pct)
    edge = win_rate - be
    need = trades_to_resolve(win_rate, baseline=be)
    afford = affordable_trades(capital, leverage=leverage)

    if edge <= 0:
        return Legibility(win_rate, tp_pct, stop_pct, observed_trades, capital,
                          need, afford, be, edge, False,
                          f"win rate {win_rate:.1%} <= break-even {be:.1%}")
    if need > afford:
        return Legibility(win_rate, tp_pct, stop_pct, observed_trades, capital,
                          need, afford, be, edge, False,
                          f"needs {need:.0f} trades to prove, account affords {afford:.0f}")
    if observed_trades < need:
        return Legibility(win_rate, tp_pct, stop_pct, observed_trades, capital,
                          need, afford, be, edge, False,
                          f"backtest has {observed_trades} trades, needs {need:.0f}")
    return Legibility(win_rate, tp_pct, stop_pct, observed_trades, capital,
                      need, afford, be, edge, True,
                      f"resolves in {need:.0f} trades; affordable and observed")
