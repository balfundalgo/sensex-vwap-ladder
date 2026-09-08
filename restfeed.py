#!/usr/bin/env python3
"""
restfeed.py — candles from the REST historical API instead of from ticks.

Why this exists
───────────────
The 2-minute candles, VWAP and ATR produced by the REST path were reconciled
against the broker's chart and matched — under two different VWAP fields, which
is strong evidence the maths is right rather than coincidentally tuned. Candles
built from websocket ticks are a second, unverified implementation of the same
thing. Rather than maintain both, the strategy now takes its candles from REST.

What this buys us
─────────────────
* Signals come from the exact code path that was verified against the chart.
* The websocket drops to Ticker mode (LTP only), so `parse_quote()` — whose
  byte layout was never confirmed against a live BFO feed — leaves the critical
  path entirely. Volume now only ever comes from REST, where it was verified.

What it cannot do
─────────────────
REST cannot see inside a candle. The entry trigger, the stop and the ladder
rungs are all evaluated on traded price (spec 5 and 6), so those stay on the
websocket LTP stream. The split is:

    REST       candles, volume, VWAP, ATR, signal generation
    WebSocket  LTP only, for the trigger / stop / rungs

The cost
────────
A signal is only as timely as the API. Every emitted bar records how long after
its close it actually became available, and that is logged and surfaced, so the
latency is measured continuously in production rather than assumed. If it drifts
above the point where the one-candle order window is compromised, the log says
so rather than the trades quietly getting worse.
"""

import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Dict, Callable, Optional, List

log = logging.getLogger("SNX")


class RestCandleFeed:
    """
    Polls the 1-minute endpoint, rolls up to `period`-minute bars, and calls
    `on_bar(leg_name, bar)` once for each newly completed bar, in order.

    A rolled-up bar is only emitted when it is genuinely finished:

      * its group holds a full `period` source minutes, or
      * the wall clock has passed the bar's close by `grace` seconds

    The second rule matters on an illiquid leg, where the next minute may not
    trade for a while and a bar would otherwise never be declared complete.
    """

    def __init__(self, legs: Dict[str, str], fetch_1m: Callable,
                 aggregate_fn: Callable, anchor_of: Callable,
                 on_bar: Callable, period: int = 2, poll: float = 4.0,
                 grace: float = 6.0, agg_mode: str = "count", days: int = 10,
                 on_latency: Optional[Callable] = None,
                 session_anchor: Optional[int] = None):
        self.legs = dict(legs)                  # {"CE": sec_id, "PE": sec_id}
        self.fetch_1m = fetch_1m
        self.aggregate = aggregate_fn
        self.anchor_of = anchor_of
        self.on_bar = on_bar
        self.on_latency = on_latency
        self.period = period
        self.poll = poll
        self.grace = grace
        self.agg_mode = agg_mode
        self.days = days
        # Nothing before today's 09:15 may ever reach the strategy.
        #
        # Learned the hard way on 08-Sep: the app was started at 08:54, before
        # the market opened. The first poll returned 244 bars from the PREVIOUS
        # session, and every one was emitted as a live bar — 488 warnings, six
        # phantom signals fired before the open, and VWAP/ATR permanently
        # polluted with yesterday's data. The trade taken that morning used a
        # VWAP of 277.34 where the correct value was 366.88.
        self.session_anchor = int(session_anchor) if session_anchor else 0

        self._last_ts: Dict[str, int] = {}      # last bar emitted, per leg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.latencies: List[float] = []
        self.polls = 0
        self.errors = 0
        self.dropped_forming = 0
        self.dropped_stale = 0
        self._warned_at = 0.0
        self._suppressed = 0
        self.late_bars = 0
        self.stalls = 0
        self.last_stall = 0.0
        self.stall_seconds = 60.0
        # A socket-level timeout is not a guarantee: on 08-Sep a call sat for
        # 1249 seconds on a dead keep-alive connection despite a 12s read
        # timeout. The poll therefore runs in a worker with a hard deadline —
        # if it overruns we abandon it and carry on rather than going blind.
        self.hard_deadline = 25.0
        self._pool = ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="restpoll")
        self._abandoned = 0

    # ─── lifecycle ───

    def prime(self, leg: str, upto_ts: int):
        """
        Mark everything at or before `upto_ts` as already handled, so the
        replay done during seeding is not emitted a second time.
        """
        self._last_ts[leg] = max(self._last_ts.get(leg, 0), int(upto_ts))

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info(f"  REST candle feed started — polling every {self.poll:.0f}s, "
                 f"{self.period}m bars, rollup={self.agg_mode}")

    def stop(self):
        self._stop.set()
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass

    # ─── internals ───

    def _loop(self):
        """
        Poll each leg in turn.

        A single call that blocks takes the whole feed with it — on 08-Sep one
        /charts/intraday call hung for 1249 seconds on a dropped connection and
        the strategy went blind for 20 minutes with nothing in the log to say
        so. The engine cannot force a socket to return, but it can notice and
        say so loudly, and it can re-sync rather than silently replaying a
        backlog as though it were live.
        """
        while not self._stop.is_set():
            for leg, sec in self.legs.items():
                if self._stop.is_set():
                    break
                t0 = time.time()
                try:
                    fut = self._pool.submit(self._poll_leg, leg, sec)
                    fut.result(timeout=self.hard_deadline)
                except FutureTimeout:
                    self._abandoned += 1
                    self.errors += 1
                    log.error(f"  [{leg}] poll exceeded {self.hard_deadline:.0f}s "
                              f"and was abandoned — carrying on rather than "
                              f"blocking the feed. The orphaned request will "
                              f"expire on its own.")
                except Exception as e:
                    self.errors += 1
                    log.error(f"  [{leg}] REST poll error: {e}")
                took = time.time() - t0
                if took > self.stall_seconds:
                    self.stalls += 1
                    self.last_stall = took
                    log.error(f"  [{leg}] poll BLOCKED for {took:.0f}s — the "
                              f"strategy was blind for that period. "
                              f"({self.stalls} stall(s) today)")
            self._stop.wait(self.poll)

    @staticmethod
    def drop_forming(raw: List[dict], now: float, minute: int = 60) -> List[dict]:
        """
        Remove the candle that is still being built.

        Dhan's intraday endpoint returns the CURRENT minute as soon as it
        starts, and keeps updating it. Measured: the bar labelled 15:05 was
        already present at 15:05:02. Its high, low, close and volume are all
        still moving.

        That is fine for a chart and fatal for us. A 2-minute bar is two
        1-minute bars, so a group can look "complete" while its second member
        is two seconds old — and we would compute VWAP, ATR and a signal from
        a candle that has not happened yet. Every bar whose close is still in
        the future is dropped here, once, before anything else sees it.
        """
        return [c for c in raw if int(c["ts"]) + minute <= now]

    def _poll_leg(self, leg: str, sec: str):
        raw = self.fetch_1m(sec, days=1)
        self.polls += 1
        if not raw:
            return

        now_pre = time.time()
        before = len(raw)
        raw = self.drop_forming(raw, now_pre)
        if before and not raw:
            return
        self.dropped_forming += (before - len(raw))

        bars = self.aggregate(raw, self.period, self.agg_mode, self.anchor_of)
        if not bars:
            return

        now = time.time()
        last = self._last_ts.get(leg, 0)
        span = self.period * 60

        # Count source minutes per rolled-up bar so a partial final group is
        # not mistaken for a finished bar.
        members = self._members_per_bar(raw, bars)

        for i, b in enumerate(bars):
            if b["ts"] <= last:
                continue
            if self.session_anchor and b["ts"] < self.session_anchor:
                self.dropped_stale += 1
                continue
            closed_at = b["ts"] + span
            complete = members[i] >= self.period or now >= closed_at + self.grace
            if not complete:
                continue

            lag = now - closed_at
            self.latencies.append(lag)
            if self.on_latency:
                try:
                    self.on_latency(leg, b["ts"], lag)
                except Exception:
                    pass
            if lag > 45:
                # Rate-limited: a stall produces one warning per backlogged bar,
                # which buries everything else in the log.
                if now - self._warned_at > 60:
                    extra = (f" ({self._suppressed} similar suppressed)"
                             if self._suppressed else "")
                    log.warning(f"  [{leg}] bar "
                                f"{time.strftime('%H:%M', time.localtime(b['ts']))} "
                                f"arrived {lag:.0f}s late — the one-candle order "
                                f"window is compromised{extra}")
                    self._warned_at = now
                    self._suppressed = 0
                    self.late_bars += 1
                else:
                    self._suppressed += 1
                    self.late_bars += 1

            self._last_ts[leg] = b["ts"]
            self.on_bar(leg, b)

    def _members_per_bar(self, raw: List[dict], bars: List[dict]) -> List[int]:
        """How many source minutes went into each rolled-up bar."""
        span = self.period * 60
        starts = [b["ts"] for b in bars]
        counts = [0] * len(bars)
        if not starts:
            return counts
        if self.agg_mode == "clock":
            for c in raw:
                ts = int(c["ts"])
                a = self.anchor_of(ts)
                key = a + ((ts - a) // span) * span
                try:
                    counts[starts.index(key)] += 1
                except ValueError:
                    pass
            return counts
        # count mode: bars are consecutive groups of the sorted source minutes
        src = sorted((int(c["ts"]) for c in raw))
        idx = {t: n for n, t in enumerate(starts)}
        cur = -1
        for t in src:
            if t in idx:
                cur = idx[t]
            if cur >= 0:
                counts[cur] += 1
        return counts

    # ─── reporting ───

    def health(self) -> dict:
        if not self.latencies:
            return {"bars": 0, "polls": self.polls, "errors": self.errors,
                    "dropped_forming": self.dropped_forming,
                    "dropped_stale": self.dropped_stale, "stalls": self.stalls,
                    "late_bars": self.late_bars, "abandoned": self._abandoned,
                    "avg_lag": None, "max_lag": None, "verdict": "no bars yet"}
        avg = sum(self.latencies) / len(self.latencies)
        mx = max(self.latencies)
        verdict = ("comfortable" if mx < 10 else
                   "tight" if mx < 30 else "TOO SLOW — entries will be missed")
        return {"bars": len(self.latencies), "polls": self.polls,
                "errors": self.errors, "dropped_forming": self.dropped_forming,
                "dropped_stale": self.dropped_stale, "stalls": self.stalls,
                "late_bars": self.late_bars, "abandoned": self._abandoned,
                "avg_lag": avg, "max_lag": mx, "verdict": verdict}
