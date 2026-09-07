"""
Smoke-test every vwap_check code path against a fake API.

Syntax checks do not catch a NameError on a branch that never runs. This walks
each output mode end to end so a stale variable cannot ship again.
"""
import sys
import io
import time
import random
import contextlib

import engine as E
import vwap_check as V

FAILS = []
ANCHOR = E.session_anchor_epoch()


def fake_bars(n=80):
    random.seed(4)
    out, px = [], 370.0
    for i in range(n):
        h = px + random.uniform(0.5, 6)
        l = px - random.uniform(0.5, 6)
        c = random.uniform(l, h)
        out.append({"ts": ANCHOR + i * 60, "open": px, "high": h, "low": l,
                    "close": c, "volume": random.randint(20000, 140000)})
        px = c
    return out


BARS = fake_bars()
E.DHAN_CLIENT_ID = "test"
E.init_credentials = lambda *a, **k: None
E.fetch_intraday_1m = lambda sec, seg, inst, days=1: BARS
E.fetch_expiry_list = lambda: ["2026-09-10"]
E.get_nearest_expiry = lambda: "2026-09-10"
E.fetch_option_chain = lambda exp: {
    "spot_price": 76400.0,
    "oc": {"76700.000000": {"pe": {"security_id": "860293"},
                            "ce": {"security_id": "860701"}}}}
# freeze "now" past the last bar so everything is confirmed
_REAL = time.time
time.time = lambda: ANCHOR + 90 * 60


def run(name, argv, expect=(), forbid=()):
    buf = io.StringIO()
    sys.argv = ["vwap_check.py"] + argv
    try:
        with contextlib.redirect_stdout(buf):
            V.main()
    except SystemExit as e:
        if e.code:
            print(f"  FAIL  {name}: exited with {e.code!r}")
            FAILS.append(name)
            return buf.getvalue()
    except Exception as e:
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        FAILS.append(name)
        return buf.getvalue()
    out = buf.getvalue()
    for token in expect:
        if token not in out:
            print(f"  FAIL  {name}: missing {token!r}")
            FAILS.append(name)
            return out
    for token in forbid:
        if token in out:
            print(f"  FAIL  {name}: unexpectedly contains {token!r}")
            FAILS.append(name)
            return out
    print(f"  PASS  {name}")
    return out


print("\nvwap_check end-to-end paths")
o = run("default table", ["--sec-id", "860293"],
        expect=["vwap:ohlc4", "vwap:close", "bars today", "VWAP now:"])
run("--head", ["--sec-id", "860293", "--head", "5"], expect=["vwap:ohlc4"])
run("--tail", ["--sec-id", "860293", "--tail", "5"], expect=["vwap:ohlc4"])
run("--basis 1m", ["--sec-id", "860293", "--basis", "1m"], expect=["vwap:ohlc4"])
run("--basis both", ["--sec-id", "860293", "--basis", "both"],
    expect=["over 2m bars", "over 1m bars", "difference"])
run("--at working", ["--sec-id", "860293", "--at",
                     E.epoch_to_ist(ANCHOR + 600, "%H:%M")],
    expect=["running sum(p*v)", "/ sum(v)"])
run("--match", ["--sec-id", "860293", "--at",
                E.epoch_to_ist(ANCHOR + 600, "%H:%M"), "--match", "372.5"],
    expect=["chart shows VWAP", "closest"])
run("by strike", ["--strike", "76700", "--leg", "PE"], expect=["vwap:ohlc4"])
run("--no-identify", ["--sec-id", "860293", "--no-identify"],
    expect=["secId 860293"])

# head and tail must actually differ
h = run("head != tail (head)", ["--sec-id", "860293", "--head", "3"])
t = run("head != tail (tail)", ["--sec-id", "860293", "--tail", "3"])
if h and t:
    hb = [x for x in h.splitlines() if x.startswith(" 0") or x.startswith(" 1")]
    tb = [x for x in t.splitlines() if x.startswith(" 0") or x.startswith(" 1")]
    if hb and tb and hb[0] == tb[0]:
        print("  FAIL  head and tail returned the same first row")
        FAILS.append("head vs tail")
    else:
        print("  PASS  head and tail show different rows")

time.time = _REAL
print("\n" + ("ALL VWAP_CHECK PATHS PASSED" if not FAILS
              else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
