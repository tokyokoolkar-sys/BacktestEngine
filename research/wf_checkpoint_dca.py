"""
wf_checkpoint_dca.py — walk-forward the live strategy family. Formal burial.

The family: candle-color checkpoint entry + DCA ladder + fixed stop, which is what
flip_20_config.yaml runs live and what lost -$25.84 over 734 trades in July 2026.

exp_payoff_structure.py already showed the grid is negative in 49 of 51 month-config
cells. This asks the sharper question: if you had honestly SELECTED parameters on
May and traded them in June, then selected on June and traded them in July — what
would you have made? That is the only number that was ever available to you in
advance, and it is the one nobody computed before wiring real money to it.

Run:  python research/wf_checkpoint_dca.py
"""

from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import BacktestConfig, DCAConfig, ExecConfig, StrategyConfig
from walkforward import Window, report, walk_forward

FLEET = ["NEARUSDT", "TIAUSDT", "OPUSDT", "FILUSDT", "UNIUSDT", "SUIUSDT"]

WINDOWS = [
    Window("MAY 2026", _ROOT / "cache" / "oos_may"),
    Window("JUNE 2026", _ROOT / "cache" / "oos_june"),
    Window("JULY 2026", _ROOT / "cache" / "oos_july"),
]

GRID = [
    {"tp": tp, "stop": stop}
    for tp, stop in product([0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0], [5.0, 3.0, 2.0])
    if tp < stop
]


def build_cfg(p: dict) -> BacktestConfig:
    return BacktestConfig(
        strategy=StrategyConfig(
            entry_mode="red_dip",
            green_gate="off",
            stop_mode="fixed",
            stop_pct=p["stop"],
            # "target", never "first_green" — live has no green-candle exit; exits
            # are exchange-side TP/STOP orders. See exp_payoff_structure.py.
            checkpoint_mode="target",
            position_model="dca",
            direction="long",
            greencount_filter=True,
            greencount_period=10,
        ),
        dca=DCAConfig(
            take_profit_pct=p["tp"],
            max_dca_orders=3,
            price_drop_step_pct=0.3,
            step_multiplier=1.0,
            size_multiplier=1.1,
            first_order_ratio=0.96,
            atr_ladder_mult=0.75,
            atr_step_min_pct=0.2,
            atr_step_max_pct=1.0,
        ),
        exec=ExecConfig(
            leverage=10, margin_usdt=2.0,
            taker_fee=0.0005, maker_fee=0.0002,
            slippage_bps=2.0, maint_margin_rate=0.05,
        ),
    )


if __name__ == "__main__":
    missing = [w.name for w in WINDOWS if not w.path.exists()]
    if missing:
        print(f"missing windows: {missing}")
        print("fetch with: python fetch_daily.py <SYMBOLS> <start> <end> <outdir>")
        sys.exit(1)

    folds = walk_forward(WINDOWS, FLEET, GRID, build_cfg)
    print(report(folds, "checkpoint-DCA (the live family)"))
