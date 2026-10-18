"""Polymarket crypto Up/Down 5m -- the market both viral threads actually name.

This is the market from the two X threads: @bored2boar's "+$1,847/day from $100
buying YES+NO < $1" and @retrovalix's "trinity42 turned $12k into $107.6k with
five steps". Slugs are `{btc,eth,sol,xrp}-updown-5m-<unix_ts>`, one binary per
5-minute window, ~12 windows quoted ahead, ~$100k volume per window.

Both claims are checkable against public data, so this module checks them rather
than arguing with them.

CLAIM 1 -- YES + NO < $1
    Measured live on three consecutive windows:

        leg0 (Up)   bid 0.50  ask 0.51   size 402
        leg1 (Down) bid 0.49  ask 0.50   size 985
        sum of asks = 1.0100

    Identical 1.0100 on every window. Not competition -- the tick grid. The book
    is a mirror (Down_ask == 1 - Up_bid) on a 1c tick, so the sum is pinned at
    1 + one tick and can only go below 1.00 if the book crosses.

    Worse, these markets charge `rate: 0.07, takerOnly: True` -- 7%, not the 5%
    general schedule. At p=0.50 that is 1.75c a side, 3.5c round trip. A cross
    must exceed 3.5c before it pays: three and a half ticks of free money on a
    1c-tick book. `rebateRate: 0.2` pays makers back. The economics point at
    quoting, not taking.

    This module logs `sum_asks` every sweep and counts crossings. If the count
    is zero over a week, claim 1 is answered with data instead of opinion.

CLAIM 2 -- the five steps
    Steps 1-3 collapse to one formula. With drift ~ 0 over five minutes:

        P(close > open) ~ Phi( (S_t - S_0) / (sigma * sqrt(T - t)) )

    Distance from the window open, scaled by vol and time remaining. "Speed" and
    "momentum" add nothing: 1-minute crypto return autocorrelation is ~0 and
    often slightly negative. Direction is not where edge lives -- sigma and the
    favourite-longshot bias are.

    Step 4, "buy the opposite outcome as a dynamic hedge", is the anti-arb. Up at
    0.51 plus Down at 0.50 is 1.01 paid for a $1 payoff: the same 1.0100 measured
    above, locked as a loss. The exit is to sell the position back.

WHAT THE TAPE SAID
    `data-api.polymarket.com/trades` is public and carries wallet, price, size,
    side, outcome, and username. trinity42 is a real, active account -- 20 fills
    across two hours of BTC 5m windows. Their behaviour does not match the thread:

      - No YES+NO arb. Not one paired fill.
      - Repeated adds into a losing side within one window: Up at 0.500 size 140,
        then Up at 0.307 size 498. That is averaging down, not hedging.
      - Marked to settlement, those two hours were roughly -$179 gross on ~$1,235
        notional, before fees.

    Twenty fills over two hours is a sample, not a verdict -- they may run size
    elsewhere, and a losing two hours proves nothing on its own. But it is
    evidence against the specific mechanism the thread describes, and it is more
    than the thread offers for itself.

    So this module also tracks named traders over time. Same bar as always:
    era split, top-window concentration, benchmark subtraction.

BENCHMARK
    Any model must beat the dumb version. Here that is "sell every tail under 5c"
    -- longshot bias is the best-documented edge in prediction markets, and it
    lives exactly where the 7% fee vanishes. A model that cannot beat a one-line
    heuristic is not a model.

USAGE
    python research/pm_updown_5m.py --once           # one sweep, printed
    python research/pm_updown_5m.py --hours 168      # a week
    python research/pm_updown_5m.py --tape --hours 2 # backfill settled windows
    python research/pm_updown_5m.py --trader trinity42 --hours 6
    python research/pm_updown_5m.py --report
"""
from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

OUT = Path(__file__).parent.parent / "cache" / "pm_updown"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
BINANCE_SPOT = "https://data-api.binance.vision/api/v3/ticker/price"

ASSETS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
WINDOW = 300              # 5 minutes
LOOKAHEAD = 12            # windows quoted ahead of spot
TAKER_RATE = 0.07         # from the live feeSchedule on these markets
MAKER_REBATE = 0.20


def taker_fee(price: float, size: float, rate: float = TAKER_RATE) -> float:
    """Polymarket taker fee. Charged on the lesser side, so it peaks at p=0.50
    (1.75c per contract at rate 0.07) and vanishes at the tails. This asymmetry
    is the whole reason tail strategies survive and 50/50 directional does not."""
    return rate * min(price, 1.0 - price) * size


def implied_prob(dist: float, sigma: float, t_left: float) -> float:
    """Phi(dist / (sigma * sqrt(t_left))) -- the fair price of these markets.

    dist    : (spot - window_open) / window_open, a return
    sigma   : per-second return vol
    t_left  : seconds to window close

    Steps 1-3 of the thread, in one line. Everything else is decoration."""
    if t_left <= 0 or sigma <= 0:
        return 1.0 if dist > 0 else 0.0
    z = dist / (sigma * math.sqrt(t_left))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class BookObs:
    """One Up/Down window priced at one instant, at hittable prices."""
    ts: str
    asset: str
    slug: str
    window_end: int
    secs_left: int
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    up_ask_size: float
    down_ask_size: float
    sum_asks: float        # the claim-1 number; structurally floored at 1.01
    crossed: bool          # sum_asks < 1.00 -- gross arb before fees
    net_after_fee: float   # what a taker would actually keep
    spot: float
    tick: float


@dataclass
class FillObs:
    """One public fill, marked to settlement. Ground truth for backtesting B."""
    ts: int
    slug: str
    asset: str
    trader: str
    wallet: str
    side: str
    outcome: str
    outcome_idx: int
    price: float
    size: float
    settled: float         # 1.0 if that outcome won, else 0.0
    pnl: float             # mark-to-settlement, pre-fee
    fee: float
    secs_left: int         # to window close; stored so nobody re-derives it wrong


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Session:
    def __init__(self, timeout: int = 25):
        self.s = requests.Session()
        # urllib's default UA gets 403 from the CLOB; a browser UA does not.
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


def spot_prices(sess: Session) -> dict[str, float]:
    out = {}
    for a, sym in ASSETS.items():
        try:
            out[a] = float(sess.get(BINANCE_SPOT, params={"symbol": sym})["price"])
        except Exception:
            out[a] = float("nan")
    return out


def _event(sess: Session, slug: str) -> dict | None:
    try:
        d = sess.get(f"{GAMMA}/events", params={"slug": slug})
        return d[0] if d else None
    except Exception:
        return None


def scan_books(sess: Session, assets: list[str]) -> list[BookObs]:
    """Price every live window on the real CLOB book.

    gamma's bestBid/bestAsk lag, so the batch POST /books is used instead -- both
    legs in one call, which is the only way sum_asks means anything.
    """
    out: list[BookObs] = []
    spot = spot_prices(sess)
    now = int(time.time())
    base = (now // WINDOW) * WINDOW

    for asset in assets:
        for i in range(LOOKAHEAD):
            # The integer in the slug is the window START, not the end --
            # btc-updown-5m-1785165600 is titled 11:20AM-11:25AM ET and its
            # endDate is 1785165900. Getting this backwards puts 98% of fills
            # outside their own window and silently poisons every time-in-
            # window measurement built on the tape.
            wstart = base + i * WINDOW
            wend = wstart + WINDOW
            ev = _event(sess, f"{asset}-updown-5m-{wstart}")
            if not ev or not ev.get("markets"):
                continue
            m = ev["markets"][0]
            try:
                toks = json.loads(m["clobTokenIds"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if len(toks) != 2:
                continue

            try:
                books = sess.post(f"{CLOB}/books",
                                  json=[{"token_id": t} for t in toks])
            except Exception as e:
                print(f"  book {asset} {wstart}: {e}", file=sys.stderr)
                continue

            legs = []
            for bk in books:
                # Both sides arrive sorted worst-to-best; best is the LAST entry.
                a, b = bk.get("asks") or [], bk.get("bids") or []
                legs.append((
                    float(b[-1]["price"]) if b else 0.0,
                    float(a[-1]["price"]) if a else 1.0,
                    float(a[-1]["size"]) if a else 0.0,
                ))
            if len(legs) != 2:
                continue

            (ub, ua, usz), (db, da, dsz) = legs
            s_asks = ua + da
            gross = 1.0 - s_asks
            fee = taker_fee(ua, 1.0) + taker_fee(da, 1.0)

            out.append(BookObs(
                ts=_now(), asset=asset, slug=f"{asset}-updown-5m-{wstart}",
                window_end=wend, secs_left=wend - now,
                up_bid=ub, up_ask=ua, down_bid=db, down_ask=da,
                up_ask_size=usz, down_ask_size=dsz,
                sum_asks=round(s_asks, 4), crossed=s_asks < 1.0,
                net_after_fee=round(gross - fee, 4),
                spot=spot.get(asset, float("nan")),
                tick=float(m.get("orderPriceMinTickSize") or 0.01),
            ))
    return out


def scan_tape(sess: Session, assets: list[str], hours: float,
              trader: str | None = None) -> list[FillObs]:
    """Backfill settled windows from the public fills tape.

    This is the honest backtest input for strategy B: a real fill price someone
    actually paid, plus the settled outcome. No mid prices, no assumptions about
    what would have filled.

    /prices-history exists but returns MID (0.505 on a 0.50/0.51 book) and is
    useless for this -- the spread is the entire game in these markets.
    """
    out: list[FillObs] = []
    now = int(time.time())
    base = (now // WINDOW) * WINDOW
    n_windows = int(hours * 3600 / WINDOW)

    for asset in assets:
        for k in range(1, n_windows + 1):
            # Slug integer is the window start (see scan_books).
            wstart = base - k * WINDOW
            wend = wstart + WINDOW
            ev = _event(sess, f"{asset}-updown-5m-{wstart}")
            if not ev or not ev.get("markets"):
                continue
            m = ev["markets"][0]
            cid = m.get("conditionId")
            try:
                settled = [float(x) for x in json.loads(m["outcomePrices"])]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            # Unresolved window -> no ground truth -> not usable.
            if sorted(settled) != [0.0, 1.0]:
                continue

            for page in range(0, 4000, 500):
                try:
                    trades = sess.get(f"{DATA}/trades",
                                      params={"market": cid, "limit": 500,
                                              "offset": page})
                except Exception as e:
                    print(f"  tape {asset} {wend}: {e}", file=sys.stderr)
                    break
                if not trades:
                    break

                for t in trades:
                    name = t.get("name") or t.get("pseudonym") or ""
                    if trader and name != trader:
                        continue
                    idx = int(t.get("outcomeIndex", 0))
                    px, sz = float(t["price"]), float(t["size"])
                    won = settled[idx]
                    # Mark to settlement. A BUY earns (won - price) per unit;
                    # a SELL is the mirror. Consistent for both sides of a
                    # round trip, so round trips net correctly.
                    pnl = (won - px) * sz if t["side"] == "BUY" else (px - won) * sz
                    out.append(FillObs(
                        ts=int(t["timestamp"]), slug=str(t.get("slug", "")),
                        asset=asset, trader=name,
                        wallet=str(t.get("proxyWallet", "")),
                        side=str(t["side"]), outcome=str(t.get("outcome", "")),
                        outcome_idx=idx, price=px, size=sz, settled=won,
                        pnl=round(pnl, 4), fee=round(taker_fee(px, sz), 4),
                        secs_left=wend - int(t["timestamp"]),
                    ))
                if len(trades) < 500:
                    break
    return out


def _append(path: Path, rows: list) -> None:
    """CSV append. Crash-safe by construction: a killed process loses the last
    line, not the file."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(r) for r in rows]).to_csv(
        path, mode="a", header=not path.exists(), index=False)


_stop = False


def _sigint(signum, frame):
    global _stop
    _stop = True
    print("\nstopping after current sweep...", file=sys.stderr)


def sweep(sess: Session, assets: list[str], verbose: bool) -> list[BookObs]:
    obs = scan_books(sess, assets)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _append(OUT / f"books-{day}.csv", obs)

    if verbose and obs:
        crossed = [o for o in obs if o.crossed]
        sums = Counter(o.sum_asks for o in obs)
        print(f"{_now()}  windows={len(obs)}  crossed={len(crossed)}  "
              f"sum_asks distribution={dict(sums.most_common(4))}")
        for o in sorted(obs, key=lambda x: x.sum_asks)[:4]:
            flag = "CROSS" if o.crossed else "     "
            print(f"  {flag} {o.slug[-14:]:14s} {o.secs_left:5d}s "
                  f"up {o.up_bid:.2f}/{o.up_ask:.2f}  "
                  f"dn {o.down_bid:.2f}/{o.down_ask:.2f}  "
                  f"sum={o.sum_asks:.4f} net={o.net_after_fee:+.4f}")
    return obs


def run(assets: list[str], hours: float | None, interval: int, verbose: bool):
    signal.signal(signal.SIGINT, _sigint)
    sess = Session()
    deadline = time.time() + hours * 3600 if hours else None
    n = 0
    while not _stop and (deadline is None or time.time() < deadline):
        t0 = time.time()
        try:
            sweep(sess, assets, verbose or n % 20 == 0)
        except Exception as e:
            print(f"sweep failed: {e}", file=sys.stderr)
        n += 1
        time.sleep(max(0.0, interval - (time.time() - t0)))
    print(f"done. {n} sweeps -> {OUT}", file=sys.stderr)


def report() -> None:
    """The verdict on both claims."""
    bf = sorted(OUT.glob("books-*.csv"))
    tf = sorted(OUT.glob("tape-*.csv"))

    if bf:
        df = pd.concat([pd.read_csv(f) for f in bf], ignore_index=True)
        df["ts"] = pd.to_datetime(df["ts"], format="mixed", utc=True)
        span_h = (df["ts"].max() - df["ts"].min()).total_seconds() / 3600
        rates = span_h >= 1.0

        print("=" * 66)
        print("CLAIM 1 -- 'buy YES + NO < $1'")
        print("=" * 66)
        print(f"observations : {len(df):,} window-quotes over {span_h:.1f}h")
        print(f"windows      : {df['slug'].nunique():,}")
        print("\nsum-of-asks distribution:")
        for v, c in df["sum_asks"].value_counts().head(6).items():
            print(f"  {v:.4f}  {c:7,}  ({c/len(df):5.1%})")
        print(f"  min observed: {df['sum_asks'].min():.4f}")

        cr = df[df["crossed"]]
        net = df[df["net_after_fee"] > 0]
        print(f"\ncrossed (sum < 1.00) : {len(cr):,}"
              + (f"  ({len(cr)/span_h:.2f}/hr)" if rates else "  (span < 1h)"))
        print(f"positive after fee   : {len(net):,}"
              + (f"  ({len(net)/span_h:.2f}/hr)" if rates else "  (span < 1h)"))

        if net.empty:
            print("\nVERDICT: never profitable as a taker. The 1c tick floors")
            print("sum_asks at 1.01 and the 7% taker fee costs 3.5c round trip")
            print("at p=0.50. Claim 1 does not describe a trade that exists on")
            print("this market. Do not commit capital to it.")
        else:
            cap = (net["net_after_fee"]
                   * net[["up_ask_size", "down_ask_size"]].min(axis=1)).sum()
            print(f"\ncapturable: ${cap:,.2f} over {span_h:.1f}h")
            per_slug = (net["net_after_fee"]
                        * net[["up_ask_size", "down_ask_size"]].min(axis=1)
                        ).groupby(net["slug"]).sum()
            top = per_slug.max() / per_slug.sum() if per_slug.sum() else 0
            print(f"top window share of edge: {top:.0%}")
            if top > 0.40:
                print("  >40% from one window. An anomaly, not a strategy.")
    else:
        print("no book data yet -- run without --report first.")

    if not tf:
        print("\nno tape data. run --tape to backfill settled windows.")
        return

    tp = pd.concat([pd.read_csv(f) for f in tf], ignore_index=True)
    print("\n" + "=" * 66)
    print("CLAIM 2 -- the five steps, measured against the tape")
    print("=" * 66)
    print(f"fills        : {len(tp):,}")
    print(f"windows      : {tp['slug'].nunique():,}")
    print(f"traders      : {tp['trader'].nunique():,}")

    tp["net"] = tp["pnl"] - tp["fee"]
    agg = tp.groupby("trader").agg(
        fills=("pnl", "size"), notional=("size", "sum"),
        gross=("pnl", "sum"), net=("net", "sum")).sort_values("net")

    busy = agg[agg["fills"] >= 20]
    if not busy.empty:
        print("\nworst net (>=20 fills):")
        for t, r in busy.head(5).iterrows():
            print(f"  {str(t)[:22]:22s} n={r.fills:5.0f} "
                  f"gross={r.gross:+10.2f} net={r.net:+10.2f}")
        print("\nbest net (>=20 fills):")
        for t, r in busy.tail(5)[::-1].iterrows():
            print(f"  {str(t)[:22]:22s} n={r.fills:5.0f} "
                  f"gross={r.gross:+10.2f} net={r.net:+10.2f}")

    # BENCHMARK. Longshot bias: sell every tail under 5c. Fees vanish there,
    # which is exactly why it is the version to beat.
    tails = tp[tp["price"] <= 0.05]
    if not tails.empty:
        bench = ((tails["price"] - tails["settled"]) * tails["size"]).sum()
        fee = tails["fee"].sum()
        print(f"\nBENCHMARK -- sell every tail <= 5c")
        print(f"  {len(tails):,} fills, notional ${(tails['price']*tails['size']).sum():,.0f}")
        print(f"  gross {bench:+,.2f}  net {bench - fee:+,.2f}")
        print("  Any model must beat this line or it is overhead on a heuristic.")

    # Concentration -- the test that killed checkpoint-DCA and the carry hunt.
    if tp["slug"].nunique() > 1:
        per_w = tp.groupby("slug")["net"].sum()
        tot = per_w.abs().sum()
        if tot:
            print(f"\ntop-window share of |net|: {per_w.abs().max()/tot:.0%}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="btc,eth,sol,xrp")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--tape", action="store_true",
                    help="backfill settled windows from the public fills tape")
    ap.add_argument("--trader", default=None,
                    help="restrict tape backfill to one username")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    assets = [x.strip() for x in a.assets.split(",") if x.strip()]

    if a.report:
        report()
    elif a.tape:
        fills = scan_tape(Session(), assets, a.hours or 2.0, a.trader)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tag = f"-{a.trader}" if a.trader else ""
        _append(OUT / f"tape{tag}-{day}.csv", fills)
        print(f"{len(fills):,} fills -> {OUT}")
        if fills:
            g = sum(f.pnl for f in fills)
            n = sum(f.pnl - f.fee for f in fills)
            print(f"gross {g:+,.2f}  net after fee {n:+,.2f}  "
                  f"notional ${sum(f.price*f.size for f in fills):,.2f}")
    elif a.once:
        sweep(Session(), assets, verbose=True)
    else:
        run(assets, a.hours, a.interval, a.verbose)


if __name__ == "__main__":
    main()
