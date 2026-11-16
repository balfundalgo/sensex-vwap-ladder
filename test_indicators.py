"""Validate indicators.py against hand-computed ChartIQ definitions."""
import sys
from indicators import (SessionVWAP, WilderATR, price_field, true_range,
                        compute_series)

FAILS = []


def check(name, got, want, tol=1e-9):
    ok = abs(got - want) < tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          f"{'' if ok else f'   got={got!r} want={want!r}'}")
    if not ok:
        FAILS.append(name)


print("\n[1] Field selection")
o, h, l, c = 250.0, 262.0, 248.0, 260.0
check("ohlc4", price_field(o, h, l, c, "ohlc4"), (250 + 262 + 248 + 260) / 4)
check("hlc3", price_field(o, h, l, c, "hlc3"), (262 + 248 + 260) / 3)
check("hl2", price_field(o, h, l, c, "hl2"), (262 + 248) / 2)
check("close", price_field(o, h, l, c, "close"), 260.0)

print("\n[2] True Range — ChartIQ form equals the textbook form")
for hh, ll, pc in [(110, 100, 105), (110, 100, 120), (110, 100, 90),
                   (110, 100, 110), (110, 100, 100)]:
    chartiq = max(hh, pc) - min(ll, pc)
    textbook = max(hh - ll, abs(hh - pc), abs(ll - pc))
    check(f"TR(h={hh},l={ll},pc={pc})", chartiq, float(textbook))
check("TR first bar = H-L", true_range(110, 100, None), 10.0)

print("\n[3] VWAP — cumulative(price*vol)/cumulative(vol), per-bar volume")
bars = [(250.0, 256.0, 249.0, 255.0, 4000),
        (255.0, 258.0, 253.0, 254.0, 9000),
        (254.0, 261.0, 254.0, 260.0, 6000),
        (260.0, 262.0, 257.0, 258.0, 3000)]
v = SessionVWAP("ohlc4")
for b in bars:
    v.update(*b)
pv = sum(((b[0] + b[1] + b[2] + b[3]) / 4.0) * b[4] for b in bars)
vol = sum(b[4] for b in bars)
check("ohlc4 VWAP", v.value, pv / vol, 1e-9)

v2 = SessionVWAP("hlc3")
for b in bars:
    v2.update(*b)
pv2 = sum(((b[1] + b[2] + b[3]) / 3.0) * b[4] for b in bars)
check("hlc3 VWAP differs from ohlc4", abs(v2.value - v.value) > 0.01, True)
check("hlc3 VWAP", v2.value, pv2 / vol, 1e-9)

print("\n[4] VWAP — a zero-volume bar must not move the line")
before = v.value
v.update(300.0, 310.0, 295.0, 305.0, 0)
check("line held", v.value, before)
check("bar still counted", v.bars, 5)

print("\n[5] The old bug: per-bar volume fed in as if cumulative")
buggy = SessionVWAP("ohlc4")
prev = None
for b in bars:
    cv = b[4] if prev is None else max(0.0, b[4] - prev)
    prev = b[4]
    buggy.update(b[0], b[1], b[2], b[3], cv)
check("buggy value is materially different", abs(buggy.value - pv / vol) > 1.0, True)
print(f"        correct={pv/vol:.4f}  buggy={buggy.value:.4f}  "
      f"error={abs(buggy.value - pv/vol):.4f}")

print("\n[6] Wilder ATR recursion")
a = WilderATR(14, "wilder")
for _ in range(14):
    a.update(110, 100, 105)          # TR = 10 every bar
check("seed = mean of first 14 TRs", a.value, 10.0)
a.update(130, 100, 120)              # TR = max(130,105)-min(100,105) = 30
check("wilder step", a.value, (10 * 13 + 30) / 14)
a.update(125, 115, 120)              # TR = max(125,120)-min(115,120) = 10
check("wilder step 2", a.value, (((10 * 13 + 30) / 14) * 13 + 10) / 14)

print("\n[7] SMA ATR variant")
s = WilderATR(3, "sma")
for hh, ll, cc in [(110, 100, 105), (120, 100, 115), (130, 110, 125)]:
    s.update(hh, ll, cc)
trs = [10.0, max(120, 105) - min(100, 105), max(130, 115) - min(110, 115)]
check("sma of last 3 TRs", s.value, sum(trs) / 3)

print("\n[8] Wilder convergence — how much history is enough")
for n in (14, 50, 100, 200, 300):
    x = WilderATR(14)
    for _ in range(n):
        x.update(110, 100, 105)
    err = x.convergence_error()
    print(f"        {n:>3} bars -> seed influence {err:.2e}"
          f"{'   <- enough' if err < 1e-4 else ''}")
x = WilderATR(14)
for _ in range(200):
    x.update(110, 100, 105)
check("200 bars converges below 1e-4", x.convergence_error() < 1e-4, True)

print("\n[9] Two ATRs seeded from different points agree once converged")
import random
random.seed(7)
series = []
px = 250.0
for _ in range(400):
    hi = px + random.uniform(1, 6)
    lo = px - random.uniform(1, 6)
    cl = random.uniform(lo, hi)
    series.append({"high": hi, "low": lo, "close": cl})
    px = cl
long_seed = WilderATR(14); long_seed.seed(series)
short_seed = WilderATR(14); short_seed.seed(series[-250:])
check("long vs short seed agree to 0.01", abs(long_seed.value - short_seed.value) < 0.01, True)
tiny_seed = WilderATR(14); tiny_seed.seed(series[-20:])
print(f"        400-bar={long_seed.value:.4f}  250-bar={short_seed.value:.4f}  "
      f"20-bar={tiny_seed.value:.4f}")
check("a 20-bar seed does NOT agree", abs(tiny_seed.value - long_seed.value) > 0.01, True)

print("\n[10] compute_series — ATR warms on prior day, VWAP starts at anchor")
anchor = 1000000
cs = ([{"ts": anchor - (300 - i) * 120, "open": 100.0, "high": 105.0,
        "low": 95.0, "close": 100.0, "volume": 500} for i in range(300)] +
      [{"ts": anchor + i * 120, "open": 100.0 + i, "high": 105.0 + i,
        "low": 95.0 + i, "close": 100.0 + i, "volume": 1000} for i in range(5)])
rows = compute_series(cs, anchor)
check("prior bars have no VWAP", rows[0]["vwap"], None)
check("prior bars do have ATR", rows[299]["atr"] is not None, True)
check("VWAP begins at the anchor", rows[300]["vwap"] is not None, True)
check("first session bar VWAP = its own ohlc4", rows[300]["vwap"], 100.0)
check("ATR carries across the boundary", rows[300]["atr_bars"], 301)

print("\n[11] Aggregation — bar-count (ChartIQ) vs wall-clock")
from indicators import aggregate, calibrate
anchor_of = lambda ts: 0
full = [{"ts": m * 60, "open": 100.0 + m, "high": 101.0 + m, "low": 99.0 + m,
         "close": 100.0 + m, "volume": 10} for m in range(8)]
ac = aggregate(full, 2, "count", anchor_of)
al = aggregate(full, 2, "clock", anchor_of)
check("no gaps: same timestamps", [b["ts"] for b in ac], [b["ts"] for b in al])
check("no gaps: same volumes", [b["volume"] for b in ac], [b["volume"] for b in al])

gapped = [b for b in full if b["ts"] != 3 * 60]        # 09:18 never traded
gc = aggregate(gapped, 2, "count", anchor_of)
gl = aggregate(gapped, 2, "clock", anchor_of)
check("count mode bar count", len(gc), 4)
check("clock mode bar count", len(gl), 4)
check("count mode conserves volume", sum(b["volume"] for b in gc), 70)
check("clock mode conserves volume", sum(b["volume"] for b in gl), 70)
check("count groups in pairs", [b["ts"] // 60 for b in gc], [0, 2, 5, 7])
check("clock keeps wall-clock slots", [b["ts"] // 60 for b in gl], [0, 2, 4, 6])
check("the two disagree after the gap",
      [b["ts"] // 60 for b in gc] != [b["ts"] // 60 for b in gl], True)
check("group takes first bar's timestamp", gc[1]["ts"], 120)
check("group high is max of members", gc[1]["high"], 105.0)
check("group close is last member's close", gc[1]["close"], 104.0)
check("group volume is the sum", gc[1]["volume"], 20)

print("\n[12] Calibration recovers the settings that generated a value")
import random
random.seed(3)
day = 86400 * 20000
src, px = [], 250.0
for i in range(600):
    hi = px + random.uniform(0.5, 4); lo = px - random.uniform(0.5, 4)
    cl = random.uniform(lo, hi)
    src.append({"ts": day + i * 60, "open": px, "high": hi, "low": lo,
                "close": cl, "volume": random.randint(100, 900)})
    px = cl
anc = day + 300 * 60
anch = lambda ts: anc if ts >= anc else day
hhmm = lambda ts: f"{(ts % 86400) // 3600:02d}:{(ts % 3600) // 60:02d}"

truth = compute_series(aggregate(src, 2, "count", anch), anc, "hlc3", 14, "wilder")
tgt = [r for r in truth if r["ts"] >= anc and r["vwap"] is not None][10]
ref = {"time": hhmm(tgt["ts"]), "close": round(tgt["close"], 2),
       "vwap": round(tgt["vwap"], 2), "atr": round(tgt["atr"], 2)}
ranked, checks = calibrate(src, anc, [ref], anch, to_hhmm=hhmm)
best = ranked[0]
check("recovers rollup mode", best["mode"], "count")
check("recovers VWAP field", best["field"], "hlc3")
check("recovers ATR method", best["method"], "wilder")
check("best error is negligible", best["score"] < 0.02, True)
check("candle check ran for both modes", len(checks), 2)
check("the correct mode matches the close",
      any(c["close_matches"] == 1 for c in checks if c["mode"] == "count"), True)
print(f"        best={best['mode']}/{best['field']}/{best['method']}  "
      f"vwap_err={best['vwap_err']:.4f}  atr_err={best['atr_err']:.4f}")
check("wrong settings score worse", ranked[-1]["score"] > best["score"], True)

print("\n" + ("ALL TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
