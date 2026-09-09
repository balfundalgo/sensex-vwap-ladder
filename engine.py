#!/usr/bin/env python3
"""
SENSEX VWAP Reclaim + ATR Ladder — Strategy Engine v1.0
════════════════════════════════════════════════════════
Spec: "The Line and the Ladder" v1.1 (BF-SNX-VWAP-ATR)

One strike (SENSEX open rounded to nearest 100), both legs (CE + PE),
each traded independently on 2-minute candles.

  Entry   : candle closes above its own session VWAP (OHLC4)
            -> buy stop at that candle's high + 0.20, valid ONE candle
  Stop    : signal candle low - 1.00
  Ladder  : Tn = E + 2*ATR*n   (ATR frozen at signal candle)
            T1 -> sell 1 lot, stop to E+2 | T2 -> sell 1 lot, stop to T1
            T3+ -> stop trails one rung behind, no ceiling
  Exit    : stop hit | candle closes below VWAP | 15:00 square-off
  Repeat  : ONE attempt per trip above VWAP. Only a close BELOW VWAP resets.

Balfund Trading Pvt Ltd | www.balfund.com
"""

import os, sys, time, json, csv, struct, threading, logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Any
from enum import Enum
from pathlib import Path

import socket
import requests
import pyotp
import websocket
from dotenv import load_dotenv, set_key

# Indicators live in their own module so that the live engine, the replay path
# and reconcile.py are guaranteed to compute identically. See indicators.py for
# the ChartIQ definitions these implement.
from indicators import (SessionVWAP, WilderATR, price_field, true_range,
                        aggregate, AGG_MODES)
from restfeed import RestCandleFeed

# ═══════════════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════════════

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent if "__file__" in globals() else Path.cwd()

ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "snx_daily_state.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

load_dotenv(str(ENV_FILE), override=True)

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING — Activity log + Trade CSV + Candle CSV (audit)
# ═══════════════════════════════════════════════════════════════════════════

_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
_activity_file = LOG_DIR / f"snx_activity_{_ts}.log"
_trade_csv = LOG_DIR / f"snx_trades_{datetime.now().strftime('%Y%m%d')}.csv"
_candle_csv = LOG_DIR / f"snx_candles_{datetime.now().strftime('%Y%m%d')}.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(str(_activity_file), encoding="utf-8"),
              logging.StreamHandler()]
)
log = logging.getLogger("SNX")


class TradeLogger:
    """One row per lot exit — full audit trail."""
    HEADERS = ["date", "time", "trade_id", "leg", "strike", "lot",
               "signal_time", "signal_high", "signal_low", "signal_close",
               "signal_vwap", "atr_frozen", "trigger_E", "fill_price",
               "exit_price", "qty", "pnl_points", "pnl_value", "exit_reason",
               "rung_reached", "hold_seconds", "entry_order_id", "exit_order_id"]

    def __init__(self):
        self.path = _trade_csv
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(self.HEADERS)

    def log_exit(self, **kw):
        try:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow([kw.get(h, "") for h in self.HEADERS])
        except Exception as e:
            log.error(f"Trade log write error: {e}")


class CandleLogger:
    """Every closed 2-min candle with its VWAP/ATR — for chart reconciliation."""
    HEADERS = ["date", "time", "leg", "sec_id", "open", "high", "low", "close",
               "candle_vol", "cum_vol", "vwap", "atr", "state", "eligible"]

    def __init__(self):
        self.path = _candle_csv
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(self.HEADERS)

    def log(self, **kw):
        try:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow([kw.get(h, "") for h in self.HEADERS])
        except Exception:
            pass


trade_logger = TradeLogger()
candle_logger = CandleLogger()

# ═══════════════════════════════════════════════════════════════════════════
# CREDENTIALS  (identical flow to RA17)
# ═══════════════════════════════════════════════════════════════════════════

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_PIN = os.getenv("DHAN_PIN", "").strip()
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "").strip()
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()

HEADERS: Dict[str, str] = {}
WS_URL: str = ""

BASE_URL = "https://api.dhan.co/v2"
IST = timezone(timedelta(hours=5, minutes=30))

# requests accepts (connect, read). Bounding the CONNECT leg separately matters
# because Python tries each resolved address in turn with the FULL timeout —
# unlike curl, which races IPv4 and IPv6 in parallel. api.dhan.co advertises 8
# IPv6 addresses, so with a dead IPv6 route and a 20s timeout, a single call
# stalls 160s before it ever reaches IPv4. That is measured, not theoretical.
CONNECT_TIMEOUT = 5.0

# ═══════════════════════════════════════════════════════════════════════════
# HTTP TRANSPORT
# ═══════════════════════════════════════════════════════════════════════════
#
# Two things matter here and both were learned the hard way.
#
# 1. CONNECTION REUSE. A bare requests.post() opens a fresh TCP connection and
#    a fresh TLS handshake for every call. The strategy polls every few seconds
#    all session, so that cost is paid hundreds of times for no reason. A
#    Session with keep-alive pays it once.
#
# 2. IPv6. If the host resolves to an IPv6 address whose route is broken, the
#    OS sits in connect() until it times out before falling back to IPv4. The
#    symptom is a call that takes 80-160 seconds and then succeeds, which looks
#    like a slow API but is nothing of the sort. Setting DHAN_FORCE_IPV4=1 in
#    .env makes urllib3 resolve A records only, skipping the dead path.

def force_ipv4(enabled: bool = True):
    """Make urllib3 resolve IPv4 only. See note above."""
    try:
        import urllib3.util.connection as u3c
        u3c.allowed_gai_family = (lambda: socket.AF_INET) if enabled else \
            (lambda: socket.AF_UNSPEC)
        return True
    except Exception as e:
        log.warning(f"Could not set IPv4-only mode: {e}")
        return False


def auto_select_ip_family(host="api.dhan.co", port=443, budget=2.0) -> str:
    """
    Decide once, at start-up, whether IPv6 is usable.

    Measured on a real machine: api.dhan.co advertises 8 IPv6 addresses, the
    route to them was dead, and Python tries each in turn with the FULL socket
    timeout — 8 x 20s = 160s per call, versus 0.03s over IPv4. curl hides this
    because it races both families (Happy Eyeballs); Python does not.

    The client runs a packaged EXE and will never edit a .env file, so this
    cannot depend on someone remembering a flag. One probe, 2s budget, once.

      DHAN_FORCE_IPV4=1  force IPv4, skip the probe
      DHAN_FORCE_IPV4=0  never force, skip the probe
      unset              probe and decide
    """
    v = os.getenv("DHAN_FORCE_IPV4", "").strip().lower()
    if v in ("1", "true", "yes"):
        force_ipv4(True)
        log.info("DHAN_FORCE_IPV4=1 — resolving IPv4 only")
        return "forced"
    if v in ("0", "false", "no"):
        log.info("DHAN_FORCE_IPV4=0 — auto-detection disabled")
        return "disabled"

    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except Exception:
        return "no-ipv6"                      # nothing to stall on
    if not infos:
        return "no-ipv6"

    af, st_, proto, _, sa = infos[0]
    sock = None
    t0 = time.time()
    try:
        sock = socket.socket(af, st_, proto)
        sock.settimeout(budget)
        sock.connect(sa)
        log.info(f"IPv6 reachable in {time.time() - t0:.2f}s — leaving it enabled")
        return "ipv6-ok"
    except Exception:
        force_ipv4(True)
        log.warning(f"IPv6 to {host} did not answer within {budget:.0f}s "
                    f"({len(infos)} address(es) advertised). Forcing IPv4 — "
                    f"without this every call would stall behind each dead "
                    f"address in turn.")
        return "auto-ipv4"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

# Two sessions, deliberately.
#
# A pooled connection that the server has quietly closed produces
# RemoteDisconnected('Remote end closed connection without response') on the
# next request. Seen twice on 08-Sep, once blocking a poll for 1249 seconds.
# The cure is to let urllib3 retry a request that died on a stale socket.
#
# But that must NEVER apply to order placement: a POST /orders that appears to
# fail may in fact have reached the exchange, and retrying it could double the
# position. So reads retry and writes do not, and they cannot be confused
# because they are different objects.
from urllib3.util.retry import Retry

_read_retry = Retry(total=3, connect=3, read=2, status=0, backoff_factor=0.4,
                    allowed_methods=frozenset(["GET", "POST"]),
                    raise_on_status=False)

# RA17 never hit this problem because it never pools — a bare requests.post()
# opens a fresh socket every call, so no socket can go stale. That is not a fix
# worth copying (it pays a TCP+TLS handshake on every poll), but it is a useful
# fallback: DHAN_NO_POOL=1 turns pooling off entirely and reverts to that
# behaviour if pooling ever misbehaves again.
_NO_POOL = os.getenv("DHAN_NO_POOL", "").strip().lower() in ("1", "true", "yes")

SESSION = requests.Session()          # read-only: quotes, charts, chains
SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=1 if _NO_POOL else 4,
    pool_maxsize=1 if _NO_POOL else 8,
    max_retries=_read_retry))
if _NO_POOL:
    SESSION.headers["Connection"] = "close"
    log.info("DHAN_NO_POOL set — connection reuse disabled")

ORDER_SESSION = requests.Session()    # orders: never retried automatically
ORDER_SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=4, max_retries=0))


class RateGate:
    def __init__(self, max_per_sec: float):
        self.min_gap = 1.0 / max_per_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            gap = time.time() - self._last
            if gap < self.min_gap:
                time.sleep(self.min_gap - gap)
            self._last = time.time()


DATA_GATE = RateGate(4.0)
ORDER_GATE = RateGate(8.0)
OC_GATE = RateGate(0.30)


class DhanTokenManager:
    def verify(self, token: str) -> bool:
        if not token: return False
        try:
            h = {"access-token": token, "client-id": DHAN_CLIENT_ID}
            return SESSION.get(f"{BASE_URL}/profile", headers=h,
                               timeout=(CONNECT_TIMEOUT, 10)).status_code == 200
        except Exception:
            return False

    def renew(self, token: str) -> Optional[str]:
        try:
            h = {"access-token": token, "dhanClientId": DHAN_CLIENT_ID,
                 "Content-Type": "application/json"}
            d = SESSION.get(f"{BASE_URL}/RenewToken", headers=h, timeout=15).json()
            if "accessToken" in d:
                log.info("Token renewed")
                return d["accessToken"]
            log.warning(f"Renew failed: {d}")
        except Exception as e:
            log.warning(f"Renew error: {e}")
        return None

    def generate(self, max_retries=3) -> Optional[str]:
        url = "https://auth.dhan.co/app/generateAccessToken"
        for attempt in range(max_retries):
            rem = 30 - (int(time.time()) % 30)
            if attempt > 0 or rem < 10:
                log.info(f"  Waiting {rem+1}s for TOTP window...")
                time.sleep(rem + 1)
            totp = pyotp.TOTP(DHAN_TOTP_SECRET).now()
            log.info(f"Attempt {attempt+1}: TOTP={totp}")
            try:
                params = {"dhanClientId": DHAN_CLIENT_ID, "pin": DHAN_PIN, "totp": totp}
                d = SESSION.post(url, params=params, timeout=15).json()
                if "accessToken" in d:
                    log.info("Token generated")
                    return d["accessToken"]
                log.warning(f"Generate attempt {attempt+1} failed: {d.get('errorMessage', d)}")
            except Exception as e:
                log.warning(f"Generate error: {e}")
        return None

    def ensure_token(self) -> str:
        if DHAN_ACCESS_TOKEN:
            log.info("Verifying existing token...")
            if self.verify(DHAN_ACCESS_TOKEN): return DHAN_ACCESS_TOKEN
            log.info("Token invalid, trying renew...")
            t = self.renew(DHAN_ACCESS_TOKEN)
            if t:
                self._save(t); return t
            log.info("Renew failed, generating via TOTP...")
        else:
            log.info("No existing token, generating via TOTP...")
        t = self.generate()
        if not t: raise RuntimeError("Could not obtain Dhan token")
        self._save(t); return t

    def _save(self, token):
        try: set_key(str(ENV_FILE), "DHAN_ACCESS_TOKEN", token)
        except Exception: pass


def set_credentials(cid, pin, totp, token=""):
    global DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DHAN_ACCESS_TOKEN
    DHAN_CLIENT_ID = cid; DHAN_PIN = pin
    DHAN_TOTP_SECRET = totp; DHAN_ACCESS_TOKEN = token


def init_credentials(status_cb=None):
    global HEADERS, WS_URL, DHAN_ACCESS_TOKEN
    auto_select_ip_family()
    log.info("Authenticating with Dhan...")
    if status_cb: status_cb("Authenticating with Dhan...")
    token = DhanTokenManager().ensure_token()
    DHAN_ACCESS_TOKEN = token
    HEADERS.update({"Content-Type": "application/json",
                    "access-token": token, "client-id": DHAN_CLIENT_ID})
    WS_URL = (f"wss://api-feed.dhan.co?version=2"
              f"&token={token}&clientId={DHAN_CLIENT_ID}&authType=2")
    log.info("Credentials initialized. WS URL ready.")


# ═══════════════════════════════════════════════════════════════════════════
# INDEX CONFIG
# ═══════════════════════════════════════════════════════════════════════════

SENSEX = {"security_id": "51", "segment": "BSE_FNO", "idx_segment": "IDX_I",
          "strike_gap": 100, "lot_size": 20}

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


def fetch_sensex_lot_size() -> Optional[int]:
    """Lot size is read fresh every session — never hard-coded (spec 2.3)."""
    log.info("  Fetching SENSEX lot size from scrip master...")
    try:
        import io, csv as csvmod
        r = SESSION.get(MASTER_URL, stream=True, timeout=120); r.raise_for_status()
        raw = r.content.decode("utf-8", errors="ignore")
        if raw.startswith("\ufeff"): raw = raw[1:]
        for row in csvmod.DictReader(io.StringIO(raw)):
            if row.get("SEM_INSTRUMENT_NAME", "").strip().upper() != "OPTIDX": continue
            ts = row.get("SEM_TRADING_SYMBOL", "").upper()
            if not ts.startswith("SENSEX"): continue
            try: lot = int(float(row.get("SEM_LOT_UNITS", "0").strip()))
            except Exception: continue
            if lot > 0:
                log.info(f"  SENSEX lot size = {lot}")
                return lot
    except Exception as e:
        log.error(f"  Scrip master error: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# TIME HELPERS — session-anchored 2-minute bucketing
# ═══════════════════════════════════════════════════════════════════════════

def now_ist(): return datetime.now(IST)


def _normalize_epoch(ts):
    """
    REST timestamps. Dhan v2 returns TRUE UNIX epoch — verified against the
    sample values in their own historical-data docs, where a daily candle
    lands exactly on 00:00 IST and consecutive candles show a three-day gap
    across a weekend. So there is nothing to normalise: pass it through.

    This used to carry a heuristic inherited from an earlier project:
    "if the timestamp is 4.5-6.5 hours ahead of now, subtract 5:30". That
    silently rewrote any bar falling in that window. A closed bar is never in
    the future, so production was safe — but it made a 14:45 bar decode as
    09:15 when the clock happened to read 09:50, which is how it was found.
    Guessing at a convention we have since verified is not worth the risk.
    """
    return int(ts)


def ws_epoch(ts):
    """
    WebSocket last-traded-time. This one genuinely may arrive shifted, and
    unlike the REST feed it has not been verified, so the heuristic stays —
    but scoped to the websocket path alone, where it cannot touch candle
    construction in REST mode.
    """
    ts = int(ts)
    now_ts = int(time.time())
    if int(4.5 * 3600) <= (ts - now_ts) <= int(6.5 * 3600):
        ts -= 19800
    return ts


def epoch_to_ist(ts, fmt="%H:%M:%S"):
    if not ts: return "-"
    return datetime.fromtimestamp(_normalize_epoch(int(ts)), tz=IST).strftime(fmt)


def session_anchor_epoch(dt: Optional[datetime] = None) -> int:
    """
    Epoch of 09:15:00 IST for the given date.

    CRITICAL: 2-minute candles must be anchored to the session open, not to
    epoch % 120. 09:15 IST is NOT on a 120-second epoch boundary, so a naive
    modulo bucket would straddle the open and every candle would be offset by
    one minute against the broker's chart.
    """
    d = (dt or now_ist()).astimezone(IST)
    return int(d.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())


def bucket_of(ts: int, interval: int = 120) -> int:
    """Session-anchored bucket index for an epoch timestamp."""
    ts = _normalize_epoch(int(ts))
    anchor = session_anchor_epoch(datetime.fromtimestamp(ts, tz=IST))
    return (ts - anchor) // interval


def bucket_start_epoch(ts: int, interval: int = 120) -> int:
    ts = _normalize_epoch(int(ts))
    anchor = session_anchor_epoch(datetime.fromtimestamp(ts, tz=IST))
    return anchor + ((ts - anchor) // interval) * interval


def hhmm(s: str):
    return datetime.strptime(s, "%H:%M").time()


# ═══════════════════════════════════════════════════════════════════════════
# REST API  (order engine lifted from RA17)
# ═══════════════════════════════════════════════════════════════════════════

def _hdrs():
    return {"Content-Type": "application/json",
            "access-token": HEADERS.get("access-token", ""),
            "client-id": HEADERS.get("client-id", "")}


SLOW_CALL_SECONDS = 3.0     # anything above this is worth knowing about


def api_post(endpoint, payload, retries=2, timeout=15):
    """
    POST with rate gating, retries, and timing.

    Dhan allows 5 requests/second on Data APIs and 1 per 3 seconds on the
    option chain; the gates above enforce that. What the gates cannot protect
    against is the API simply being slow, which matters enormously here — the
    strategy acts on a candle close and the order lives for one candle, so a
    call that takes 30 seconds is not a slow call, it is a missed trade. Every
    call is therefore timed and anything sluggish is logged.
    """
    if "optionchain" in endpoint: OC_GATE.wait()
    else: DATA_GATE.wait()
    for att in range(retries + 1):
        t0 = time.time()
        try:
            r = SESSION.post(f"{BASE_URL}{endpoint}", headers=_hdrs(),
                              json=payload,
                              timeout=(CONNECT_TIMEOUT, timeout))
            dt = time.time() - t0
            if dt > SLOW_CALL_SECONDS:
                log.warning(f"  SLOW API {endpoint} took {dt:.1f}s "
                            f"(HTTP {r.status_code})")
            if r.status_code == 200: return r.json()
            if r.status_code == 429:
                log.warning(f"  API {endpoint} rate limited, backing off")
                time.sleep(2 ** (att + 1)); continue
            log.error(f"  API {endpoint} -> HTTP {r.status_code}: {r.text[:200]}")
            if att < retries: time.sleep(1.5)
        except requests.exceptions.Timeout:
            log.error(f"  API {endpoint} TIMED OUT after {timeout}s "
                      f"(attempt {att + 1}/{retries + 1})")
            if att < retries: time.sleep(1.5)
        except Exception as e:
            log.error(f"  API {endpoint} error after {time.time() - t0:.1f}s: {e}")
            if att < retries: time.sleep(1.5)
    return None


def place_order_limit_ioc(security_id, segment, side, qty, price,
                          price_buffer=0.10, max_retries=3) -> dict:
    """Limit+IOC retry engine -> Market fallback."""
    for att in range(max_retries):
        ORDER_GATE.wait()
        adj = round(price + price_buffer * (1 if side == "BUY" else -1), 2)
        pl = {"dhanClientId": str(DHAN_CLIENT_ID), "transactionType": side,
              "exchangeSegment": segment, "productType": "INTRADAY",
              "orderType": "LIMIT", "validity": "IOC",
              "securityId": str(security_id), "quantity": int(qty),
              "disclosedQuantity": 0, "price": adj,
              "triggerPrice": 0, "afterMarketOrder": False}
        log.info(f"  [ORDER] {side} {segment}:{security_id} qty={qty} "
                 f"LIMIT@{adj} (att {att+1}/{max_retries})")
        try:
            r = ORDER_SESSION.post(f"{BASE_URL}/orders", headers=_hdrs(), json=pl, timeout=15)
            if r.status_code == 200:
                d = r.json()
                oid = str(d.get("orderId", ""))
                status = str(d.get("orderStatus", "")).upper()
                avg = float(d.get("averageTradedPrice", 0) or 0)
                if status == "TRADED" and avg > 0:
                    log.info(f"  [FILLED] {side} qty={qty} @ Rs.{avg:.2f} id={oid}")
                    return {"filled": True, "price": avg, "order_id": oid}
                if status in ("REJECTED", "CANCELLED"):
                    err = d.get("omsErrorDescription") or d.get("errorMessage") or status
                    log.warning(f"  [REJECTED] {err} id={oid}")
                    break
                if oid:
                    for _ in range(6):
                        time.sleep(0.4); ORDER_GATE.wait()
                        try:
                            pr = ORDER_SESSION.get(f"{BASE_URL}/orders/{oid}",
                                              headers=_hdrs(), timeout=10)
                            if pr.status_code == 200:
                                pd = pr.json()
                                ps = str(pd.get("orderStatus", "")).upper()
                                pp = float(pd.get("averageTradedPrice", 0) or 0)
                                if ps == "TRADED" and pp > 0:
                                    log.info(f"  [FILLED] {side} qty={qty} @ Rs.{pp:.2f}")
                                    return {"filled": True, "price": pp, "order_id": oid}
                                if ps in ("REJECTED", "CANCELLED"): break
                        except Exception: pass
                    try:
                        ORDER_SESSION.delete(f"{BASE_URL}/orders/{oid}", headers=_hdrs(), timeout=10)
                    except Exception: pass
            else:
                log.error(f"  [ORDER] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.error(f"  [ORDER] Error: {e}")
        price_buffer += 0.10
        time.sleep(0.5)

    log.warning(f"  [FALLBACK] {side} {segment}:{security_id} -> MARKET")
    return place_order_market(security_id, segment, side, qty)


def place_order_market(security_id, segment, side, qty) -> dict:
    ORDER_GATE.wait()
    pl = {"dhanClientId": str(DHAN_CLIENT_ID), "transactionType": side,
          "exchangeSegment": segment, "productType": "INTRADAY",
          "orderType": "MARKET", "validity": "DAY",
          "securityId": str(security_id), "quantity": int(qty),
          "disclosedQuantity": 0, "price": 0, "triggerPrice": 0,
          "afterMarketOrder": False}
    try:
        r = ORDER_SESSION.post(f"{BASE_URL}/orders", headers=_hdrs(), json=pl, timeout=15)
        if r.status_code == 200:
            d = r.json()
            oid = str(d.get("orderId", ""))
            avg = float(d.get("averageTradedPrice", 0) or 0)
            if avg > 0:
                log.info(f"  [MKT FILLED] {side} qty={qty} @ Rs.{avg:.2f}")
                return {"filled": True, "price": avg, "order_id": oid}
            for _ in range(8):
                time.sleep(0.5); ORDER_GATE.wait()
                try:
                    pr = ORDER_SESSION.get(f"{BASE_URL}/orders/{oid}", headers=_hdrs(), timeout=10)
                    if pr.status_code == 200:
                        pp = float(pr.json().get("averageTradedPrice", 0) or 0)
                        if pp > 0:
                            log.info(f"  [MKT FILLED] {side} qty={qty} @ Rs.{pp:.2f}")
                            return {"filled": True, "price": pp, "order_id": oid}
                except Exception: pass
            return {"filled": False, "price": 0, "order_id": oid}
        log.error(f"  [MKT ORDER] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"  [MKT ORDER] Error: {e}")
    return {"filled": False, "price": 0, "order_id": ""}


# ═══════════════════════════════════════════════════════════════════════════
# DATA FETCH
# ═══════════════════════════════════════════════════════════════════════════

def fetch_expiry_list() -> List[str]:
    payload = {"UnderlyingScrip": int(SENSEX["security_id"]), "UnderlyingSeg": "IDX_I"}
    resp = api_post("/optionchain/expirylist", payload)
    if resp and resp.get("status") == "success":
        return resp.get("data", [])
    log.error(f"  Expiry API failed: {resp}")
    return []


def get_nearest_expiry() -> Optional[str]:
    """Nearest available SENSEX weekly expiry — resolved at runtime (spec 2.1)."""
    exps = fetch_expiry_list()
    if not exps: return None
    today = now_ist().date()
    future = sorted([(datetime.strptime(e, "%Y-%m-%d").date(), e) for e in exps
                     if datetime.strptime(e, "%Y-%m-%d").date() >= today])
    if future:
        log.info(f"  Expiries available: {[e for _, e in future[:4]]}")
        return future[0][1]
    return None


def fetch_option_chain(expiry: str) -> Optional[dict]:
    payload = {"UnderlyingScrip": int(SENSEX["security_id"]),
               "UnderlyingSeg": "IDX_I", "Expiry": expiry}
    resp = api_post("/optionchain", payload)
    if resp and resp.get("status") == "success":
        return {"spot_price": float(resp["data"]["last_price"]), "oc": resp["data"]["oc"]}
    return None


def fetch_intraday_1m(security_id, segment, instrument, days=6) -> List[dict]:
    """
    Dhan intraday charts support 1/5/15/25/60-minute intervals — there is no
    native 2-minute. We pull 1-minute and aggregate (see aggregate_2m).
    """
    to_d = now_ist().strftime("%Y-%m-%d")
    fr_d = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    payload = {"securityId": str(security_id), "exchangeSegment": segment,
               "instrument": instrument, "interval": "1",
               "fromDate": fr_d, "toDate": to_d}
    # Short timeout on purpose: a candle that arrives very late is useless to
    # the strategy, so failing fast and retrying next poll beats blocking.
    resp = api_post("/charts/intraday", payload, retries=1, timeout=12)
    if not resp or "open" not in resp: return []
    n = len(resp["open"])
    tss = resp.get("timestamp", [0] * n)
    vols = resp.get("volume", [0] * n)
    out = []
    for i in range(n):
        out.append({"ts": int(tss[i]) if i < len(tss) else 0,
                    "open": float(resp["open"][i]), "high": float(resp["high"][i]),
                    "low": float(resp["low"][i]), "close": float(resp["close"][i]),
                    "volume": float(vols[i]) if i < len(vols) else 0.0})
    return out


def aggregate_2m(candles_1m: List[dict], mode: str = "count") -> List[dict]:
    """
    Roll Dhan's 1-minute bars into the 2-minute bars the chart draws.

    mode="count" groups every 2 SURVIVING bars, which is what ChartIQ (and so
    Kite) does — gaps are collapsed, not preserved. mode="clock" buckets by
    wall clock instead. They agree only when no minute is missing.
    """
    return aggregate(candles_1m, period=2, mode=mode,
                     anchor_of=lambda ts: session_anchor_epoch(
                         datetime.fromtimestamp(_normalize_epoch(ts), tz=IST)))


def get_sensex_open_0915() -> Optional[float]:
    """
    The 09:15 opening index level — the strike reference (spec 2.2).
    Read from the index's own 1-minute history so a late start still resolves
    the same strike the strategy would have chosen at the open.
    """
    candles = fetch_intraday_1m(SENSEX["security_id"], "IDX_I", "INDEX", days=2)
    if not candles: return None
    today = now_ist().date()
    anchor = session_anchor_epoch()
    for c in candles:
        ts = _normalize_epoch(c["ts"])
        if ts == anchor:
            return float(c["open"])
    todays = [c for c in candles
              if datetime.fromtimestamp(_normalize_epoch(c["ts"]), tz=IST).date() == today]
    if todays:
        todays.sort(key=lambda x: _normalize_epoch(x["ts"]))
        return float(todays[0]["open"])
    return None


# ═══════════════════════════════════════════════════════════════════════════
# WS BINARY PARSERS
# ═══════════════════════════════════════════════════════════════════════════

EXCH_SEG_MAP = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CUR",
                4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CUR", 8: "BSE_FNO"}


def parse_header(msg):
    if len(msg) < 8: return None
    return {"resp_code": msg[0], "seg": EXCH_SEG_MAP.get(msg[3], str(msg[3])),
            "security_id": str(struct.unpack_from("<I", msg, 4)[0]), "payload": msg[8:]}


def parse_ticker(payload):
    """Response code 2 — LTP + LTT (16-byte packet)."""
    if len(payload) < 8: return None
    return {"ltp": float(struct.unpack_from("<f", payload, 0)[0]),
            "ltt": int(struct.unpack_from("<I", payload, 4)[0])}


def parse_quote(payload):
    """
    Response code 4 — Quote packet (50 bytes total, 42-byte payload).

    Layout after the 8-byte header:
      0  float32  LTP
      4  int16    last traded quantity
      6  int32    last trade time
      10 float32  average traded price
      14 int32    volume (cumulative for the day)
      18 int32    total sell quantity
      22 int32    total buy quantity
      26 float32  day open
      30 float32  day close
      34 float32  day high
      38 float32  day low

    VWAP needs traded volume, which the ticker packet does not carry — this is
    why the option legs subscribe in Quote mode (RequestCode 17) rather than
    Ticker mode. The layout above must be verified against a live feed before
    go-live; the engine logs the first few parsed packets for that purpose.
    """
    if len(payload) < 42: return None
    return {"ltp": float(struct.unpack_from("<f", payload, 0)[0]),
            "ltq": int(struct.unpack_from("<h", payload, 4)[0]),
            "ltt": int(struct.unpack_from("<I", payload, 6)[0]),
            "atp": float(struct.unpack_from("<f", payload, 10)[0]),
            "volume": int(struct.unpack_from("<I", payload, 14)[0]),
            "day_open": float(struct.unpack_from("<f", payload, 26)[0]),
            "day_high": float(struct.unpack_from("<f", payload, 34)[0]),
            "day_low": float(struct.unpack_from("<f", payload, 38)[0])}


# ═══════════════════════════════════════════════════════════════════════════
# CANDLE ENGINE — session-anchored 2-minute
# ═══════════════════════════════════════════════════════════════════════════

class CandleEngine:
    def __init__(self, sec_id, label, interval=120, on_close=None,
                 opening_cum_vol: float = 0.0):
        self.sec_id = sec_id; self.label = label; self.interval = interval
        self.on_close = on_close
        self.lock = threading.Lock()
        self.current: Optional[dict] = None
        self.last_ltp: Optional[float] = None
        self.last_cum_vol: float = float(opening_cum_vol)
        # Cumulative day volume as at the close of the previous completed bar.
        # This is the ONLY place cumulative-vs-per-bar volume is reconciled;
        # every candle leaving this class carries a per-bar "volume".
        self._cum_at_prev_close: float = float(opening_cum_vol)
        self.ticks = 0

    def on_tick(self, ltp, ltt, cum_vol=None):
        ltp = float(ltp); ltt = ws_epoch(int(ltt))
        b = bucket_start_epoch(ltt, self.interval)
        completed = None
        with self.lock:
            self.last_ltp = ltp; self.ticks += 1
            if cum_vol is not None:
                self.last_cum_vol = float(cum_vol)
            if self.current is None:
                self.current = {"ts": b, "open": ltp, "high": ltp, "low": ltp,
                                "close": ltp, "cum_vol": self.last_cum_vol}
                return
            if b == self.current["ts"]:
                self.current["high"] = max(self.current["high"], ltp)
                self.current["low"] = min(self.current["low"], ltp)
                self.current["close"] = ltp
                self.current["cum_vol"] = self.last_cum_vol
                return
            if b > self.current["ts"]:
                completed = dict(self.current)
                completed["volume"] = max(
                    0.0, completed["cum_vol"] - self._cum_at_prev_close)
                self._cum_at_prev_close = completed["cum_vol"]
                self.current = {"ts": b, "open": ltp, "high": ltp, "low": ltp,
                                "close": ltp, "cum_vol": self.last_cum_vol}
        if completed is not None and self.on_close:
            self.on_close(self.sec_id, completed)


# ═══════════════════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG & STATE
# ═══════════════════════════════════════════════════════════════════════════

class LegState(Enum):
    IDLE = "IDLE"                # eligible, waiting for a close above VWAP
    ARMED = "ARMED"              # order resting, valid for one candle
    IN_TRADE = "IN_TRADE"
    STOOD_DOWN = "STOOD_DOWN"    # attempt spent, waiting for a close below VWAP
    HALTED = "HALTED"


@dataclass
class StrategyConfig:
    paper_mode: bool = True
    entry_buffer: float = 0.20        # above signal candle high
    stop_buffer: float = 1.00         # below signal candle low
    atr_period: int = 14
    vwap_field: str = "ohlc4"         # must match the label on his chart
    agg_mode: str = "count"           # "count" = ChartIQ/Kite | "clock"
    data_source: str = "rest"         # "rest" (verified path) | "websocket"
    rest_poll: float = 4.0            # seconds between REST polls
    rest_grace: float = 6.0           # declare a bar closed this long after
    ws_silence_seconds: float = 90.0  # force a reconnect after this much quiet
    open_countdown_seconds: float = 120.0   # how often to report the wait
    atr_method: str = "wilder"        # "wilder" (ChartIQ Math) | "sma"
    rung_atr_mult: float = 2.0        # Tn = E + 2*ATR*n
    cost_plus: float = 2.00           # stop after T1 = E + 2
    lots: int = 3
    last_entry: str = "14:45"
    square_off: str = "15:00"
    atr_seed_bars: int = 300          # Wilder needs ~200 bars to converge
    min_premium: float = 0.0          # 0 = disabled (open item)
    daily_loss_limit: float = 0.0     # 0 = disabled (open item)
    first_candle_can_signal: bool = True   # open item — spec 12.3 item 5
    max_slippage: float = 0.0         # 0 = disabled; else skip fill beyond E+x


@dataclass
class Position:
    leg: str
    sec_id: str
    strike: int
    trade_id: str
    E: float                 # trigger price — the reference for the whole ladder
    fill_price: float
    atr_frozen: float
    stop: float
    initial_stop: float
    lots_open: int
    lot_size: int
    highest_rung: int = 0
    opened_at: float = field(default_factory=time.time)
    entry_order_id: str = ""
    realised: float = 0.0
    signal: dict = field(default_factory=dict)

    def rung_price(self, n: int, mult: float) -> float:
        return self.E + mult * self.atr_frozen * n


@dataclass
class Leg:
    name: str                 # "CE" / "PE"
    sec_id: str = ""
    strike: int = 0
    label: str = ""
    state: LegState = LegState.IDLE
    eligible: bool = True
    vwap: SessionVWAP = field(default_factory=SessionVWAP)
    atr: WilderATR = field(default_factory=WilderATR)
    seed_bars: int = 0
    seed_residual: float = 1.0
    ltp: float = 0.0
    last_candle: Optional[dict] = None
    signal_candle: Optional[dict] = None
    trigger: float = 0.0
    pending_stop: float = 0.0
    armed_bucket: int = -1
    position: Optional[Position] = None
    pnl: float = 0.0
    trades: int = 0
    attempts: int = 0
    atr_ok: bool = False
    candles_seen: int = 0
    seeded_upto: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class SensexVWAPLadderEngine:
    VERSION = "1.0"

    def __init__(self, config: Optional[StrategyConfig] = None, gui_callback=None):
        self.config = config or StrategyConfig()
        self.gui_cb = gui_callback or (lambda *a: None)
        self.stop_event = threading.Event()

        self.expiry: Optional[str] = None
        self.strike: int = 0
        self.sensex_open: float = 0.0
        self.spot: float = 0.0
        self.lot_size: int = SENSEX["lot_size"]

        self.legs: Dict[str, Leg] = {"CE": Leg("CE"), "PE": Leg("PE")}
        self.by_sec: Dict[str, Leg] = {}

        self.candle_engines: Dict[str, CandleEngine] = {}
        self.rest_feed: Optional[RestCandleFeed] = None
        self.ws = None
        self.ws_connected = threading.Event()

        self.trade_lock = threading.Lock()
        self.closed: List[dict] = []
        self.packet_count = 0
        self.last_tick_at = 0.0
        self._quote_debug = 0
        self._halted = False
        self._trade_seq = 0

    # ─── events ───

    def _emit(self, event, data=None):
        try: self.gui_cb(event, data or {})
        except Exception: pass

    # ─── initialise ───

    def initialize(self) -> bool:
        self._emit("status", {"msg": "Initializing..."})

        lot = fetch_sensex_lot_size()
        if not lot:
            self._emit("error", {"msg": "Lot size unavailable — refusing to trade"})
            log.error("Instrument master unreachable. Halting rather than assuming "
                      "a stale lot size (spec 2.3).")
            return False
        self.lot_size = lot

        self.expiry = get_nearest_expiry()
        if not self.expiry:
            self._emit("error", {"msg": "No SENSEX expiry found"}); return False
        log.info(f"  Expiry: {self.expiry}")

        self.sensex_open = self._wait_for_session_open()
        if self.sensex_open <= 0:
            self._emit("error", {"msg": "Could not resolve the 09:15 SENSEX open"})
            return False

        self.strike = int(round(self.sensex_open / 100.0) * 100)
        log.info(f"  SENSEX 09:15 open = {self.sensex_open:.2f} -> strike {self.strike}")
        self._emit("strike", {"open": self.sensex_open, "strike": self.strike,
                              "expiry": self.expiry, "lot_size": self.lot_size})

        oc = fetch_option_chain(self.expiry)
        if not oc:
            self._emit("error", {"msg": "Option chain unavailable"}); return False
        self.spot = oc["spot_price"]

        if not self._resolve_legs(oc["oc"]):
            return False

        for name, leg in self.legs.items():
            self._seed_leg(leg)

        return True

    def _wait_for_session_open(self, timeout_s: float = 1800.0) -> float:
        """
        The strike comes from the 09:15 index open, so it cannot be decided
        before 09:15. If the app is started early, wait — do not guess.

        On 08-Sep the app was started at 08:54 and fell back to the pre-market
        spot of 76132.81, choosing strike 76100. The real 09:15 open was
        75970.28, which is strike 76000. The client traded the wrong pair of
        options all morning and only discovered it on a restart at 11:38.
        A wrong strike is not a degraded trade, it is a different instrument.

        Two phases, reported differently because they are different things:

          before 09:15   the market is shut. Count down to the OPEN, not to
                         the first candle's close — an earlier build counted
                         to 09:17 while saying "market opens in", which was
                         wrong by two minutes.
          from 09:15     the market is open. Dhan publishes the 09:15 candle
                         while it is still forming, and a candle's OPEN is
                         fixed the instant it opens, so the strike resolves at
                         about 09:15:05 rather than waiting until 09:17.
        """
        anchor = session_anchor_epoch()          # 09:15:00 today

        val = get_sensex_open_0915()
        if val and time.time() >= anchor:
            log.info(f"  SENSEX 09:15 open = {val:.2f}")
            return val

        deadline = time.time() + timeout_s
        announced = False
        last_tick = 0.0
        while time.time() < deadline and not self.stop_event.is_set():
            now = time.time()

            # ── phase 1: the market has not opened ──
            if now < anchor:
                wait = int(anchor - now)
                if not announced:
                    log.info(f"  MARKET NOT OPEN YET. It opens at 09:15 "
                             f"({wait // 60}m {wait % 60}s away); the strike is "
                             f"read from that candle. Progress every "
                             f"{int(self.config.open_countdown_seconds // 60)} min.")
                    announced = True
                    last_tick = 0.0
                if now - last_tick >= self.config.open_countdown_seconds:
                    last_tick = now
                    mm = wait // 60
                    txt = f"{mm}m" if mm else f"{wait}s"
                    self._emit("waiting_for_open", {"seconds": wait, "text": txt})
                    self._emit("status", {"msg": f"Market opens in ~{txt}"})
                    if mm:
                        log.info(f"  market opens in about {mm} min")
                self.stop_event.wait(min(5.0, max(0.05, anchor - now)))
                continue

            # ── phase 2: open, waiting for the candle to appear ──
            val = get_sensex_open_0915()
            if val:
                log.info(f"  SENSEX 09:15 open = {val:.2f}  "
                         f"(resolved {int(now - anchor)}s after the open)")
                return val
            if now - last_tick >= 5.0:
                last_tick = now
                self._emit("waiting_for_open",
                           {"seconds": 0, "text": "reading 09:15 candle"})
                self._emit("status", {"msg": "Market open — reading the 09:15 candle"})
            log.info("  09:15 candle not published yet, retrying in 2s...")
            self.stop_event.wait(2.0)

        log.error("  Gave up waiting for the 09:15 open. Refusing to guess a "
                  "strike from the pre-market spot.")
        return 0.0

    def _resolve_legs(self, oc: dict) -> bool:
        found = 0
        for sk, sd in oc.items():
            try: sv = float(sk)
            except Exception: continue
            if abs(sv - self.strike) > 0.01: continue
            for side, key in (("CE", "ce"), ("PE", "pe")):
                if key not in sd: continue
                opt = sd[key]
                leg = self.legs[side]
                leg.sec_id = str(opt["security_id"])
                leg.strike = self.strike
                leg.label = f"SENSEX {self.strike} {side}"
                leg.ltp = float(opt.get("last_price", 0) or 0)
                self.by_sec[leg.sec_id] = leg
                found += 1
                log.info(f"  {leg.label}: secId={leg.sec_id} LTP=Rs.{leg.ltp:.2f}")
        if found < 2:
            self._emit("error", {"msg": f"Strike {self.strike} not fully listed "
                                        f"({found}/2 legs)"})
            return False
        return True

    def _seed_leg(self, leg: Leg):
        """Seed the continuous ATR from prior-session candles (spec 3.1)."""
        log.info(f"  Seeding indicators: {leg.label}...")
        raw = fetch_intraday_1m(leg.sec_id, SENSEX["segment"], "OPTIDX", days=10)

        # Dhan serves the minute that is still building. If it survives into
        # the seed, the last replayed 2-minute bar is built from a half-formed
        # minute, that wrong value is baked permanently into VWAP and ATR, and
        # seeded_upto primes the feed PAST it so the correct version never
        # arrives. Drop it here, before anything is computed.
        n_raw = len(raw or [])
        raw = RestCandleFeed.drop_forming(
            [{**c, "ts": _normalize_epoch(c["ts"])} for c in (raw or [])],
            time.time())
        if n_raw - len(raw):
            log.info(f"  [{leg.label}] excluded {n_raw - len(raw)} still-forming "
                     f"minute(s) from the seed")

        c2 = aggregate_2m(raw, self.config.agg_mode)

        anchor = session_anchor_epoch()
        prior = [c for c in c2 if c["ts"] < anchor]
        today = [c for c in c2 if c["ts"] >= anchor]

        leg.vwap = SessionVWAP(self.config.vwap_field)
        leg.atr = WilderATR(self.config.atr_period, self.config.atr_method)

        if len(prior) < self.config.atr_seed_bars:
            log.warning(f"  [{leg.label}] Only {len(prior)} prior candles "
                        f"(need {self.config.atr_seed_bars}). Wilder ATR has "
                        f"infinite memory, so a short seed will NOT match the "
                        f"terminal. Falling back to a session-only ATR; this leg "
                        f"waits for 14 candles before trading (edge case 6b).")
            leg.atr_ok = False
        else:
            leg.atr.seed(prior)          # feed everything we have
            leg.seed_bars = leg.atr.bars
            leg.seed_residual = leg.atr.convergence_error()
            leg.atr_ok = leg.atr.value is not None
            note = "converged" if leg.seed_residual < 1e-4 else "NOT CONVERGED"
            log.info(f"    ATR seeded from {leg.seed_bars} prior bars -> "
                     f"{leg.atr.value:.2f} | seed influence "
                     f"{leg.seed_residual:.2e} ({note})")
            self._emit("seeded", {"leg": leg.name, "bars": leg.seed_bars,
                                  "atr": leg.atr.value,
                                  "residual": leg.seed_residual})
            if leg.seed_residual >= 1e-4:
                log.warning(f"    [{leg.label}] Seed has not converged — ATR may "
                            f"disagree with the terminal. Pull more history.")

        # Replay any candles already elapsed today so VWAP and state are correct
        # when the engine is started mid-session.
        for c in today:
            self._process_candle(leg, c, replay=True)

        if not leg.atr_ok and leg.atr.value is not None:
            leg.atr_ok = True
            log.info(f"  [{leg.label}] Session-only ATR now valid: {leg.atr.value:.2f}")

        if self.config.data_source == "websocket":
            ce = CandleEngine(leg.sec_id, leg.label, 120,
                              on_close=self._on_candle_close)
            self.candle_engines[leg.sec_id] = ce
        leg.seeded_upto = today[-1]["ts"] if today else 0
        log.info(f"  [{leg.label}] seeded: {len(prior)} prior + {len(today)} today, "
                 f"ATR={leg.atr.value if leg.atr.value else 0:.2f}, "
                 f"VWAP={leg.vwap.value if leg.vwap.value else 0:.2f}")

    # ─── candle handling ───

    def _on_rest_bar(self, leg_name: str, bar: dict):
        leg = self.legs.get(leg_name)
        if not leg:
            return
        try:
            self._process_candle(leg, bar, replay=False)
            if leg.candles_seen <= 3 or leg.candles_seen % 30 == 0:
                log.info(f"  [{leg.label}] live bar "
                         f"{epoch_to_ist(bar['ts'], '%H:%M')} "
                         f"O{bar['open']:.2f} H{bar['high']:.2f} "
                         f"L{bar['low']:.2f} C{bar['close']:.2f} "
                         f"V{bar.get('volume', 0):.0f} | "
                         f"VWAP {leg.vwap.value:.2f} ATR {leg.atr.value:.2f}"
                         if leg.vwap.value and leg.atr.value else "")
        except Exception as e:
            log.error(f"  [{leg.label}] REST bar error: {e}")

    def _on_rest_latency(self, leg_name: str, ts: int, lag: float):
        self._emit("bar_latency", {"leg": leg_name, "lag": lag,
                                   "time": epoch_to_ist(ts, "%H:%M")})

    def _start_rest_feed(self):
        legs = {n: l.sec_id for n, l in self.legs.items() if l.sec_id}
        self.rest_feed = RestCandleFeed(
            legs=legs,
            fetch_1m=lambda sec, days=1: fetch_intraday_1m(
                sec, SENSEX["segment"], "OPTIDX", days=days),
            aggregate_fn=aggregate,
            anchor_of=lambda ts: session_anchor_epoch(
                datetime.fromtimestamp(_normalize_epoch(ts), tz=IST)),
            on_bar=self._on_rest_bar,
            on_latency=self._on_rest_latency,
            period=2, poll=self.config.rest_poll,
            grace=self.config.rest_grace, agg_mode=self.config.agg_mode,
            session_anchor=session_anchor_epoch())
        for name, leg in self.legs.items():
            if leg.seeded_upto:
                self.rest_feed.prime(name, leg.seeded_upto)
        self.rest_feed.start()

    def _on_candle_close(self, sec_id, candle):
        leg = self.by_sec.get(sec_id)
        if not leg: return
        try:
            self._process_candle(leg, candle, replay=False)
        except Exception as e:
            log.error(f"  [{leg.label}] Candle processing error: {e}")

    def _process_candle(self, leg: Leg, c: dict, replay: bool = False):
        """
        Ordering matters and follows spec 7:
          1. update indicators
          2. position exits driven by the close (VWAP)
          3. expire an unfilled armed order
          4. eligibility reset / new signal
        """
        anchor = session_anchor_epoch()
        if int(c["ts"]) < anchor:
            # Belt and braces. The feed already filters these, but a bar from a
            # previous session must never reach VWAP, ATR or the state machine
            # by any route — on 08-Sep 244 of them did, and every downstream
            # number was wrong for the rest of the day.
            if not replay:
                log.warning(f"  [{leg.label}] ignored a bar from "
                            f"{epoch_to_ist(c['ts'], '%d-%b %H:%M')} — before "
                            f"today's session")
            return

        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        # "volume" is ALWAYS this bar's own traded volume — historical candles
        # arrive that way from the API, and CandleEngine converts the live
        # cumulative day total before emitting. Feeding a running total in here
        # is what produced the VWAP mismatch against the terminal.
        bar_vol = float(c.get("volume", 0.0))

        vwap = leg.vwap.update(o, h, l, cl, bar_vol)
        atr = leg.atr.update(h, l, cl)
        leg.last_candle = c
        leg.candles_seen += 1
        if not leg.atr_ok and atr is not None:
            leg.atr_ok = True

        bkt = bucket_of(c["ts"])
        tstr = epoch_to_ist(c["ts"], "%H:%M")

        if not replay:
            candle_logger.log(date=now_ist().strftime("%Y-%m-%d"), time=tstr,
                              leg=leg.name, sec_id=leg.sec_id, open=f"{o:.2f}",
                              high=f"{h:.2f}", low=f"{l:.2f}", close=f"{cl:.2f}",
                              candle_vol=f"{bar_vol:.0f}",
                              cum_vol=f"{c.get('cum_vol', bar_vol):.0f}",
                              vwap=f"{vwap:.2f}" if vwap else "",
                              atr=f"{atr:.2f}" if atr else "",
                              state=leg.state.value, eligible=leg.eligible)
            self._emit("candle", {"leg": leg.name, "time": tstr, "open": o, "high": h,
                                  "low": l, "close": cl, "volume": bar_vol,
                                  "vwap": vwap, "atr": atr,
                                  "state": leg.state.value, "eligible": leg.eligible})

        if vwap is None:
            return  # no valid line yet — candle neither signals nor resets

        # ── 2. VWAP exit (unconditional, applies even to the fill candle) ──
        if leg.position and cl < vwap:
            if not replay:
                self._exit_all(leg, cl, "VWAP_CLOSE")
            else:
                leg.position = None
            leg.state = LegState.IDLE
            leg.eligible = True
            return

        # ── 3. expire an armed order that never filled ──
        if leg.state == LegState.ARMED and bkt > leg.armed_bucket:
            if not replay:
                log.info(f"  [{leg.label}] Order at {leg.trigger:.2f} not taken "
                         f"— cancelled. Leg stands down until a close below VWAP.")
                self._emit("order_cancelled", {"leg": leg.name, "trigger": leg.trigger})
            leg.state = LegState.STOOD_DOWN
            leg.signal_candle = None
            leg.trigger = 0.0

        # ── 4. eligibility / signal ──
        if cl < vwap:
            if not leg.eligible and not replay:
                log.info(f"  [{leg.label}] Close {cl:.2f} < VWAP {vwap:.2f} — RESET, "
                         f"leg is eligible again")
            leg.eligible = True
            if leg.state in (LegState.STOOD_DOWN,):
                leg.state = LegState.IDLE
            return

        if cl <= vwap:
            return  # exactly on the line holds

        # close is strictly above VWAP
        if leg.position or leg.state in (LegState.ARMED, LegState.HALTED):
            return
        if not leg.eligible:
            return
        if not leg.atr_ok or leg.atr.value is None:
            return
        if self.config.first_candle_can_signal is False and bkt == 0:
            return
        if not self._entry_window_open(c["ts"]):
            return

        self._arm(leg, c, vwap, replay)

    def _entry_window_open(self, ts) -> bool:
        """
        Spec 8: the signal candle must CLOSE at or before 14:45, and the trigger
        must fill by 14:45. `ts` is the bucket start, so the close is ts + 120.
        A candle closing at 14:44 may arm; the order then lives into the
        14:44-14:46 candle but can only fill while the clock is <= 14:45.
        """
        close_t = datetime.fromtimestamp(_normalize_epoch(int(ts)) + 120, tz=IST).time()
        return close_t <= hhmm(self.config.last_entry)

    def _fill_window_open(self) -> bool:
        return now_ist().time() <= hhmm(self.config.last_entry)

    def _arm(self, leg: Leg, c: dict, vwap: float, replay: bool):
        cfg = self.config
        trigger = round(c["high"] + cfg.entry_buffer, 2)
        stop = round(c["low"] - cfg.stop_buffer, 2)

        leg.eligible = False        # spent on PLACEMENT, filled or not
        leg.attempts += 1

        if cfg.min_premium > 0 and c["close"] < cfg.min_premium:
            if not replay:
                log.info(f"  [{leg.label}] Signal suppressed — premium "
                         f"{c['close']:.2f} below floor {cfg.min_premium:.2f}. "
                         f"Attempt still consumed (edge case 8).")
            leg.state = LegState.STOOD_DOWN
            return

        if trigger - stop < 0.15:
            if not replay:
                log.info(f"  [{leg.label}] Signal skipped — stop {stop:.2f} too close "
                         f"to trigger {trigger:.2f}.")
            leg.state = LegState.STOOD_DOWN
            return

        leg.signal_candle = dict(c)
        leg.signal_candle["vwap"] = vwap
        leg.signal_candle["atr"] = leg.atr.value
        leg.trigger = trigger
        leg.pending_stop = stop
        leg.armed_bucket = bucket_of(c["ts"])
        leg.state = LegState.ARMED

        if not replay:
            a = leg.atr.value
            log.info(f"  [{leg.label}] SIGNAL @ {epoch_to_ist(c['ts'], '%H:%M')} "
                     f"close={c['close']:.2f} > VWAP={vwap:.2f} | "
                     f"trigger={trigger:.2f} stop={stop:.2f} ATR={a:.2f} | "
                     f"T1={trigger + 2*a:.2f} T2={trigger + 4*a:.2f}")
            self._emit("armed", {"leg": leg.name, "trigger": trigger, "stop": stop,
                                 "atr": a, "vwap": vwap,
                                 "t1": trigger + 2 * a, "t2": trigger + 4 * a})

    # ─── tick handling ───

    def _on_tick(self, leg: Leg, ltp: float):
        leg.ltp = ltp
        if leg.state == LegState.ARMED:
            if not self._fill_window_open():
                log.info(f"  [{leg.label}] Past {self.config.last_entry} — resting "
                         f"order cancelled unfilled.")
                leg.state = LegState.STOOD_DOWN
                leg.trigger = 0.0
                return
            if ltp >= leg.trigger:
                self._fill(leg, ltp)
            return
        if leg.position:
            self._manage(leg, ltp)

    def _fill(self, leg: Leg, ltp: float):
        cfg = self.config
        if cfg.max_slippage > 0 and ltp > leg.trigger + cfg.max_slippage:
            log.warning(f"  [{leg.label}] Trigger gapped through "
                        f"({ltp:.2f} vs {leg.trigger:.2f}) — entry skipped.")
            leg.state = LegState.STOOD_DOWN
            return

        qty = cfg.lots * self.lot_size
        seg = SENSEX["segment"]

        if cfg.paper_mode:
            fill_price = leg.trigger
            oid = "PAPER"
            ok = True
        else:
            res = place_order_limit_ioc(leg.sec_id, seg, "BUY", qty, leg.trigger)
            ok = res.get("filled", False)
            fill_price = res.get("price", 0) or leg.trigger
            oid = res.get("order_id", "")
            if not ok:
                log.error(f"  [{leg.label}] Entry order failed — leg stands down.")
                leg.state = LegState.STOOD_DOWN
                return

        self._trade_seq += 1
        sig = leg.signal_candle or {}
        pos = Position(
            leg=leg.name, sec_id=leg.sec_id, strike=leg.strike,
            trade_id=f"{leg.name}-{self._trade_seq}-{now_ist().strftime('%H%M%S')}",
            E=leg.trigger,                    # ladder reference = trigger, not fill
            fill_price=fill_price,
            atr_frozen=float(sig.get("atr") or leg.atr.value or 0),
            stop=leg.pending_stop, initial_stop=leg.pending_stop,
            lots_open=cfg.lots, lot_size=self.lot_size,
            entry_order_id=oid, signal=sig)

        leg.position = pos
        leg.state = LegState.IN_TRADE
        leg.trades += 1

        icon = "PAPER" if cfg.paper_mode else "LIVE"
        log.info(f"  [{leg.label}] ENTRY ({icon}) {cfg.lots} lots x {self.lot_size} "
                 f"@ E={pos.E:.2f} fill={fill_price:.2f} stop={pos.stop:.2f} "
                 f"ATR={pos.atr_frozen:.2f}")
        self._emit("entry", {"leg": leg.name, "strike": leg.strike, "E": pos.E,
                             "fill": fill_price, "stop": pos.stop,
                             "atr": pos.atr_frozen, "qty": qty,
                             "paper": cfg.paper_mode})

    def _manage(self, leg: Leg, ltp: float):
        """Ladder + trailing stop. Price-driven events, evaluated on every tick."""
        pos = leg.position
        if not pos: return
        cfg = self.config

        # Rungs latch: walk up as far as price has reached this tick.
        # A zero/absent ATR would make every rung equal E and spin forever.
        n = pos.highest_rung + 1
        while pos.atr_frozen > 0 and ltp >= pos.rung_price(n, cfg.rung_atr_mult):
            pos.highest_rung = n
            if n == 1:
                self._scale_out(leg, pos.rung_price(1, cfg.rung_atr_mult), "T1")
                pos.stop = round(pos.E + cfg.cost_plus, 2)
            elif n == 2:
                self._scale_out(leg, pos.rung_price(2, cfg.rung_atr_mult), "T2")
                pos.stop = round(pos.rung_price(1, cfg.rung_atr_mult), 2)
            else:
                pos.stop = round(pos.rung_price(n - 1, cfg.rung_atr_mult), 2)
            log.info(f"  [{leg.label}] T{n} reached @ {pos.rung_price(n, cfg.rung_atr_mult):.2f} "
                     f"-> stop {pos.stop:.2f}")
            self._emit("rung", {"leg": leg.name, "rung": n, "stop": pos.stop,
                                "price": pos.rung_price(n, cfg.rung_atr_mult)})
            if not leg.position:   # scale-out closed everything
                return
            n += 1

        if ltp <= pos.stop:
            reason = "STOP" if pos.highest_rung == 0 else f"TRAIL_T{pos.highest_rung}"
            self._exit_all(leg, pos.stop, reason)
            leg.state = LegState.STOOD_DOWN     # a finished trade spends the attempt

    def _scale_out(self, leg: Leg, price: float, tag: str):
        pos = leg.position
        if not pos or pos.lots_open <= 1:
            return
        qty = self.lot_size
        exit_price = price
        oid = "PAPER"
        if not self.config.paper_mode:
            res = place_order_market(pos.sec_id, SENSEX["segment"], "SELL", qty)
            if res.get("filled"):
                exit_price = res["price"]
            oid = res.get("order_id", "")
            if not res.get("filled"):
                log.error(f"  [{leg.label}] {tag} scale-out failed — retrying as full exit")
                self._exit_all(leg, price, f"{tag}_FAILED")
                return
        pnl_pts = exit_price - pos.E
        pnl_val = pnl_pts * qty
        pos.lots_open -= 1
        pos.realised += pnl_val
        leg.pnl += pnl_val
        self._log_lot(leg, pos, exit_price, qty, pnl_pts, pnl_val, tag, oid)
        log.info(f"  [{leg.label}] {tag} sold 1 lot @ {exit_price:.2f} "
                 f"PnL=Rs.{pnl_val:+,.0f} ({pos.lots_open} lots left)")
        self._emit("scale_out", {"leg": leg.name, "tag": tag, "price": exit_price,
                                 "pnl": pnl_val, "lots_left": pos.lots_open})

    def _exit_all(self, leg: Leg, price: float, reason: str):
        pos = leg.position
        if not pos: return
        qty = pos.lots_open * self.lot_size
        exit_price = price
        oid = "PAPER"
        if not self.config.paper_mode and qty > 0:
            res = place_order_market(pos.sec_id, SENSEX["segment"], "SELL", qty)
            if res.get("filled"):
                exit_price = res["price"]
            oid = res.get("order_id", "")
        pnl_pts = exit_price - pos.E
        pnl_val = pnl_pts * qty
        pos.realised += pnl_val
        leg.pnl += pnl_val
        self._log_lot(leg, pos, exit_price, qty, pnl_pts, pnl_val, reason, oid)
        log.info(f"  [{leg.label}] EXIT {reason} @ {exit_price:.2f} "
                 f"qty={qty} PnL=Rs.{pnl_val:+,.0f} | trade total Rs.{pos.realised:+,.0f}")
        self.closed.append({"trade_id": pos.trade_id, "leg": leg.name,
                            "pnl": pos.realised, "reason": reason})
        self._emit("trade_closed", {"leg": leg.name, "pnl": pos.realised,
                                    "reason": reason, "rung": pos.highest_rung})
        leg.position = None
        self._check_daily_loss()

    def _log_lot(self, leg, pos, exit_price, qty, pnl_pts, pnl_val, reason, oid):
        sig = pos.signal or {}
        trade_logger.log_exit(
            date=now_ist().strftime("%Y-%m-%d"), time=now_ist().strftime("%H:%M:%S"),
            trade_id=pos.trade_id, leg=leg.name, strike=pos.strike,
            lot=f"{pos.lots_open}", signal_time=epoch_to_ist(sig.get("ts", 0), "%H:%M"),
            signal_high=f"{sig.get('high', 0):.2f}", signal_low=f"{sig.get('low', 0):.2f}",
            signal_close=f"{sig.get('close', 0):.2f}",
            signal_vwap=f"{sig.get('vwap', 0):.2f}", atr_frozen=f"{pos.atr_frozen:.2f}",
            trigger_E=f"{pos.E:.2f}", fill_price=f"{pos.fill_price:.2f}",
            exit_price=f"{exit_price:.2f}", qty=qty, pnl_points=f"{pnl_pts:.2f}",
            pnl_value=f"{pnl_val:.2f}", exit_reason=reason,
            rung_reached=pos.highest_rung,
            hold_seconds=int(time.time() - pos.opened_at),
            entry_order_id=pos.entry_order_id, exit_order_id=oid)

    def _check_daily_loss(self):
        lim = self.config.daily_loss_limit
        if lim <= 0: return
        total = sum(l.pnl for l in self.legs.values())
        if total <= -abs(lim):
            log.error(f"  DAILY LOSS LIMIT hit (Rs.{total:+,.0f}) — halting.")
            self._halt("DAILY_LOSS")

    def _halt(self, reason: str):
        self._halted = True
        if self.rest_feed:
            self.rest_feed.stop()
            log.info(f"  REST feed stopped ({reason})")
        for leg in self.legs.values():
            if leg.position:
                self._exit_all(leg, leg.ltp or leg.position.E, reason)
            leg.state = LegState.HALTED
        self._emit("halted", {"reason": reason})

    # ─── square-off ───

    def _check_square_off(self):
        if self._halted: return
        if now_ist().time() >= hhmm(self.config.square_off):
            for leg in self.legs.values():
                if leg.position:
                    log.warning(f"  [{leg.label}] 15:00 SQUARE-OFF")
                    self._exit_all(leg, leg.ltp or leg.position.E, "SQUARE_OFF")
                leg.state = LegState.HALTED
            self._halted = True
            if self.rest_feed:
                self.rest_feed.stop()
            log.info(f"  {self.config.square_off} SQUARE-OFF — engine halted, "
                     f"REST feed stopped. Nothing further will be polled or "
                     f"traded today.")
            self._emit("halted", {"reason": "SQUARE_OFF"})

    # ─── websocket ───

    def _subscribe(self, ws):
        idx = [{"ExchangeSegment": "IDX_I", "SecurityId": SENSEX["security_id"]}]
        opts = [{"ExchangeSegment": SENSEX["segment"], "SecurityId": l.sec_id}
                for l in self.legs.values() if l.sec_id]

        if self.config.data_source == "rest":
            # Candles and volume come from REST, so the feed only has to carry
            # LTP for the trigger, the stop and the ladder. Ticker mode (15) is
            # enough, which keeps the unverified quote-packet layout out of the
            # critical path entirely.
            ws.send(json.dumps({"RequestCode": 15,
                                "InstrumentCount": len(idx) + len(opts),
                                "InstrumentList": idx + opts}))
            log.info(f"WebSocket subscribed (ticker/LTP only) — "
                     f"index + {len(opts)} legs. Candles come from REST.")
        else:
            ws.send(json.dumps({"RequestCode": 15, "InstrumentCount": len(idx),
                                "InstrumentList": idx}))
            # Quote mode (17) carries traded volume, which tick-built VWAP needs.
            ws.send(json.dumps({"RequestCode": 17, "InstrumentCount": len(opts),
                                "InstrumentList": opts}))
            log.info(f"WebSocket subscribed — index (ticker) + "
                     f"{len(opts)} legs (quote)")
        self._emit("ws_connected", {"instruments": len(idx) + len(opts),
                                    "mode": self.config.data_source})

    def _on_ws_open(self, ws):
        self.ws_connected.set()
        try:
            self._subscribe(ws)
        except Exception as e:
            log.error(f"Subscribe error: {e}")

    def _on_ws_message(self, ws, msg):
        if isinstance(msg, str): return
        hdr = parse_header(bytes(msg))
        if not hdr: return
        self.packet_count += 1
        self.last_tick_at = time.time()
        sec_id = hdr["security_id"]
        code = hdr["resp_code"]

        if code == 2:
            t = parse_ticker(hdr["payload"])
            if not t: return
            if sec_id == SENSEX["security_id"]:
                self.spot = t["ltp"]
                self._emit("spot_tick", {"spot": t["ltp"]})
                return
            leg = self.by_sec.get(sec_id)
            if leg:
                ce = self.candle_engines.get(sec_id)
                if ce:
                    ce.on_tick(t["ltp"], t["ltt"])
                # Always drive execution from the tick, whichever source owns
                # the candles: the trigger, the stop and the rungs need price
                # resolution finer than a bar.
                self._on_tick(leg, t["ltp"])
            return

        if code == 4:
            q = parse_quote(hdr["payload"])
            if not q: return
            leg = self.by_sec.get(sec_id)
            if not leg: return
            if self._quote_debug < 3:
                self._quote_debug += 1
                log.info(f"  [QUOTE CHECK] {leg.label} ltp={q['ltp']:.2f} "
                         f"vol={q['volume']} atp={q['atp']:.2f} "
                         f"ltt={epoch_to_ist(q['ltt'])} — verify against the terminal")
            ce = self.candle_engines.get(sec_id)
            if ce: ce.on_tick(q["ltp"], q["ltt"], cum_vol=q["volume"])
            self._on_tick(leg, q["ltp"])
            return

    def _on_ws_error(self, ws, error):
        log.error(f"WS error: {error}")

    def _on_ws_close(self, ws, code, msg):
        self.ws_connected.clear()
        log.warning(f"WS closed: {code} {msg}")
        self._emit("ws_disconnected", {})

    def _ws_watchdog(self):
        """
        A websocket can stay 'connected' and deliver nothing.

        On 08-Sep the feed dropped four times with ping/pong timeouts and lost
        connections. Reconnecting on a visible close is easy; the dangerous
        case is a socket that is open but silent, because the stop and the
        ladder run on LTP. If no tick arrives for a while during market hours,
        force a reconnect rather than trusting the connection.
        """
        while not self.stop_event.is_set():
            self.stop_event.wait(10.0)
            if self.stop_event.is_set() or self._halted:
                continue
            t = now_ist().time()
            if not (hhmm("09:15") <= t <= hhmm("15:30")):
                continue
            if not self.last_tick_at:
                continue
            quiet = time.time() - self.last_tick_at
            if quiet > self.config.ws_silence_seconds:
                log.warning(f"  No websocket tick for {quiet:.0f}s during market "
                            f"hours — forcing a reconnect.")
                self.last_tick_at = time.time()
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass

    def _run_ws(self):
        while not self.stop_event.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    WS_URL, on_open=self._on_ws_open, on_message=self._on_ws_message,
                    on_error=self._on_ws_error, on_close=self._on_ws_close)
                # 20/10 proved too tight on a real connection: four drops in a
                # session, all client-side ping timeouts. Give the server room.
                self.ws.run_forever(ping_interval=30, ping_timeout=15)
            except Exception as e:
                log.error(f"WS exception: {e}")
            if not self.stop_event.is_set():
                log.info("WS reconnecting in 2s...")
                time.sleep(2)

    # ─── summary ───

    def get_summary(self):
        """
        P&L is reported in three parts.

        `leg.pnl` only ever accumulates on a scale-out or an exit, so while a
        position is open it contributes nothing — the counter sat at zero with
        a live trade on screen. Unrealised mark-to-market is computed here from
        the last traded price against the ladder reference E, the same basis
        the exits use, so realised and unrealised are directly comparable.
        """
        legs = []
        total = 0.0
        realised_total = 0.0
        unreal_total = 0.0
        for name, leg in self.legs.items():
            pos = leg.position
            unreal = 0.0
            if pos and leg.ltp and pos.lots_open:
                unreal = (leg.ltp - pos.E) * pos.lots_open * pos.lot_size
            legs.append({
                "leg": name, "label": leg.label, "strike": leg.strike,
                "state": leg.state.value, "eligible": leg.eligible,
                "ltp": leg.ltp, "vwap": leg.vwap.value, "atr": leg.atr.value,
                "pnl": leg.pnl, "trades": leg.trades, "attempts": leg.attempts,
                "candles": leg.candles_seen,
                "realised": leg.pnl, "unrealised": unreal,
                "open_pnl": leg.pnl + unreal,
                "trigger": leg.trigger if leg.state == LegState.ARMED else 0,
                "stop": pos.stop if pos else 0,
                "lots_open": pos.lots_open if pos else 0,
                "rung": pos.highest_rung if pos else 0,
                "E": pos.E if pos else 0,
            })
            realised_total += leg.pnl
            unreal_total += unreal
            total += leg.pnl + unreal
        feed = self.rest_feed.health() if self.rest_feed else None
        return {"legs": legs, "total_pnl": total,
                "realised_pnl": realised_total, "unrealised_pnl": unreal_total,
                "spot": self.spot,
                "feed": feed, "data_source": self.config.data_source,
                "strike": self.strike, "expiry": self.expiry,
                "lot_size": self.lot_size, "packets": self.packet_count,
                "halted": self._halted}

    # ─── run / stop ───

    def run(self):
        if not self.initialize():
            self._emit("error", {"msg": "Initialization failed"})
            return

        if self.config.data_source == "rest":
            self._start_rest_feed()

        threading.Thread(target=self._run_ws, daemon=True).start()
        threading.Thread(target=self._ws_watchdog, daemon=True).start()
        self._emit("status", {"msg": "Waiting for WebSocket..."})
        self.ws_connected.wait(timeout=20)
        if not self.ws_connected.is_set():
            self._emit("error", {"msg": "WebSocket failed"}); return
        self._emit("status", {"msg": "LIVE — Monitoring"})

        while not self.stop_event.is_set():
            try:
                self._check_square_off()
                self._emit("tick_update", self.get_summary())
                time.sleep(1)
            except Exception as e:
                log.error(f"Monitor error: {e}")
                time.sleep(1)

    def stop(self):
        self.stop_event.set()
        if self.rest_feed:
            h = self.rest_feed.health()
            if h["bars"]:
                log.info(f"  REST feed: {h['bars']} bars, avg lag "
                         f"{h['avg_lag']:.1f}s, max {h['max_lag']:.1f}s "
                         f"({h['verdict']})")
                if h.get("late_bars"):
                    log.warning(f"  {h['late_bars']} bar(s) arrived too late to "
                                f"be traded on.")
                if h.get("stalls"):
                    log.warning(f"  REST feed stalled {h['stalls']} time(s) — "
                                f"longest {self.rest_feed.last_stall:.0f}s. The "
                                f"strategy was blind for those periods.")
                if h.get("dropped_stale"):
                    log.info(f"  {h['dropped_stale']} bar(s) from a previous "
                             f"session were correctly ignored.")
            self.rest_feed.stop()
        if self.ws:
            try: self.ws.close()
            except Exception: pass
        log.info("Engine stopped")


# ═══════════════════════════════════════════════════════════════════════════
# STANDALONE
# ═══════════════════════════════════════════════════════════════════════════

def _console_monitor():
    """
    Print the same things the GUI shows, to the terminal.

    Every closed bar, every state change, and a status line each minute. This
    is the observation surface for a paper session run from VS Code — the log
    file records everything, but this is what you actually watch.
    """
    state = {"last_status": 0.0, "bars": 0}

    def cb(event, d):
        if event == "candle":
            state["bars"] += 1
            v, a = d.get("vwap"), d.get("atr")
            print(f"  {d['leg']:<3} {d['time']}  "
                  f"O{d['open']:>8.2f} H{d['high']:>8.2f} L{d['low']:>8.2f} "
                  f"C{d['close']:>8.2f} V{d.get('volume', 0):>9.0f}  "
                  f"vwap {v:>8.2f}  atr {a:>6.2f}  "
                  f"{'ABOVE' if v and d['close'] > v else 'below':<5} "
                  f"{d.get('state', ''):<10} "
                  f"{'eligible' if d.get('eligible') else 'spent'}"
                  if v and a else
                  f"  {d['leg']:<3} {d['time']}  warming up")

        elif event == "waiting_for_open":
            print(f"  market opens in ~{d.get('text','?')} "
                  f"(the strike comes from the 09:15 candle)")
        elif event == "strike":
            print(f"\n  09:15 open {d.get('open', 0):.2f} -> strike "
                  f"{d.get('strike')}   expiry {d.get('expiry')}   "
                  f"lot {d.get('lot_size')}\n")
        elif event == "seeded":
            note = "converged" if d.get("residual", 1) < 1e-4 else "NOT CONVERGED"
            print(f"  [{d['leg']}] ATR seeded {d['bars']} bars -> "
                  f"{d.get('atr', 0):.2f}  ({note})")
        elif event == "ws_connected":
            print(f"  websocket up — {d.get('instruments')} instruments, "
                  f"candles from {d.get('mode')}\n")
            print(f"  {'leg':<4}{'time':<7}{'open':>9}{'high':>9}{'low':>9}"
                  f"{'close':>9}{'volume':>10}   {'vwap':>8}   {'atr':>6}")
            print("  " + "-" * 96)
        elif event == "armed":
            print(f"\n  >> [{d['leg']}] ARMED  trigger {d['trigger']:.2f}  "
                  f"stop {d['stop']:.2f}  atr {d['atr']:.2f}  "
                  f"T1 {d['t1']:.2f}  T2 {d['t2']:.2f}\n")
        elif event == "order_cancelled":
            print(f"\n  >> [{d['leg']}] order {d['trigger']:.2f} NOT TAKEN — "
                  f"leg stands down until a close below VWAP\n")
        elif event == "entry":
            print(f"\n  >> [{d['leg']}] ENTRY {'(paper)' if d.get('paper') else 'LIVE'}"
                  f"  E {d['E']:.2f}  fill {d['fill']:.2f}  qty {d['qty']}  "
                  f"stop {d['stop']:.2f}\n")
        elif event == "rung":
            print(f"     T{d['rung']} at {d['price']:.2f} -> stop {d['stop']:.2f}")
        elif event == "scale_out":
            print(f"     {d['tag']} sold 1 lot at {d['price']:.2f}  "
                  f"pnl {d['pnl']:+,.0f}  ({d['lots_left']} left)")
        elif event == "trade_closed":
            print(f"\n  >> [{d['leg']}] CLOSED {d['reason']} at rung T{d['rung']}"
                  f"  pnl {d['pnl']:+,.0f}\n")
        elif event == "halted":
            print(f"\n  >> HALTED — {d.get('reason')}\n")
        elif event == "error":
            print(f"  !! {d.get('msg')}")
        elif event == "tick_update":
            now = time.time()
            if now - state["last_status"] < 60:
                return
            state["last_status"] = now
            f = d.get("feed") or {}
            lag = (f"lag avg {f['avg_lag']:.1f}s max {f['max_lag']:.1f}s"
                   if f.get("bars") else "no bars yet")
            parts = []
            for lg in d.get("legs", []):
                parts.append(f"{lg['leg']} {lg['state']:<10} "
                             f"ltp {lg['ltp']:>8.2f} "
                             f"vwap {(lg['vwap'] or 0):>8.2f} "
                             f"{'elig' if lg['eligible'] else 'spent'}")
            r, u = d.get("realised_pnl", 0), d.get("unrealised_pnl", 0)
            print(f"  [{now_ist():%H:%M:%S}] spot {d.get('spot', 0):>9.2f}  "
                  f"pnl {d.get('total_pnl', 0):+,.0f} "
                  f"(booked {r:+,.0f} / open {u:+,.0f})  {lag}")
            for pt in parts:
                print(f"             {pt}")
    return cb


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="SENSEX VWAP Ladder — console runner (paper by default)")
    ap.add_argument("--live", action="store_true",
                    help="place REAL orders. Paper mode is the default.")
    ap.add_argument("--source", choices=["rest", "websocket"], default="rest",
                    help="where candles come from")
    ap.add_argument("--field", default="ohlc4",
                    choices=["ohlc4", "hlc3", "hl2", "close"])
    ap.add_argument("--atr-method", choices=["wilder", "sma"], default="wilder")
    ap.add_argument("--agg", choices=["count", "clock"], default="count")
    ap.add_argument("--poll", type=float, default=4.0)
    ap.add_argument("--lots", type=int, default=3)
    args = ap.parse_args()

    if not DHAN_CLIENT_ID or not DHAN_PIN or not DHAN_TOTP_SECRET:
        raise SystemExit("Missing credentials in .env")

    cfg = StrategyConfig(paper_mode=not args.live, data_source=args.source,
                         vwap_field=args.field, atr_method=args.atr_method,
                         agg_mode=args.agg, rest_poll=args.poll, lots=args.lots)

    print("=" * 100)
    print(f"  SENSEX VWAP Ladder — {'LIVE ORDERS' if args.live else 'PAPER'}"
          f"   candles={cfg.data_source}  vwap={cfg.vwap_field}  "
          f"atr={cfg.atr_method}  rollup={cfg.agg_mode}  lots={cfg.lots}")
    print(f"  logs -> {LOG_DIR}")
    print("=" * 100)
    if args.live:
        print("\n  LIVE MODE. Real orders will be placed. Ctrl+C within 5s to abort.")
        time.sleep(5)

    init_credentials()
    engine = SensexVWAPLadderEngine(cfg, gui_callback=_console_monitor())
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n  stopping...")
        engine.stop()
        summary = engine.get_summary()
        print(f"\n  Day P&L {summary['total_pnl']:+,.0f}")
        for lg in summary["legs"]:
            print(f"    {lg['leg']}  {lg['attempts']} attempt(s), "
                  f"{lg['trades']} filled, pnl {lg['pnl']:+,.0f}")
        f = summary.get("feed") or {}
        if f.get("bars"):
            print(f"    feed: {f['bars']} bars, lag avg {f['avg_lag']:.1f}s "
                  f"max {f['max_lag']:.1f}s ({f['verdict']})")
        print(f"\n  logs written to {LOG_DIR}\n")


if __name__ == "__main__":
    main()
