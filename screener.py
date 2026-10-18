"""screener.py — Multi-symbol, out-of-sample-validated long-market screener.

Imports config, data, engine, metrics.  No network I/O here; all downloads
are delegated to data.download_klines.  The OOS train/validate split and
overfit flag are the anti-curve-fitting heart of this module.

Public API
----------
    split_train_validate(klines, frac) -> (train_df, validate_df)
    regime_stats(klines)               -> dict
    screen_symbol(symbol, klines, funding, stop_grid, frac, base_kwargs) -> dict
    screen_universe(symbols, months, stop_grid, frac, fetch, base_kwargs) -> list[dict]
    format_screen(rows)                -> str
    summarize(rows)                    -> str
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from config import BacktestConfig, ExecConfig, StrategyConfig
from data import download_klines, load_klines
from engine import run_backtest
from metrics import scorecard

logger = logging.getLogger(__name__)

# Root dir mirrors data.py (same parent of this file)
_ROOT = Path(__file__).parent

# Minimum per-trade expectancy (USDT) to count as a real edge rather than noise.
# Per-trade expectancy noise over a multi-thousand-trade window is ~0.001 USDT; this floor
# (~5x that) stops sub-noise micro-edges from producing spurious "edge"/"overfit" verdicts.
_MIN_EDGE = 0.005


# ---------------------------------------------------------------------------
# Empty funding frame — engine tolerates it: searchsorted over [] → 0 funding
# ---------------------------------------------------------------------------

def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame({
        "funding_time": pd.to_datetime([], utc=True),
        "funding_rate": pd.Series([], dtype="float64"),
    })


# ---------------------------------------------------------------------------
# Time-ordered train / validate split
# ---------------------------------------------------------------------------

def split_train_validate(
    klines: pd.DataFrame,
    frac: float = 0.7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Time-ordered split; first `frac` rows = train, remainder = validate.

    Returns (train, validate) each with a reset RangeIndex.
    No shuffling — time-series must stay ordered.
    """
    n = len(klines)
    cutoff = int(math.floor(n * frac))
    train = klines.iloc[:cutoff].reset_index(drop=True)
    validate = klines.iloc[cutoff:].reset_index(drop=True)
    return train, validate


# ---------------------------------------------------------------------------
# Regime statistics
# ---------------------------------------------------------------------------

def regime_stats(klines: pd.DataFrame) -> dict:
    """Compute macro regime labels for the full klines series.

    Returns dict with keys:
        total_return_pct  : float
        daily_vol_pct     : float   (annualised to 1-day via sqrt(1440))
        trend_label       : "up" | "down" | "flat"
        vol_label         : "high" | "low"
    """
    close = klines["close"]
    if len(close) < 2:
        return {
            "total_return_pct": 0.0,
            "daily_vol_pct": 0.0,
            "trend_label": "flat",
            "vol_label": "low",
        }

    total_return_pct = (close.iloc[-1] / close.iloc[0] - 1.0) * 100.0

    # 1-minute pct-change → annualise to daily via sqrt(1440)
    pct_changes = close.pct_change().dropna()
    daily_vol_pct = float(pct_changes.std() * math.sqrt(1440) * 100.0)

    if total_return_pct > 20.0:
        trend_label = "up"
    elif total_return_pct < -20.0:
        trend_label = "down"
    else:
        trend_label = "flat"

    vol_label = "high" if daily_vol_pct > 8.0 else "low"

    return {
        "total_return_pct": total_return_pct,
        "daily_vol_pct": daily_vol_pct,
        "trend_label": trend_label,
        "vol_label": vol_label,
    }


# ---------------------------------------------------------------------------
# Single-symbol screener
# ---------------------------------------------------------------------------

def _make_cfg(symbol: str, stop_pct: float | None, base_kwargs: dict | None) -> BacktestConfig:
    """Build a BacktestConfig for the color-scalp strategy (long or short)."""
    bk = base_kwargs or {}
    direction = bk.get("direction", "long")
    if stop_pct is None:
        strat = StrategyConfig(
            entry_mode="red_dip",
            green_gate="off",
            stop_mode="none",
            stop_pct=None,
            checkpoint_mode="first_green",
            position_model="single",
            direction=direction,
        )
    else:
        strat = StrategyConfig(
            entry_mode="red_dip",
            green_gate="off",
            stop_mode="fixed",
            stop_pct=stop_pct,
            checkpoint_mode="first_green",
            position_model="single",
            direction=direction,
        )
    return BacktestConfig(
        symbol=symbol,
        strategy=strat,
        exec=ExecConfig(**{k: v for k, v in bk.items() if k in ExecConfig.__dataclass_fields__}),
    )


def screen_symbol(
    symbol: str,
    klines: pd.DataFrame,
    funding: pd.DataFrame,
    stop_grid: list[float],
    frac: float = 0.7,
    base_kwargs: dict | None = None,
) -> dict:
    """Screen one symbol: grid-search on TRAIN, evaluate chosen stop on VALIDATE.

    Parameters
    ----------
    symbol      : ticker string (used for BacktestConfig.symbol)
    klines      : full klines DataFrame (will be split internally)
    funding     : funding DataFrame (may be empty)
    stop_grid   : list of stop_pct values to try; "none" candidate is always added
    frac        : fraction of rows for train split
    base_kwargs : optional overrides for ExecConfig fields

    Returns dict with keys:
        symbol, best_stop, train_exp, oos_exp, train_net, oos_net,
        n_train, n_valid, overfit(bool), verdict, + regime_stats fields.
    """
    _NO_DATA = {
        "symbol": symbol,
        "best_stop": None,
        "train_exp": 0.0,
        "oos_exp": 0.0,
        "train_net": 0.0,
        "oos_net": 0.0,
        "n_train": 0,
        "n_valid": 0,
        "overfit": False,
        "verdict": "no-data",
        "total_return_pct": 0.0,
        "daily_vol_pct": 0.0,
        "trend_label": "flat",
        "vol_label": "low",
        "oos_return_pct": 0.0,
        "oos_trend": "flat",
        "oos_vol": "low",
    }

    if klines is None or len(klines) < 100:
        logger.warning("%s: too few rows (%d) — skipping", symbol, len(klines) if klines is not None else 0)
        return _NO_DATA

    # 1. Split
    train, validate = split_train_validate(klines, frac)

    if len(train) < 50 or len(validate) < 20:
        logger.warning("%s: splits too small (train=%d, val=%d)", symbol, len(train), len(validate))
        row = dict(_NO_DATA)
        row["n_train"] = len(train)
        row["n_valid"] = len(validate)
        return row

    # 2. Grid search over TRAIN: candidates = [None] + stop_grid
    candidates: list[float | None] = [None] + list(stop_grid)
    best_stop: float | None = None
    best_train_exp: float = float("-inf")
    best_train_net: float = 0.0

    for stop_val in candidates:
        cfg = _make_cfg(symbol, stop_val, base_kwargs)
        try:
            result = run_backtest(train, funding, cfg)
            sc = scorecard(result)
            exp = sc["expectancy"]
            if exp > best_train_exp:
                best_train_exp = exp
                best_stop = stop_val
                best_train_net = sc["net_pnl"]
        except Exception as exc:
            logger.debug("%s stop=%s train error: %s", symbol, stop_val, exc)
            continue

    if best_train_exp == float("-inf"):
        # All grid candidates raised — surface it (likely a data-quality problem)
        logger.warning("%s: all %d train candidates failed — marking no-data", symbol, len(candidates))
        row = dict(_NO_DATA)
        row["n_train"] = len(train)
        row["n_valid"] = len(validate)
        row["verdict"] = "no-data"
        return row

    # 3. Evaluate chosen stop on VALIDATE (OOS)
    cfg_oos = _make_cfg(symbol, best_stop, base_kwargs)
    oos_ok = True
    try:
        result_oos = run_backtest(validate, funding, cfg_oos)
        sc_oos = scorecard(result_oos)
        oos_exp = sc_oos["expectancy"]
        oos_net = sc_oos["net_pnl"]
    except Exception as exc:
        logger.warning("%s OOS eval failed: %s", symbol, exc)
        oos_ok = False
        oos_exp = 0.0
        oos_net = 0.0

    # 4. Regime: FULL series for context, VALIDATE window for the honest "edge condition".
    #    Using the full-series label would leak the OOS outcome into the condition report.
    reg_full = regime_stats(klines)
    reg_oos = regime_stats(validate)

    # 5. Verdict — with a significance floor (_MIN_EDGE) so sub-noise micro-edges don't qualify,
    #    and a distinct "no-data" when the OOS backtest could not be evaluated.
    if not oos_ok:
        overfit = False
        verdict = "no-data"
    else:
        overfit = bool(best_train_exp > _MIN_EDGE and oos_exp <= _MIN_EDGE)
        if oos_exp > _MIN_EDGE and not overfit:
            verdict = "edge"
        elif overfit:
            verdict = "overfit"
        else:
            verdict = "no-edge"

    return {
        "symbol": symbol,
        "best_stop": best_stop,
        "train_exp": best_train_exp,
        "oos_exp": oos_exp,
        "train_net": best_train_net,
        "oos_net": oos_net,
        "n_train": len(train),
        "n_valid": len(validate),
        "overfit": overfit,
        "verdict": verdict,
        "total_return_pct": reg_full["total_return_pct"],
        "daily_vol_pct": reg_full["daily_vol_pct"],
        "trend_label": reg_full["trend_label"],
        "vol_label": reg_full["vol_label"],
        "oos_return_pct": reg_oos["total_return_pct"],
        "oos_trend": reg_oos["trend_label"],
        "oos_vol": reg_oos["vol_label"],
    }


# ---------------------------------------------------------------------------
# Universe screener
# ---------------------------------------------------------------------------

def screen_universe(
    symbols: list[str],
    months: list[str],
    stop_grid: list[float],
    frac: float = 0.7,
    fetch: bool = True,
    base_kwargs: dict | None = None,
) -> list[dict]:
    """Screen every symbol in the list; continue past individual failures.

    Per-symbol cache directory: <ROOT>/cache/<symbol>/klines.parquet
    Funding is always the empty DataFrame (screener assumption).
    """
    funding = _empty_funding()
    rows: list[dict] = []

    for symbol in symbols:
        cache_dir = _ROOT / "cache" / symbol
        parquet_path = cache_dir / "klines.parquet"

        # --- ensure cache ---
        if not parquet_path.exists():
            if not fetch:
                logger.warning(
                    "%s: no cache at %s and --fetch not set; skipping",
                    symbol, parquet_path,
                )
                print(f"[screener] SKIP {symbol}: no cache and --fetch not set.")
                continue
            try:
                print(f"[screener] Downloading {symbol} …")
                download_klines(symbol, months, out_dir=str(cache_dir))
            except RuntimeError as exc:
                logger.warning("%s: download failed — %s; skipping", symbol, exc)
                print(f"[screener] SKIP {symbol}: {exc}")
                continue
            except Exception as exc:
                logger.warning("%s: unexpected download error — %s; skipping", symbol, exc)
                print(f"[screener] SKIP {symbol}: {exc}")
                continue

        # --- load ---
        try:
            klines = load_klines(parquet_path)
        except Exception as exc:
            logger.warning("%s: load failed — %s; skipping", symbol, exc)
            print(f"[screener] SKIP {symbol}: load error: {exc}")
            continue

        if klines is None or len(klines) == 0:
            print(f"[screener] SKIP {symbol}: empty klines after load.")
            continue

        # --- screen ---
        print(f"[screener] Screening {symbol} ({len(klines):,} rows) …")
        try:
            row = screen_symbol(symbol, klines, funding, stop_grid, frac, base_kwargs)
        except Exception as exc:
            logger.warning("%s: screen_symbol failed — %s; skipping", symbol, exc)
            print(f"[screener] SKIP {symbol}: screen error: {exc}")
            continue

        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_screen(rows: list[dict]) -> str:
    """Aligned monospaced table sorted by OOS expectancy descending.

    Columns: symbol | trend | vol | best_stop | train_exp | OOS_exp | overfit | verdict
    Overfits are marked with  ***  in the overfit column.
    """
    if not rows:
        return "(no results)"

    sorted_rows = sorted(rows, key=lambda r: r.get("oos_exp", float("-inf")), reverse=True)

    # Header
    hdr = f"{'SYMBOL':<12} {'TREND':<5} {'VOL':<4} {'STOP%':>6} {'TRAIN_EXP':>10} {'OOS_EXP':>10} {'OVERFIT':<8} {'VERDICT':<8}"
    sep = "-" * len(hdr)
    lines = [sep, hdr, sep]

    for r in sorted_rows:
        symbol    = str(r.get("symbol", "?"))
        trend     = str(r.get("oos_trend", r.get("trend_label", "?")))
        vol       = str(r.get("oos_vol", r.get("vol_label", "?")))
        stop_val  = r.get("best_stop")
        stop_str  = f"{stop_val:.2f}" if stop_val is not None else "none"
        train_exp = r.get("train_exp", 0.0)
        oos_exp   = r.get("oos_exp", 0.0)
        overfit   = r.get("overfit", False)
        verdict   = str(r.get("verdict", "?"))

        # Highlight overfits visually
        ovf_str   = "***YES***" if overfit else "no"

        # Colour verdict with a marker for edge
        verdict_str = f">>> {verdict} <<<" if verdict == "edge" else verdict

        lines.append(
            f"{symbol:<12} {trend:<5} {vol:<4} {stop_str:>6} "
            f"{train_exp:>+10.4f} {oos_exp:>+10.4f} {ovf_str:<9} {verdict_str}"
        )

    lines.append(sep)
    return "\n".join(lines)


def summarize(rows: list[dict]) -> str:
    """One-paragraph summary of screener results.

    Reports survivors (verdict=="edge") and any regime conditions they share.
    If none, states plainly that the long color-scalp shows no OOS edge.
    """
    if not rows:
        return "No symbols were screened."

    survivors = [r for r in rows if r.get("verdict") == "edge"]
    n_total = len(rows)
    n_edge = len(survivors)
    n_overfit = sum(1 for r in rows if r.get("verdict") == "overfit")
    n_noedge  = sum(1 for r in rows if r.get("verdict") == "no-edge")
    n_nodata  = sum(1 for r in rows if r.get("verdict") == "no-data")

    lines = [
        f"Universe: {n_total} symbols screened  |  "
        f"edge: {n_edge}  overfit: {n_overfit}  no-edge: {n_noedge}  no-data: {n_nodata}"
    ]

    if n_edge == 0:
        lines.append(
            "CONCLUSION: The candle-color scalp shows NO out-of-sample edge across "
            "this universe. This is a valid finding — the strategy does not generalise."
        )
        return "\n".join(lines)

    # Report survivors
    lines.append(f"\nSurvivors with OOS edge ({n_edge}):")
    for r in survivors:
        stop_val = r.get("best_stop")
        stop_str = f"{stop_val:.2f}%" if stop_val is not None else "none"
        lines.append(
            f"  {r['symbol']:<12}  trend={r.get('oos_trend','?')}  "
            f"vol={r.get('oos_vol','?')}  stop={stop_str}  "
            f"OOS_exp={r.get('oos_exp',0.0):+.4f}"
        )

    # Shared regime conditions
    trends = {r.get("oos_trend") for r in survivors}
    vols   = {r.get("oos_vol")   for r in survivors}

    shared = []
    if len(trends) == 1:
        shared.append(f"trend={next(iter(trends))}")
    if len(vols) == 1:
        shared.append(f"vol={next(iter(vols))}")

    if shared:
        lines.append(
            f"\nTransferable condition(s) shared by all survivors: {', '.join(shared)}"
        )
    else:
        lines.append(
            "\nNo single regime label is shared by all survivors — "
            "edge may not be robustly transferable."
        )

    return "\n".join(lines)
