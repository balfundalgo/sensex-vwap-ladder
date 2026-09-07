#!/usr/bin/env python3
"""
vwap_check.py — VWAP only, nothing else, for eyeballing against the chart.

    python vwap_check.py --strike 76700 --leg PE
    python vwap_check.py --strike 76700 --leg PE --expiry 2026-09-10
    python vwap_check.py --sec-id 860293
    python vwap_check.py --strike 76700 --leg PE --at 12:33
    python vwap_check.py --strike 76700 --leg PE --tail 20

Every VWAP field is shown side by side so you can see at a glance which column
the chart is tracking. Still-forming candles are excluded — Dhan serves the
current minute while it is being built, and comparing that against a finished
bar on the chart is comparing two different things.

The arithmetic is deliberately visible:

    VWAP = running sum(price x volume) / running sum(volume)

--at prints the full working for one bar so it can be checked by hand.
"""

import argparse
import sys
import time

import engine as E
from indicators import price_field, FIELDS
from restfeed import RestCandleFeed

SHOW = ("ohlc4", "hlc3", "hl2", "close")


def identify(sec_id, expiries):
    """
    Work out which contract a bare security id belongs to.

    Passing --sec-id skips the lookups, which is fast but leaves you staring at
    a number with no idea which strike or expiry you are comparing. Walk the
    nearest few chains and name it.
    """
    for exp in expiries[:3]:
        oc = E.fetch_option_chain(exp)
        if not oc:
            continue
        for sk, sd in oc["oc"].items():
            try:
                strike = int(float(sk))
            except ValueError:
                continue
            for side, key in (("CE", "ce"), ("PE", "pe")):
                if key in sd and str(sd[key]["security_id"]) == str(sec_id):
                    return f"SENSEX {strike} {side}", exp
    return None, None


def resolve(args):
    """Return (security_id, label). Either given directly or looked up."""
    if args.sec_id:
        if args.no_identify:
            return args.sec_id, f"secId {args.sec_id}"
        exps = E.fetch_expiry_list()
        name, exp = identify(args.sec_id, exps) if exps else (None, None)
        if name:
            return args.sec_id, f"{name}   expiry {exp}"
        return args.sec_id, f"secId {args.sec_id} (contract not identified)"

    expiry = args.expiry
    if not expiry:
        exps = E.fetch_expiry_list()
        if not exps:
            sys.exit("Could not fetch the expiry list")
        expiry = E.get_nearest_expiry()
        print(f"  expiries: {exps[:4]}")
    print(f"  using expiry {expiry}")

    strike = args.strike
    if not strike:
        io = E.get_sensex_open_0915()
        if not io:
            sys.exit("Could not read the 09:15 index open — pass --strike")
        strike = int(round(io / 100.0) * 100)
        print(f"  09:15 open {io:.2f} -> strike {strike}")

    oc = E.fetch_option_chain(expiry)
    if not oc:
        sys.exit("Option chain unavailable")
    for sk, sd in oc["oc"].items():
        try:
            if abs(float(sk) - strike) > 0.01:
                continue
        except ValueError:
            continue
        key = "ce" if args.leg == "CE" else "pe"
        if key in sd:
            return str(sd[key]["security_id"]), f"SENSEX {strike} {args.leg}"
    sys.exit(f"{args.leg} not listed for strike {strike} on {expiry}")


def build_rows_1m(raw, agg, anchor, now=None, include_forming=False, period=2):
    """
    Same VWAP, accumulated over the 1-MINUTE bars instead of the 2-minute ones.

    This is not a cosmetic difference. ohlc4 of a merged bar is not the
    volume-weighted mean of the ohlc4s that went into it, because the merged
    high and low come from different minutes. On a single bar the two bases can
    differ by rupees, and it compounds across a session.

    Rows are still reported at 2-minute boundaries so the two can be compared
    line by line against the chart.
    """
    now = time.time() if now is None else now
    span = period * 60
    marks = E.aggregate_2m(raw, agg)
    if not include_forming:
        marks = [b for b in marks if b["ts"] + span <= now]
    valid = {b["ts"]: b for b in marks if b["ts"] >= anchor}
    if not valid:
        return []
    cutoff = max(valid) + span

    pv = {f: 0.0 for f in SHOW}
    vol = 0.0
    out = []
    for c in sorted(raw, key=lambda x: x["ts"]):
        ts = int(c["ts"])
        if ts < anchor or ts >= cutoff:
            continue
        v = float(c.get("volume", 0.0))
        if v > 0:
            for f in SHOW:
                pv[f] += price_field(c["open"], c["high"], c["low"],
                                     c["close"], f) * v
            vol += v
        bar_start = ts - ((ts - anchor) % span)
        if ts == bar_start + span - 60 and bar_start in valid:
            b = valid[bar_start]
            out.append({**b,
                        "vwap": {f: (pv[f] / vol if vol else None) for f in SHOW},
                        "cum_vol": vol, "cum_pv": dict(pv)})
    return out


def build_rows(raw, agg, anchor, now=None, include_forming=False, period=2):
    """
    Accumulate every VWAP field over the session's bars.

    A 2-minute bar is two 1-minute bars, so removing the still-forming MINUTE
    is not enough: the resulting 2-minute group can still be missing its second
    half. Unless include_forming is set, any trailing bar whose close is still
    in the future is dropped, because it is not the bar the chart is drawing.

    This is how a row could report volume 32,200 at 12:42 and 65,980 for the
    same 12:41 bar ten minutes later — it was half a candle both times, and
    only the second reading was complete.
    """
    now = time.time() if now is None else now
    span = period * 60
    bars = E.aggregate_2m(raw, agg)
    if not include_forming:
        bars = [b for b in bars if b["ts"] + span <= now]
    pv = {f: 0.0 for f in SHOW}
    vol = 0.0
    out = []
    for b in bars:
        if b["ts"] < anchor:
            continue
        v = float(b.get("volume", 0.0))
        if v > 0:
            for f in SHOW:
                pv[f] += price_field(b["open"], b["high"], b["low"],
                                     b["close"], f) * v
            vol += v
        out.append({**b,
                    "vwap": {f: (pv[f] / vol if vol else None) for f in SHOW},
                    "cum_vol": vol, "cum_pv": dict(pv)})
    return out


def fmt_row(r, marker=" "):
    vals = " ".join(
        f"{r['vwap'][f]:>11.2f}" if r["vwap"][f] is not None else f"{'-':>11}"
        for f in SHOW)
    return (f"{marker}{E.epoch_to_ist(r['ts'], '%H:%M'):>5} {r['close']:>9.2f} "
            f"{r.get('volume', 0):>10.0f}  |{vals}")


def watch(sec, args, anchor, seen_upto):
    """
    Wait for the candle boundary, then grab the bar the moment it appears.

    Blind polling on a fixed interval is wasted effort: between boundaries no
    new closed bar can exist. So the loop sleeps until the next 2-minute close,
    then polls every `fast` seconds until the bar lands, prints it with the lag
    it actually took, and goes back to sleep.

    Nothing here makes a bar arrive sooner. `fast` is how often we ask, not how
    long we wait. Dhan published a bar 0.1s to 3.0s after its close when this
    was measured, so most rows show +1s or so — but a slow one shows +3s, and a
    genuinely late one shows whatever it took. After 60s the loop says the bar
    never came rather than pretending otherwise.

    Two kinds of line:
      (blank)  a CONFIRMED bar, with how long after its close it arrived
      ~        the bar still forming, VWAP provisional and moving

    The chart's VWAP line also moves during the forming bar, so the ~ line is
    what matches your screen right now; the plain line is where it settles.
    """
    print(f"\nwaiting for candle closes — Ctrl+C to stop")
    print(f"  ~ = still forming (refreshed every {args.every:.0f}s)")
    print(f"  at each close, polls every {args.fast:.1f}s until the bar actually")
    print(f"  arrives; +Xs on each row is when it did. Gives up after 60s.\n")

    last_prov = ""

    def fetch():
        raw = E.fetch_intraday_1m(sec, E.SENSEX["segment"], "OPTIDX", days=1) or []
        return [{**c, "ts": int(c["ts"])} for c in raw]

    def show_provisional(raw_all):
        nonlocal last_prov
        prov = build_rows(raw_all, args.agg, anchor, include_forming=True)
        if not prov:
            return
        p = prov[-1]
        if p["ts"] > seen_upto:
            line = fmt_row(p, "~")
            if line != last_prov:
                last_prov = line
                print(line, end="\r", flush=True)

    try:
        while True:
            now = time.time()
            n = int((now - anchor) // 120) + 1
            close_at = anchor + n * 120

            # ── idle until the bar closes, refreshing the provisional line ──
            next_prov = 0.0
            while time.time() < close_at:
                if time.time() >= next_prov:
                    next_prov = time.time() + args.every
                    try:
                        show_provisional(fetch())
                    except Exception:
                        pass
                time.sleep(min(0.5, max(0.05, close_at - time.time())))

            # ── the bar has closed; poll hard until it appears ──
            got = False
            deadline = close_at + 60
            while time.time() < deadline:
                try:
                    raw_all = fetch()
                except Exception as ex:
                    print(f"  fetch error: {ex}")
                    time.sleep(args.fast)
                    continue
                closed = RestCandleFeed.drop_forming(raw_all, time.time())
                for r in build_rows(closed, args.agg, anchor):
                    if r["ts"] > seen_upto:
                        seen_upto = r["ts"]
                        lag = time.time() - (r["ts"] + 120)
                        print(f"{fmt_row(r)}   +{lag:.1f}s")
                        got = True
                if got:
                    last_prov = ""
                    break
                time.sleep(args.fast)

            if not got:
                print(f"  {E.epoch_to_ist(int(close_at - 120), '%H:%M')} bar never "
                      f"arrived within 60s — market may be closed")
    except KeyboardInterrupt:
        print("\n\nstopped.")


def main():
    ap = argparse.ArgumentParser(description="VWAP only, vs the chart")
    ap.add_argument("--strike", type=int, default=0)
    ap.add_argument("--leg", choices=["CE", "PE"], default="PE")
    ap.add_argument("--expiry", default="", help="YYYY-MM-DD; default nearest")
    ap.add_argument("--sec-id", default="", help="probe this id directly")
    ap.add_argument("--no-identify", action="store_true",
                    help="with --sec-id, skip working out which contract it is")
    ap.add_argument("--basis", choices=["2m", "1m", "both"], default="2m",
                    help="accumulate VWAP over the 2-minute bars (default) or "
                         "over the underlying 1-minute bars. 'both' compares "
                         "them so you can see which one the chart is using.")
    ap.add_argument("--field", default="ohlc4", choices=list(SHOW),
                    help="which field to compare when --basis both")
    ap.add_argument("--agg", choices=["count", "clock"], default="count")
    ap.add_argument("--days", type=int, default=10,
                    help="days of history pulled — same default as reconcile.py")
    ap.add_argument("--tail", type=int, default=0,
                    help="show only the last N bars (0 = all)")
    ap.add_argument("--head", type=int, default=0,
                    help="show only the FIRST N bars from 09:15 — this is what "
                         "reconcile.py shows, so use it to compare like for like")
    ap.add_argument("--solve-anchor", type=float, default=None,
                    help="the VWAP the chart shows at --at; scans every possible "
                         "accumulation start and reports which one reproduces it")
    ap.add_argument("--anchor", default="",
                    help="start accumulating at HH:MM instead of 09:15")
    ap.add_argument("--match", type=float, default=None,
                    help="the VWAP the chart shows; with --at, says which field "
                         "and basis reproduces it")
    ap.add_argument("--at", default="", help="show the full working for one bar, e.g. 12:33")
    ap.add_argument("--watch", action="store_true",
                    help="keep running and print each bar as it closes")
    ap.add_argument("--every", type=float, default=5.0,
                    help="how often to refresh the ~ provisional line")
    ap.add_argument("--fast", type=float, default=0.7,
                    help="poll interval immediately after a candle closes")
    args = ap.parse_args()

    if not E.DHAN_CLIENT_ID:
        sys.exit("Missing credentials — fill .env first")
    E.init_credentials()

    sec, label = resolve(args)

    raw = E.fetch_intraday_1m(sec, E.SENSEX["segment"], "OPTIDX", days=args.days)
    if not raw:
        sys.exit("No intraday data returned")
    n_all = len(raw)
    raw = RestCandleFeed.drop_forming(
        [{**c, "ts": int(c["ts"])} for c in raw], time.time())
    dropped = n_all - len(raw)

    anchor = E.session_anchor_epoch()
    if args.anchor:
        hh, mm = args.anchor.split(":")
        base = E.datetime.fromtimestamp(anchor, tz=E.IST).replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0)
        anchor = int(base.timestamp())
        print(f"  accumulating from {args.anchor} instead of 09:15")

    rows = build_rows(raw, args.agg, anchor)
    rows = [r for r in rows if r["ts"] >= anchor]
    if not rows:
        sys.exit("No bars for today yet")

    if args.basis in ("1m", "both"):
        rows1 = build_rows_1m(raw, args.agg, anchor)
        if args.basis == "1m":
            rows = rows1

    print(f"\n{'=' * 92}")
    print(f"VWAP — {label}")
    print(f"secId {sec}   rollup={args.agg}   "
          f"{E.epoch_to_ist(E.session_anchor_epoch(), '%d-%b')} session")
    print(f"{len(rows)} bars today"
          + (f"   ({dropped} still-forming minute(s) excluded)" if dropped else ""))
    print("=" * 92)

    if args.solve_anchor is not None:
        if not args.at:
            sys.exit("--solve-anchor needs --at HH:MM")
        bars = [b for b in E.aggregate_2m(raw, args.agg)
                if b["ts"] >= E.session_anchor_epoch()
                and b["ts"] + 120 <= time.time()]
        tgt = next((b for b in bars
                    if E.epoch_to_ist(b["ts"], "%H:%M") == args.at), None)
        if not tgt:
            sys.exit(f"No bar at {args.at}")
        upto = [b for b in bars if b["ts"] <= tgt["ts"]]

        print(f"\nBar {args.at}: chart shows VWAP {args.solve_anchor} "
              f"(field {args.field})\n")
        print("Scanning every possible accumulation start:\n")
        print(f"  {'start from':>11}{'bars':>7}{'our VWAP':>12}{'diff':>11}")
        print("  " + "-" * 41)
        results = []
        for i in range(len(upto)):
            pv = vol = 0.0
            for b in upto[i:]:
                v = float(b.get("volume", 0.0))
                if v > 0:
                    pv += price_field(b["open"], b["high"], b["low"],
                                      b["close"], args.field) * v
                    vol += v
            if vol <= 0:
                continue
            val = pv / vol
            results.append((abs(val - args.solve_anchor),
                            E.epoch_to_ist(upto[i]["ts"], "%H:%M"),
                            len(upto) - i, val))
        results.sort()
        for d, t, n, val in results[:6]:
            print(f"  {t:>11}{n:>7}{val:>12.2f}{val - args.solve_anchor:>+11.2f}")
        best = results[0]
        print()
        if best[0] < 0.5:
            print(f"  The chart is accumulating from {best[1]}, not 09:15 —")
            print(f"  only the last {best[2]} bars are in its average.")
            print(f"  That is what a chart does when it has not loaded the whole")
            print(f"  session: the study runs over the bars it holds in memory,")
            print(f"  so its 'Session' VWAP starts at the first bar it has.")
            print(f"\n  Scroll the chart left to load the morning; its VWAP should")
            print(f"  fall towards ours. If it does, our number is the right one.")
        else:
            print(f"  No start point reproduces {args.solve_anchor}. Closest is "
                  f"{best[1]} at {best[3]:.2f}.")
            print(f"  So it is not a windowing difference — something else.")
        print()
        return

    if args.match is not None:
        if not args.at:
            sys.exit("--match needs --at HH:MM (the bar the value belongs to)")
        rows1 = build_rows_1m(raw, args.agg, anchor)
        by1 = {r["ts"]: r for r in rows1}
        target = next((r for r in rows
                       if E.epoch_to_ist(r["ts"], "%H:%M") == args.at), None)
        if not target:
            have = [E.epoch_to_ist(r["ts"], "%H:%M") for r in rows]
            sys.exit(f"No bar at {args.at}. Have {have[0]}..{have[-1]}")
        alt = by1.get(target["ts"])
        cands = []
        for f in SHOW:
            if target["vwap"][f] is not None:
                cands.append(("over 2m bars", f, target["vwap"][f]))
            if alt and alt["vwap"][f] is not None:
                cands.append(("over 1m bars", f, alt["vwap"][f]))
        cands.sort(key=lambda x: abs(x[2] - args.match))
        print(f"\nBar {args.at}   chart shows VWAP {args.match}\n")
        print(f"  {'basis':<14}{'field':<8}{'ours':>10}{'difference':>13}")
        print("  " + "-" * 45)
        for basis, f, val in cands:
            mark = "   <== closest" if (basis, f, val) == cands[0] else ""
            print(f"  {basis:<14}{f:<8}{val:>10.2f}{val - args.match:>+13.2f}{mark}")
        best = cands[0]
        print()
        if abs(best[2] - args.match) < 0.02:
            print(f"  MATCH: the chart is using {best[1]} accumulated "
                  f"{best[0]}.")
        else:
            print(f"  Nothing matches. Closest is {best[1]} {best[0]}, still "
                  f"{abs(best[2] - args.match):.2f} out.")
            print(f"  That means the bars themselves differ, not the formula —")
            print(f"  check the close and volume for {args.at} against the chart.")
        print()
        return

    if args.at:
        match = next((r for r in rows if E.epoch_to_ist(r["ts"], "%H:%M") == args.at),
                     None)
        if not match:
            have = [E.epoch_to_ist(r["ts"], "%H:%M") for r in rows]
            sys.exit(f"No bar at {args.at}. Have {have[0]}..{have[-1]}")
        o, h, l, c = match["open"], match["high"], match["low"], match["close"]
        print(f"\nBar {args.at}   O {o:.2f}   H {h:.2f}   L {l:.2f}   C {c:.2f}"
              f"   volume {match.get('volume', 0):.0f}\n")
        for f in SHOW:
            p = price_field(o, h, l, c, f)
            print(f"  {f:<6} price {p:>9.4f}   "
                  f"running sum(p*v) {match['cum_pv'][f]:>16.2f}   "
                  f"/ sum(v) {match['cum_vol']:>12.0f}   "
                  f"= {match['vwap'][f]:>9.4f}")
        print(f"\n  Compare the column your chart is set to against its VWAP line.")
        print("=" * 92)
        return

    if args.basis == "both":
        f = args.field
        by_ts = {r["ts"]: r for r in rows1}
        print(f"Comparing accumulation basis, field = {f}")
        print(f" {'time':>5} {'close':>9} {'volume':>10}  |"
              f"{'over 2m bars':>14}{'over 1m bars':>14}{'difference':>13}")
        print("-" * 92)
        show = rows[-args.tail:] if args.tail else rows
        for r in show:
            o = by_ts.get(r["ts"])
            a2 = r["vwap"][f]
            a1 = o["vwap"][f] if o else None
            d = (f"{a1 - a2:+13.2f}" if (a1 is not None and a2 is not None)
                 else f"{'-':>13}")
            print(f" {E.epoch_to_ist(r['ts'], '%H:%M'):>5} {r['close']:>9.2f} "
                  f"{r.get('volume', 0):>10.0f}  |"
                  f"{a2:>14.2f}"
                  f"{(a1 if a1 is not None else float('nan')):>14.2f}{d}")
        print("-" * 92)
        print("Whichever column matches your chart is the basis the broker uses.")
        print("=" * 92)
        return

    print(f" {'time':>5} {'close':>9} {'volume':>10}  |" +
          " ".join(f"{'vwap:' + f:>11}" for f in SHOW))
    print(f" {'':<5} {'the bar':>9} {'the bar':>10}  |"
          + f"{'  same VWAP, four different price fields':<44}")
    print("-" * 92)
    if args.head:
        show = rows[:args.head]
    elif args.tail:
        show = rows[-args.tail:]
    else:
        show = rows
    for r in show:
        print(fmt_row(r))
    print("-" * 92)
    last = rows[-1]["vwap"]
    lastbar = rows[-1]
    if all(v is not None for v in last.values()):
        spread = max(last.values()) - min(last.values())
        print("VWAP now:  " + "   ".join(f"{f}={last[f]:.2f}" for f in SHOW))
        print(f"spread across the four fields: {spread:.2f}")
        gap = lastbar["close"] - last["ohlc4"]
        print(f"last close {lastbar['close']:.2f} is {gap:+.2f} vs vwap:ohlc4 "
              f"{last['ohlc4']:.2f}")
        print("  (VWAP is the volume-weighted average since 09:15, so on a day")
        print("   where the premium has trended it sits well away from spot.)")
    print("=" * 92)

    if args.watch:
        watch(sec, args, anchor, rows[-1]["ts"])


if __name__ == "__main__":
    main()
