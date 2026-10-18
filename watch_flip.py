"""watch_flip.py — staged dip entry on SOLUSDT, with protection armed on fill.

THE TRADE
---------
Long SOL into a flush. Kool's read: a dip just hit, it may rebound, but support at
~73.4 could give first and 2-3 more red 1H candles are plausible. So: do not buy
here, buy the bounce off a lower low -- and if the bounce never comes, never trade.

WHY THIS IS A WATCHER AND NOT JUST AN ORDER
-------------------------------------------
Two exchange behaviours make the obvious version wrong.

1. A BUY TRAILING_STOP_MARKET with activationPrice BELOW mark is silently clamped
   to mark. Not rejected -- clamped. I sent activationPrice=74.60 and the exchange
   stored 75.35 (spot), returning HTTP 200. The order then trails from spot, which
   is the exact behaviour the activation price was meant to prevent, and the only
   way to notice is to read the order back. So the "wait for the zone" stage has to
   happen client-side: poll, and only place the trailing order once price is in.

2. A BUY trailing entry cannot carry a stop. Between fill and stop placement the
   account holds naked 10x on ~59% of equity. That window gets a watcher, not hope.

Protective orders here are ALGO orders: invisible to futures_get_open_orders(), and
futures_get_open_algo_orders() returns a LIST on this client version, not a dict.

SIZING
------
qty is fixed at Kool's 60%-of-equity call. The stop is STRUCTURAL (73.05, under the
14d low of 73.32 and beneath the volume air pocket below 74.00) rather than a
percentage of fill -- a 2%-of-fill stop lands at 74.5, inside a band price has
wicked through 41 times. Risk therefore depends on fill price, so it is checked
against a hard budget at arming time and the stop is tightened if it breaches.

BREADTH -- WHY THERE IS A BTC GATE
-----------------------------------
The dip was not a SOL event. 20 of 20 majors closed the 21:00 hour red, median
-2.00%, on 2.6-11.6x average volume. That is an index-wide liquidation cascade, so
"SOL bounces off 73.38" is substantially the claim "BTC stopped falling".

SOL's beta to BTC is 1.07 (corr 0.84, 1h returns, 14d) -- it is NOT a leveraged BTC
proxy, and ~16% of its variance is its own, so its volume shelves remain a valid
frame for the stop. But a 0.8% bounce in SOL while BTC is still printing fresh lows
is beta noise inside a continuing move, not a reversal. The trailing entry cannot
tell those apart; it only sees SOL. So the confirmation is taken here: the entry may
only stage while BTC is off its own 2h low.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)
from binance.client import Client  # noqa: E402

SYMBOL = "SOLUSDT"
LEAD = "BTCUSDT"         # the thing actually driving this tape -- see BREADTH below
QTY = 5.90               # Kool's 60%-of-equity sizing at ~$75
ARM_BELOW = 74.60        # only start hunting once price enters the shelf
LEAD_LOOKBACK = 8        # 15m bars BTC must not be breaking down over (2h)
CALLBACK = 0.8           # ~1x ATR14(1h); rebound needed to trigger the buy
STOP_PRICE = 73.05       # under the 14d low (73.32); below the <74.00 volume void
TP_PCT = 0.02            # Kool's 2% target
MAX_LOSS_FRAC = 0.14     # hard risk budget as a fraction of equity
GIVE_UP_ABOVE = 77.50    # thesis invalidated -- it rallied without us; stop watching
TICK = 0.01
POLL_SEC = 5
STATE_FILE = "flip_state.json"

c = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))


def log(msg: str) -> None:
    line = f"{datetime.now():%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open("flip_watch.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def round_tick(p: float) -> float:
    return round(round(p / TICK) * TICK, 2)


def mark() -> float:
    return float(c.futures_mark_price(symbol=SYMBOL)["markPrice"])


def algo_orders() -> list:
    """Protective and conditional orders live here, not in futures_get_open_orders()."""
    try:
        a = c.futures_get_open_algo_orders()
        a = a if isinstance(a, list) else a.get("orders", [])
        return [x for x in a if x.get("symbol") == SYMBOL]
    except Exception as e:  # noqa: BLE001
        log(f"algo read failed: {e}")
        return []


def lead_ok() -> tuple[bool, str]:
    """True while BTC is NOT making fresh lows.

    A cascade ends when the leader stops falling, not when a laggard ticks up. If BTC
    is still printing new 2h lows then SOL's bounce is beta inside a continuing move,
    and the trailing entry -- which only watches SOL -- would buy straight into it.
    Fails OPEN: a data outage should not silently block a staged entry forever.
    """
    try:
        k = c.futures_klines(symbol=LEAD, interval="15m", limit=LEAD_LOOKBACK + 1)
    except Exception as e:  # noqa: BLE001
        log(f"lead read failed ({e}) -- gate open")
        return True, "no data"
    lows = [float(b[3]) for b in k]
    last, prior_low = lows[-1], min(lows[:-1])
    return last > prior_low, f"BTC 15m low {last:.0f} vs 2h low {prior_low:.0f}"


def position() -> tuple[float, float]:
    """(qty, entry). Flat symbols are omitted from the response entirely."""
    try:
        rows = c.futures_position_information(symbol=SYMBOL)
    except Exception as e:  # noqa: BLE001
        log(f"position read failed: {e}")
        return 0.0, 0.0
    if not rows:
        return 0.0, 0.0
    return float(rows[0]["positionAmt"]), float(rows[0]["entryPrice"])


def place_trailing_entry() -> bool:
    """Place the trailing buy. Only valid once mark is already in the zone."""
    try:
        o = c.futures_create_order(
            symbol=SYMBOL, side="BUY", type="TRAILING_STOP_MARKET",
            quantity=QTY, callbackRate=CALLBACK, workingType="MARK_PRICE",
        )
        log(f"trailing entry placed algoId={o.get('algoId')} "
            f"activate={o.get('activatePrice')} cb={CALLBACK}%")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"entry placement FAILED: {e}")
        return False


def arm(entry: float, qty: float) -> None:
    stop_px = round_tick(STOP_PRICE)
    tp_px = round_tick(entry * (1 + TP_PCT))
    eq = float(c.futures_account()["totalWalletBalance"])

    risk = (entry - stop_px) * qty
    log(f"FILLED entry={entry:.4f} qty={qty} -> stop {stop_px:.2f} "
        f"({(stop_px/entry-1)*100:+.2f}%)  tp {tp_px:.2f} (+{TP_PCT*100:.1f}%)")
    log(f"  risk ${risk:.2f} = {risk/eq*100:.1f}% of ${eq:.2f}")

    # A trailing entry can gap through its trigger, so the fill price -- and the risk
    # the structural stop implies -- is not knowable in advance. Enforce the budget.
    if risk > eq * MAX_LOSS_FRAC:
        stop_px = round_tick(entry - (eq * MAX_LOSS_FRAC) / qty)
        log(f"  *** risk over {MAX_LOSS_FRAC*100:.0f}% budget -> stop tightened to {stop_px:.2f} ***")

    for kind, px in (("STOP_MARKET", stop_px), ("TAKE_PROFIT_MARKET", tp_px)):
        for attempt in range(1, 6):
            try:
                c.futures_create_order(
                    symbol=SYMBOL, side="SELL", type=kind,
                    stopPrice=px, closePosition=True,
                    workingType="MARK_PRICE", priceProtect=True,
                )
                log(f"  sent {kind} @ {px:.2f}")
                break
            except Exception as e:  # noqa: BLE001
                log(f"  {kind} attempt {attempt} failed: {e}")
                time.sleep(2)

    # A create call that does not raise is NOT proof. The exchange silently clamped
    # an activationPrice earlier in this same session. Read the orders back.
    time.sleep(2)
    live = algo_orders()
    kinds = {o.get("orderType") for o in live}
    for o in live:
        log(f"    live: {o.get('orderType')} {o.get('side')} "
            f"trigger={o.get('stopPrice') or o.get('triggerPrice')}")
    ok = "STOP_MARKET" in kinds
    log("  PROTECTED" if ok else "  *** NO STOP CONFIRMED -- POSITION IS NAKED ***")

    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"entry": entry, "qty": qty, "stop": stop_px, "tp": tp_px,
                   "risk": risk, "protected": ok,
                   "ts": datetime.now().isoformat()}, fh, indent=2)


def main() -> None:
    log(f"WATCH {SYMBOL} qty={QTY} | arm<{ARM_BELOW} cb={CALLBACK}% "
        f"stop={STOP_PRICE} tp=+{TP_PCT*100:.0f}% | giveup>{GIVE_UP_ABOVE}")
    staged = bool(algo_orders())
    armed = False
    last_log = 0.0

    while True:
        qty, entry = position()

        if qty > 0 and not armed:
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
            m = mark()

            if not staged:
                if m <= ARM_BELOW:
                    ok, why = lead_ok()
                    if not ok:
                        if time.time() - last_log > 900:
                            log(f"mark {m:.2f} in zone but LEAD STILL FALLING ({why}) -- holding")
                            last_log = time.time()
                    else:
                        log(f"mark {m:.2f} entered zone (<{ARM_BELOW}), {why} -- staging entry")
                        staged = place_trailing_entry()
                elif m >= GIVE_UP_ABOVE:
                    log(f"mark {m:.2f} above {GIVE_UP_ABOVE} -- rallied without us, standing down")
                    return
                elif time.time() - last_log > 900:
                    log(f"waiting  mark={m:.2f}  need <={ARM_BELOW} "
                        f"({(ARM_BELOW/m-1)*100:+.2f}% away)")
                    last_log = time.time()
            else:
                if not algo_orders():
                    log("staged entry vanished without a fill -- re-checking next loop")
                    staged = False
                elif time.time() - last_log > 900:
                    log(f"hunting  mark={m:.2f}  trailing for +{CALLBACK}% bounce")
                    last_log = time.time()

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
