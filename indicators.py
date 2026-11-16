#!/usr/bin/env python3
"""
indicators.py — VWAP and ATR, implemented to ChartIQ's published definitions.

Kite's charts are ChartIQ, so ChartIQ's "Built-in Studies Reference Guide" is
the reference implementation we must match:
https://documentation.chartiq.com/tutorial-Using%20and%20Customizing%20Studies%20-%20Definitions.html

────────────────────────────────────────────────────────────────────────────
VWAP  (ChartIQ: Volume Weighted Average Price)

    VWAP(i) = cumulative(price(i) * volume(i)) / cumulative(volume(i))

  * price(i) is the selected Field. ChartIQ's default is HLC3 ("the mean of
    the high, low, and close price"). The client's chart is set to OHLC4, so
    that is our default here — but the field is configurable, because his two
    panes were set differently (hlc3 on one, ohlc4 on the other).
  * volume(i) is the volume OF THAT BAR, not the running day total.
  * Accumulates from the session open (09:15) and resets each session.

────────────────────────────────────────────────────────────────────────────
ATR  (ChartIQ: Average True Range)

    TR(i)  = max(High(i), Close(i-1)) - min(Low(i), Close(i-1))
    ATR(i) = (ATR(i-1) * (N-1) + TR(i)) / N          <- Wilder recursion

  ChartIQ's page lists "Formula: ATR = SMA(True Range, n-period)" in prose but
  its Math block gives the Wilder recursion above; the Math block is what the
  library actually computes, so that is what we implement. `method="sma"` is
  provided so reconciliation can test the other reading if needed.

  IMPORTANT — Wilder ATR has infinite memory. ChartIQ notes that its studies
  "use all the data loaded into the chart... As the chart is compressed or
  panned it will load more data forcing the study to recalculate and will
  produce different values." The seed influence decays as (1-1/N)^k, so with
  N=14 you need roughly 200 prior bars before the starting point stops
  mattering (13/14)^200 = 5e-7. Feed the ATR plenty of history or it will not
  match the terminal.
"""

from typing import Optional, List, Dict, Sequence, Tuple

FIELDS = ("ohlc4", "hlc3", "hl2", "close", "open", "high", "low")
AGG_MODES = ("count", "clock")


def price_field(o: float, h: float, l: float, c: float, field: str = "ohlc4") -> float:
    """The Field parameter, matching the labels Kite shows on the study."""
    f = (field or "ohlc4").lower()
    if f == "ohlc4":  return (o + h + l + c) / 4.0
    if f == "hlc3":   return (h + l + c) / 3.0
    if f == "hl2":    return (h + l) / 2.0
    if f == "close":  return c
    if f == "open":   return o
    if f == "high":   return h
    if f == "low":    return l
    raise ValueError(f"Unknown VWAP field: {field!r} (expected one of {FIELDS})")


def true_range(h: float, l: float, prev_close: Optional[float]) -> float:
    """
    ChartIQ: TR(i) = max(High(i), Close(i-1)) - min(Low(i), Close(i-1))

    Algebraically identical to the textbook
    max(H-L, |H-Cprev|, |L-Cprev|); on the very first bar, where there is no
    previous close, it degenerates to High - Low.
    """
    if prev_close is None:
        return h - l
    return max(h, prev_close) - min(l, prev_close)


class SessionVWAP:
    """
    Session VWAP, ChartIQ definition. Reset at 09:15 every day.

    update() takes the volume OF THAT BAR. If your feed gives you a running
    day total instead (Dhan's quote packet does), convert it before calling —
    CandleEngine does this once, at the point the candle is built, so that
    there is exactly one place in the system where the two conventions meet.
    """

    __slots__ = ("field", "pv", "vol", "value", "bars")

    def __init__(self, field: str = "ohlc4"):
        if (field or "ohlc4").lower() not in FIELDS:
            raise ValueError(f"Unknown VWAP field: {field!r}")
        self.field = (field or "ohlc4").lower()
        self.pv = 0.0
        self.vol = 0.0
        self.value: Optional[float] = None
        self.bars = 0

    def reset(self):
        self.pv = 0.0
        self.vol = 0.0
        self.value = None
        self.bars = 0

    def update(self, o: float, h: float, l: float, c: float,
               volume: float) -> Optional[float]:
        """Add one CLOSED bar. `volume` is that bar's own traded volume."""
        self.bars += 1
        v = float(volume)
        if v <= 0:
            # No trades in the bar contributes nothing to either sum. The line
            # holds its previous value, which is what the chart draws.
            return self.value
        self.pv += price_field(o, h, l, c, self.field) * v
        self.vol += v
        self.value = self.pv / self.vol
        return self.value


class WilderATR:
    """
    ATR(14) to the ChartIQ Math block.

    method="wilder"  ATR(i) = (ATR(i-1)*(N-1) + TR(i)) / N     [default]
    method="sma"     ATR(i) = mean of the last N true ranges

    `continuous=True` (our case) means the series is never reset at the
    session boundary, so TR on the first bar of the day is measured against
    yesterday's last close and therefore includes the overnight gap. That is
    what the terminal draws and what the client asked for.
    """

    __slots__ = ("period", "method", "prev_close", "_seed", "_win",
                 "value", "bars")

    def __init__(self, period: int = 14, method: str = "wilder"):
        if period < 1:
            raise ValueError("ATR period must be >= 1")
        if method not in ("wilder", "sma"):
            raise ValueError("ATR method must be 'wilder' or 'sma'")
        self.period = period
        self.method = method
        self.prev_close: Optional[float] = None
        self._seed: List[float] = []
        self._win: List[float] = []
        self.value: Optional[float] = None
        self.bars = 0

    def update(self, h: float, l: float, c: float) -> Optional[float]:
        tr = true_range(h, l, self.prev_close)
        self.prev_close = c
        self.bars += 1

        if self.method == "sma":
            self._win.append(tr)
            if len(self._win) > self.period:
                self._win.pop(0)
            if len(self._win) == self.period:
                self.value = sum(self._win) / self.period
            return self.value

        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value

    def seed(self, candles: Sequence[dict]) -> Optional[float]:
        """Warm up from prior bars. Feed >= 200 for the seed to stop mattering."""
        for c in candles:
            self.update(c["high"], c["low"], c["close"])
        return self.value

    def convergence_error(self) -> float:
        """
        Residual influence of the seeding choice, (1-1/N)^bars.

        Below ~1e-4 the starting point is irrelevant and our ATR will agree
        with the terminal regardless of where its own history began. Log this
        at start-up; if it is large, we have not loaded enough history.
        """
        if self.value is None:
            return 1.0
        return (1.0 - 1.0 / self.period) ** self.bars


def compute_series(candles: Sequence[dict], session_anchor: int,
                   vwap_field: str = "ohlc4", atr_period: int = 14,
                   atr_method: str = "wilder") -> List[dict]:
    """
    Run both studies over a list of 2-minute candles and return one row per
    bar. Used by reconcile.py and by the engine's replay path, so live and
    offline always agree by construction.

    Each candle needs: ts, open, high, low, close, volume  (volume = that
    bar's own volume). Bars before `session_anchor` warm the ATR only; VWAP
    starts accumulating at the anchor.
    """
    vwap = SessionVWAP(vwap_field)
    atr = WilderATR(atr_period, atr_method)
    out = []
    for c in candles:
        a = atr.update(c["high"], c["low"], c["close"])
        v = None
        if c["ts"] >= session_anchor:
            v = vwap.update(c["open"], c["high"], c["low"], c["close"],
                            c.get("volume", 0.0))
        out.append({**c, "vwap": v, "atr": a,
                    "atr_bars": atr.bars, "vwap_bars": vwap.bars})
    return out


# ═══════════════════════════════════════════════════════════════════════════
# AGGREGATION — 1-minute source bars into N-minute chart bars
# ═══════════════════════════════════════════════════════════════════════════
#
# Kite has no native 2-minute feed. Zerodha serves 1-minute bars and ChartIQ
# rolls them up client-side. How it rolls them up matters enormously on an
# instrument whose feed has gaps, which every option has.
#
# From ChartIQ's own documentation:
#
#   "period describes the number of raw ticks from masterData to roll-up
#    together into one data point on the chart"
#   "if masterData contains bars of 1 minute periodicity but the chart is set
#    to 3 minute periodicity, the dataSet will be 1/3 the length"
#   "Aggregation is done by systematically picking the first element in each
#    periodicity range and tracking High, Low, Volume and Close"
#   "Chart data can contain gaps... By default, the library will collapse
#    these gaps"
#
# So ChartIQ counts BARS, not clock time: gaps are collapsed, then surviving
# bars are grouped in twos and the group takes the FIRST bar's timestamp.
#
#   mode="count"  bar-count grouping    <- what Kite does
#   mode="clock"  wall-clock buckets    <- the intuitive reading, and wrong
#
# With no missing minutes the two are identical. One missing minute desyncs
# them permanently: every later candle is built from different source minutes,
# so OHLC, volume, VWAP and ATR all diverge and never re-sync.


def aggregate(candles_1m: Sequence[dict], period: int = 2,
              mode: str = "count", anchor_of=None) -> List[dict]:
    """
    Roll 1-minute bars into `period`-minute bars.

    mode="count": group every `period` surviving bars, label with the first
                  bar's timestamp. Matches ChartIQ/Kite.
    mode="clock": group by wall-clock bucket relative to the session anchor.
                  `anchor_of(ts) -> session anchor epoch` is required.
    """
    if mode not in AGG_MODES:
        raise ValueError(f"mode must be one of {AGG_MODES}")
    src = [c for c in candles_1m if c.get("ts") is not None]
    src.sort(key=lambda c: c["ts"])
    if not src:
        return []

    if mode == "count":
        out = []
        # Restart the grouping each session, so a new day always begins a new
        # bar rather than pairing with yesterday's last minute.
        day_of = (lambda ts: anchor_of(ts)) if anchor_of else (lambda ts: ts // 86400)
        run: List[dict] = []
        cur_day = None
        for c in src:
            d = day_of(c["ts"])
            if cur_day is None:
                cur_day = d
            if d != cur_day:
                out += _group_by_count(run, period)
                run, cur_day = [], d
            run.append(c)
        out += _group_by_count(run, period)
        return out

    if anchor_of is None:
        raise ValueError("mode='clock' needs anchor_of")
    step = period * 60
    buckets: Dict[int, dict] = {}
    for c in src:
        a = anchor_of(c["ts"])
        key = a + ((c["ts"] - a) // step) * step
        _merge(buckets, key, c)
    return [buckets[k] for k in sorted(buckets)]


def _group_by_count(run: List[dict], period: int) -> List[dict]:
    out = []
    for i in range(0, len(run), period):
        chunk = run[i:i + period]
        b = {"ts": chunk[0]["ts"], "open": chunk[0]["open"],
             "high": chunk[0]["high"], "low": chunk[0]["low"],
             "close": chunk[-1]["close"],
             "volume": sum(x.get("volume", 0.0) for x in chunk)}
        for x in chunk[1:]:
            b["high"] = max(b["high"], x["high"])
            b["low"] = min(b["low"], x["low"])
        out.append(b)
    return out


def _merge(buckets: Dict[int, dict], key: int, c: dict):
    b = buckets.get(key)
    if b is None:
        buckets[key] = {"ts": key, "open": c["open"], "high": c["high"],
                        "low": c["low"], "close": c["close"],
                        "volume": c.get("volume", 0.0)}
    else:
        b["high"] = max(b["high"], c["high"])
        b["low"] = min(b["low"], c["low"])
        b["close"] = c["close"]
        b["volume"] += c.get("volume", 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# CALIBRATION — find the settings that reproduce the terminal
# ═══════════════════════════════════════════════════════════════════════════

def calibrate(candles_1m: Sequence[dict], anchor: int, refs: Sequence[dict],
              anchor_of, period: int = 2, atr_period: int = 14,
              fields: Sequence[str] = ("ohlc4", "hlc3", "hl2", "close"),
              methods: Sequence[str] = ("wilder", "sma"),
              modes: Sequence[str] = AGG_MODES,
              to_hhmm=None) -> Tuple[List[dict], List[dict]]:
    """
    Brute-force every combination of aggregation mode, VWAP field and ATR
    method, and rank them against values read off the terminal.

    `refs` are what the chart shows, e.g.
        [{"time": "10:07", "close": 503.85, "vwap": 497.61, "atr": 33.69}]
    Only the keys present are scored, so close-only refs are fine.

    Returns (ranked_combinations, per_mode_candle_check).

    The candle check comes first and matters most: if no aggregation mode
    reproduces the CLOSE the terminal shows, the source bars themselves differ
    and no choice of formula will ever agree. That is a data-vendor problem,
    not a maths problem, and it is the one thing worth knowing before tuning
    anything else.
    """
    to_hhmm = to_hhmm or (lambda ts: "")

    candle_check = []
    series_cache = {}
    for mode in modes:
        bars = aggregate(candles_1m, period, mode, anchor_of)
        series_cache[mode] = bars
        sess = [b for b in bars if b["ts"] >= anchor]
        hits, tot, worst = 0, 0, 0.0
        for r in refs:
            if "close" not in r:
                continue
            tot += 1
            m = next((b for b in sess if to_hhmm(b["ts"]) == r["time"]), None)
            if m is None:
                continue
            err = abs(m["close"] - r["close"])
            worst = max(worst, err)
            if err < 0.005:
                hits += 1
        candle_check.append({"mode": mode, "session_bars": len(sess),
                             "close_matches": hits, "close_refs": tot,
                             "worst_close_err": worst})

    ranked = []
    for mode in modes:
        bars = series_cache[mode]
        for field in fields:
            for method in methods:
                rows = compute_series(bars, anchor, field, atr_period, method)
                sess = [r for r in rows if r["ts"] >= anchor]
                ve, ae, n = 0.0, 0.0, 0
                vn = an = 0
                for r in refs:
                    m = next((b for b in sess if to_hhmm(b["ts"]) == r["time"]), None)
                    if m is None:
                        continue
                    n += 1
                    if r.get("vwap") is not None and m["vwap"] is not None:
                        ve += abs(m["vwap"] - r["vwap"]); vn += 1
                    if r.get("atr") is not None and m["atr"] is not None:
                        ae += abs(m["atr"] - r["atr"]); an += 1
                ranked.append({
                    "mode": mode, "field": field, "method": method,
                    "matched": n,
                    "vwap_err": (ve / vn) if vn else None,
                    "atr_err": (ae / an) if an else None,
                    "score": (ve / vn if vn else 0) + (ae / an if an else 0)
                             + (0 if n else 1e9)})
    ranked.sort(key=lambda x: x["score"])
    return ranked, candle_check
