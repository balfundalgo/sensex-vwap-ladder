#!/usr/bin/env python3
"""
reconcile.py — print our 2-minute candles, VWAP and ATR so they can be diffed
against the Kite chart, bar by bar.

This is stage 1 of the build plan. Nothing downstream is trustworthy until the
numbers here match the terminal to the paisa.

Usage
    python reconcile.py                      # today, both legs of today's strike
    python reconcile.py --strike 77400       # a specific strike
    python reconcile.py --leg PE --rows 40
    python reconcile.py --field hlc3         # try the other VWAP field
    python reconcile.py --atr-method sma     # try the other ATR reading
    python reconcile.py --csv out.csv        # write instead of print

What to check on the terminal, in this order:

  1. TIMESTAMPS. Does our 09:15 row have the same OHLC as the chart's first
     2-minute candle? If every row is one minute out of step, the candles are
     not anchored to the session open.
  2. VOLUME. Does our per-bar volume match the volume histogram? VWAP cannot
     be right if this is wrong, and this was the original defect.
  3. VWAP. Compare a few rows spread across the session, not just the last one.
  4. ATR. Compare the 09:15 value first — that is the one the overnight gap
     affects — then a mid-session value.

If VWAP is right but ATR is not, re-run with --atr-method sma. If both are out
by a constant factor, the field is wrong: re-run with --field hlc3.
"""

import argparse
import csv as csvmod
import sys
from datetime import datetime

import engine as E
from indicators import compute_series, SessionVWAP, WilderATR


def main():
    ap = argparse.ArgumentParser(description="Reconcile VWAP/ATR against Kite")
    ap.add_argument("--strike", type=int, default=0,
                    help="strike to check (default: today's opening strike)")
    ap.add_argument("--leg", choices=["CE", "PE", "BOTH"], default="BOTH")
    ap.add_argument("--field", default="ohlc4",
                    help="VWAP price field: ohlc4 (default), hlc3, hl2, close")
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--atr-method", choices=["wilder", "sma"], default="wilder")
    ap.add_argument("--seed-days", type=int, default=10,
                    help="days of history pulled to warm the ATR")
    ap.add_argument("--rows", type=int, default=30,
                    help="session rows to print (0 = all)")
    ap.add_argument("--csv", default="", help="write to this file instead")
    args = ap.parse_args()

    if not E.DHAN_CLIENT_ID:
        sys.exit("Missing credentials — fill .env first")
    E.init_credentials()

    expiry = E.get_nearest_expiry()
    if not expiry:
        sys.exit("No SENSEX expiry found")

    strike = args.strike
    if not strike:
        idx_open = E.get_sensex_open_0915()
        if not idx_open:
            sys.exit("Could not read the 09:15 index open")
        strike = int(round(idx_open / 100.0) * 100)
        print(f"SENSEX 09:15 open {idx_open:.2f} -> strike {strike}")

    oc = E.fetch_option_chain(expiry)
    if not oc:
        sys.exit("Option chain unavailable")

    legs = {}
    for sk, sd in oc["oc"].items():
        try:
            if abs(float(sk) - strike) > 0.01:
                continue
        except ValueError:
            continue
        for side, key in (("CE", "ce"), ("PE", "pe")):
            if key in sd:
                legs[side] = str(sd[key]["security_id"])

    wanted = ["CE", "PE"] if args.leg == "BOTH" else [args.leg]
    anchor = E.session_anchor_epoch()
    all_rows = []

    for side in wanted:
        sec = legs.get(side)
        if not sec:
            print(f"\n{side}: strike {strike} not listed")
            continue

        raw = E.fetch_intraday_1m(sec, E.SENSEX["segment"], "OPTIDX",
                                  days=args.seed_days)
        c2 = E.aggregate_2m(raw)
        prior = [c for c in c2 if c["ts"] < anchor]
        today = [c for c in c2 if c["ts"] >= anchor]

        rows = compute_series(c2, anchor, args.field, args.atr_period,
                              args.atr_method)
        sess = [r for r in rows if r["ts"] >= anchor]

        atr_at_open = sess[0]["atr"] if sess else None
        residual = (1 - 1 / args.atr_period) ** len(prior) if prior else 1.0

        print(f"\n{'='*104}")
        print(f"SENSEX {strike} {side}   secId={sec}   expiry={expiry}")
        print(f"VWAP field={args.field}   ATR({args.atr_period},"
              f"{args.atr_method}) continuous across sessions")
        print(f"Warm-up: {len(prior)} prior bars, seed influence {residual:.2e} "
              f"{'(converged)' if residual < 1e-4 else '(NOT CONVERGED — pull more history)'}")
        print(f"Session bars today: {len(today)}   "
              f"ATR at 09:15 = {atr_at_open if atr_at_open is None else round(atr_at_open, 2)}")
        if len(today) > 1:
            gaps = [i for i in range(1, len(today))
                    if today[i]["ts"] - today[i - 1]["ts"] != 120]
            if gaps:
                print(f"  ** {len(gaps)} gap(s) in the session — bars with no trades "
                      f"are absent. Check whether the chart draws them; ATR is "
                      f"bar-count sensitive. First gap at "
                      f"{E.epoch_to_ist(today[gaps[0]]['ts'], '%H:%M')}")
        print(f"{'-'*104}")
        print(f"{'time':>6} {'open':>9} {'high':>9} {'low':>9} {'close':>9} "
              f"{'volume':>10} {'VWAP':>9} {'ATR':>8} {'above?':>7}")
        print(f"{'-'*104}")

        show = sess if args.rows == 0 else sess[:args.rows]
        for r in show:
            v = r["vwap"]
            a = r["atr"]
            above = ""
            if v is not None:
                above = "YES" if r["close"] > v else ("=" if r["close"] == v else "no")
            print(f"{E.epoch_to_ist(r['ts'], '%H:%M'):>6} "
                  f"{r['open']:>9.2f} {r['high']:>9.2f} {r['low']:>9.2f} "
                  f"{r['close']:>9.2f} {r.get('volume', 0):>10.0f} "
                  f"{('%.2f' % v) if v is not None else '-':>9} "
                  f"{('%.2f' % a) if a is not None else '-':>8} {above:>7}")
        if args.rows and len(sess) > args.rows:
            print(f"       ... {len(sess) - args.rows} more bars "
                  f"(use --rows 0 for all)")

        for r in sess:
            all_rows.append({"leg": side, "strike": strike, "sec_id": sec,
                             "time": E.epoch_to_ist(r["ts"], "%H:%M"),
                             "open": r["open"], "high": r["high"],
                             "low": r["low"], "close": r["close"],
                             "volume": r.get("volume", 0),
                             "vwap": r["vwap"], "atr": r["atr"]})

    if args.csv and all_rows:
        with open(args.csv, "w", newline="") as f:
            w = csvmod.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nWrote {len(all_rows)} rows -> {args.csv}")


if __name__ == "__main__":
    main()
