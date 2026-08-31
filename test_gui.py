"""
Headless GUI smoke test. Boots the real window under Xvfb, drives the
reconcile panel with synthetic candle events, and verifies the table and CSV
export. Catches the class of bug you only otherwise find by clicking around.

    xvfb-run -a python test_gui.py
"""
import os, sys, csv, glob

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '   ' + str(extra)}")
    if not cond:
        FAILS.append(name)


import app as A

print("\n[1] Window builds")
win = A.SensexApp()
win.update()
check("app constructed", win is not None)
check("reconcile panel exists", hasattr(win, "candle_box"))
check("history button exists", hasattr(win, "btn_recon"))
check("study dropdowns exist", hasattr(win, "opt_field") and hasattr(win, "opt_atrm"))
check("VWAP field defaults to ohlc4", win.opt_field.get() == "ohlc4", win.opt_field.get())
check("ATR method defaults to wilder", win.opt_atrm.get() == "wilder", win.opt_atrm.get())
check("ATR seed default is 300", win.ent_seed.get() == "300", win.ent_seed.get())

print("\n[2] Live candles land in the reconcile table")
win._clear_candles()
win._write_candles(f"{'leg':<4}{A.CANDLE_HEADER}\n")
bars = [
    dict(leg="PE", time="09:15", open=250.0, high=256.0, low=249.0, close=255.0,
         volume=4000, vwap=252.50, atr=12.40),
    dict(leg="PE", time="09:17", open=255.0, high=258.0, low=253.0, close=254.0,
         volume=9000, vwap=254.10, atr=12.90),
    dict(leg="PE", time="09:19", open=254.0, high=261.0, low=254.0, close=260.0,
         volume=6000, vwap=255.80, atr=13.20),
]
for b in bars:
    win._handle("candle", b)
win.update()
body = win.candle_box.get("1.0", "end")
check("3 rows appended", len(win.candle_rows) == 3, len(win.candle_rows))
check("times rendered", all(b["time"] in body for b in bars))
check("vwap rendered", "252.50" in body and "255.80" in body)
check("atr rendered", "13.20" in body)
check("volume rendered", "9000" in body)
check("above-VWAP flag correct", body.count("YES") == 2 and body.count("no") == 1,
      f"YES={body.count('YES')} no={body.count('no')}")
check("counter updated", "3 bars" in win.lbl_recon.cget("text"),
      win.lbl_recon.cget("text"))

print("\n[3] Row formatting is column-aligned")
r1 = win._fmt_row("PE", "09:15", 250.0, 256.0, 249.0, 255.0, 4000, 252.5, 12.4)
r2 = win._fmt_row("CE", "14:31", 8.05, 9.20, 7.95, 8.10, 123456, 8.44, 0.62)
check("equal row widths", len(r1) == len(r2), f"{len(r1)} vs {len(r2)}")
hdr_w = len(f"{'leg':<4}{A.CANDLE_HEADER}")
check("header width matches rows", hdr_w == len(r1.rstrip("\n")),
      f"header={hdr_w} row={len(r1.rstrip())}")

print("\n[4] Missing VWAP/ATR degrade to '-' instead of crashing")
win._handle("candle", dict(leg="CE", time="09:15", open=1.0, high=2.0, low=0.5,
                           close=1.5, volume=0, vwap=None, atr=None))
win.update()
check("null row rendered", "-" in win.candle_box.get("end-2l", "end"))
check("4 rows now", len(win.candle_rows) == 4, len(win.candle_rows))

print("\n[5] CSV export")
for f in glob.glob(str(A.LOG_DIR / "reconcile_*.csv")):
    os.remove(f)
win._export_csv()
win.update()
files = glob.glob(str(A.LOG_DIR / "reconcile_*.csv"))
check("file written", len(files) == 1, files)
if files:
    with open(files[0]) as f:
        rows = list(csv.DictReader(f))
    check("4 data rows", len(rows) == 4, len(rows))
    check("columns present",
          {"leg", "time", "open", "high", "low", "close", "volume", "vwap",
           "atr", "source"} <= set(rows[0].keys()), list(rows[0].keys()))
    check("source tagged live", rows[0]["source"] == "live", rows[0]["source"])

print("\n[6] Clear resets everything")
win._clear_candles()
win.update()
check("rows dropped", len(win.candle_rows) == 0)
check("textbox emptied", win.candle_box.get("1.0", "end").strip() == "")
check("counter cleared", win.lbl_recon.cget("text") == "")

print("\n[7] Reconcile refuses to run without credentials")
win.ent_cid.delete(0, "end"); win.ent_pin.delete(0, "end"); win.ent_totp.delete(0, "end")
win._on_reconcile()
win.update()
check("button stays enabled", win.btn_recon.cget("state") == "normal",
      win.btn_recon.cget("state"))
check("error logged", "Fill credentials" in win.trade_log.get("1.0", "end"))

print("\n[8] Config picks up the study settings")
win.opt_field.set("hlc3"); win.opt_atrm.set("sma")
cfg = win._build_config()
check("vwap_field wired", cfg.vwap_field == "hlc3", cfg.vwap_field)
check("atr_method wired", cfg.atr_method == "sma", cfg.atr_method)
check("seed bars wired", cfg.atr_seed_bars == 300, cfg.atr_seed_bars)
check("paper mode default on", cfg.paper_mode is True)

print("\n[9] Other engine events still render")
for ev, d in [("strike", {"open": 78111.0, "strike": 78100, "expiry": "2026-08-27",
                          "lot_size": 20}),
              ("armed", {"leg": "PE", "trigger": 512.2, "stop": 475.85, "atr": 33.69,
                         "t1": 579.58, "t2": 646.96}),
              ("entry", {"leg": "PE", "strike": 78100, "E": 512.2, "fill": 512.2,
                         "stop": 475.85, "atr": 33.69, "qty": 60, "paper": True}),
              ("rung", {"leg": "PE", "rung": 1, "stop": 514.2, "price": 579.58}),
              ("trade_closed", {"leg": "PE", "pnl": 5390.0, "reason": "TRAIL_T2",
                                "rung": 2}),
              ("tick_update", {"total_pnl": 5390.0, "packets": 1234, "spot": 78222.5,
                               "legs": [{"leg": "PE", "state": "STOOD_DOWN",
                                         "eligible": False, "ltp": 560.0, "vwap": 540.0,
                                         "atr": 33.69, "pnl": 5390.0, "trades": 1,
                                         "attempts": 1, "trigger": 0, "stop": 0,
                                         "lots_open": 0, "rung": 0, "E": 0}]})]:
    win._handle(ev, d)
win.update()
lg = win.trade_log.get("1.0", "end")
check("strike logged", "78100" in lg)
check("armed logged", "ARMED" in lg)
check("entry logged", "ENTRY" in lg)
check("close logged", "CLOSED" in lg)
check("pnl card updated", "5,390" in win.lbl_pnl.cget("text"), win.lbl_pnl.cget("text"))
check("stood-down shown", win.leg_cards["PE"]["state"].cget("text") == "STOOD_DOWN")
check("ineligible message shown",
      "attempt spent" in win.leg_cards["PE"]["elig"].cget("text"))

win.destroy()
print("\n" + ("ALL GUI TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
