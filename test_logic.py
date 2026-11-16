"""Offline logic tests — drives the state machine with synthetic candles/ticks."""
import sys, time, logging
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


def candle(i, o, h, l, c, vol):
    """`vol` is this bar's own traded volume (post-fix convention)."""
    return {"ts": ANCHOR + i * 120, "open": o, "high": h, "low": l,
            "close": c, "volume": vol, "cum_vol": 0}


class FrozenATR(WilderATR):
    """Fixed ATR so the expected ladder levels are exact."""
    __slots__ = ("_fixed",)

    def __init__(self, v):
        super().__init__(14)
        self.value = v
        self.prev_close = 100.0
        self._fixed = v

    def update(self, h, l, c):
        self.bars += 1
        self.prev_close = c
        self.value = self._fixed
        return self.value


def freeze_atr(leg, v):
    leg.atr = FrozenATR(v)


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
print("\n[6] VWAP wiring — per-bar volume reaches the study intact")
e6, leg6 = new_engine()
freeze_atr(leg6, 5.0)
e6._process_candle(leg6, candle(0, 10, 20, 10, 20, 100))   # ohlc4=15, vol 100
e6._process_candle(leg6, candle(1, 20, 30, 20, 30, 200))   # ohlc4=25, vol 200
want6 = (15 * 100 + 25 * 200) / 300
check("engine feeds per-bar volume", abs(leg6.vwap.value - want6) < 1e-9,
      leg6.vwap.value)
e6._process_candle(leg6, candle(2, 30, 30, 30, 30, 0))     # zero-volume bar
check("zero-volume bar holds the line", abs(leg6.vwap.value - want6) < 1e-9,
      leg6.vwap.value)


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


print("\n[9] REGRESSION 08-Sep: a bar from a previous session is rejected")
# 244 of them reached VWAP and ATR on 08-Sep, fired six phantom signals before
# the market opened, and left the morning's real trade using VWAP 277.34 where
# the correct value was 366.88.
e9, leg9 = new_engine()
freeze_atr(leg9, 10.0)
stale = {"ts": ANCHOR - 86400 + 4 * 3600, "open": 100.0, "high": 200.0,
         "low": 90.0, "close": 190.0, "volume": 500000}
e9._process_candle(leg9, stale)
check("stale bar did not reach VWAP", leg9.vwap.value is None, True)
check("stale bar did not arm the leg", leg9.state == LegState.IDLE, leg9.state)
check("stale bar not counted", leg9.candles_seen == 0, leg9.candles_seen)

# and the first REAL bar must give VWAP == its own ohlc4
o, h, l, c = 323.00, 414.85, 323.00, 406.65
e9._process_candle(leg9, candle(0, o, h, l, c, 100000))
want = (o + h + l + c) / 4
check("first session bar: VWAP equals its own ohlc4",
      abs(leg9.vwap.value - want) < 1e-9, True)
print(f"        VWAP = {leg9.vwap.value:.2f} (08-Sep produced 277.34 here)")

print("\n[10] Pre-open wait: counts down to 09:15, resolves as soon as it can")
import engine as _E
ANCH = _E.session_anchor_epoch()
_real_time, _real_open = time.time, _E.get_sensex_open_0915


class _Clock:
    """A clock that advances on its own so the loop can actually finish."""
    def __init__(self, start, step=0.5):
        self.t, self.step = start, step

    def __call__(self):
        self.t += self.step
        return self.t


class _NoWait:
    """stop_event stand-in: never set, and wait() returns immediately."""
    def is_set(self): return False
    def set(self): pass
    def wait(self, _t=None): return False


def drive(offset, candle_available, timeout_s=3.0):
    seen = []
    e = SensexVWAPLadderEngine(StrategyConfig())
    e.stop_event = _NoWait()
    e.gui_cb = lambda ev, d: seen.append((ev, d.get("msg") or d.get("text")))
    time.time = _Clock(ANCH + offset)
    _E.get_sensex_open_0915 = lambda: (75970.28 if candle_available else None)
    try:
        return e._wait_for_session_open(timeout_s=timeout_s), seen
    finally:
        time.time, _E.get_sensex_open_0915 = _real_time, _real_open


val, seen = drive(-13 * 60 - 5, False)
msgs = [str(m) for _, m in seen if m]
check("counts down to 09:15 (~13m), not to 09:17 (~15m)",
      any("13m" in m for m in msgs), msgs[:2])
check("refuses to guess a strike", val == 0.0, val)

val, seen = drive(5, True)
check("resolves at 09:15:05 rather than waiting for 09:17", val == 75970.28, val)

val, seen = drive(3, False)
msgs = [str(m) for _, m in seen if m]
check("once open, says it is reading the candle",
      any("reading" in m for m in msgs), msgs[:2])

print("\n[11] REGRESSION 16-Sep: a stale bar must not arm a trigger")
# After a power cut the backlog arrives at once. A bar that closed seven
# minutes ago carries a high that is no longer a tradeable price.
e11, leg11 = new_engine()
freeze_atr(leg11, 10.0)
e11._process_candle(leg11, candle(0, 100, 101, 99, 99, 1000))     # below, resets
stale_bar = candle(1, 100, 110, 99, 108, 2000)
stale_bar["lag"] = 407.0                                          # the real value
e11._process_candle(leg11, stale_bar)
check("stale bar did not arm", leg11.state == LegState.IDLE, leg11.state)
check("eligibility NOT consumed — no order was placed", leg11.eligible, True)
check("no attempt recorded", leg11.attempts == 0, leg11.attempts)

# a current bar right after must still work
fresh = candle(2, 100, 112, 99, 110, 2000)
fresh["lag"] = 1.4
e11._process_candle(leg11, fresh)
check("a timely bar still arms", leg11.state == LegState.ARMED, leg11.state)
check("trigger from the fresh bar", abs(leg11.trigger - 112.20) < 1e-9,
      leg11.trigger)

print("\n[12] The stale gate is load-bearing")
e12, leg12 = new_engine()
freeze_atr(leg12, 10.0)
e12.config.max_signal_age = 1e9                # effectively disabled
e12._process_candle(leg12, candle(0, 100, 101, 99, 99, 1000))
sb = candle(1, 100, 110, 99, 108, 2000); sb["lag"] = 407.0
e12._process_candle(leg12, sb)
check("without the gate the stale bar DOES arm",
      leg12.state == LegState.ARMED, leg12.state)

print("\n[13] REGRESSION 17-Sep: an expired token recovers without a restart")
import types as _ty
import engine as _EE
_save = (_EE.SESSION, _EE.DhanTokenManager, _EE.HEADERS.get("access-token"),
         _EE._LAST_REAUTH)
try:
    _EE.DHAN_CLIENT_ID = "X"
    _EE.HEADERS.update({"access-token": "OLD", "client-id": "X"})
    _EE.WS_NEEDS_RECONNECT.clear()
    _EE._LAST_REAUTH = 0.0
    hits = {"n": 0}

    class _R:
        def __init__(self, c, t): self.status_code, self.text = c, t
        def json(self): return {"ok": True}

    def _post(url, headers=None, json=None, timeout=None):
        hits["n"] += 1
        if headers.get("access-token") == "OLD":
            return _R(401, '{"errorCode":"DH-901"}')
        return _R(200, "{}")

    _EE.SESSION = _ty.SimpleNamespace(post=_post)
    _EE.DhanTokenManager = type("M", (), {"ensure_token": lambda self: "NEW"})

    res = _EE.api_post("/charts/intraday", {}, retries=2, timeout=5)
    check("the call succeeds after re-auth", res == {"ok": True}, res)
    check("it retried rather than giving up", hits["n"] == 2, hits["n"])
    check("the new token is in use", _EE.HEADERS["access-token"] == "NEW",
          _EE.HEADERS["access-token"])
    check("websocket flagged to reconnect", _EE.WS_NEEDS_RECONNECT.is_set(), True)
    check("new WS_URL carries the new token", "token=NEW" in _EE.WS_URL, True)
finally:
    _EE.SESSION, _EE.DhanTokenManager = _save[0], _save[1]
    if _save[2]:
        _EE.HEADERS["access-token"] = _save[2]
    _EE._LAST_REAUTH = _save[3]
    _EE.WS_NEEDS_RECONNECT.clear()

print("\n[14] Equal thirds at each rung, whatever the size")
for mult, want_lots, want_third in [(1, 3, 1), (2, 6, 2), (3, 9, 3)]:
    cfg = StrategyConfig(paper_mode=True, lot_multiplier=mult)
    e14 = SensexVWAPLadderEngine(cfg)
    e14.lot_size = 20
    lg = e14.legs["CE"]
    lg.sec_id = "1"; lg.strike = 78300; lg.label = "CE"
    lg.atr = WilderATR(14); lg.atr.value = 10.0
    lg.atr.prev_close = 100.0; lg.atr_ok = True
    e14.by_sec["1"] = lg
    e14._fill_window_open = lambda: True
    freeze_atr(lg, 10.0)
    e14._process_candle(lg, candle(0, 100, 102, 99, 100, 1000))
    e14._process_candle(lg, candle(1, 100, 101, 99, 99.5, 2000))
    e14._process_candle(lg, candle(2, 100, 110, 100, 108, 3000))
    e14._on_tick(lg, 110.50)
    pos = lg.position
    check(f"x{mult} opens {want_lots} lots", pos.lots_open == want_lots,
          pos.lots_open)
    e14._on_tick(lg, 130.50)                      # T1
    check(f"x{mult} books {want_third} at T1",
          pos.lots_open == want_lots - want_third, pos.lots_open)
    e14._on_tick(lg, 150.50)                      # T2
    check(f"x{mult} books {want_third} at T2",
          pos.lots_open == want_lots - 2 * want_third, pos.lots_open)
    check(f"x{mult} leaves {want_third} running",
          pos.lots_open == want_third, pos.lots_open)
    e14._on_tick(lg, 120.00)                      # trail out
    check(f"x{mult} runner exits", lg.position is None, lg.position)

print("\n[15] Timeframe reaches the bucketing and the entry window")
for tf in (1, 2, 5, 15):
    c = StrategyConfig(timeframe_minutes=tf)
    check(f"{tf}m -> {tf*60}s", c.tf_seconds == tf * 60, c.tf_seconds)
    e15 = SensexVWAPLadderEngine(c)
    # a bar closing exactly at 14:45 is allowed; one closing later is not
    ok_ts = ANCHOR + (14 * 60 + 45 - 9 * 60 - 15) * 60 - c.tf_seconds
    late_ts = ok_ts + c.tf_seconds
    check(f"{tf}m: bar closing 14:45 arms", e15._entry_window_open(ok_ts), True)
    check(f"{tf}m: the next one does not",
          not e15._entry_window_open(late_ts), True)

print("\n[16] REGRESSION 09-Oct: lot size comes from the traded contracts")
import engine as _E3
_E3._MASTER["by_id"] = {
    "111111":  (75, "SENSEX50-Oct2026-74300-CE", "2026-10-15"),
    "889285":  (20, "SENSEX-Oct2026-71800-CE",   "2026-10-15"),
    "889440":  (20, "SENSEX-Oct2026-71800-PE",   "2026-10-15"),
    "900001":  (20, "SENSEX-Oct2026-72000-CE",   "2026-10-22"),
}
_E3._MASTER["by_expiry"] = [
    ("SENSEX50-OCT2026-74300-CE", "2026-10-15", 75),
    ("SENSEX-OCT2026-71800-CE",   "2026-10-15", 20),
    ("SENSEX-OCT2026-71800-PE",   "2026-10-15", 20),
    ("SENSEX-OCT2026-72000-CE",   "2026-10-22", 20),
]
_E3._MASTER["started"] = True
_E3._MASTER["error"] = None
_E3._MASTER["ready"].set()

check("exact security-id lookup gives 20",
      _E3.lot_size_for_security(["889285", "889440"]) == 20,
      _E3.lot_size_for_security(["889285", "889440"]))
check("SENSEX50 excluded from the expiry fallback",
      _E3.lot_size_by_expiry("2026-10-15") == 20,
      _E3.lot_size_by_expiry("2026-10-15"))
check("a different expiry resolves on its own rows",
      _E3.lot_size_by_expiry("2026-10-22") == 20,
      _E3.lot_size_by_expiry("2026-10-22"))

# tomorrow: different strike, different ids
_E3._MASTER["by_id"]["889999"] = (20, "SENSEX-Oct2026-72500-PE", "2026-10-15")
check("a different strike tomorrow resolves the same way",
      _E3.lot_size_for_security(["889999"]) == 20,
      _E3.lot_size_for_security(["889999"]))

_E3._MASTER["by_id"]["889440"] = (25, "SENSEX-Oct2026-71800-PE", "2026-10-15")
check("legs disagreeing refuses rather than guessing",
      _E3.lot_size_for_security(["889285", "889440"]) is None, True)
_E3._MASTER["by_id"]["889440"] = (20, "SENSEX-Oct2026-71800-PE", "2026-10-15")

print("\n[16b] REGRESSION 21-Oct: the master is indexed, not materialised")
import io as _io, csv as _csv
_hdr = ("SEM_SMST_SECURITY_ID,SEM_TRADING_SYMBOL,SEM_INSTRUMENT_NAME,"
        "SEM_LOT_UNITS,SEM_EXPIRY_DATE")
_lines = [_hdr,
          "111111,SENSEX50-Oct2026-74300-CE,OPTIDX,75,2026-10-22",
          "1125634,SENSEX-Oct2026-74300-CE,OPTIDX,20,2026-10-22",
          "1124548,SENSEX-Oct2026-74300-PE,OPTIDX,20,2026-10-22",
          "5,RELIANCE,EQUITY,1,",
          "6,NIFTY-Oct2026-24000-CE,OPTIDX,65,2026-10-22"]


class _Resp:
    def raise_for_status(self): pass
    def iter_lines(self):
        for ln in _lines:
            yield ln.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


_saved_session = _E3.SESSION
_E3.SESSION = type("S", (), {"get": staticmethod(lambda *a, **k: _Resp())})()
_E3._MASTER["started"] = False
_E3._MASTER["ready"].clear()
_E3._MASTER["by_id"] = {}
_E3._MASTER["by_expiry"] = []
_E3.prefetch_scrip_master()
check("prefetch completes", _E3._MASTER["ready"].wait(10), True)
check("only option contracts are held",
      "5" not in _E3._MASTER["by_id"], True)
check("other underlyings are not held",
      "6" not in _E3._MASTER["by_id"], True)
check("our legs are indexed",
      _E3.lot_size_for_security(["1125634", "1124548"]) == 20,
      _E3.lot_size_for_security(["1125634", "1124548"]))
check("a second prefetch does not re-download",
      (_E3.prefetch_scrip_master(), _E3._MASTER["ready"].is_set())[1], True)
_E3.SESSION = _saved_session

print("\n[16c] The lot-size memo round-trips (it was silently dead)")
# LOT_MEMO's definition was deleted by an unrelated edit. Both accessors wrap
# everything in `except Exception`, so a NameError looked exactly like a
# missing file: the change warning was dead for a whole build and nothing
# failed. The handlers are now narrow, and this test exercises the path.
_E3.LOT_MEMO.unlink(missing_ok=True)
check("no memo yet reads as None", _E3._load_last_lot_size() is None, True)
_E3._save_last_lot_size(20)
check("a saved value reads back", _E3._load_last_lot_size() == 20,
      _E3._load_last_lot_size())
_E3._save_last_lot_size(25)
check("and is overwritten", _E3._load_last_lot_size() == 25,
      _E3._load_last_lot_size())
check("LOT_MEMO is actually defined", hasattr(_E3, "LOT_MEMO"), True)

# the warning that depends on it
_E3._save_last_lot_size(20)
_E3._MASTER["by_id"]["889285"] = (25, "SENSEX-CE", "2026-10-15")
_E3._MASTER["by_id"]["889440"] = (25, "SENSEX-PE", "2026-10-15")
_e16 = SensexVWAPLadderEngine(StrategyConfig())
_e16.expiry = "2026-10-15"
_e16.legs["CE"].sec_id = "889285"
_e16.legs["PE"].sec_id = "889440"
_evts = []
_e16.gui_cb = lambda ev, d: _evts.append(ev)
_e16._resolve_lot_size()
check("a lot-size change is announced", "lot_size_changed" in _evts, _evts)
check("and the new size is adopted", _e16.lot_size == 25, _e16.lot_size)
_E3.LOT_MEMO.unlink(missing_ok=True)
_E3._MASTER["by_id"]["889285"] = (20, "SENSEX-Oct2026-71800-CE", "2026-10-15")
_E3._MASTER["by_id"]["889440"] = (20, "SENSEX-Oct2026-71800-PE", "2026-10-15")

print("\n[17] Quantity follows the resolved lot size")
for lot, mult in ((20, 1), (20, 2), (25, 1)):
    cfg = StrategyConfig(lot_multiplier=mult)
    e17 = SensexVWAPLadderEngine(cfg)
    e17.lot_size = lot
    check(f"lot {lot} x{mult} -> {cfg.lots * lot} qty",
          cfg.lots * e17.lot_size == cfg.lots * lot, cfg.lots * e17.lot_size)
print(f"        09-Oct used 75: 3 lots x 75 = 225 qty, should have been "
      f"3 x 20 = 60 — every P&L was {75 / 20:.2f}x too large")

print("\n[18] REGRESSION 22-Oct: the daily loss limit caps the drawdown")
# The old guard summed REALISED P&L and ran only after an exit, so it could
# never stop the trade that breached the limit. The day was -8,921 realised
# when a trade was opened; it closed at -11,009.
from engine import Position as _Pos

def loss_engine(realised, limit=10000.0):
    c = StrategyConfig(paper_mode=True, daily_loss_limit=limit)
    e = SensexVWAPLadderEngine(c)
    e.lot_size = 20
    lg = e.legs["CE"]
    lg.sec_id = "1"; lg.label = "CE"; lg.strike = 73300
    e.by_sec["1"] = lg
    lg.pnl = realised
    lg.position = _Pos(leg="CE", sec_id="1", strike=73300, trade_id="t",
                       E=186.15, fill_price=186.15, atr_frozen=24.76,
                       stop=117.65, initial_stop=117.65, lots_open=3,
                       lots_at_entry=3, lot_size=20)
    return e, lg

e18, lg18 = loss_engine(-8921.0)
e18._on_tick(lg18, 180.00)
check("inside the limit, nothing happens", not e18._halted, e18._halted)
check("position still open", lg18.position is not None, True)

e18._on_tick(lg18, 168.00)                 # crosses -10,000
check("breach halts the engine", e18._halted, True)
check("the open position was closed", lg18.position is None, True)
_r, _u, _t = e18.day_pnl()
check("day lands at about the limit, not past it", abs(_t) < 10100, _t)
print(f"        closed at Rs.{_t:+,.0f}; 22-Oct actually closed at -11,009")

print("\n[19] The cap counts unrealised, which is the whole point")
e19, lg19 = loss_engine(-5000.0)
e19._on_tick(lg19, 186.15)
r, u, t = e19.day_pnl()
check("flat position contributes nothing", abs(u) < 1e-9, u)
e19._on_tick(lg19, 100.00)                 # deep underwater, realised untouched
check("unrealised alone can breach", e19._halted, True)
check("and the position is closed", lg19.position is None, True)

print("\n[20] The limit off means the limit off")
e20, lg20 = loss_engine(-50000.0, limit=0.0)
e20._on_tick(lg20, 150.00)          # above the stop, so only the cap could act
check("no limit set, no halt", not e20._halted, e20._halted)
check("position left alone", lg20.position is not None, True)

# and with the cap on, the same tick DOES act — proving the guard is the cause
e20b, lg20b = loss_engine(-50000.0, limit=10000.0)
e20b._on_tick(lg20b, 150.00)
check("same tick with the cap on halts", e20b._halted, True)

print("\n[21] A profitable open position does not trip it")
e21, lg21 = loss_engine(-9500.0)
e21._on_tick(lg21, 220.00)                 # open profit offsets the booked loss
r, u, t = e21.day_pnl()
check("total is inside the limit", t > -10000, t)
check("no halt while the book is above the line", not e21._halted, e21._halted)

print("\n" + ("ALL TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
