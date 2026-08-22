"""Offline logic tests — drives the state machine with synthetic candles/ticks."""
import sys, logging
logging.disable(logging.INFO)

from engine import (SensexVWAPLadderEngine, StrategyConfig, Leg, LegState,
                    WilderATR, SessionVWAP, session_anchor_epoch, aggregate_2m)

ANCHOR = session_anchor_epoch()
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


def new_engine(**kw):
    cfg = StrategyConfig(paper_mode=True, **kw)
    e = SensexVWAPLadderEngine(cfg)
    e.lot_size = 20
    e.strike = 78300
    leg = e.legs["CE"]
    leg.sec_id = "999"; leg.strike = 78300; leg.label = "SENSEX 78300 CE"
    leg.atr = WilderATR(14)
    leg.atr.value = 10.0          # pre-seeded continuous ATR
    leg.atr.prev_close = 100.0
    leg.atr_ok = True
    e.by_sec["999"] = leg
    # Tests run outside market hours — neutralise the wall-clock fill gate so the
    # state machine itself is what is under test.
    e._fill_window_open = lambda: True
    return e, leg


def candle(i, o, h, l, c, cum_vol):
    return {"ts": ANCHOR + i * 120, "open": o, "high": h, "low": l,
            "close": c, "cum_vol": cum_vol}


def freeze_atr(leg, v):
    """Keep ATR fixed so expected ladder levels are exact."""
    leg.atr.update = lambda h, l, c: v
    leg.atr.value = v


# ── 1. basic signal -> fill -> T1 -> T2 -> trail out ────────────────────────
print("\n[1] entry, ladder, trailing exit")
e, leg = new_engine()
freeze_atr(leg, 10.0)
e._process_candle(leg, candle(0, 100, 102, 99, 100, 1000))   # vwap ~100.25
e._process_candle(leg, candle(1, 100, 101, 99, 99.5, 2000))
vwap = leg.vwap.value
e._process_candle(leg, candle(2, 100, 110, 100, 108, 3000))  # close > vwap -> ARM
check("armed after close above vwap", leg.state == LegState.ARMED, leg.state)
check("trigger = high + 0.20", abs(leg.trigger - 110.20) < 1e-6, leg.trigger)
check("stop = low - 1.00", abs(leg.pending_stop - 99.0) < 1e-6, leg.pending_stop)
check("eligibility consumed on placement", leg.eligible is False)

e._on_tick(leg, 110.50)
check("filled on trigger touch", leg.state == LegState.IN_TRADE, leg.state)
pos = leg.position
check("E = trigger not fill", abs(pos.E - 110.20) < 1e-6, pos.E)
check("initial stop carried", abs(pos.stop - 99.0) < 1e-6, pos.stop)

e._on_tick(leg, 130.30)   # T1 = 110.20 + 20 = 130.20
check("T1 latched", pos.highest_rung == 1, pos.highest_rung)
check("T1 sells one lot", pos.lots_open == 2, pos.lots_open)
check("stop -> E + 2", abs(pos.stop - 112.20) < 1e-6, pos.stop)

e._on_tick(leg, 150.50)   # T2 = 150.20
check("T2 latched", pos.highest_rung == 2, pos.highest_rung)
check("T2 sells one lot", pos.lots_open == 1, pos.lots_open)
check("stop -> T1", abs(pos.stop - 130.20) < 1e-6, pos.stop)

e._on_tick(leg, 170.50)   # T3 = 170.20, runner held
check("T3 no scale-out", pos.lots_open == 1, pos.lots_open)
check("stop -> T2", abs(pos.stop - 150.20) < 1e-6, pos.stop)

e._on_tick(leg, 150.00)   # trail hit
check("runner stopped out", leg.position is None)
check("leg stands down after a completed trade", leg.state == LegState.STOOD_DOWN,
      leg.state)
expected = (130.20 - 110.20) * 20 + (150.20 - 110.20) * 20 + (150.20 - 110.20) * 20
check("pnl matches ladder", abs(leg.pnl - expected) < 1e-6, f"{leg.pnl} vs {expected}")


# ── 2. no re-entry until a close below VWAP ────────────────────────────────
print("\n[2] stand-down: closes above the line are ignored")
e, leg = new_engine()
freeze_atr(leg, 5.0)
e._process_candle(leg, candle(0, 100, 101, 99, 100, 1000))
e._process_candle(leg, candle(1, 100, 101, 99, 99, 2000))
e._process_candle(leg, candle(2, 100, 105, 99, 104, 3000))   # ARM
check("armed", leg.state == LegState.ARMED)
trig = leg.trigger
e._process_candle(leg, candle(3, 104, 105.0, 103, 104.5, 4000))  # high < trigger
check("cancelled -> stood down", leg.state == LegState.STOOD_DOWN, leg.state)
check("still ineligible", leg.eligible is False)
a = leg.attempts
e._process_candle(leg, candle(4, 104, 108, 104, 107, 5000))   # above line, ignored
e._process_candle(leg, candle(5, 107, 110, 106, 109, 6000))   # above line, ignored
check("no new attempt while stood down", leg.attempts == a, leg.attempts)
check("still stood down", leg.state == LegState.STOOD_DOWN, leg.state)
below = leg.vwap.value - 5
e._process_candle(leg, candle(6, 109, 109, below - 2, below, 7000))  # close below
check("close below vwap resets", leg.eligible is True)
check("state back to idle", leg.state == LegState.IDLE, leg.state)
e._process_candle(leg, candle(7, below, below + 12, below, leg.vwap.value + 3, 8000))
check("new signal accepted after reset", leg.state == LegState.ARMED, leg.state)
check("attempt counter advanced", leg.attempts == a + 1)


# ── 3. fill candle closes below VWAP -> immediate exit ─────────────────────
print("\n[3] in and out on the same candle")
e, leg = new_engine()
freeze_atr(leg, 8.0)
e._process_candle(leg, candle(0, 100, 101, 99, 100, 1000))
e._process_candle(leg, candle(1, 100, 101, 99, 99, 2000))
e._process_candle(leg, candle(2, 100, 106, 99, 105, 3000))   # ARM, trigger 106.20
e._on_tick(leg, 106.50)
check("filled", leg.state == LegState.IN_TRADE)
vw = leg.vwap.value
e._process_candle(leg, candle(3, 106, 107, 98, vw - 3, 4000))  # closes below line
check("exited on the close", leg.position is None)
check("close below vwap also resets", leg.eligible is True)
check("state idle not stood down", leg.state == LegState.IDLE, leg.state)


# ── 4. entry cut-off at 14:45 ──────────────────────────────────────────────
print("\n[4] 14:45 cut-off")
e, leg = new_engine()
freeze_atr(leg, 5.0)
late = int((14 * 60 + 46 - 9 * 60 - 15) / 2)      # bucket index just after 14:45
e._process_candle(leg, candle(0, 100, 101, 99, 99, 1000))
e._process_candle(leg, candle(late, 100, 110, 99, 108, 2000))
check("no arm after cut-off", leg.state != LegState.ARMED, leg.state)
early = int((14 * 60 + 42 - 9 * 60 - 15) / 2)   # closes 14:44 -> allowed
e2, leg2 = new_engine()
freeze_atr(leg2, 5.0)
e2._process_candle(leg2, candle(0, 100, 101, 99, 99, 1000))
e2._process_candle(leg2, candle(early, 100, 110, 99, 108, 2000))
check("arm allowed when candle closes 14:44", leg2.state == LegState.ARMED, leg2.state)
e2._fill_window_open = lambda: False
e2._on_tick(leg2, 200.0)
check("fill refused past cut-off", leg2.position is None and leg2.state == LegState.STOOD_DOWN,
      leg2.state)


# ── 5. gap-through several rungs in one tick ───────────────────────────────
print("\n[5] gap-through")
e, leg = new_engine()
freeze_atr(leg, 10.0)
e._process_candle(leg, candle(0, 100, 101, 99, 100, 1000))
e._process_candle(leg, candle(1, 100, 101, 99, 99, 2000))
e._process_candle(leg, candle(2, 100, 110, 100, 108, 3000))
e._on_tick(leg, 110.30)
pos = leg.position
e._on_tick(leg, 175.00)   # blows through T1(130.2) T2(150.2) T3(170.2)
check("all crossed rungs latched", pos.highest_rung == 3, pos.highest_rung)
check("both scale-outs done", pos.lots_open == 1, pos.lots_open)
check("stop at rung below highest", abs(pos.stop - 150.20) < 1e-6, pos.stop)


# ── 6. VWAP is OHLC4 volume-weighted ───────────────────────────────────────
print("\n[6] VWAP maths")
v = SessionVWAP()
v.update(10, 20, 10, 20, 100)      # ohlc4 = 15, vol 100
v.update(20, 30, 20, 30, 300)      # ohlc4 = 25, vol 200
check("ohlc4 weighted", abs(v.value - (15 * 100 + 25 * 200) / 300) < 1e-9, v.value)
v.update(30, 30, 30, 30, 300)      # zero volume candle
check("zero-volume candle carried forward",
      abs(v.value - (15 * 100 + 25 * 200) / 300) < 1e-9, v.value)


# ── 7. Wilder ATR seeding ──────────────────────────────────────────────────
print("\n[7] Wilder ATR")
a = WilderATR(14)
for i in range(14):
    a.update(110, 100, 105)        # TR = 10 first bar, then max(10,5,5)=10
check("seed = SMA of first 14 TRs", abs(a.value - 10.0) < 1e-9, a.value)
a.update(130, 100, 120)            # TR = 30
check("wilder step", abs(a.value - (10 * 13 + 30) / 14) < 1e-9, a.value)


# ── 8. 2-minute aggregation is session-anchored ────────────────────────────
print("\n[8] session-anchored bucketing")
one_min = [{"ts": ANCHOR + i * 60, "open": 100 + i, "high": 105 + i,
            "low": 95 + i, "close": 100 + i, "volume": 10} for i in range(6)]
agg = aggregate_2m(one_min)
check("6 x 1min -> 3 x 2min", len(agg) == 3, len(agg))
check("first bucket starts at 09:15", agg[0]["ts"] == ANCHOR, agg[0]["ts"])
check("volumes summed", agg[0]["volume"] == 20, agg[0]["volume"])
check("high is max of pair", agg[0]["high"] == 106, agg[0]["high"])


print("\n" + ("ALL TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
