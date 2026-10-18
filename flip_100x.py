"""flip_100x.py — 100x BTCUSDC short, whole account, protection armed on fill.

THE TRADE
---------
Kool asked for a high-leverage futures coinflip and, after seeing the ruin math,
chose 100x on the whole account. Direction is not a literal coin: six independent
reads agreed (1h -2.35% 5/6 red, 4h -3.04% 5/6 red, crowd L/S rising 1.687->1.804,
OI rising 104.7k->106.2k BTC *while price fell*, funding still positive, perp at a
-0.038% discount to index). Longs are adding into a decline and have not been
flushed. So: short. It also collects funding rather than paying it.

WHY BTCUSDC AND NOT A USDT PAIR
-------------------------------
Screened all 572 perps. USDC perps are the only ones with a 0bp maker fee
(verified per-symbol via futures_commission_rate: maker 0, taker 4bp, vs USDT's
2bp/5bp). Of the 38 USDC perps BTCUSDC wins every other column too -- tightest
spread on the board (0.02bp), deepest book ($2.48B/24h), lowest realized vol
(37.8% ann), and the lowest maintenance margin ratio on the exchange (0.40% vs
0.50-1.00% for every alt). Those all share a cause, so it is not a coincidence.

The 0bp edge exists ONLY on the maker side. A market entry pays 4bp and throws
away the entire reason this symbol was chosen -- hence GTX (post-only). The cost
of that choice is that the order may never fill, which is the correct thing to
risk here.

THE GEOMETRY, STATED PLAINLY
----------------------------
At 100x, liquidation is 1/L - MMR = 1/100 - 0.004 = 0.600% against. ATR(1h) on
BTCUSDC is 0.642%. The liquidation price is INSIDE one ordinary hour of movement
-- buffer 0.93 ATR. This is not a margin of safety; it is a cliff edge, and
BTCUSDC is the *best* symbol available on that metric (SOL 0.49, ZEC already dead
at 100x because its 1.0% MMR exceeds the 1% margin).

Because liquidation sits at +0.600%, any protective stop is only meaningful if it
is TIGHTER than that. A stop at +0.40% leaves real margin behind; a stop at +0.7%
would never fire, because the exchange closes the position first and takes
everything. That is why STOP_PCT below is 0.0040 and not a rounder, looser number.

R:R is deliberately asymmetric (stop 0.40%, tp 0.60%). Symmetric brackets at this
size need a 55% win rate to overcome the 4bp taker exit; 1:1.5 needs 44%. Neither
is 50%, which is the honest problem with the whole idea and was reported before
the trade was authorised.

THE NAKED WINDOW
----------------
A post-only entry cannot carry a stop, so between fill and stop placement the
account holds 100x on 100% of equity with no protection. At this leverage that
window is the single largest risk in the script -- larger than the trade thesis.
It gets retries, and if the stop still cannot be placed the position is closed at
market rather than left running. A 4bp taker exit is cheap; an unprotected 100x
whole-account position is the exact "runaway" event this repo exists to measure.

Protective orders are ALGO orders: they do NOT appear in futures_get_open_orders()
on this account, so a create call returning 200 is not proof. They are read back
from futures_get_open_algo_orders() (which returns a LIST on this client version).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)
from binance.client import Client  # noqa: E402

SYMBOL = "BTCUSDC"
SIDE = "SELL"           # short
EXIT_SIDE = "BUY"
LEVERAGE = 100
EQUITY_FRAC = 0.99      # 1% held back so the order is not rejected on a tick
STOP_PCT = 0.0040       # +0.40% -- must stay inside liquidation at +0.600%
TP_PCT = 0.0060         # -0.60%
TICK = 0.1
STEP = 0.001
GIVE_UP_SEC = 1800      # unfilled post-only after 30m -> stand down, do not chase
POLL_SEC = 3
STATE_FILE = "flip_100x_state.json"
LOG_FILE = "flip_100x.log"

c = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))


def log(msg: str) -> None:
    line = f"{datetime.now():%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def round_tick(p: float) -> float:
    return round(round(p / TICK) * TICK, 1)


def round_step(q: float) -> float:
    return round(int(q / STEP) * STEP, 3)


def algo_orders() -> list:
    """Protective orders live here, not in futures_get_open_orders()."""
    try:
        a = c.futures_get_open_algo_orders()
        a = a if isinstance(a, list) else a.get("orders", [])
        return [x for x in a if x.get("symbol") == SYMBOL]
    except Exception as e:  # noqa: BLE001
        log(f"algo read failed: {e}")
        return []


def protective_kinds() -> set:
    """Every venue-side resting order for this symbol, from both endpoints."""
    kinds = {o.get("orderType") or o.get("type") for o in algo_orders()}
    try:
        for o in c.futures_get_open_orders(symbol=SYMBOL):
            kinds.add(o.get("type"))
    except Exception as e:  # noqa: BLE001
        log(f"open-order read failed: {e}")
    return {k for k in kinds if k}


def position() -> tuple[float, float]:
    """(qty, entry). Flat symbols may be omitted from the response entirely."""
    try:
        rows = c.futures_position_information(symbol=SYMBOL)
    except Exception as e:  # noqa: BLE001
        log(f"position read failed: {e}")
        return 0.0, 0.0
    if not rows:
        return 0.0, 0.0
    return float(rows[0]["positionAmt"]), float(rows[0]["entryPrice"])


def panic_close(qty: float, why: str) -> None:
    """Unprotected at 100x is worse than any exit fee. Get flat."""
    log(f"*** PANIC CLOSE ({why}) ***")
    for attempt in range(1, 6):
        try:
            c.futures_create_order(
                symbol=SYMBOL, side=EXIT_SIDE, type="MARKET",
                quantity=abs(qty), reduceOnly=True,
            )
            log("  closed at market")
            return
        except Exception as e:  # noqa: BLE001
            log(f"  panic close attempt {attempt} failed: {e}")
            time.sleep(1)
    log("  *** COULD NOT CLOSE -- MANUAL INTERVENTION NEEDED ***")


def arm(entry: float, qty: float) -> None:
    aq = abs(qty)
    stop_px = round_tick(entry * (1 + STOP_PCT))
    tp_px = round_tick(entry * (1 - TP_PCT))
    liq_px = round_tick(entry * (1 + 1.0 / LEVERAGE - 0.004))
    eq = float(c.futures_account()["totalWalletBalance"])
    notional = aq * entry

    log(f"FILLED short {aq} @ {entry:.1f}  notional ${notional:,.0f}")
    log(f"  stop {stop_px:.1f} (+{STOP_PCT*100:.2f}%)  risk ${notional*STOP_PCT:.2f}")
    log(f"  tp   {tp_px:.1f} (-{TP_PCT*100:.2f}%)  reward ${notional*TP_PCT:.2f}")
    log(f"  LIQ  {liq_px:.1f} (+0.600%)  equity ${eq:.2f}")

    placed = {}
    for kind, px in (("STOP_MARKET", stop_px), ("TAKE_PROFIT_MARKET", tp_px)):
        for attempt in range(1, 7):
            try:
                c.futures_create_order(
                    symbol=SYMBOL, side=EXIT_SIDE, type=kind,
                    stopPrice=px, closePosition=True,
                    workingType="MARK_PRICE", priceProtect=True,
                )
                log(f"  sent {kind} @ {px:.1f}")
                placed[kind] = px
                break
            except Exception as e:  # noqa: BLE001
                log(f"  {kind} attempt {attempt} failed: {e}")
                time.sleep(1.5)

    # A 200 is not proof -- read the orders back before believing the stop exists.
    time.sleep(2)
    kinds = protective_kinds()
    for o in algo_orders():
        log(f"    live: {o.get('orderType')} {o.get('side')} "
            f"trigger={o.get('stopPrice') or o.get('triggerPrice')}")
    ok = "STOP_MARKET" in kinds
    log("  PROTECTED" if ok else "  *** NO STOP CONFIRMED ***")

    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"symbol": SYMBOL, "side": SIDE, "qty": aq, "entry": entry,
                   "stop": stop_px, "tp": tp_px, "liq": liq_px,
                   "notional": notional, "leverage": LEVERAGE,
                   "protected": ok, "ts": datetime.now().isoformat()}, fh, indent=2)

    if not ok:
        panic_close(qty, "stop could not be confirmed")


def place_entry() -> tuple[bool, float]:
    """Post-only limit at the ask. GTX rejects rather than crossing, keeping 0bp."""
    ob = c.futures_order_book(symbol=SYMBOL, limit=5)
    ask = float(ob["asks"][0][0])
    avail = float(c.futures_account()["availableBalance"])
    qty = round_step(avail * EQUITY_FRAC * LEVERAGE / ask)
    notional = qty * ask
    log(f"equity ${avail:.2f} -> qty {qty} @ {ask:.1f} = ${notional:,.0f} notional "
        f"(margin ${notional/LEVERAGE:.2f})")
    try:
        o = c.futures_create_order(
            symbol=SYMBOL, side=SIDE, type="LIMIT", timeInForce="GTX",
            quantity=qty, price=f"{ask:.1f}",
        )
        log(f"post-only SHORT placed id={o.get('orderId')} @ {ask:.1f} qty={qty}")
        return True, ask
    except Exception as e:  # noqa: BLE001
        log(f"entry placement FAILED: {e}")
        return False, 0.0


def main() -> None:
    log("=" * 72)
    log(f"FLIP {SYMBOL} {SIDE} {LEVERAGE}x  stop +{STOP_PCT*100:.2f}%  "
        f"tp -{TP_PCT*100:.2f}%  liq +0.600%")

    qty, _ = position()
    if qty != 0:
        log(f"already in position ({qty}) -- refusing to stack. exiting.")
        return

    staged, _ = place_entry()
    if not staged:
        return

    armed = False
    t0 = time.time()
    last_log = 0.0

    while True:
        qty, entry = position()

        if qty != 0 and not armed:
            arm(entry, qty)
            armed = True

        elif qty == 0 and armed:
            log("position CLOSED")
            for o in algo_orders():
                try:
                    c.futures_cancel_algo_order(algoId=o["algoId"])
                    log(f"  cancelled leftover {o.get('orderType')}")
                except Exception as e:  # noqa: BLE001
                    log(f"  cancel failed: {e}")
            log(f"  balance {c.futures_account()['availableBalance']}")
            return

        elif qty == 0 and not armed:
            open_o = c.futures_get_open_orders(symbol=SYMBOL)
            if not open_o:
                log("post-only rejected or cancelled without a fill -- standing down")
                return
            if time.time() - t0 > GIVE_UP_SEC:
                log(f"unfilled after {GIVE_UP_SEC//60}m -- cancelling, not chasing")
                for o in open_o:
                    try:
                        c.futures_cancel_order(symbol=SYMBOL, orderId=o["orderId"])
                    except Exception as e:  # noqa: BLE001
                        log(f"  cancel failed: {e}")
                return
            if time.time() - last_log > 60:
                mk = float(c.futures_mark_price(symbol=SYMBOL)["markPrice"])
                log(f"waiting for fill  mark={mk:.1f}  {int(time.time()-t0)}s")
                last_log = time.time()

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
