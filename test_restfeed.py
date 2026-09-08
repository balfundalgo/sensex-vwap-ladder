"""Offline tests for restfeed.RestCandleFeed — no network."""
import sys
import time
import logging
logging.disable(logging.WARNING)      # synthetic lags are huge; not a finding
from indicators import aggregate
from restfeed import RestCandleFeed

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          f"{'' if ok else f'   got={got!r} want={want!r}'}")
    if not ok:
        FAILS.append(name)


DAY = 86400 * 20000          # a clean session anchor for arithmetic
ANCHOR = DAY
anchor_of = lambda ts: ANCHOR


def bars_1m(n, start=0, step=1):
    """n one-minute bars beginning `start` minutes after the anchor."""
    return [{"ts": ANCHOR + (start + i * step) * 60, "open": 100.0 + i,
             "high": 101.0 + i, "low": 99.0 + i, "close": 100.0 + i,
             "volume": 100 + i} for i in range(n)]


def make(source, now, poll=0.01, grace=6.0):
    got = []
    feed = RestCandleFeed(
        legs={"PE": "999"},
        fetch_1m=lambda sec, days=1: source,
        aggregate_fn=aggregate, anchor_of=anchor_of,
        on_bar=lambda leg, b: got.append((leg, b)),
        period=2, poll=poll, grace=grace)
    real = time.time
    time.time = lambda: now
    try:
        feed._poll_leg("PE", "999")
    finally:
        time.time = real
    return feed, got


print("\n[1] A bar with both source minutes is emitted immediately")
src = bars_1m(4)                                   # 09:15 09:16 09:17 09:18
now = ANCHOR + 19 * 60                             # well past all of them
feed, got = make(src, now)
check("two complete bars emitted", len(got), 2)
check("first bar is 09:15", got[0][1]["ts"], ANCHOR)
check("second bar is 09:17", got[1][1]["ts"], ANCHOR + 120)
check("volumes summed", got[0][1]["volume"], 201)

print("\n[2] A half-formed final bar is held back")
src = bars_1m(3)                                   # 09:17 has only one minute
now = ANCHOR + 4 * 60 + 2                          # 09:19:02, grace not yet up
feed, got = make(src, now)
check("only the complete bar emitted", len(got), 1)
check("it is 09:15", got[0][1]["ts"], ANCHOR)

print("\n[3] ...but released once grace passes, so an illiquid leg is not stuck")
now = ANCHOR + 4 * 60 + 30                         # past 09:17 close + 6s grace
feed, got = make(src, now)
check("both bars emitted", len(got), 2)
check("the partial bar is 09:17", got[1][1]["ts"], ANCHOR + 120)
check("partial bar holds one minute of volume", got[1][1]["volume"], 102)

print("\n[4] Nothing is emitted twice")
src = bars_1m(6)
now = ANCHOR + 20 * 60
feed = RestCandleFeed(legs={"PE": "999"},
                      fetch_1m=lambda sec, days=1: src,
                      aggregate_fn=aggregate, anchor_of=anchor_of,
                      on_bar=lambda leg, b: seen.append(b["ts"]),
                      period=2, poll=0.01)
seen = []
real = time.time
time.time = lambda: now
try:
    feed._poll_leg("PE", "999")
    first = len(seen)
    feed._poll_leg("PE", "999")
    feed._poll_leg("PE", "999")
finally:
    time.time = real
check("first poll emitted 3 bars", first, 3)
check("later polls emitted nothing new", len(seen), 3)

print("\n[5] prime() suppresses bars already handled during seeding")
seen = []
feed = RestCandleFeed(legs={"PE": "999"},
                      fetch_1m=lambda sec, days=1: src,
                      aggregate_fn=aggregate, anchor_of=anchor_of,
                      on_bar=lambda leg, b: seen.append(b["ts"]),
                      period=2, poll=0.01)
feed.prime("PE", ANCHOR + 120)                     # 09:15 and 09:17 already done
real = time.time
time.time = lambda: now
try:
    feed._poll_leg("PE", "999")
finally:
    time.time = real
check("only the unseen bar emitted", seen, [ANCHOR + 240])

print("\n[6] Latency is measured against the bar's close, not its start")
feed, got = make(bars_1m(2), ANCHOR + 120 + 7)     # 7s after 09:15 bar closed
check("one bar", len(got), 1)
lag = feed.latencies[0]
check("lag is ~7s", round(lag), 7)
h = feed.health()
check("health reports comfortable", h["verdict"], "comfortable")

print("\n[7] A slow feed is called out rather than silently tolerated")
feed, got = make(bars_1m(2), ANCHOR + 120 + 40)
check("lag ~40s", round(feed.latencies[0]), 40)
check("verdict warns", feed.health()["verdict"],
      "TOO SLOW — entries will be missed")

print("\n[8] Gaps: member counting works in count mode")
src = [b for b in bars_1m(6) if b["ts"] != ANCHOR + 60]   # 09:16 never traded
now = ANCHOR + 20 * 60
feed, got = make(src, now)
rolled = aggregate(src, 2, "count", anchor_of)
check("emits every complete rolled bar", len(got), len(rolled))
check("no volume lost", sum(b["volume"] for _, b in got),
      sum(b["volume"] for b in rolled))

print("\n[9] An empty response is survivable")
feed, got = make([], ANCHOR + 600)
check("nothing emitted", len(got), 0)
check("no crash, no latency recorded", len(feed.latencies), 0)

print("\n[10] The still-forming candle is dropped before anything sees it")
# Dhan returns the current minute as soon as it starts. At 09:19:02 the bar
# labelled 09:19 exists but covers 09:19:00-09:19:59 and is 2 seconds old.
src = bars_1m(5)                                   # 09:15 .. 09:19
now = ANCHOR + 4 * 60 + 2                          # 09:19:02
kept = RestCandleFeed.drop_forming(src, now)
check("forming bar removed", len(kept), 4)
check("newest kept bar is 09:18", kept[-1]["ts"], ANCHOR + 3 * 60)
check("a fully closed set is untouched",
      len(RestCandleFeed.drop_forming(src, ANCHOR + 10 * 60)), 5)

print("\n[11] A 2-min bar is never built from a half-formed minute")
# Without the filter, [09:18, 09:19] looks like a complete pair at 09:19:02
# even though 09:19 is two seconds old. This is the trap.
feed, got = make(bars_1m(5), ANCHOR + 4 * 60 + 2)
emitted = [b["ts"] for _, b in got]
check("09:19 bar NOT emitted", (ANCHOR + 4 * 60) in emitted, False)
check("only genuinely closed bars emitted", emitted, [ANCHOR, ANCHOR + 120])
check("forming bars counted", feed.dropped_forming >= 1, True)

print("\n[12] Latency is now measured against real closes, never negative")
feed, got = make(bars_1m(4), ANCHOR + 4 * 60 + 5)
check("no negative latency", all(l >= 0 for l in feed.latencies), True)

print("\n[13] REGRESSION 08-Sep: yesterday's bars must never be emitted")
# The app was started at 08:54. days=1 returned 244 bars from the previous
# session and every one was emitted as live: 488 warnings, six phantom
# signals before the open, VWAP and ATR polluted for the whole day.
YESTERDAY = ANCHOR - 86400
stale = [{"ts": YESTERDAY + i * 60, "open": 100.0, "high": 101.0, "low": 99.0,
          "close": 100.0, "volume": 5000} for i in range(30)]
fresh = bars_1m(4)
got = []
feed = RestCandleFeed(legs={"PE": "999"},
                      fetch_1m=lambda sec, days=1: stale + fresh,
                      aggregate_fn=aggregate, anchor_of=anchor_of,
                      on_bar=lambda leg, b: got.append(b["ts"]),
                      period=2, poll=0.01, session_anchor=ANCHOR)
real = time.time
time.time = lambda: ANCHOR + 10 * 60
try:
    feed._poll_leg("PE", "999")
finally:
    time.time = real
check("no pre-session bar emitted", all(t >= ANCHOR for t in got), True)
check("today's bars still emitted", len(got), 2)
check("stale bars counted", feed.dropped_stale > 0, True)

print("\n[14] Started before the open: nothing is emitted at all")
got = []
feed = RestCandleFeed(legs={"PE": "999"},
                      fetch_1m=lambda sec, days=1: stale,
                      aggregate_fn=aggregate, anchor_of=anchor_of,
                      on_bar=lambda leg, b: got.append(b["ts"]),
                      period=2, poll=0.01, session_anchor=ANCHOR)
real = time.time
time.time = lambda: ANCHOR - 20 * 60          # 08:55, before the market opens
try:
    feed._poll_leg("PE", "999")
finally:
    time.time = real
check("nothing emitted before the open", len(got), 0)

print("\n[15] Without the anchor the old behaviour returns (guard is load-bearing)")
got = []
feed = RestCandleFeed(legs={"PE": "999"},
                      fetch_1m=lambda sec, days=1: stale + fresh,
                      aggregate_fn=aggregate, anchor_of=anchor_of,
                      on_bar=lambda leg, b: got.append(b["ts"]),
                      period=2, poll=0.01)          # no session_anchor
real = time.time
time.time = lambda: ANCHOR + 10 * 60
try:
    feed._poll_leg("PE", "999")
finally:
    time.time = real
check("unguarded feed does emit stale bars", any(t < ANCHOR for t in got), True)

print("\n[16] The lag warning is rate-limited")
import logging as _lg
recs = []
class Cap(_lg.Handler):
    def emit(self, r): recs.append(r.getMessage())
_lg.disable(_lg.NOTSET)
lg = _lg.getLogger("SNX"); lg.addHandler(Cap()); lg.setLevel(_lg.WARNING)
late = [{"ts": ANCHOR + i * 60, "open": 100.0, "high": 101.0, "low": 99.0,
         "close": 100.0, "volume": 500} for i in range(40)]
got = []
feed = RestCandleFeed(legs={"PE": "999"},
                      fetch_1m=lambda sec, days=1: late,
                      aggregate_fn=aggregate, anchor_of=anchor_of,
                      on_bar=lambda leg, b: got.append(b["ts"]),
                      period=2, poll=0.01, session_anchor=ANCHOR)
real = time.time
time.time = lambda: ANCHOR + 3 * 3600          # everything is hours late
try:
    feed._poll_leg("PE", "999")
finally:
    time.time = real
warns = [m for m in recs if "late" in m]
check("20 late bars emitted", len(got), 20)
check("but only one warning logged", len(warns), 1)
check("every late bar is still counted", feed.health()["late_bars"], 20)
_lg.disable(_lg.WARNING)

print("\n" + ("ALL RESTFEED TESTS PASSED" if not FAILS
              else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
