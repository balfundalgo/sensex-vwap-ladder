"""
Headless GUI smoke test. Boots the real window under Xvfb, drives the
reconcile panel with synthetic candle events, and verifies the table, the CSV
export and the settings round-trip. Catches the class of bug you only
otherwise find by clicking around.

    xvfb-run -a python test_gui.py
"""
import os, sys, csv, glob, json, tempfile

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '   ' + str(extra)}")
    if not cond:
        FAILS.append(name)


# Point the app at a throwaway settings file BEFORE importing it: the real one
# sits beside the EXE and belongs to the client. A test that reads it would
# pass or fail depending on what the client last clicked, and a test that
# wrote to it would silently change his parameters.
_SETTINGS = os.path.join(tempfile.mkdtemp(prefix="snx_test_"), "snx_settings.json")
os.environ["SNX_SETTINGS_FILE"] = _SETTINGS
if os.path.exists(_SETTINGS):
    os.remove(_SETTINGS)

import app as A

check("settings file is the test one", str(A.SETTINGS_FILE) == _SETTINGS,
      A.SETTINGS_FILE)

print("\n[1] Window builds")
win = A.SensexApp()
win.update()
check("app constructed", win is not None)
check("live candle panel exists", hasattr(win, "candle_box"))
check("reconcile tooling removed", not hasattr(win, "btn_recon"), True)
check("calibrate tooling removed", not hasattr(win, "cal_time"), True)
check("VWAP field dropdown kept", hasattr(win, "opt_field"))
check("VWAP field defaults to ohlc4", win.opt_field.get() == "ohlc4", win.opt_field.get())
check("ATR method control removed", not hasattr(win, "opt_atrm"), True)
check("rollup control removed", not hasattr(win, "opt_agg"), True)
check("candle-source control removed", not hasattr(win, "opt_src"), True)
check("timeframe selector exists", hasattr(win, "opt_tf"))
check("timeframe defaults to 2", win.opt_tf.get() == "2", win.opt_tf.get())
check("ATR seed default is 300", win.ent_seed.get() == "300", win.ent_seed.get())

print("\n[2] Live candles land in the table with VWAP and ATR")
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
check("above/below VWAP flag correct",
      body.count("ABOVE") == 2 and body.count("below") == 1,
      f"ABOVE={body.count('ABOVE')} below={body.count('below')}")
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
for f in glob.glob(str(A.LOG_DIR / "candles_*.csv")):
    os.remove(f)
win._export_csv()
win.update()
files = glob.glob(str(A.LOG_DIR / "candles_*.csv"))
check("file written", len(files) == 1, files)
if files:
    with open(files[0]) as f:
        rows = list(csv.DictReader(f))
    check("4 data rows", len(rows) == 4, len(rows))
    check("columns present",
          {"leg", "time", "open", "high", "low", "close", "volume", "vwap",
           "atr"} <= set(rows[0].keys()), list(rows[0].keys()))
    check("vwap in export", rows[0]["vwap"] not in ("", None), rows[0]["vwap"])
    check("atr in export", rows[0]["atr"] not in ("", None), rows[0]["atr"])

print("\n[6] Clear resets everything")
win._clear_candles()
win.update()
check("rows dropped", len(win.candle_rows) == 0)
check("textbox emptied", win.candle_box.get("1.0", "end").strip() == "")
check("counter cleared", win.lbl_recon.cget("text") == "")

print("\n[7] Studies are shown on the leg cards")
win._handle("tick_update", {"total_pnl": 0, "packets": 1, "spot": 76400.0,
    "legs": [{"leg": "PE", "state": "IDLE", "eligible": True, "ltp": 616.00,
              "vwap": 549.55, "atr": 15.61, "pnl": 0, "trades": 0,
              "attempts": 0, "trigger": 0, "stop": 0, "lots_open": 0,
              "rung": 0, "E": 0}]})
win.update()
cells = win.leg_cards["PE"]["cells"]
check("VWAP on the card", "549.55" in cells["vwap"].cget("text"),
      cells["vwap"].cget("text"))
check("ATR on the card", "15.61" in cells["atr"].cget("text"),
      cells["atr"].cget("text"))
check("LTP on the card", "616.00" in cells["ltp"].cget("text"),
      cells["ltp"].cget("text"))

print("\n[7b] Waiting-for-open countdown is shown")
win._handle("waiting_for_open", {"seconds": 754, "text": "12m"})
win.update()
check("countdown on the status bar", "12m" in win.lbl_status.cget("text"),
      win.lbl_status.cget("text"))
check("says market opens", "OPENS" in win.lbl_status.cget("text"), True)

print("\n[7c] Open position P&L is shown, not just realised")
win._handle("tick_update", {"total_pnl": 762.0, "realised_pnl": 0.0,
    "unrealised_pnl": 762.0, "packets": 9, "spot": 75216.0,
    "legs": [{"leg": "PE", "state": "IN_TRADE", "eligible": False,
              "ltp": 318.50, "vwap": 300.0, "atr": 15.4, "pnl": 0.0,
              "realised": 0.0, "unrealised": 762.0, "open_pnl": 762.0,
              "trades": 1, "attempts": 1, "trigger": 0, "stop": 150.0,
              "lots_open": 3, "rung": 0, "E": 305.80}]})
win.update()
check("header shows the open trade", "762" in win.lbl_pnl.cget("text"),
      win.lbl_pnl.cget("text"))
check("header splits booked vs open",
      "booked" in win.lbl_pnl_split.cget("text"), win.lbl_pnl_split.cget("text"))
check("leg card shows open P&L",
      "762" in win.leg_cards["PE"]["cells"]["upnl"].cget("text"),
      win.leg_cards["PE"]["cells"]["upnl"].cget("text"))

print("\n[7d] Trade totals cannot be confused with per-lot lines")
win._handle("scale_out", {"leg": "PE", "tag": "T1", "price": 363.34,
                          "pnl": 1230, "lots_left": 2, "lots_sold": 2,
                          "qty": 40})
win._handle("trade_closed", {"leg": "PE", "pnl": 4919, "reason": "TRAIL_T2",
                             "rung": 2, "scaled": 3689, "last_leg_pnl": 1230})
win.update()
lg = win.trade_log.get("1.0", "end")
check("per-lot line says 'these lots'", "these lots" in lg, True)
check("closing line says TRADE TOTAL", "TRADE TOTAL" in lg, True)
check("and shows the breakdown", "scaled" in lg and "final" in lg, True)
check("scale-out names the lot count", "sold 2 lots" in lg, True)
check("scale-out names the quantity", "(40 qty)" in lg, True)

print("\n[7e] Power cut and stale signal are surfaced")
win._handle("suspended", {"seconds": 407})
win._handle("stale_signal", {"leg": "CE", "age": 407, "time": "11:31"})
win.update()
lg = win.trade_log.get("1.0", "end")
check("suspension reported", "was not running" in lg, True)
check("stale signal reported", "not tradeable" in lg, True)

print("\n[7f] Lots multiplier is spelled out")
for typed, lots, qty, third in [("1", 3, 60, 1), ("2", 6, 120, 2), ("3", 9, 180, 3)]:
    win.ent_lots.delete(0, "end"); win.ent_lots.insert(0, typed)
    win._refresh_size_hint(); win.update()
    txt = win.lbl_size.cget("text")
    check(f"typing {typed} shows {lots} lots",
          f"= {lots} lots" in txt, txt)
    check(f"typing {typed} shows {qty} qty", f"{qty} qty" in txt, txt)
    check(f"typing {typed} splits {third} per rung",
          f"{third} at T1 · {third} at T2 · {third} runner" in txt, txt)
win.ent_lots.delete(0, "end"); win.ent_lots.insert(0, "1")
win._refresh_size_hint()

print("\n[7g] A lot-size change is called out")
win._handle("lot_size_changed", {"previous": 20, "now": 25})
win.update()
check("change reported", "LOT SIZE CHANGED" in win.trade_log.get("1.0", "end"), True)

print("\n[7h] The daily loss cap is reported")
win._handle("daily_loss_hit", {"realised": -8921, "unrealised": -1089,
                               "total": -10010, "limit": 10000, "open_legs": 1})
win.update()
lg = win.trade_log.get("1.0", "end")
check("limit reported", "DAILY LOSS LIMIT" in lg, True)
check("booked and open shown", "booked" in lg and "open" in lg, True)
check("status bar changes", "LOSS LIMIT" in win.lbl_status.cget("text"),
      win.lbl_status.cget("text"))

print("\n[7i] Order and position alarms reach the screen")
win._handle("order_unresolved", {"leg": "PE", "order_id": "M1"})
win._handle("position_adopted", {"leg": "PE", "qty": 60, "price": 778.40})
win._handle("partial_fill", {"leg": "PE", "got": 40, "wanted": 60})
win._handle("position_mismatch", {"leg": "PE", "broker": 60, "engine": 0,
                                  "avg": 778.40})
win.update()
lg = win.trade_log.get("1.0", "end")
check("unresolved order shown", "unconfirmed" in lg, True)
check("adoption shown", "adopted" in lg, True)
check("partial fill shown", "PARTIAL FILL" in lg, True)
check("mismatch shown", "MISMATCH" in lg, True)
check("status bar warns", "MISMATCH" in win.lbl_status.cget("text"),
      win.lbl_status.cget("text"))

print("\n[8] Config picks up the study settings")
win.opt_field.set("hlc3"); win.opt_tf.set("5")
win.ent_lots.delete(0, "end"); win.ent_lots.insert(0, "2")
cfg = win._build_config()
check("vwap_field wired", cfg.vwap_field == "hlc3", cfg.vwap_field)
check("timeframe wired", cfg.timeframe_minutes == 5, cfg.timeframe_minutes)
check("multiplier becomes lots", cfg.lots == 6, cfg.lots)
check("locked settings keep their defaults",
      (cfg.atr_method, cfg.agg_mode, cfg.data_source) == ("wilder", "count", "rest"),
      (cfg.atr_method, cfg.agg_mode, cfg.data_source))
check("seed bars wired", cfg.atr_seed_bars == 300, cfg.atr_seed_bars)
check("paper mode default on", cfg.paper_mode is True)
win.opt_tf.set("2"); win.ent_lots.delete(0, "end"); win.ent_lots.insert(0, "1")

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

print("\n[10] The mode badge is unmistakable and never says LIVE by accident")
win.var_paper.set(True); win._refresh_mode(); win.update()
check("paper badge says PAPER", "PAPER" in win.lbl_mode.cget("text"),
      win.lbl_mode.cget("text"))
check("paper badge is amber", win.lbl_mode.cget("fg_color") == A.YEL,
      win.lbl_mode.cget("fg_color"))
check("paper badge never says LIVE", "LIVE" not in win.lbl_mode.cget("text"),
      win.lbl_mode.cget("text"))
win.var_paper.set(False); win._refresh_mode(); win.update()
check("live badge says LIVE", "LIVE" in win.lbl_mode.cget("text"),
      win.lbl_mode.cget("text"))
check("live badge is red", win.lbl_mode.cget("fg_color") == A.RED,
      win.lbl_mode.cget("fg_color"))
check("live hint warns about real money",
      "REAL ORDERS" in win.lbl_mode_hint.cget("text"),
      win.lbl_mode_hint.cget("text"))
win._handle("ws_connected", {"instruments": 3})
win.update()
check("websocket does not claim LIVE",
      "LIVE" not in win.lbl_status.cget("text"), win.lbl_status.cget("text"))
check("websocket says RUNNING", "RUNNING" in win.lbl_status.cget("text"),
      win.lbl_status.cget("text"))

print("\n[11] SAVE writes every parameter to disk")
win.var_paper.set(False)          # LIVE — the setting that must survive
win.var_first.set(False)
win.opt_tf.set("5"); win.opt_field.set("hlc3")
for ent, val in [(win.ent_lots, "2"), (win.ent_buf, "0.35"),
                 (win.ent_sl, "1.50"), (win.ent_slip, "3"),
                 (win.ent_atrp, "14"), (win.ent_mult, "2.5"),
                 (win.ent_cost, "3.00"), (win.ent_seed, "400"),
                 (win.ent_le, "14:30"), (win.ent_so, "15:05"),
                 (win.ent_minp, "25"), (win.ent_dll, "10000")]:
    ent.delete(0, "end"); ent.insert(0, val)
win._on_save(); win.update()
check("settings file created", os.path.exists(_SETTINGS), _SETTINGS)
saved = json.load(open(_SETTINGS))
check("paper_mode stored as False", saved.get("paper_mode") is False,
      saved.get("paper_mode"))
check("timeframe stored", saved.get("timeframe_minutes") == "5",
      saved.get("timeframe_minutes"))
check("vwap field stored", saved.get("vwap_field") == "hlc3", saved.get("vwap_field"))
check("multiplier stored", saved.get("lot_multiplier") == "2",
      saved.get("lot_multiplier"))
check("day loss cap stored", saved.get("daily_loss_limit") == "10000",
      saved.get("daily_loss_limit"))
check("first-candle switch stored",
      saved.get("first_candle_can_signal") is False,
      saved.get("first_candle_can_signal"))
check("square off stored", saved.get("square_off") == "15:05", saved.get("square_off"))
check("stamped with a save time", bool(saved.get("saved_at")), saved.get("saved_at"))
check("save confirmed on screen", "saved" in win.lbl_saved.cget("text").lower(),
      win.lbl_saved.cget("text"))
check("credentials are NOT in the settings file",
      not any(k.lower().startswith("dhan") or "pin" in k.lower()
              or "totp" in k.lower() for k in saved),
      list(saved.keys()))
win.destroy()

print("\n[12] A fresh launch comes back on the saved parameters, not defaults")
win = A.SensexApp()
win.update()
check("LIVE mode survived the restart", win.var_paper.get() is False,
      win.var_paper.get())
check("badge is red on launch", win.lbl_mode.cget("fg_color") == A.RED,
      win.lbl_mode.cget("fg_color"))
check("timeframe restored", win.opt_tf.get() == "5", win.opt_tf.get())
check("vwap field restored", win.opt_field.get() == "hlc3", win.opt_field.get())
check("multiplier restored", win.ent_lots.get() == "2", win.ent_lots.get())
check("entry buffer restored", win.ent_buf.get() == "0.35", win.ent_buf.get())
check("stop buffer restored", win.ent_sl.get() == "1.50", win.ent_sl.get())
check("max slip restored", win.ent_slip.get() == "3", win.ent_slip.get())
check("ATR per rung restored", win.ent_mult.get() == "2.5", win.ent_mult.get())
check("T1 stop restored", win.ent_cost.get() == "3.00", win.ent_cost.get())
check("seed bars restored", win.ent_seed.get() == "400", win.ent_seed.get())
check("last entry restored", win.ent_le.get() == "14:30", win.ent_le.get())
check("square off restored", win.ent_so.get() == "15:05", win.ent_so.get())
check("min premium restored", win.ent_minp.get() == "25", win.ent_minp.get())
check("day loss cap restored", win.ent_dll.get() == "10000", win.ent_dll.get())
check("first-candle switch restored", win.var_first.get() is False,
      win.var_first.get())
check("size hint recomputed", "6 lots" in win.lbl_size.cget("text"),
      win.lbl_size.cget("text"))

cfg = win._build_config()
check("config built from restored values is LIVE", cfg.paper_mode is False,
      cfg.paper_mode)
check("config lots = 6", cfg.lots == 6, cfg.lots)
check("config timeframe = 5", cfg.timeframe_minutes == 5, cfg.timeframe_minutes)
check("config day loss cap = 10000", cfg.daily_loss_limit == 10000.0,
      cfg.daily_loss_limit)
check("config first candle off", cfg.first_candle_can_signal is False,
      cfg.first_candle_can_signal)
win.destroy()

print("\n[13] A corrupt settings file falls back to defaults, it does not crash")
with open(_SETTINGS, "w") as f:
    f.write("{ this is not json")
win = A.SensexApp()
win.update()
check("window still builds", win is not None)
check("defaults to PAPER after a bad file", win.var_paper.get() is True,
      win.var_paper.get())
check("defaults to 2m candles", win.opt_tf.get() == "2", win.opt_tf.get())
check("the problem is reported", "could not be read"
      in win.trade_log.get("1.0", "end"), win.trade_log.get("1.0", "end")[-200:])
win.destroy()

print("\n[14] A half-written file keeps defaults for the missing keys")
with open(_SETTINGS, "w") as f:
    json.dump({"paper_mode": False, "timeframe_minutes": "15",
               "lot_multiplier": "", "square_off": None}, f)
win = A.SensexApp()
win.update()
check("known keys applied", win.var_paper.get() is False and win.opt_tf.get() == "15",
      (win.var_paper.get(), win.opt_tf.get()))
check("blank value left at default", win.ent_lots.get() == "1", win.ent_lots.get())
check("null value left at default", win.ent_so.get() == "15:00", win.ent_so.get())
check("unspecified key left at default", win.ent_buf.get() == "0.20",
      win.ent_buf.get())

print("\n[15] Closing the window saves instead of discarding")
win.ent_dll.delete(0, "end"); win.ent_dll.insert(0, "7500")
win._on_close()
reloaded = json.load(open(_SETTINGS))
check("close wrote the file", reloaded.get("daily_loss_limit") == "7500",
      reloaded.get("daily_loss_limit"))
check("close kept the mode", reloaded.get("paper_mode") is False,
      reloaded.get("paper_mode"))
try:
    gone = not win.winfo_exists()
except Exception:
    gone = True          # the interpreter is gone with it, which is the point
check("window was destroyed on close", gone)
print("\n" + ("ALL GUI TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
