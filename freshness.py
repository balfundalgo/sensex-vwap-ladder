#!/usr/bin/env python3
"""
freshness.py — how long after a 1-minute candle closes does Dhan's REST API
actually return it?

This is the measurement that decides whether the strategy can be driven from
REST instead of the websocket. The rule fires on a candle close and the order
lives for exactly one candle, so a signal that arrives late is not a slightly
worse signal — it is a missed trade.

    python freshness.py                 watch for 10 minutes
    python freshness.py --minutes 30

Run it during market hours. It polls every few seconds, notes the newest bar
the API is willing to return, and reports the delay between that bar's close
and the moment it first appeared.

Reading the result:

    < 10s   REST-driven signals are comfortable. A 2-minute candle closing at
            09:17:00 is available well before the 09:17-09:19 candle matters.
    10-30s  Workable but tight. The entry order would be placed late into the
            one-candle validity window.
    > 30s   REST cannot drive entries. Keep candles on the websocket, or accept
            that some triggers are missed.
"""

import argparse
import time
from datetime import datetime

import engine as E
from restfeed import RestCandleFeed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=10, help="how long to watch")
    ap.add_argument("--every", type=int, default=3, help="poll interval, seconds")
    ap.add_argument("--strike", type=int, default=0)
    ap.add_argument("--leg", choices=["CE", "PE"], default="PE")
    ap.add_argument("--sec-id", default="",
                    help="security id to probe directly, skipping the expiry "
                         "and option-chain lookups (use the id doctor printed)")
    args = ap.parse_args()

    if not E.DHAN_CLIENT_ID:
        raise SystemExit("Missing credentials — fill .env first")

    def timed(label, fn, *a, **kw):
        t0 = time.time()
        try:
            out = fn(*a, **kw)
        except Exception as ex:
            print(f"  {label:<28} FAILED after {time.time()-t0:5.1f}s  {ex}")
            raise
        dt = time.time() - t0
        flag = "" if dt < 3 else ("   <- slow" if dt < 15 else "   <- VERY SLOW")
        print(f"  {label:<28} {dt:5.1f}s{flag}")
        return out

    print("\nSetup timings — anything above a second or two is a problem:")
    timed("login", E.init_credentials)

    strike = args.strike
    if args.sec_id:
        sec = args.sec_id
        print(f"  using security id {sec} directly (skipping expiry + chain)")
    else:
        expiry = timed("expiry list", E.get_nearest_expiry)
        if not strike:
            io = timed("index 09:15 open", E.get_sensex_open_0915)
            if not io:
                raise SystemExit("Could not read the 09:15 open")
            strike = int(round(io / 100.0) * 100)
        oc = timed("option chain", E.fetch_option_chain, expiry)
        if not oc:
            raise SystemExit("Option chain unavailable")
        sec = None
        for sk, sd in oc["oc"].items():
            try:
                if abs(float(sk) - strike) > 0.01:
                    continue
            except ValueError:
                continue
            key = "ce" if args.leg == "CE" else "pe"
            if key in sd:
                sec = str(sd[key]["security_id"])
        if not sec:
            raise SystemExit(f"{args.leg} not listed for {strike}")

    t0 = time.time()
    probe = E.fetch_intraday_1m(sec, E.SENSEX["segment"], "OPTIDX", days=1)
    dt = time.time() - t0
    n_all = len(probe) if probe else 0
    now_s = time.time()
    closed = RestCandleFeed.drop_forming(
        [{**c, "ts": E._normalize_epoch(c["ts"])} for c in (probe or [])], now_s)
    print(f"  intraday fetch (the one that matters)  {dt:5.1f}s   "
          f"{n_all} bars, {n_all - len(closed)} still forming")
    if closed:
        newest = max(c["ts"] for c in closed)
        age = now_s - (newest + 60)
        print(f"  newest CLOSED bar is {E.epoch_to_ist(newest, '%H:%M')}, "
              f"{age:.0f}s old")
    if not probe:
        raise SystemExit("\nThe intraday endpoint returned nothing — nothing to "
                         "measure. Check the security id and market hours.")

    print(f"\nWatching SENSEX {strike or '?'} {args.leg} (secId={sec}) for "
          f"{args.minutes} min, polling every {args.every}s")
    print("Waiting for the first new candle to appear...\n")
    print(f"{'bar closed':>12} {'first seen':>12} {'delay':>9}   verdict")
    print("-" * 56)

    seen = set()
    delays = []
    forming = 0
    deadline = time.time() + args.minutes * 60
    warm = True

    while time.time() < deadline:
        try:
            raw = E.fetch_intraday_1m(sec, E.SENSEX["segment"], "OPTIDX", days=1)
        except Exception as ex:
            print(f"  fetch error: {ex}")
            time.sleep(args.every)
            continue

        now_s = time.time()
        raw_n = len(raw) if raw else 0
        # Dhan serves the current minute while it is still forming. Measuring
        # against that produces negative delays, which is how the bug surfaced.
        raw = RestCandleFeed.drop_forming(
            [{**c, "ts": E._normalize_epoch(c["ts"])} for c in (raw or [])],
            now_s)
        forming += (raw_n - len(raw))
        if raw:
            ts = max(c["ts"] for c in raw)
            if warm:
                # Ignore whatever is already there when we start; only measure
                # bars that appear while we are watching.
                seen.add(ts)
                warm = False
            elif ts not in seen:
                seen.add(ts)
                closed_at = ts + 60          # bar labelled ts covers ts..ts+59
                delay = time.time() - closed_at
                delays.append(delay)
                verdict = ("comfortable" if delay < 10 else
                           "tight" if delay < 30 else "TOO SLOW")
                c_str = datetime.fromtimestamp(closed_at, tz=E.IST).strftime("%H:%M:%S")
                n_str = datetime.now(E.IST).strftime("%H:%M:%S")
                print(f"{c_str:>12} {n_str:>12} {delay:>8.1f}s   {verdict}")
        time.sleep(args.every)

    print("-" * 56)
    if not delays:
        print("No new candles appeared. Are you running this during market hours?")
        return
    lo, hi = min(delays), max(delays)
    avg = sum(delays) / len(delays)
    print(f"{len(delays)} candles observed:  min {lo:.1f}s   avg {avg:.1f}s   "
          f"max {hi:.1f}s")
    print("(delay = time from the candle closing to it appearing in the API,")
    print(" counting only bars that had actually finished)")
    print(f"{forming} still-forming bars were seen and discarded — Dhan serves")
    print("the current minute as it builds, so it must never reach the strategy.")
    if lo < 0:
        print("\nNEGATIVE DELAY: a bar appeared before it closed. That is not")
        print("possible, so the timestamp convention is not what we assume.")
        print("Stop and investigate rather than trusting these numbers.")
        return
    print()
    if hi < 10:
        print("REST can drive the strategy. Signals will be timely.")
    elif hi < 30:
        print("REST is workable but tight. The one-candle order window would be\n"
              "entered late; expect some triggers to be missed on fast bars.")
    else:
        print("REST cannot drive entries at this latency. Build candles from the\n"
              "websocket and use REST only for seeding and reconciliation.")


if __name__ == "__main__":
    main()
