"""Prediction-market book logger — measures whether strategy A actually exists.

Strategy A is "buy YES + NO for less than $1". On a unified CLOB that is
arithmetic nonsense: the NO book is the mirror of the YES book, so
NO_ask == 1 - YES_bid by construction and the sum is under 1 only when the
book is crossed. Verified live on both venues before writing this:

    Polymarket YES bid/ask 0.019/0.020  ->  NO bid/ask 0.980/0.981  (sizes identical)
    Kalshi     yes_bid 0.0000 yes_ask 0.0100 -> no_bid 0.9900 no_ask 1.0000

So the naive version is dead and this logger does not look for it. What it
looks for is the two places the same trade genuinely can exist:

  1. COMPLEMENTARY SETS. A mutually exclusive, exhaustive set of N outcomes
     must sum to exactly $1 at settlement. If the best asks across the whole
     set sum to less than $1, buying one of each is a locked payoff. Kalshi
     flags these as `mutually_exclusive: True` (BTC/ETH hourly strike ladders,
     80-188 markets per event). Polymarket flags them `negRisk: True`.

  2. CROSS-VENUE. Same question on two venues at different prices. NOT logged
     as arb here, because it is not arb -- see SETTLEMENT BASIS below.

Everything is measured at the price you would actually hit (best ask, with the
size resting there), never at mid. Mid-price backtests of prediction markets
are fiction; the spread is the entire game.

WHAT THIS ANSWERS
    frequency  - how many times per day does a set sum below $1
    depth      - how much size is actually available at those prices
    net edge   - what survives taker fees
    decay      - how long the dislocation persists (are you racing a bot)

If the answer is "twice a week, $40 deep, 0.3c net", strategy A is dead and
that cost one week of free data instead of live capital.

SETTLEMENT BASIS (the risk that hides inside "riskless")
    Kalshi crypto settles off CF Benchmarks. Polymarket uses its own oracle.
    Two same-sounding questions can differ in index, rounding, and cutoff
    millisecond. A cross-venue pair is a basis trade wearing arb's coat, so
    cross-venue observations are recorded with venue+source attached and are
    NEVER scored as locked edge.

SECOND JOB
    The spot tape written alongside is what strategy B (the 5-step directional
    model) needs to be backtested honestly. B's edge lives in sigma and in the
    favourite-longshot bias, and neither can be measured without knowing the
    ask you would have paid at each moment. This file produces that.

USAGE
    python research/pm_book_logger.py                    # run until Ctrl-C
    python research/pm_book_logger.py --hours 168        # one week
    python research/pm_book_logger.py --once             # single sweep, prints
    python research/pm_book_logger.py --report           # analyse what exists
"""
from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

OUT = Path(__file__).parent.parent / "cache" / "pm_books"

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
# fapi.binance.com is ISP-blocked in this environment; the .vision mirror is not.
BINANCE_SPOT = "https://data-api.binance.vision/api/v3/ticker/price"

# Kalshi crypto series whose events are mutually exclusive strike ladders.
KALSHI_SERIES = ["KXBTC", "KXETH", "KXBTCD", "KXETHD", "KXSOL", "KXXRP"]
SPOT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# Log any set summing below this. Deliberately above 1.0 so we capture the
# near-misses too -- the distribution of how close it gets is the real answer,
# not just the count of crossings.
LOG_THRESHOLD = 1.02


# --------------------------------------------------------------------------
# fees
# --------------------------------------------------------------------------

def kalshi_fee(price: float, contracts: int = 1) -> float:
    """Kalshi taker fee: ceil(0.07 * C * P * (1-P)) in cents, per their schedule.

    Peaks at 1.75c/contract at P=0.50 and vanishes at the tails. This is why
    the tails are where prediction-market edge survives and the middle is not.
    """
    return math.ceil(0.07 * contracts * price * (1.0 - price) * 100) / 100.0


def polymarket_fee(price: float, size: float, rate: float = 0.05) -> float:
    """Polymarket taker fee, from the live `feeSchedule` block on each market.

    Observed: {'exponent': 1, 'rate': 0.05, 'takerOnly': True}. Charged on the
    lesser side, so it is symmetric in price and also vanishes at the tails.
    Fees were zero historically -- do not trust any older writeup that says so.
    """
    return rate * min(price, 1.0 - price) * size


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass
class SetObs:
    """One mutually-exclusive set priced at one instant."""
    ts: str
    venue: str
    event: str
    n_outcomes: int
    n_quoted: int          # outcomes with a live ask; < n_outcomes means incomplete
    sum_asks: float        # cost to buy one of every outcome -> pays exactly $1
    sum_bids: float        # proceeds from selling one of every outcome
    min_depth: float       # binding constraint: smallest resting size in the set
    gross_edge: float      # 1 - sum_asks
    fee_est: float
    net_edge: float
    complete: bool         # every outcome quoted; false = not a real arb
    close_time: str


@dataclass
class SpotObs:
    ts: str
    symbol: str
    price: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# venue readers
# --------------------------------------------------------------------------

class Session:
    def __init__(self, timeout: int = 25):
        self.s = requests.Session()
        # urllib's default UA gets a 403 from the CLOB; a browser UA does not.
        self.s.headers["User-Agent"] = "Mozilla/5.0"
        self.timeout = timeout

    def get(self, url, **kw):
        r = self.s.get(url, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, url, **kw):
        r = self.s.post(url, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r.json()


def scan_kalshi(sess: Session) -> list[SetObs]:
    """Price every open mutually-exclusive crypto event on Kalshi.

    `with_nested_markets` returns the whole strike ladder in one call, so an
    event of 188 markets costs one request rather than 188. That matters: the
    set has to be priced at one instant or the sum is meaningless.
    """
    out: list[SetObs] = []
    for series in KALSHI_SERIES:
        try:
            data = sess.get(
                f"{KALSHI}/events",
                params={"series_ticker": series, "status": "open",
                        "with_nested_markets": "true", "limit": 40},
            )
        except Exception as e:
            print(f"  kalshi {series}: {e}", file=sys.stderr)
            continue

        for ev in data.get("events", []):
            if not ev.get("mutually_exclusive"):
                continue
            mkts = ev.get("markets", [])
            if len(mkts) < 2:
                continue

            asks, bids, depths = [], [], []
            for m in mkts:
                a = float(m.get("yes_ask_dollars") or 0.0)
                b = float(m.get("yes_bid_dollars") or 0.0)
                sz = float(m.get("yes_ask_size_fp") or 0.0)
                # ask of 0 means no offer, not a free contract.
                if a > 0:
                    asks.append(a)
                    depths.append(sz)
                bids.append(b)

            if not asks:
                continue

            sum_asks = sum(asks)
            fee = sum(kalshi_fee(a) for a in asks)
            gross = 1.0 - sum_asks
            complete = len(asks) == len(mkts)

            if sum_asks < LOG_THRESHOLD:
                out.append(SetObs(
                    ts=_now(), venue="kalshi", event=ev.get("event_ticker", ""),
                    n_outcomes=len(mkts), n_quoted=len(asks),
                    sum_asks=round(sum_asks, 4), sum_bids=round(sum(bids), 4),
                    min_depth=min(depths) if depths else 0.0,
                    gross_edge=round(gross, 4), fee_est=round(fee, 4),
                    net_edge=round(gross - fee, 4), complete=complete,
                    close_time=str(ev.get("strike_date", "")),
                ))
    return out


def scan_polymarket(sess: Session, top_n: int = 60) -> list[SetObs]:
    """Price Polymarket negRisk sets off the real CLOB book, not gamma's cache.

    gamma's bestAsk lags. The batch POST /books call takes every token in the
    set at once, which is the only way the sum means anything.
    """
    out: list[SetObs] = []
    try:
        events = sess.get(
            f"{GAMMA}/events",
            params={"closed": "false", "limit": top_n,
                    "order": "volume24hr", "ascending": "false"},
        )
    except Exception as e:
        print(f"  polymarket events: {e}", file=sys.stderr)
        return out

    for ev in events:
        if not ev.get("negRisk"):
            continue
        mkts = [m for m in ev.get("markets", []) if m.get("active")]
        if len(mkts) < 2:
            continue

        # token[0] is the YES leg of each outcome.
        tokens = []
        for m in mkts:
            try:
                ids = json.loads(m.get("clobTokenIds") or "[]")
                if ids:
                    tokens.append(ids[0])
            except (json.JSONDecodeError, TypeError):
                continue
        if len(tokens) < 2:
            continue

        try:
            books = sess.post(f"{CLOB}/books",
                              json=[{"token_id": t} for t in tokens])
        except Exception as e:
            print(f"  polymarket books {ev.get('slug','')}: {e}", file=sys.stderr)
            continue

        asks, bids, depths = [], [], []
        for bk in books:
            # Both sides arrive sorted worst-to-best, so best is the LAST entry.
            a_side, b_side = bk.get("asks") or [], bk.get("bids") or []
            if a_side:
                best = a_side[-1]
                asks.append(float(best["price"]))
                depths.append(float(best["size"]) * float(best["price"]))
            if b_side:
                bids.append(float(b_side[-1]["price"]))

        if not asks:
            continue

        sum_asks = sum(asks)
        fee = sum(polymarket_fee(a, 1.0) for a in asks)
        gross = 1.0 - sum_asks

        if sum_asks < LOG_THRESHOLD:
            out.append(SetObs(
                ts=_now(), venue="polymarket", event=str(ev.get("slug", ""))[:80],
                n_outcomes=len(mkts), n_quoted=len(asks),
                sum_asks=round(sum_asks, 4), sum_bids=round(sum(bids), 4),
                min_depth=round(min(depths), 2) if depths else 0.0,
                gross_edge=round(gross, 4), fee_est=round(fee, 4),
                net_edge=round(gross - fee, 4),
                complete=len(asks) == len(mkts),
                close_time=str(ev.get("endDate", "")),
            ))
    return out


def scan_spot(sess: Session) -> list[SpotObs]:
    """Spot tape. Strategy B cannot be backtested without the underlying."""
    out = []
    ts = _now()
    for sym in SPOT_SYMBOLS:
        try:
            d = sess.get(BINANCE_SPOT, params={"symbol": sym})
            out.append(SpotObs(ts=ts, symbol=sym, price=float(d["price"])))
        except Exception as e:
            print(f"  spot {sym}: {e}", file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def _append(path: Path, rows: list) -> None:
    """Append as CSV. Crash-safe by construction -- a killed process loses the
    last line, not the file. Convert to parquet at analysis time."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([asdict(r) for r in rows])
    df.to_csv(path, mode="a", header=not path.exists(), index=False)


# --------------------------------------------------------------------------
# loop
# --------------------------------------------------------------------------

_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    print("\nstopping after current sweep...", file=sys.stderr)


def sweep(sess: Session, verbose: bool = False) -> tuple[list[SetObs], list[SpotObs]]:
    sets = scan_kalshi(sess) + scan_polymarket(sess)
    spot = scan_spot(sess)

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _append(OUT / f"sets-{day}.csv", sets)
    _append(OUT / f"spot-{day}.csv", spot)

    if verbose:
        live = [s for s in sets if s.complete and s.net_edge > 0]
        print(f"{_now()}  sets={len(sets)}  spot={len(spot)}  "
              f"positive_net={len(live)}")
        for s in sorted(sets, key=lambda x: x.sum_asks)[:6]:
            flag = "ARB " if (s.complete and s.net_edge > 0) else "    "
            print(f"  {flag}{s.venue:11s} {s.event[:34]:34s} "
                  f"n={s.n_quoted}/{s.n_outcomes} sum={s.sum_asks:.4f} "
                  f"net={s.net_edge:+.4f} depth={s.min_depth:.0f}")
    return sets, spot


def run(hours: float | None, interval: int, verbose: bool) -> None:
    signal.signal(signal.SIGINT, _handle_sigint)
    sess = Session()
    deadline = time.time() + hours * 3600 if hours else None
    n = 0
    while not _stop and (deadline is None or time.time() < deadline):
        t0 = time.time()
        try:
            sweep(sess, verbose=verbose or n % 20 == 0)
        except Exception as e:
            print(f"sweep failed: {e}", file=sys.stderr)
        n += 1
        time.sleep(max(0.0, interval - (time.time() - t0)))
    print(f"done. {n} sweeps -> {OUT}", file=sys.stderr)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def report() -> None:
    """The verdict on strategy A. Only complete sets count -- an incomplete set
    sums low because outcomes are unquoted, not because there is free money."""
    files = sorted(OUT.glob("sets-*.csv"))
    if not files:
        print(f"no data in {OUT}. run the logger first.")
        return

    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], format="mixed", utc=True)
    span_h = (df["ts"].max() - df["ts"].min()).total_seconds() / 3600

    # Rates over a near-zero span are meaningless -- a single sweep would read
    # as thousands per hour. Suppress them rather than print a flattering lie.
    rates = span_h >= 1.0

    def per_hr(n: int) -> str:
        return f"({n / span_h:.2f}/hr)" if rates else "(span too short for a rate)"

    print(f"observations : {len(df):,} over {span_h:.1f}h "
          f"({df['ts'].min():%Y-%m-%d %H:%M} -> {df['ts'].max():%Y-%m-%d %H:%M})")
    print(f"events seen  : {df['event'].nunique()}")

    comp = df[df["complete"]]
    print(f"\ncomplete sets (every outcome quoted): {len(comp):,} of {len(df):,}")
    if comp.empty:
        print("\nNo complete set was ever observed. Strategy A has no surface here.")
        return

    print("\nsum-of-asks distribution, complete sets only")
    for q in [0.001, 0.01, 0.05, 0.25, 0.50]:
        print(f"  p{q*100:>5.1f}  {comp['sum_asks'].quantile(q):.4f}")
    print(f"  min    {comp['sum_asks'].min():.4f}")

    arb = comp[comp["gross_edge"] > 0]
    net = comp[comp["net_edge"] > 0]
    print(f"\ngross < $1.00 : {len(arb):,}  {per_hr(len(arb))}")
    print(f"net   > 0 fees: {len(net):,}  {per_hr(len(net))}")

    if net.empty:
        print("\nVERDICT: no dislocation survived fees. Strategy A is dead as a")
        print("taker. Do not commit capital. The remaining question is whether")
        print("quoting these sets as a maker pays -- different strategy.")
        return

    cap = (net["net_edge"] * net["min_depth"]).sum()
    print("\nby venue:")
    for v, g in net.groupby("venue"):
        print(f"  {v:11s} n={len(g):4d}  median net={g['net_edge'].median():+.4f}"
              f"  median depth=${g['min_depth'].median():,.0f}")

    print(f"\ncapturable (net_edge x min_depth, all events): ${cap:,.2f}")
    if rates:
        print(f"per hour                                      : ${cap/span_h:,.2f}")

    # Concentration. One event carrying the whole result is a fitted result,
    # the same failure that killed checkpoint-DCA and the carry hunt.
    per_ev = (net["net_edge"] * net["min_depth"]).groupby(net["event"]).sum()
    top = per_ev.max() / per_ev.sum() if per_ev.sum() else 0
    print(f"top event share of total edge                 : {top:.0%}")
    if top > 0.40:
        print("  >40% from one event. Not a strategy, a single anomaly.")

    print("\nBENCHMARK: before believing this, compare against doing nothing and")
    print("against simply quoting the set. An arb that pays less than the")
    print("spread you would have earned as maker is not worth the leg risk.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=None,
                    help="stop after N hours (default: run until Ctrl-C)")
    ap.add_argument("--interval", type=int, default=30,
                    help="seconds between sweeps (default 30)")
    ap.add_argument("--once", action="store_true", help="one sweep, print, exit")
    ap.add_argument("--report", action="store_true", help="analyse collected data")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.report:
        report()
    elif a.once:
        sweep(Session(), verbose=True)
    else:
        run(a.hours, a.interval, a.verbose)


if __name__ == "__main__":
    main()
