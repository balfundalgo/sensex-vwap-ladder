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

## Setup

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
