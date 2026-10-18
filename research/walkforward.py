"""
walkforward.py — pick parameters on one window, score them on the NEXT one.

WHY THIS EXISTS
---------------
Every "winning" configuration this repo has produced was selected by scanning a
grid and reading off the best cell — on the same data used to score it. That is
not a backtest result, it is the maximum of a noise distribution. The July 2026
payoff sweep made the failure legible:

    config (tp=2.0, stop=3.0)      MAY -10.44    JUNE -41.37    JULY +5.79
    config (tp=3.0, stop=5.0)      MAY  +0.22    JUNE -23.46    JULY +5.74

Reading July alone says "8 of 17 configs are profitable". Reading all three says
the ranking does not survive contact with the next month. The live account traded
July's shape and lost money anyway, because the parameters came from May.

So: never report an in-sample number again. This module scores a parameter set
the only way that means anything —

    select on window N  ->  score on window N+1  ->  never look back

and reports the OUT-OF-SAMPLE result of that procedure. If the selection rule has
no skill, the OOS number is negative and the family is dead. That is a result, and
it is worth more than another grid maximum.

WHAT COUNTS AS A PASS
---------------------
A strategy family is worth deploying only if, across every fold:
    1. pooled OOS net PnL > 0
    2. the majority of individual folds are OOS-positive (not one fold carrying)
    3. pooled OOS gross > 0 BEFORE fees — otherwise the "edge" is a fee illusion
       that any change in cost structure erases
    4. zero liquidations, or liquidation PnL small vs total

Rule 3 is the one that killed the checkpoint-DCA family: June gross was -$25.81
before a cent of commission. No parameter fixes a signal that is wrong on entry.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pandas as pd

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import BacktestConfig
from data import load_funding, load_klines
from engine import Result, run_backtest
from metrics import scorecard


@dataclass(frozen=True)
class Window:
    """One contiguous evaluation period: a directory of per-symbol parquet."""
    name: str
    path: Path


@dataclass
class FoldResult:
    train: str
    test: str
    chosen: dict          # the parameter dict selected on `train`
    train_net: float
    test_net: float
    test_gross: float
    test_fees: float
    test_trades: int
    test_wr: float
    test_liq: int


def load_symbol(window: Window, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Load klines + funding for one symbol in one window. None if absent."""
    for name in (f"{symbol}-1m-klines.parquet", "klines.parquet"):
        kp = window.path / name
        if kp.exists():
            klines = load_klines(kp)
            break
    else:
        return None

    fp = _ROOT / "cache" / f"{symbol}-funding.parquet"
    funding = load_funding(fp) if fp.exists() else pd.DataFrame(
        {"funding_time": pd.to_datetime([], utc=True), "funding_rate": []}
    )
    return klines, funding


def run_pooled(window: Window, symbols: Sequence[str], cfg: BacktestConfig) -> Result:
    """Run one config across every symbol and pool the trades into one Result.

    Pooling before scoring matters: a fleet win rate is not the mean of per-symbol
    win rates, because symbols contribute wildly different trade counts.
    """
    pooled: list = []
    for sym in symbols:
        loaded = load_symbol(window, sym)
        if loaded is None:
            continue
        pooled.extend(run_backtest(loaded[0], loaded[1], cfg).trades)
    return Result(trades=pooled, equity_curve=[], cfg=cfg)


def summarize(res: Result) -> dict:
    """Scorecard plus the pre-fee decomposition, which is the honest signal test."""
    if not res.trades:
        return {"n_trades": 0, "net": 0.0, "gross": 0.0, "fees": 0.0,
                "wr": 0.0, "liq": 0, "exp": 0.0}
    sc = scorecard(res)
    gross = sum(t.gross_pnl for t in res.trades)
    fees = sum(t.fees for t in res.trades)
    liq = sum(1 for t in res.trades if t.exit_reason == "liquidation")
    return {
        "n_trades": sc["n_trades"],
        "net": sc["net_pnl"],
        "gross": gross,
        "fees": fees,
        "wr": sc["win_rate"] * 100.0,
        "liq": liq,
        "exp": sc["net_pnl"] / sc["n_trades"],
    }


def walk_forward(
    windows: Sequence[Window],
    symbols: Sequence[str],
    grid: Iterable[dict],
    build_cfg: Callable[[dict], BacktestConfig],
    select: Callable[[list[tuple[dict, dict]]], dict] | None = None,
) -> list[FoldResult]:
    """Roll through windows: fit on i, test on i+1. Returns one FoldResult per fold.

    `grid`      — parameter dicts to search over on the training window.
    `build_cfg` — turns one such dict into a BacktestConfig.
    `select`    — picks the winner from [(params, summary), ...]. Defaults to max
                  net PnL, which is the naive rule the repo has used implicitly.
                  Pass something stricter to test whether a better *selection rule*
                  rescues a family that the naive rule cannot.
    """
    grid = list(grid)
    if select is None:
        def select(scored: list[tuple[dict, dict]]) -> dict:
            return max(scored, key=lambda ps: ps[1]["net"])[0]

    folds: list[FoldResult] = []
    for train_w, test_w in zip(windows, windows[1:]):
        scored: list[tuple[dict, dict]] = []
        for params in grid:
            summ = summarize(run_pooled(train_w, symbols, build_cfg(params)))
            if summ["n_trades"] == 0:
                continue
            scored.append((params, summ))
        if not scored:
            continue

        chosen = select(scored)
        train_net = next(s["net"] for p, s in scored if p == chosen)
        t = summarize(run_pooled(test_w, symbols, build_cfg(chosen)))

        folds.append(FoldResult(
            train=train_w.name, test=test_w.name, chosen=chosen,
            train_net=train_net, test_net=t["net"], test_gross=t["gross"],
            test_fees=t["fees"], test_trades=t["n_trades"], test_wr=t["wr"],
            test_liq=t["liq"],
        ))
    return folds


def verdict(folds: Sequence[FoldResult]) -> tuple[bool, list[str]]:
    """Apply the four pass rules. Returns (passed, reasons) — reasons always populated."""
    if not folds:
        return False, ["no folds ran — need at least 2 windows"]

    oos_net = sum(f.test_net for f in folds)
    oos_gross = sum(f.test_gross for f in folds)
    positive = sum(1 for f in folds if f.test_net > 0)
    liqs = sum(f.test_liq for f in folds)

    reasons = []
    ok_net = oos_net > 0
    ok_maj = positive * 2 > len(folds)
    ok_gross = oos_gross > 0
    ok_liq = liqs == 0

    reasons.append(f"{'PASS' if ok_net else 'FAIL'}  pooled OOS net {oos_net:+.2f}")
    reasons.append(f"{'PASS' if ok_maj else 'FAIL'}  {positive}/{len(folds)} folds OOS-positive")
    reasons.append(f"{'PASS' if ok_gross else 'FAIL'}  pooled OOS gross (pre-fee) {oos_gross:+.2f}")
    reasons.append(f"{'PASS' if ok_liq else 'WARN'}  {liqs} liquidations in OOS")

    return (ok_net and ok_maj and ok_gross), reasons


def report(folds: Sequence[FoldResult], title: str) -> str:
    lines = ["=" * 92, f"  WALK-FORWARD: {title}", "=" * 92,
             f"{'train':>12} {'test':>12} {'IS net':>9} {'OOS net':>9} "
             f"{'OOSgross':>9} {'fees':>8} {'trades':>7} {'WR':>7} {'liq':>4}  params"]
    for f in folds:
        p = ",".join(f"{k}={v}" for k, v in f.chosen.items())
        lines.append(
            f"{f.train:>12} {f.test:>12} {f.train_net:+9.2f} {f.test_net:+9.2f} "
            f"{f.test_gross:+9.2f} {f.test_fees:8.2f} {f.test_trades:7d} "
            f"{f.test_wr:6.2f}% {f.test_liq:4d}  {p}"
        )
    passed, reasons = verdict(folds)
    lines.append("-" * 92)
    lines.extend("  " + r for r in reasons)
    lines.append("-" * 92)
    lines.append("  VERDICT: " + ("DEPLOYABLE" if passed
                                  else "NOT DEPLOYABLE — do not risk capital"))
    lines.append("=" * 92)
    return "\n".join(lines)
