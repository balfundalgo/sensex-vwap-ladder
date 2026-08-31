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

from typing import Optional, List, Dict, Sequence

FIELDS = ("ohlc4", "hlc3", "hl2", "close", "open", "high", "low")


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
