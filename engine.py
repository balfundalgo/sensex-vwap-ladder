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

import requests
import pyotp
import websocket
from dotenv import load_dotenv, set_key

# Indicators live in their own module so that the live engine, the replay path
# and reconcile.py are guaranteed to compute identically. See indicators.py for
# the ChartIQ definitions these implement.
from indicators import SessionVWAP, WilderATR, price_field, true_range

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
            return requests.get(f"{BASE_URL}/profile", headers=h, timeout=10).status_code == 200
        except Exception:
            return False

    def renew(self, token: str) -> Optional[str]:
        try:
            h = {"access-token": token, "dhanClientId": DHAN_CLIENT_ID,
                 "Content-Type": "application/json"}
            d = requests.get(f"{BASE_URL}/RenewToken", headers=h, timeout=15).json()
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
                d = requests.post(url, params=params, timeout=15).json()
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
        r = requests.get(MASTER_URL, stream=True, timeout=120); r.raise_for_status()
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
    ts = int(ts); now_ts = int(time.time())
    if int(4.5 * 3600) <= (ts - now_ts) <= int(6.5 * 3600): ts -= 19800
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


def api_post(endpoint, payload, retries=2):
    if "optionchain" in endpoint: OC_GATE.wait()
    else: DATA_GATE.wait()
    for att in range(retries + 1):
        try:
            r = requests.post(f"{BASE_URL}{endpoint}", headers=_hdrs(),
                              json=payload, timeout=20)
            if r.status_code == 200: return r.json()
            if r.status_code == 429:
                time.sleep(2 ** (att + 1)); continue
            log.error(f"  API {endpoint} -> HTTP {r.status_code}: {r.text[:200]}")
            if att < retries: time.sleep(1.5)
        except Exception as e:
            log.error(f"  API {endpoint} error: {e}")
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
            r = requests.post(f"{BASE_URL}/orders", headers=_hdrs(), json=pl, timeout=15)
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
                            pr = requests.get(f"{BASE_URL}/orders/{oid}",
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
                        requests.delete(f"{BASE_URL}/orders/{oid}", headers=_hdrs(), timeout=10)
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
        r = requests.post(f"{BASE_URL}/orders", headers=_hdrs(), json=pl, timeout=15)
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
                    pr = requests.get(f"{BASE_URL}/orders/{oid}", headers=_hdrs(), timeout=10)
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
    resp = api_post("/charts/intraday", payload)
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


def aggregate_2m(candles_1m: List[dict]) -> List[dict]:
    """Aggregate 1-minute candles into session-anchored 2-minute candles."""
    buckets: Dict[int, dict] = {}
    for c in candles_1m:
        ts = _normalize_epoch(c["ts"])
        if ts <= 0: continue
        key = bucket_start_epoch(ts)
        b = buckets.get(key)
        if b is None:
            buckets[key] = {"ts": key, "open": c["open"], "high": c["high"],
                            "low": c["low"], "close": c["close"],
                            "volume": c["volume"]}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c["volume"]
    return [buckets[k] for k in sorted(buckets)]


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
        ltp = float(ltp); ltt = _normalize_epoch(int(ltt))
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
        self.ws = None
        self.ws_connected = threading.Event()

        self.trade_lock = threading.Lock()
        self.closed: List[dict] = []
        self.packet_count = 0
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

        self.sensex_open = get_sensex_open_0915() or 0.0
        if self.sensex_open <= 0:
            oc = fetch_option_chain(self.expiry)
            if oc:
                self.sensex_open = oc["spot_price"]
                log.warning("  09:15 open unavailable — falling back to current spot. "
                            "The strike may differ from the intended one.")
            else:
                self._emit("error", {"msg": "Could not resolve SENSEX open"}); return False

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
        c2 = aggregate_2m(raw)

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

        ce = CandleEngine(leg.sec_id, leg.label, 120, on_close=self._on_candle_close)
        self.candle_engines[leg.sec_id] = ce
        log.info(f"  [{leg.label}] seeded: {len(prior)} prior + {len(today)} today, "
                 f"ATR={leg.atr.value if leg.atr.value else 0:.2f}, "
                 f"VWAP={leg.vwap.value if leg.vwap.value else 0:.2f}")

    # ─── candle handling ───

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
                              cum_vol=f"{c.get('cum_vol', 0):.0f}",
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
            self._emit("halted", {"reason": "SQUARE_OFF"})

    # ─── websocket ───

    def _subscribe(self, ws):
        idx = [{"ExchangeSegment": "IDX_I", "SecurityId": SENSEX["security_id"]}]
        ws.send(json.dumps({"RequestCode": 15, "InstrumentCount": len(idx),
                            "InstrumentList": idx}))
        opts = [{"ExchangeSegment": SENSEX["segment"], "SecurityId": l.sec_id}
                for l in self.legs.values() if l.sec_id]
        # RequestCode 17 = Quote mode. Required because VWAP needs traded volume,
        # which the ticker packet does not carry.
        ws.send(json.dumps({"RequestCode": 17, "InstrumentCount": len(opts),
                            "InstrumentList": opts}))
        log.info(f"WebSocket subscribed — index (ticker) + {len(opts)} legs (quote)")
        self._emit("ws_connected", {"instruments": len(idx) + len(opts)})

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
                if ce: ce.on_tick(t["ltp"], t["ltt"])
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

    def _run_ws(self):
        while not self.stop_event.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    WS_URL, on_open=self._on_ws_open, on_message=self._on_ws_message,
                    on_error=self._on_ws_error, on_close=self._on_ws_close)
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.error(f"WS exception: {e}")
            if not self.stop_event.is_set():
                log.info("WS reconnecting in 2s...")
                time.sleep(2)

    # ─── summary ───

    def get_summary(self):
        legs = []
        total = 0.0
        for name, leg in self.legs.items():
            pos = leg.position
            legs.append({
                "leg": name, "label": leg.label, "strike": leg.strike,
                "state": leg.state.value, "eligible": leg.eligible,
                "ltp": leg.ltp, "vwap": leg.vwap.value, "atr": leg.atr.value,
                "pnl": leg.pnl, "trades": leg.trades, "attempts": leg.attempts,
                "candles": leg.candles_seen,
                "trigger": leg.trigger if leg.state == LegState.ARMED else 0,
                "stop": pos.stop if pos else 0,
                "lots_open": pos.lots_open if pos else 0,
                "rung": pos.highest_rung if pos else 0,
                "E": pos.E if pos else 0,
            })
            total += leg.pnl
        return {"legs": legs, "total_pnl": total, "spot": self.spot,
                "strike": self.strike, "expiry": self.expiry,
                "lot_size": self.lot_size, "packets": self.packet_count,
                "halted": self._halted}

    # ─── run / stop ───

    def run(self):
        if not self.initialize():
            self._emit("error", {"msg": "Initialization failed"})
            return

        threading.Thread(target=self._run_ws, daemon=True).start()
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
        if self.ws:
            try: self.ws.close()
            except Exception: pass
        log.info("Engine stopped")


# ═══════════════════════════════════════════════════════════════════════════
# STANDALONE
# ═══════════════════════════════════════════════════════════════════════════

def main():
    if not DHAN_CLIENT_ID or not DHAN_PIN or not DHAN_TOTP_SECRET:
        raise SystemExit("Missing credentials in .env")
    init_credentials()
    engine = SensexVWAPLadderEngine(StrategyConfig(paper_mode=True))
    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
