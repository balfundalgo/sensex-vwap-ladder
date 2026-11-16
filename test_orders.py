"""
Order-path tests — the live failure modes, offline.

13-Nov: a market order was accepted, the fill was not reported within four
seconds, the engine declared "Entry order failed" and walked away. The client
was left holding a real position with no stop, no ladder and no square-off.
It happened twice in eight minutes.

Nothing here touches the network.
"""
import sys
import types
import logging

logging.disable(logging.ERROR)

import engine as E
from engine import SensexVWAPLadderEngine, StrategyConfig, LegState, WilderATR

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          f"{'' if cond else '   ' + str(extra)}")
    if not cond:
        FAILS.append(name)


class R:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload
        self.text = str(payload)

    def json(self):
        return self._p


def wire(order_post, order_get):
    E.DHAN_CLIENT_ID = "X"
    E.HEADERS.update({"access-token": "t", "client-id": "X"})
    E.ORDER_GATE = types.SimpleNamespace(wait=lambda: None)
    E.ORDER_SESSION = types.SimpleNamespace(
        post=order_post, get=order_get, delete=lambda *a, **k: R(200, {}))


def armed_engine(paper=False, broker_qty=0, broker_avg=0.0):
    cfg = StrategyConfig(paper_mode=paper, lot_multiplier=1)
    e = SensexVWAPLadderEngine(cfg)
    e.lot_size = 20
    leg = e.legs["PE"]
    leg.sec_id = "822216"; leg.label = "PE"; leg.strike = 81800
    leg.state = LegState.ARMED
    leg.trigger = 777.25
    leg.pending_stop = 756.90
    leg.pending_atr = 20.54
    leg.signal_candle = {"high": 777.05, "low": 757.90, "close": 769.65, "ts": 0}
    leg.atr = WilderATR(14); leg.atr.value = 20.54; leg.atr_ok = True
    e._fill_window_open = lambda: True
    return e, leg


POSITIONS = lambda qty, avg: [{"securityId": "822216", "netQty": qty,
                               "buyAvg": avg}]

print("\n[1] REGRESSION 13-Nov: an unconfirmed fill is resolved, not assumed")
wire(lambda url, headers=None, json=None, timeout=None:
     R(200, {"orderId": "L1", "orderStatus": "CANCELLED"})
     if json.get("orderType") == "LIMIT"
     else R(200, {"orderId": "M1", "orderStatus": "PENDING",
                  "averageTradedPrice": 0}),
     lambda url, headers=None, timeout=None:
     R(200, POSITIONS(60, 778.40)) if "/positions" in url
     else R(200, {"orderId": "M1", "orderStatus": "PENDING",
                  "averageTradedPrice": 0}))
E._await_order = lambda oid, seconds=30.0: {
    "filled": False, "price": 0.0, "qty": 0, "order_id": oid,
    "status": "UNKNOWN"}
e, leg = armed_engine()
e._fill(leg, 777.30)
check("the position is adopted, not abandoned", leg.position is not None, leg.position)
if leg.position:
    p = leg.position
    check("quantity matches the broker", p.lots_open * p.lot_size == 60,
          p.lots_open * p.lot_size)
    check("the real fill price is recorded", abs(p.fill_price - 778.40) < 0.01,
          p.fill_price)
    check("E stays the trigger, per the spec", abs(p.E - 777.25) < 0.01, p.E)
    check("it has a stop", abs(p.stop - 756.90) < 0.01, p.stop)
    check("the leg is IN_TRADE", leg.state == LegState.IN_TRADE, leg.state)

print("\n[2] Unknown, and the broker holds nothing — a genuine miss")
wire(lambda url, headers=None, json=None, timeout=None:
     R(200, {"orderId": "L1", "orderStatus": "CANCELLED"})
     if json.get("orderType") == "LIMIT"
     else R(200, {"orderId": "M1", "orderStatus": "PENDING"}),
     lambda url, headers=None, timeout=None:
     R(200, POSITIONS(0, 0)) if "/positions" in url
     else R(200, {"orderId": "M1", "orderStatus": "PENDING"}))
e, leg = armed_engine()
e._fill(leg, 777.30)
check("no position invented", leg.position is None, leg.position)
check("leg stands down", leg.state == LegState.STOOD_DOWN, leg.state)

print("\n[3] Unknown AND the positions endpoint is unreachable -> HALT")
wire(lambda url, headers=None, json=None, timeout=None:
     R(200, {"orderId": "M1", "orderStatus": "PENDING"}),
     lambda url, headers=None, timeout=None: R(500, "boom"))
e, leg = armed_engine()
e._fill(leg, 777.30)
check("engine halts rather than guessing", e._halted, e._halted)
check("no position invented", leg.position is None, leg.position)

print("\n[4] A partial fill is managed, not discarded")
wire(lambda url, headers=None, json=None, timeout=None:
     R(200, {"orderId": "L1", "orderStatus": "TRADED",
             "averageTradedPrice": 777.35}),
     lambda url, headers=None, timeout=None: R(200, {}))
E._await_order = lambda oid, seconds=30.0: {
    "filled": True, "price": 777.35, "qty": 40, "order_id": oid,
    "status": "TRADED"}
e, leg = armed_engine()
# the immediate TRADED response carries the full qty; force the partial path
E.place_order_limit_ioc = lambda *a, **k: {
    "filled": True, "price": 777.35, "qty": 40, "order_id": "L1",
    "status": "TRADED"}
e._fill(leg, 777.30)
check("a position exists", leg.position is not None, leg.position)
if leg.position:
    check("sized to what actually filled",
          leg.position.lots_open * leg.position.lot_size == 40,
          leg.position.lots_open * leg.position.lot_size)
E.place_order_limit_ioc = E.__dict__["place_order_limit_ioc"]

print("\n[5] DH-905 halts instead of retrying forever")
import importlib
importlib.reload(E)
from engine import SensexVWAPLadderEngine as SE2, StrategyConfig as SC2
wire(lambda url, headers=None, json=None, timeout=None:
     R(400, '{"errorCode":"DH-905","errorMessage":"Invalid IP"}'),
     lambda url, headers=None, timeout=None: R(200, {}))
res = E.place_order_limit_ioc("822216", "BSE_FNO", "BUY", 60, 777.25)
check("recognised as a configuration fault",
      res["status"] == "IP_NOT_WHITELISTED", res["status"])
check("reported as not filled", res["filled"] is False, res["filled"])

print("\n[6] The market fallback is IOC, so nothing rests at the exchange")
sent = {}
wire(lambda url, headers=None, json=None, timeout=None:
     (sent.update(json), R(200, {"orderId": "M1", "orderStatus": "TRADED",
                                 "averageTradedPrice": 778.40}))[1],
     lambda url, headers=None, timeout=None: R(200, {}))
E.place_order_market("822216", "BSE_FNO", "BUY", 60)
check("validity is IOC, not DAY", sent.get("validity") == "IOC",
      sent.get("validity"))
check("order type is MARKET", sent.get("orderType") == "MARKET",
      sent.get("orderType"))

print("\n[7] Reconciliation spots a position the engine does not know about")
wire(lambda *a, **k: R(200, {}),
     lambda url, headers=None, timeout=None:
     R(200, POSITIONS(60, 778.40)) if "/positions" in url else R(200, {}))
e = SE2(SC2(paper_mode=False))
e.lot_size = 20
e.legs["PE"].sec_id = "822216"; e.legs["PE"].label = "PE"
e.legs["CE"].sec_id = ""
evts = []
e.gui_cb = lambda ev, d: evts.append(ev)
out = e.reconcile_positions()
check("mismatch detected", len(out) == 1, out)
check("it reports the broker quantity", out and out[0]["broker"] == 60, out)
check("and that the engine has none", out and out[0]["engine"] == 0, out)
check("the GUI is told", "position_mismatch" in evts, evts)

print("\n[8] Matching positions produce no noise")
wire(lambda *a, **k: R(200, {}),
     lambda url, headers=None, timeout=None:
     R(200, POSITIONS(0, 0)) if "/positions" in url else R(200, {}))
e = SE2(SC2(paper_mode=False))
e.lot_size = 20
e.legs["PE"].sec_id = "822216"; e.legs["CE"].sec_id = ""
check("flat and flat agree", e.reconcile_positions() == [], e.reconcile_positions())

print("\n[9] Paper mode never calls the broker")
called = {"n": 0}
wire(lambda *a, **k: R(200, {}),
     lambda *a, **k: (called.update(n=called["n"] + 1), R(200, []))[1])
e = SE2(SC2(paper_mode=True))
e.legs["PE"].sec_id = "822216"
check("no positions call in paper", e.reconcile_positions() == [] and called["n"] == 0,
      called["n"])

print("\n" + ("ALL ORDER TESTS PASSED" if not FAILS
              else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
