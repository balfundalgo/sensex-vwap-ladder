# 📈 SENSEX VWAP Reclaim + ATR Ladder

**Balfund Trading Pvt Ltd** | Strategy code `BF-SNX-VWAP-ATR` | Broker: **Dhan v2**

Implements *"The Line and the Ladder"* v1.1. One strike, both legs, traded independently.

## Rules

```
strike   = round(SENSEX 09:15 open / 100) * 100     # fixed for the day
legs     = CE and PE of that one strike             # fully independent state
timeframe= 2-minute, session-anchored to 09:15

ENTRY    candle closes above its own session VWAP (OHLC4, volume-weighted)
         -> buy stop at that candle's HIGH + 0.20, 3 lots, valid ONE candle
STOP     signal candle LOW - 1.00
LADDER   A = ATR(14) frozen at the signal candle;  E = trigger price
         Tn = E + 2*A*n         (open-ended, no final target)
         T1 -> sell 1 lot, stop to E + 2
         T2 -> sell 1 lot, stop to T1
         T3+ -> runner held, stop trails one rung behind
EXIT     stop hit | a candle closes below VWAP | 15:00 square-off
REPEAT   ONE attempt per trip above VWAP, per leg.
         Placing the order spends it — filled or not.
         A completed trade spends it too.
         Only a candle closing BELOW VWAP resets the leg.
TIME     first signal 09:17 | last trigger 14:45 | flat by 15:00
```

## Indicators — matched to ChartIQ

Kite renders with ChartIQ, so [ChartIQ's Built-in Studies Reference Guide](https://documentation.chartiq.com/tutorial-Using%20and%20Customizing%20Studies%20-%20Definitions.html)
is the reference implementation. `indicators.py` implements it directly and is
shared by the live engine, the replay path and `reconcile.py`, so all three
agree by construction.

```
VWAP   cumulative(field(i) * volume(i)) / cumulative(volume(i))   reset 09:15
       field = ohlc4 (client's setting). ChartIQ's own default is hlc3.
       volume(i) is THAT BAR's volume, never a running day total.

TR     max(High, Close[-1]) - min(Low, Close[-1])
ATR    ATR(i) = (ATR(i-1)*(N-1) + TR(i)) / N        Wilder, continuous
```

Wilder ATR has infinite memory, and ChartIQ notes its studies use however much
history the chart has loaded. The seed's influence decays as (1-1/N)^bars, so
N=14 needs ~200 prior bars before the starting point stops mattering. We seed
with 300 and log the residual at start-up; if it is above 1e-4 the ATR will not
match the terminal and the log says so.

## The token expires mid-session — recover, do not wait to be restarted

17-Sep: the token verified fine at 08:50 and expired at **09:24**, twenty-six
minutes into the session. Every poll after that returned `HTTP 401 DH-901`.
907 identical error lines, no candles, no signals. The app was not idle, it was
blind, and it had no way to say so. The client stopped and started it at 09:51
and it worked instantly — because startup re-authenticates.

The token manager already knew how to recover (verify → renew → TOTP). It just
only ran at startup. Now:

* any `401`, `403`, `DH-901` or `DH-906` triggers `reauthenticate()` in place
  and the call is retried immediately
* it is serialised and rate-limited to 40s, since every failing poll will call
  it and a TOTP window is only 30 seconds
* the **websocket URL embeds the token**, so a re-auth rebuilds it and forces
  the socket to reconnect — otherwise LTP keeps running on a dead credential
* repeated identical API errors log once a minute with a suppressed count,
  instead of 907 lines

**And a blind feed now announces itself.** No candles for five minutes during
market hours logs an error and turns the status bar to **NO DATA**. Silence
should never look the same as "no setups today".

## Power cuts, sleep, and stale signals

The client's machine lost power mid-session on 10-Sep and 16-Sep. The app
cannot prevent that, but it must not trade its way out of it.

**A gap is named, not inferred.** The monitor loop ticks once a second; if far
more wall clock than that has passed, the process was not running. It now says
so outright. Previously this had to be worked out from log lines that were
*absent* — the websocket watchdog reporting "no tick for 1874s" when it checks
every 10 seconds proved it had not run, which is a poor way to learn the
machine went off.

**A stale bar cannot arm a trigger.** When power returns the whole backlog
arrives at once. A bar that closed seven minutes ago carries a high that is no
longer a tradeable price — and the next tick, carrying the *current* price,
would fill it instantly at a level never available. In paper that books
fictitious profit; live, the limit would miss, the market fallback would fill
far away, and the stop and ladder would be computed from a price never traded.

Bars older than `max_signal_age` (90s) are ignored for signalling. Eligibility
is **not** consumed, because no order was placed. On 16-Sep a 407-second-old
bar did arm; it was cancelled a second later only because the next backlogged
bar arrived immediately. That was luck.

## Per-lot P&L is not trade P&L

The trade log shows both, and they are different units:

```
T1 sold 1 lot @ 363.34   this lot +1,230
T2 sold 1 lot @ 424.83   this lot +2,460
CLOSED TRAIL_T2  rung=T2  TRADE TOTAL +4,919  = +3,689 scaled + +1,230 final
```

Adding every number on screen gives +8,609 for a trade that made +4,919. The
labels and the breakdown exist so the relationship is visible on the line.

## The daily loss limit is a cap, not a note

The old guard summed **realised** P&L and ran only **after an exit**, so it
could never stop the trade that breached the limit — only notice afterwards.

22-Oct, limit ₹10,000: the day was **−8,921 realised** when a trade was opened
risking a further **4,110** against **1,079** of remaining budget. It closed at
**−11,009**. Had the stop been hit it would have been **−13,031**.

Now the total includes open positions marked to the last traded price, and is
evaluated **on every tick**. Breaching it closes every open leg immediately and
halts for the day. Replaying 22-Oct through it, the day ends at **−10,010** —
the ₹10 being the tick that crossed the line.

```
  ltp     open P&L   day total   action
186.15           0      -8,921
180.00        -369      -9,290
175.00        -669      -9,590
171.00        -909      -9,830
168.00           0     -10,010   HALTED, position closed
```

Worth being explicit about the cost: this is a **fourth exit reason** the
strategy spec does not have, and it overrides the stop, the VWAP close and the
square-off. It will sometimes close a trade that would have recovered. That is
what a hard cap buys and it was chosen deliberately.

The GUI field now reads **"Stop day at −₹"** rather than "Daily loss ₹", since
most of the original surprise was the wording rather than the number.

## Lot size comes from the contracts being traded

The old lookup took the **first** OPTIDX row whose trading symbol **started
with** "SENSEX" and returned its lot units. Two faults: the prefix also
matches `SENSEX50`, and "first row" depends on however Dhan happened to order
the CSV that morning.

It returned 20 for five straight sessions and then **75 on 09-Oct-2026**,
where the correct value for the 15-Oct expiry was 20. Every P&L that day was
3.75x too large, and in live mode it would have sent **225 quantity where 60
was intended**.

The strike is different every morning, so nothing is cached. By the time the
lot size is needed the option chain has already given us the exact security
IDs of today's two legs, so they are matched exactly:

```
09:15:02  index open 71776.67        -> strike 71800
09:15:02  option chain, 15-Oct       -> CE secId 889285, PE secId 889440
09:15:02  master, exact id match     -> lot 20 on both
```

Lot size is a property of the underlying and expiry, not the strike, so:

* **both legs must agree** — if they report different lot sizes the lookup is
  wrong and the engine refuses to trade rather than sizing on a guess
* **a missing id** falls back to the most common lot size across that
  underlying and expiry, so one stray row cannot swing it the way "first
  match" did; `SENSEX50` is excluded explicitly
* **a change from the previous session is logged and shown** — BSE moves
  SENSEX from 20 to 25 for January 2027 expiries from 30 October 2026, and
  that should arrive as a warning rather than a silent 25% jump in size

## Timeframe and position size

**Timeframe** is selectable: 1, 2, 3, 5, 10 or 15 minutes (default 2). Candles
are always built by rolling up 1-minute bars, at every timeframe — including 5
and 15 where Dhan has a native interval. One code path, and it is the one
reconciled against the chart; a native 5-minute bar and a rollup of five
1-minute bars disagree whenever a minute is missing.

Two things scale with it automatically:

* **History requested.** Wilder needs ~300 *bars*, so 15-minute candles need
  about twelve sessions where 2-minute needs two. `seed_days` scales rather
  than sitting at a fixed 10.
* **The stale-signal limit.** Was a flat 90s, which is most of a candle at
  1 minute and needlessly strict at 15. Now a quarter of the timeframe, floored
  at 20s.

**Size** is entered as a multiplier of 3, so the ladder always divides evenly:

```
you type 1  ->  3 lots  ->  1 at T1 · 1 at T2 · 1 runner
you type 2  ->  6 lots  ->  2 at T1 · 2 at T2 · 2 runner
you type 3  ->  9 lots  ->  3 at T1 · 3 at T2 · 3 runner
```

The GUI shows the arithmetic under the field and updates as you type, because
"Lots ×3" is not self-explanatory and a client who types 2 expecting two lots
would have a bad day.

**Locked and no longer in the GUI:** ATR method (Wilder), rollup (bar count),
candle source (REST). All three were reconciled against the chart. Only the
VWAP field stays adjustable, since the chart's own setting varies. The console
runner still exposes all of them.

## Nothing from a previous session may reach the strategy

Two independent guards, because on 08-Sep this went wrong and every downstream
number was wrong with it.

The app was started at 08:54, before the market opened. `days=1` returned 244
bars from the previous session and the feed emitted every one as live: 488
warnings, six phantom signals fired before the open, and VWAP and ATR polluted
for the rest of the day. The morning's trade used a VWAP of 277.34 where the
correct value — the first bar's own OHLC4 — was 366.88.

* `RestCandleFeed` is given the session anchor and drops anything before it
* `_process_candle` rejects them again, so no other route can let one in

The first bar of any session must have `VWAP == its own OHLC4`. That is
asserted in the test suite, with the 08-Sep numbers.

## Started before the open? It waits, and shows a countdown

The app can be launched at any time. If the market has not opened it does not
guess and it does not sit silently — it reports the wait once at startup and
then every two minutes (`open_countdown_seconds`), so the log stays readable.
Only when the 09:15 candle exists does it resolve the strike and start work.

```
MARKET NOT OPEN YET. It opens at 09:15 (13m 05s away); the strike is
read from that candle. Progress every 2 min.
  market opens in about 11 min
  market opens in about 9 min
  ...
  SENSEX 09:15 open = 75970.28  (resolved 4s after the open) -> strike 76000
```

Two phases, because they are different things. **Before 09:15** it counts down
to the *open*. An earlier build counted to 09:17 — when the first candle
closes — while saying "market opens in", so it was wrong by two minutes.
**From 09:15** it polls for the candle: Dhan publishes it while it is still
forming and a candle's open is fixed the instant it opens, so the strike
resolves around 09:15:05 rather than 09:17.

## The strike cannot be chosen before 09:15

Also from 08-Sep: started early, the engine fell back to the pre-market spot
of 76132.81 and chose strike **76100**. The real 09:15 open was 75970.28 —
strike **76000**. The client traded the wrong pair of options all morning and
only found out on a restart at 11:38.

The engine now waits for the 09:15 candle, however long that takes, and says
so. It will not guess a strike from a pre-market quote. A wrong strike is not
a degraded trade, it is a different instrument.

## The 2-minute rollup — the thing that bites

Kite has no native 2-minute feed. Zerodha serves **1-minute** bars and ChartIQ
rolls them up in the browser. ChartIQ's own docs say how:

> *"period describes the number of raw ticks from masterData to roll-up
> together into one data point"* … *"the dataSet will be 1/3 the length of the
> masterData"* … *"Aggregation is done by systematically picking the first
> element in each periodicity range"* … *"Chart data can contain gaps… By
> default, the charting library will collapse these gaps"*

That is **bar-count** grouping, not wall-clock bucketing. Gaps are collapsed,
surviving bars are grouped in twos, and the group takes the first bar's
timestamp. On an option whose feed is missing even one minute, the two schemes
desync permanently:

```
1-min bars present:  09:15 09:16 09:17 __:__ 09:19 09:20 09:21 09:22

  bar-count (Kite):  09:15[15,16]  09:17[17,19]  09:20[20,21]  09:22[22]
  wall-clock (naive):09:15[15,16]  09:17[17]     09:19[19,20]  09:21[21,22]
                                        ^ diverges here and never re-syncs
```

Default is `agg_mode="count"`. `"clock"` is kept so calibration can prove which
one the terminal is using.

## Reconciling against the terminal — in the app

The **RECONCILE** panel at the bottom of the window shows every closed 2-minute
candle with its per-bar volume, VWAP, ATR and whether the close was above the
line. Two ways to fill it:

* **Live** — start the engine; rows append as each candle closes.
* **LOAD FROM HISTORY** — pulls today's bars on demand. No engine, no market
  hours. Leave the strike box empty for today's opening strike, or type one in
  to check a past day's strike such as 77400.

The header reports the ATR warm-up length, the seed convergence residual, the
09:15 ATR, and any gaps where the feed has no bar. **Export CSV** writes
everything to `logs/reconcile_*.csv`.

Change **VWAP field** or **ATR method** in the left panel and press LOAD again
to re-run instantly — that is how you settle ohlc4-vs-hlc3 or wilder-vs-sma.

Check in this order: timestamps line up, then per-bar volume, then VWAP, then
ATR (09:15 value first — that is the one the overnight gap moves).

### vwap_check.py — VWAP alone

```bash
python vwap_check.py --sec-id 860293 --head 30      # morning bars
python vwap_check.py --sec-id 860293 --tail 10      # afternoon bars
python vwap_check.py --sec-id 860293 --at 14:19     # full working for one bar
python vwap_check.py --sec-id 860293 --at 14:19 --match 546.02
python vwap_check.py --sec-id 860293 --basis both   # 2m vs 1m accumulation
python vwap_check.py --sec-id 860293 --tail 10 --watch
```

All four price fields side by side. `--match` takes the value the chart shows
and reports which field and accumulation basis reproduces it.

**Compare the right row.** `--tail` puts the newest bar at the bottom, which is
the natural one to look at and was for a while the one bar that could be wrong
(a half-formed candle). That is fixed, but the habit is worth keeping: a
confirmed bar never changes again, so if a row moves between two runs, it was
not confirmed.

### CALIBRATE — let the software find the settings

Type into the calibrate row what Kite shows for **one bar** — the time, and any
of close / VWAP / ATR — and press **FIND MATCH**. It brute-forces every
combination of rollup mode x VWAP field x ATR method and ranks them by error.

It reports in two steps, and step 1 is the one that matters:

* **Step 1 — does our candle match the chart's candle at all?** If neither
  rollup reproduces the close Kite shows, the *source bars* differ. Dhan and
  Zerodha are different data vendors; no formula setting can reconcile a
  different candle. Stop and decide on the data source.
* **Step 2 — ranked settings.** If the candle does match, this tells you
  exactly which three dropdown values to set.

`reconcile.py` does the same thing headlessly for scripting; the GUI panel is
the primary tool.

## Tests

```bash
python test_indicators.py    # ChartIQ formulas, hand-computed
python test_logic.py         # strategy state machine
python test_gui.py           # GUI + reconcile panel (needs a display;
                             # on Linux: xvfb-run -a python test_gui.py)
```

## Two things that are easy to get wrong

**Candles are anchored to 09:15, not to `epoch % 120`.** 09:15 IST is not on a
120-second epoch boundary, so a naive modulo bucket straddles the open and every
candle ends up a minute out of step with the broker's chart. See
`session_anchor_epoch()` / `bucket_of()`.

**ATR is continuous, VWAP is not.** VWAP resets at 09:15; ATR carries across the
session boundary so a reading exists from the first candle. This matches the
Kite chart exactly, which is the client's stated priority — but it means the
09:15 candle's true range includes the overnight gap, so ATR is inflated for
roughly 14 candles and early-morning rungs sit too wide. Measure the
09:15–10:00 window separately in testing.

## Where the data comes from

Two sources, split by what each is good at:

| | Source | Why |
|---|---|---|
| Candles, volume, VWAP, ATR, **signals** | **REST** | This is the path reconciled bar-by-bar against the broker's chart — and it matched under two different VWAP fields, which is why it is trusted |
| LTP for the **trigger, stop and ladder rungs** | **WebSocket (ticker)** | REST cannot see inside a candle; spec §5 and §6 evaluate on traded price |

Consequence worth stating: with REST owning candles, the websocket only needs
Ticker mode, so `parse_quote()` — whose byte layout was never confirmed against
a live BFO feed — is out of the critical path entirely. Volume now only ever
comes from REST, where it was verified.

`data_source="websocket"` restores the old tick-built candles if ever needed.

### Dhan serves the candle that is still forming

Measured: the bar labelled 15:05 was already in the API at **15:05:02**, and it
keeps updating as the minute runs. Over a 10-minute probe, a still-forming bar
was present on **every single poll**.

That is fine for a chart and fatal here. A 2-minute bar is two 1-minute bars,
so a pair can look complete while its second member is two seconds old — and a
signal would be computed from a candle whose high, low, close and volume are
all still moving. `RestCandleFeed.drop_forming()` removes every bar whose close
is still in the future, once, before anything else sees the data. It is applied
in the live feed, in `reconcile.py` and in the app's LOAD FROM HISTORY.

### Connection handling

Learned from 08-Sep, where two `RemoteDisconnected` errors appeared and one
poll blocked for 1249 seconds:

RA17 never hit this because it never pools — a bare `requests.post()` opens a
fresh socket every call, so no socket can go stale. That is not a fix worth
copying, since it pays a TCP and TLS handshake on every poll, but it is a
useful fallback: **`DHAN_NO_POOL=1`** disables reuse entirely and reverts to
that behaviour. RA17's websocket handling is identical to ours line for line
(20/10 pings, 2s reconnect, no watchdog), so it would drop the same way.

* **Two HTTP sessions.** Reads (charts, chains, quotes) retry on a stale
  pooled connection. Orders **never** retry automatically — a POST that looks
  like it failed may have reached the exchange, and a blind retry could double
  the position. They are separate objects so the two can never be confused.
* **Hard deadline on every poll.** A socket timeout is not a guarantee; that
  1249s call had a 12s read timeout. Each poll now runs in a worker with a 25s
  deadline and is abandoned if it overruns, so one dead connection cannot
  blind the strategy.
* **WebSocket watchdog.** A socket can stay open and deliver nothing, which is
  worse than a clean drop because the stop and the ladder run on LTP. No tick
  for 90 seconds during market hours forces a reconnect. Ping timings were
  loosened from 20/10 to 30/15, which caused four client-side drops in one
  session.

### Measured latency

```
10 candles:  min 0.1s   avg 1.4s   max 3.0s
```

Comfortable. A 2-minute bar closing at 09:17 is in hand by 09:17:03, leaving
almost the whole of the next candle for the order to fill.

### The cost, and how it is watched

A REST signal is only as timely as the API. Every bar records how long after
its close it actually arrived. That is logged, shown in the app's status bar
(`lag avg / max`), and warned about above 30s. The one-candle order window is
what suffers if this degrades, so it is measured continuously rather than
assumed. `python freshness.py --minutes 10` measures the same thing
independently before you rely on it.

## Data feed

The option legs subscribe in **Quote mode (RequestCode 17)**, not Ticker mode,
because VWAP needs traded volume and the ticker packet does not carry it.
Per-candle volume is derived from the cumulative day volume in each quote packet.

The first three quote packets per leg are logged with LTP / volume / ATP as a
`[QUOTE CHECK]` line. **Verify these against the terminal on day one** — the
binary layout in `parse_quote()` is from the Dhan v2 spec and has not yet been
confirmed against a live BFO feed.

## Entry execution

Entries are **tick-triggered**: the engine watches ticks and fires a
Limit+IOC order (with market fallback) when LTP reaches the trigger. It does
*not* rest a stop-market order at the exchange. This is a deliberate deviation
from spec §12.1 — it keeps the one-candle validity rule under our control and
is exactly equivalent in paper mode. Revisit before live capital if fills lag.

## Logs

Three files per session in `logs/`:

| File | Contents |
|---|---|
| `snx_activity_*.log` | Full activity trace |
| `snx_trades_*.csv` | One row per lot exit — signal candle, frozen ATR, E, fill, exit, reason, rung |
| `snx_candles_*.csv` | Every closed 2-min candle with its VWAP and ATR |

The candle CSV exists for stage 1 of the build plan: reconcile it against the
broker's chart before trusting anything downstream.

## Guards (off by default)

`min_premium` and `daily_loss_limit` are both open items in spec §12.3 and
ship disabled. Neither is the client's rule — turn them on only once he has
given a number.

## Setup — VS Code (recommended)

```bash
cd sensex-vwap-ladder
./setup.sh
```

That creates `.venv`, installs dependencies, creates `.env` from the template
and runs the offline tests. Then:

1. `code .` (or File → Open Folder)
2. Cmd+Shift+P → **Python: Select Interpreter** → `./.venv/bin/python`
3. Fill your Dhan credentials into `.env`
4. Press **F5** and pick a configuration

| F5 configuration | What it does |
|---|---|
| 1 · Doctor (offline) | Python, deps, tkinter, `.env` — no network |
| 2 · Doctor + API | Also logs in and probes the data feed |
| 3 · App (GUI, paper) | The main window |
| 4 · Reconcile | Today's strike, both legs, to the terminal |
| 5 · Reconcile (prompted) | Asks for strike / leg / field / ATR method |
| 6 · Engine only | Console, no GUI |
| T1 / T2 / T3 | The three test suites |

Breakpoints work everywhere. Good ones to start with: `_process_candle` in
`engine.py` to watch a candle become a signal, and `SessionVWAP.update` in
`indicators.py` to watch the line build bar by bar.

Cmd+Shift+P → **Tasks: Run Task** gives the same things without the debugger,
including **Run all tests**.

### Start here every time

```bash
python doctor.py --api
```

It checks the login, the lot size, the expiry, the strike resolution, and then
the historical feed — first bar of the day, how many gaps it has, whether the
ATR warm-up is long enough, and whether the two rollup modes disagree. If
something is wrong it usually says so before you open the GUI.

### IPv6 is detected automatically

`init_credentials()` probes IPv6 once with a 2-second budget and forces IPv4 if
it does not answer. The client runs a packaged EXE and will never edit a `.env`,
so this cannot depend on remembering a flag. Override with `DHAN_FORCE_IPV4=1`
to always force, or `=0` to never.

### If everything is slow

A call that takes 80–160 seconds and *then succeeds* is almost never the API.
It is usually a dead IPv6 route: macOS tries IPv6 first, stalls until the OS
gives up, then falls back to IPv4. `doctor.py` times both paths and says so
outright. The fix is one line in `.env`:

```
DHAN_FORCE_IPV4=1
```

All HTTP now goes through a pooled `requests.Session` with keep-alive, so the
TCP and TLS handshake is paid once rather than on every poll — which matters a
great deal for a strategy that polls every few seconds all session.

### tkinter on macOS

The GUI needs tkinter. Python from **python.org** bundles it; **Homebrew**
python does not. If `doctor.py` reports it missing:

```bash
brew install python-tk
```

or install Python 3.11 from python.org, delete `.venv`, and re-run `./setup.sh`.

## Setup — plain terminal

```bash
pip install -r requirements.txt
cp .env.example .env      # fill credentials
python test_logic.py      # offline state-machine tests, no network
python app.py             # GUI
python engine.py          # console, paper mode
```

## Build stages

1. **The two lines** — VWAP and ATR matched to the paisa against the terminal
2. **Historical test** — profitable after doubling assumed cost per trade
3. **Settings review** — vary buffers and ATR multiple
4. **Paper trading** — 20 sessions
5. **Live, small** — 1 lot instead of 3, 30 sessions

**Balfund Trading Pvt Ltd** | www.balfund.com
