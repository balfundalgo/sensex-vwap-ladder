"""
SENSEX VWAP Reclaim + ATR Ladder — GUI Application v1.3
════════════════════════════════════════════════════════
Two legs (CE + PE) of one strike | Light Theme

v1.3 adds persistent settings and an unmistakable mode badge.

Every parameter on the left-hand panel is now written to snx_settings.json
beside the EXE when you press SAVE (and again when you press START), and read
back into the controls the next time the app opens. Before this, SAVE only
held the values in memory for the current run: closing the app threw them
away and the next launch silently came up on defaults — which meant Paper
Trading, because that is the default. On 16-Nov that is exactly what happened:
the client restarted after an aborted 09:11 launch, the switch had reverted
to Paper, and a session intended to be live traded on paper.

So the mode is now impossible to miss: a coloured badge in the title bar,
a confirmation box before any live run, and a banner in both the activity log
and the trade log naming the mode on every start.

v1.1 added the RECONCILE panel: every closed candle with its per-bar volume,
VWAP and ATR — live during the session, or pulled from history on demand with
no engine and no market hours. Diff against Kite without leaving the app.

Balfund Trading Pvt Ltd
"""

import os, sys, csv, json, logging, threading
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
from dotenv import load_dotenv, set_key

from engine import (
    SensexVWAPLadderEngine, StrategyConfig,
    init_credentials, set_credentials, now_ist, ENV_FILE
)

if getattr(sys, 'frozen', False):
    _APP_DIR = Path(sys.executable).parent
else:
    _APP_DIR = Path(__file__).parent if "__file__" in globals() else Path.cwd()

LOG_DIR = _APP_DIR / "logs"; LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("SNX_GUI")

# Where the strategy parameters live between runs. Beside the EXE, so a
# client who copies the folder to another machine carries the settings with
# it. The env override exists for the test suite, which must never read or
# write the real client file.
SETTINGS_FILE = Path(os.getenv("SNX_SETTINGS_FILE")
                     or (_APP_DIR / "snx_settings.json"))
SETTINGS_VERSION = 1

ctk.set_appearance_mode("light"); ctk.set_default_color_theme("blue")

BG = "#f0f2f5"; CARD = "#ffffff"; ACC = "#0369a1"; GRN = "#16a34a"; RED = "#dc2626"
YEL = "#ca8a04"; TXT = "#111827"; DIM = "#6b7280"; BD = "#d1d5db"; AMB = "#b45309"
FT = ("Segoe UI", 18, "bold"); FH = ("Segoe UI", 14, "bold"); FM = ("Segoe UI", 13, "bold")
FS = ("Segoe UI", 12); FX = ("Consolas", 11); FL = ("Segoe UI", 11); FN = ("Segoe UI", 10)
FB = ("Segoe UI", 28, "bold"); FC = ("Segoe UI", 15, "bold"); FT2 = ("Segoe UI", 9)

STATE_COLORS = {"IDLE": DIM, "ARMED": AMB, "IN_TRADE": GRN,
                "STOOD_DOWN": RED, "HALTED": RED}
FMONO = ("Consolas", 11)
CANDLE_HEADER = (f"{'time':>6} {'open':>9} {'high':>9} {'low':>9} {'close':>9} "
                 f"{'volume':>10} {'VWAP':>9} {'ATR':>8} {'vs':>6}")

TF_CHOICES = ("1", "2", "3", "5", "10", "15")
FIELD_CHOICES = ("ohlc4", "hlc3", "hl2", "close")


class SensexApp(ctk.CTk):
    VERSION = "1.3"

    def __init__(self):
        super().__init__()
        load_dotenv(str(ENV_FILE), override=True)
        self.title(f"SENSEX VWAP Ladder v{self.VERSION} — Balfund Trading")
        self.geometry("1560x980"); self.minsize(1280, 820)
        self.configure(fg_color=BG)
        self.engine = None; self.is_running = False
        self.candle_rows = []          # everything shown in the reconcile panel
        self._lot_size = 20            # replaced by the real one at start-up
        self._build_ui()
        self._load_env()
        self._load_settings()          # must come after the widgets exist
        self._refresh_mode()
        self._refresh_size_hint()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()

    # ═══════════ LAYOUT ═══════════
    def _build_ui(self):
        top = ctk.CTkFrame(self, fg_color=CARD, height=52, corner_radius=0,
                           border_width=1, border_color=BD)
        top.pack(fill="x"); top.pack_propagate(False)
        ctk.CTkLabel(top, text="📈 SENSEX VWAP Ladder", font=FT, text_color=ACC
                     ).pack(side="left", padx=16)
        ctk.CTkLabel(top, text=f"v{self.VERSION}", font=FN, text_color=DIM
                     ).pack(side="left", padx=4, pady=(4, 0))
        # The mode badge. Nothing else in this window is allowed to say the
        # word LIVE, so that this label alone answers "is it trading real
        # money?" from across the room.
        self.lbl_mode = ctk.CTkLabel(top, text="", font=FM, text_color="white",
                                     corner_radius=6, width=190, height=28)
        self.lbl_mode.pack(side="left", padx=14)
        self.lbl_clock = ctk.CTkLabel(top, text="--:--:--", font=FM, text_color=TXT)
        self.lbl_clock.pack(side="right", padx=16)
        self.lbl_status = ctk.CTkLabel(top, text="● IDLE", font=FM, text_color=YEL)
        self.lbl_status.pack(side="right", padx=12)
        self.lbl_ws = ctk.CTkLabel(top, text="WS —", font=FL, text_color=DIM)
        self.lbl_ws.pack(side="right", padx=10)

        body = ctk.CTkFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        self.left = ctk.CTkScrollableFrame(body, fg_color=CARD, width=300,
                                           corner_radius=8, border_width=1, border_color=BD)
        self.left.pack(side="left", fill="y", padx=(0, 4))
        self.center = ctk.CTkFrame(body, fg_color=BG)
        self.center.pack(side="left", fill="both", expand=True, padx=4)
        self.right = ctk.CTkFrame(body, fg_color=CARD, width=360, corner_radius=8,
                                  border_width=1, border_color=BD)
        self.right.pack(side="right", fill="y", padx=(4, 0)); self.right.pack_propagate(False)

        self._build_settings(); self._build_dashboard()
        self._build_trade_log(); self._build_bottom()

    # ═══════════ SETTINGS ═══════════
    def _sec(self, p, text):
        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkLabel(f, text=text, font=FH, text_color=ACC).pack(anchor="w")
        ctk.CTkFrame(f, fg_color=BD, height=1).pack(fill="x", pady=(2, 0))

    def _entry(self, p, label, show=None):
        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(f, text=label, font=FM, text_color=TXT, width=90, anchor="w"
                     ).pack(side="left")
        e = ctk.CTkEntry(f, font=FS, fg_color="#fafbfc", border_color=BD,
                         text_color=TXT, height=28, show=show or "")
        e.pack(side="left", fill="x", expand=True); return e

    def _gentry(self, p, label, default, row):
        ctk.CTkLabel(p, text=label, font=FM, text_color=TXT, width=118, anchor="w"
                     ).grid(row=row, column=0, padx=2, pady=2, sticky="w")
        e = ctk.CTkEntry(p, font=FS, fg_color="#fafbfc", border_color=BD,
                         text_color=TXT, height=26, width=70)
        e.grid(row=row, column=1, padx=2, pady=2, sticky="e"); e.insert(0, default)
        return e

    def _build_settings(self):
        p = self.left
        self._sec(p, "🔐 CREDENTIALS")
        self.ent_cid = self._entry(p, "Client ID")
        self.ent_pin = self._entry(p, "PIN", show="•")
        self.ent_totp = self._entry(p, "TOTP Secret", show="•")

        self._sec(p, "⚙️ MODE")
        self.var_paper = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(p, text="Paper Trading", font=FM, variable=self.var_paper,
                      progress_color=YEL, text_color=TXT,
                      command=self._refresh_mode).pack(anchor="w", padx=14, pady=4)
        self.lbl_mode_hint = ctk.CTkLabel(p, text="", font=FT2, text_color=DIM,
                                          wraplength=252, justify="left")
        self.lbl_mode_hint.pack(anchor="w", padx=14, pady=(0, 2))

        self._sec(p, "🎯 ENTRY & STOP")
        g = ctk.CTkFrame(p, fg_color="transparent"); g.pack(fill="x", padx=14, pady=2)
        self.ent_buf = self._gentry(g, "Entry buffer ₹", "0.20", 0)
        self.ent_sl = self._gentry(g, "Stop buffer ₹", "1.00", 1)
        self.ent_slip = self._gentry(g, "Max slip ₹", "0", 2)

        self._sec(p, "🕐 TIMEFRAME & SIZE")
        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(f, text="Candle", font=FM, text_color=TXT, width=100,
                     anchor="w").pack(side="left")
        self.opt_tf = ctk.CTkOptionMenu(
            f, values=list(TF_CHOICES), font=FS, width=104,
            height=26, fg_color=ACC, button_color=ACC, text_color="white",
            command=lambda _v: self._refresh_size_hint())
        self.opt_tf.set("2"); self.opt_tf.pack(side="left")
        ctk.CTkLabel(f, text="min", font=FL, text_color=DIM).pack(side="left", padx=4)

        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(f, text="Lots  ×3", font=FM, text_color=TXT, width=100,
                     anchor="w").pack(side="left")
        self.ent_lots = ctk.CTkEntry(f, font=FS, fg_color="#fafbfc",
                                     border_color=BD, text_color=TXT,
                                     height=26, width=70)
        self.ent_lots.insert(0, "1"); self.ent_lots.pack(side="left")
        self.ent_lots.bind("<KeyRelease>", lambda _e: self._refresh_size_hint())
        self.lbl_size = ctk.CTkLabel(p, text="", font=FT2, text_color=ACC,
                                     wraplength=252, justify="left")
        self.lbl_size.pack(anchor="w", padx=14, pady=(2, 0))

        self._sec(p, "📐 VWAP FIELD")
        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(f, text="Field", font=FM, text_color=TXT, width=100,
                     anchor="w").pack(side="left")
        self.opt_field = ctk.CTkOptionMenu(f, values=list(FIELD_CHOICES),
                                           font=FS, width=104, height=26,
                                           fg_color=ACC, button_color=ACC,
                                           text_color="white")
        self.opt_field.set("ohlc4"); self.opt_field.pack(side="left")
        ctk.CTkLabel(p, text="Must match the VWAP source on the client's chart. "
                             "ATR is Wilder and continuous; candles come from "
                             "REST and roll up by bar count. Those are fixed.",
                     font=FT2, text_color=DIM, wraplength=252, justify="left"
                     ).pack(anchor="w", padx=14, pady=(2, 0))

        self._sec(p, "🪜 LADDER")
        g = ctk.CTkFrame(p, fg_color="transparent"); g.pack(fill="x", padx=14, pady=2)
        self.ent_atrp = self._gentry(g, "ATR period", "14", 0)
        self.ent_mult = self._gentry(g, "ATR per rung", "2.0", 1)
        self.ent_cost = self._gentry(g, "T1 stop = E+", "2.00", 2)
        self.ent_seed = self._gentry(g, "ATR seed bars", "300", 3)

        self._sec(p, "⏰ TIMING")
        g = ctk.CTkFrame(p, fg_color="transparent"); g.pack(fill="x", padx=14, pady=2)
        self.ent_le = self._gentry(g, "Last entry", "14:45", 0)
        self.ent_so = self._gentry(g, "Square off", "15:00", 1)

        self._sec(p, "🛡 GUARDS  (0 = off)")
        g = ctk.CTkFrame(p, fg_color="transparent"); g.pack(fill="x", padx=14, pady=2)
        self.ent_minp = self._gentry(g, "Min premium ₹", "0", 0)
        self.ent_dll = self._gentry(g, "Stop day at −₹", "0", 1)
        ctk.CTkLabel(p, text="Stop day at: counts booked AND open P&L on every "
                             "tick. On breach it closes any open position "
                             "immediately and stops trading for the day.",
                     font=FT2, text_color=DIM, wraplength=250, justify="left"
                     ).pack(anchor="w", padx=14, pady=(2, 0))

        self.var_first = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(p, text="09:15 candle may signal", font=FL, variable=self.var_first,
                      progress_color=ACC, text_color=TXT, width=36
                      ).pack(anchor="w", padx=14, pady=6)

        ctk.CTkFrame(p, fg_color="transparent", height=8).pack()
        self.btn_start = ctk.CTkButton(p, text="▶  START ENGINE", font=FM, fg_color=GRN,
                                       hover_color="#15803d", text_color="white",
                                       height=44, corner_radius=8, command=self._on_start)
        self.btn_start.pack(fill="x", padx=14, pady=4)
        self.btn_save = ctk.CTkButton(p, text="💾  SAVE PARAMETERS", font=FM, fg_color=ACC,
                                      hover_color="#025a8c", text_color="white",
                                      height=44, corner_radius=8, command=self._on_save)
        self.btn_save.pack(fill="x", padx=14, pady=4)
        self.lbl_saved = ctk.CTkLabel(p, text="", font=FT2, text_color=DIM,
                                      wraplength=252, justify="left")
        self.lbl_saved.pack(anchor="w", padx=14, pady=(0, 2))
        self.btn_stop = ctk.CTkButton(p, text="■  STOP ENGINE", font=FM, fg_color=RED,
                                      hover_color="#b91c1c", text_color="white",
                                      height=44, corner_radius=8, command=self._on_stop,
                                      state="disabled")
        self.btn_stop.pack(fill="x", padx=14, pady=(0, 10))

    # ═══════════ DASHBOARD ═══════════
    def _build_dashboard(self):
        c = self.center

        pnl_card = ctk.CTkFrame(c, fg_color=CARD, height=85, corner_radius=10,
                                border_width=1, border_color=BD)
        pnl_card.pack(fill="x", pady=(0, 4)); pnl_card.pack_propagate(False)
        ctk.CTkLabel(pnl_card, text="TOTAL DAY P&L", font=FL,
                     text_color=DIM).pack(pady=(6, 0))
        self.lbl_pnl = ctk.CTkLabel(pnl_card, text="₹ 0", font=FB, text_color=TXT)
        self.lbl_pnl.pack()
        self.lbl_pnl_split = ctk.CTkLabel(pnl_card, text="", font=FT2,
                                          text_color=DIM)
        self.lbl_pnl_split.pack()

        info = ctk.CTkFrame(c, fg_color=CARD, corner_radius=8, border_width=1,
                            border_color=BD)
        info.pack(fill="x", pady=4)
        row = ctk.CTkFrame(info, fg_color="transparent"); row.pack(fill="x", pady=6)
        self.lbl_spot = self._info(row, "SENSEX", "—")
        self.lbl_strike = self._info(row, "STRIKE", "—")
        self.lbl_expiry = self._info(row, "EXPIRY", "—")
        self.lbl_lot = self._info(row, "LOT SIZE", "—")

        self._sec(c, "🎛 LEGS")
        self.leg_cards = {}
        legs_row = ctk.CTkFrame(c, fg_color="transparent"); legs_row.pack(fill="x", pady=2)
        for name in ["CE", "PE"]:
            self.leg_cards[name] = self._leg_card(legs_row, name)

        self._build_candles(c)

    def _info(self, parent, label, value):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(f, text=label, font=FT2, text_color=DIM).pack()
        lb = ctk.CTkLabel(f, text=value, font=FC, text_color=TXT); lb.pack()
        return lb

    def _leg_card(self, parent, name):
        cd = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8, border_width=1,
                          border_color=BD)
        cd.pack(side="left", fill="both", expand=True, padx=3, pady=2)

        hdr = ctk.CTkFrame(cd, fg_color="transparent"); hdr.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(hdr, text=f"{name} LEG", font=FH, text_color=ACC).pack(side="left")
        st = ctk.CTkLabel(hdr, text="IDLE", font=FM, text_color=DIM); st.pack(side="right")

        pnl = ctk.CTkLabel(cd, text="₹0", font=("Segoe UI", 22, "bold"), text_color=TXT)
        pnl.pack(pady=(2, 0))

        grid = ctk.CTkFrame(cd, fg_color="transparent"); grid.pack(fill="x", padx=10, pady=4)
        cells = {}
        rows = [("LTP", "ltp"), ("VWAP", "vwap"), ("ATR", "atr"), ("Trigger", "trig"),
                ("Entry E", "E"), ("Stop", "stop"), ("T1 / T2", "tt"),
                ("Lots open", "lots"), ("Open P&L", "upnl"), ("Rung", "rung"),
                ("Attempts", "att")]
        for i, (lbl, key) in enumerate(rows):
            ctk.CTkLabel(grid, text=lbl, font=FL, text_color=DIM, width=80, anchor="w"
                         ).grid(row=i, column=0, sticky="w", pady=1)
            v = ctk.CTkLabel(grid, text="—", font=FS, text_color=TXT, anchor="e", width=110)
            v.grid(row=i, column=1, sticky="e", pady=1)
            cells[key] = v

        elig = ctk.CTkLabel(cd, text="eligible", font=FT2, text_color=GRN)
        elig.pack(pady=(0, 6))
        return {"frame": cd, "state": st, "pnl": pnl, "cells": cells, "elig": elig}

    # ═══════════ LIVE CANDLES ═══════════
    def _build_candles(self, c):
        """
        Every closed candle with the VWAP and ATR the strategy used.

        This is the audit surface: if a signal fires, the bar that produced it
        is on screen with the exact indicator values behind the decision.
        """
        wrap = ctk.CTkFrame(c, fg_color=CARD, corner_radius=8, border_width=1,
                            border_color=BD)
        wrap.pack(fill="both", expand=True, pady=(4, 4))

        bar = ctk.CTkFrame(wrap, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(bar, text="🕯 LIVE CANDLES — VWAP & ATR", font=FH,
                     text_color=ACC).pack(side="left")
        self.lbl_recon = ctk.CTkLabel(bar, text="", font=FT2, text_color=DIM)
        self.lbl_recon.pack(side="left", padx=12)
        ctk.CTkButton(bar, text="Export CSV", font=FL, width=92, height=28,
                      fg_color="#e5e7eb", text_color=TXT, hover_color="#d1d5db",
                      command=self._export_csv).pack(side="right", padx=3)

        self.candle_box = ctk.CTkTextbox(wrap, fg_color="#fafbfc", font=FMONO,
                                         text_color=TXT, border_width=1,
                                         border_color=BD, corner_radius=4,
                                         wrap="none", state="disabled")
        self.candle_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._write_candles(
            "Closed candles appear here once the engine is running,\n"
            "each with the VWAP and ATR used to evaluate it.\n")

    def _write_candles(self, text, clear=False):
        self.candle_box.configure(state="normal")
        if clear:
            self.candle_box.delete("1.0", "end")
        self.candle_box.insert("end", text)
        self.candle_box.see("end")
        self.candle_box.configure(state="disabled")

    def _clear_candles(self):
        self.candle_rows = []
        self._write_candles("", clear=True)
        self.lbl_recon.configure(text="")

    def _fmt_row(self, leg, t, o, h, l, c, vol, vwap, atr):
        above = ""
        if vwap is not None:
            above = "ABOVE" if c > vwap else ("=" if c == vwap else "below")
        return (f"{leg:<4}{t:>6} {o:>9.2f} {h:>9.2f} {l:>9.2f} {c:>9.2f} "
                f"{vol:>10.0f} {('%.2f' % vwap) if vwap is not None else '-':>9} "
                f"{('%.2f' % atr) if atr is not None else '-':>8} {above:>6}\n")

    def _append_candle(self, d):
        self._write_candles(self._fmt_row(
            d.get("leg", ""), d.get("time", ""), d.get("open", 0), d.get("high", 0),
            d.get("low", 0), d.get("close", 0), d.get("volume", 0),
            d.get("vwap"), d.get("atr")))
        self.candle_rows.append({
            "leg": d.get("leg"), "time": d.get("time"), "open": d.get("open"),
            "high": d.get("high"), "low": d.get("low"), "close": d.get("close"),
            "volume": d.get("volume"), "vwap": d.get("vwap"), "atr": d.get("atr")})
        self.lbl_recon.configure(text=f"{len(self.candle_rows)} bars")

    def _export_csv(self):
        if not self.candle_rows:
            self._log("Nothing to export yet"); return
        path = LOG_DIR / f"candles_{now_ist().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(self.candle_rows[0].keys()))
                w.writeheader(); w.writerows(self.candle_rows)
            self._log(f"Exported {len(self.candle_rows)} bars -> {path.name}")
        except Exception as ex:
            self._log(f"ERROR Export failed: {ex}")

    # ═══════════ TRADE LOG ═══════════
    def _build_trade_log(self):
        r = self.right
        ctk.CTkLabel(r, text="📋 TRADE LOG", font=FH, text_color=ACC
                     ).pack(pady=(8, 4), padx=10, anchor="w")
        self.trade_log = ctk.CTkTextbox(r, fg_color="#fafbfc", font=FX, text_color=TXT,
                                        border_width=1, border_color=BD, corner_radius=4,
                                        wrap="word", state="disabled")
        self.trade_log.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def _build_bottom(self):
        bot = ctk.CTkFrame(self, fg_color=CARD, height=26, corner_radius=0,
                           border_width=1, border_color=BD)
        bot.pack(fill="x", side="bottom"); bot.pack_propagate(False)
        self.lbl_pkts = ctk.CTkLabel(bot, text="Packets: 0", font=FN, text_color=DIM)
        self.lbl_pkts.pack(side="left", padx=12)
        ctk.CTkLabel(bot, text="Balfund Trading Pvt Ltd", font=FN, text_color=DIM
                     ).pack(side="right", padx=12)

    # ═══════════ SETTINGS PERSISTENCE ═══════════
    #
    # Everything on the left panel except the credentials, which stay in .env.
    # Values are stored exactly as typed so a round-trip through the file
    # changes nothing on screen.

    def _param_widgets(self):
        """name in the settings file -> the entry box that holds it."""
        return {
            "entry_buffer":     self.ent_buf,
            "stop_buffer":      self.ent_sl,
            "max_slippage":     self.ent_slip,
            "lot_multiplier":   self.ent_lots,
            "atr_period":       self.ent_atrp,
            "rung_atr_mult":    self.ent_mult,
            "cost_plus":        self.ent_cost,
            "atr_seed_bars":    self.ent_seed,
            "last_entry":       self.ent_le,
            "square_off":       self.ent_so,
            "min_premium":      self.ent_minp,
            "daily_loss_limit": self.ent_dll,
        }

    def _collect_settings(self):
        d = {k: str(w.get()).strip() for k, w in self._param_widgets().items()}
        d["paper_mode"] = bool(self.var_paper.get())
        d["first_candle_can_signal"] = bool(self.var_first.get())
        d["timeframe_minutes"] = str(self.opt_tf.get())
        d["vwap_field"] = str(self.opt_field.get())
        return d

    def _apply_settings(self, d):
        """
        Put a settings dict back on screen.

        Anything missing or unrecognised is left at its default rather than
        blanking the control — a half-written file must not produce an empty
        entry box that then parses as zero.
        """
        for key, w in self._param_widgets().items():
            val = d.get(key)
            if val is None or str(val).strip() == "":
                continue
            w.delete(0, "end"); w.insert(0, str(val).strip())

        if isinstance(d.get("paper_mode"), bool):
            self.var_paper.set(d["paper_mode"])
        if isinstance(d.get("first_candle_can_signal"), bool):
            self.var_first.set(d["first_candle_can_signal"])

        tf = str(d.get("timeframe_minutes", "")).strip()
        if tf in TF_CHOICES:
            self.opt_tf.set(tf)
        fld = str(d.get("vwap_field", "")).strip()
        if fld in FIELD_CHOICES:
            self.opt_field.set(fld)

        self._refresh_mode(); self._refresh_size_hint()

    def _save_settings(self, note=""):
        """
        Write the panel to disk atomically.

        Atomically because the alternative — truncate, then write — leaves an
        empty file if the machine dies mid-write, and the next launch would
        come up on defaults with no warning. That is the exact failure this
        whole function exists to end.
        """
        data = self._collect_settings()
        data["version"] = SETTINGS_VERSION
        data["saved_at"] = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
        except OSError as ex:
            self._log(f"ERROR Could not save parameters to "
                      f"{SETTINGS_FILE.name}: {ex}")
            self.lbl_saved.configure(text="⚠ NOT SAVED — see the trade log",
                                     text_color=RED)
            return False
        self.lbl_saved.configure(
            text=f"saved {data['saved_at']} → {SETTINGS_FILE.name}{note}",
            text_color=DIM)
        return True

    def _load_settings(self):
        """Restore the panel from disk. Silent and harmless on a fresh install."""
        if not SETTINGS_FILE.exists():
            self._log(f"No saved parameters yet — using defaults. "
                      f"Press SAVE to create {SETTINGS_FILE.name}.")
            return False
        try:
            raw = SETTINGS_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as ex:
            self._log(f"ERROR {SETTINGS_FILE.name} could not be read ({ex}) "
                      f"— starting on defaults. Check it before trading.")
            return False
        if not isinstance(data, dict):
            self._log(f"ERROR {SETTINGS_FILE.name} is not a settings file "
                      f"— starting on defaults.")
            return False
        self._apply_settings(data)
        when = data.get("saved_at", "")
        mode = "PAPER" if self.var_paper.get() else "LIVE"
        self._log(f"Loaded saved parameters{' from ' + when if when else ''} "
                  f"— mode {mode}, {self.opt_tf.get()}-minute candles, "
                  f"{max(1, self._i(self.ent_lots, 1)) * 3} lots.")
        self.lbl_saved.configure(text=f"loaded {when}" if when else "loaded",
                                 text_color=DIM)
        return True

    # ═══════════ HELPERS ═══════════
    def _load_env(self):
        self.ent_cid.insert(0, os.getenv("DHAN_CLIENT_ID", ""))
        self.ent_pin.insert(0, os.getenv("DHAN_PIN", ""))
        self.ent_totp.insert(0, os.getenv("DHAN_TOTP_SECRET", ""))

    def _refresh_mode(self):
        """
        Repaint the mode badge.

        A client who believes a paper run is live loses the day's trades; one
        who believes a live run is paper loses money. Neither is acceptable
        from a switch that is easy to miss, so the mode is painted in the
        title bar in colour, and again beside the switch in words.
        """
        if bool(self.var_paper.get()):
            self.lbl_mode.configure(text="PAPER — NO REAL ORDERS", fg_color=YEL)
            self.lbl_mode_hint.configure(
                text="Simulated fills only. Nothing reaches the broker.",
                text_color=AMB)
        else:
            self.lbl_mode.configure(text="● LIVE — REAL ORDERS", fg_color=RED)
            self.lbl_mode_hint.configure(
                text="REAL ORDERS will be sent to Dhan with real money.",
                text_color=RED)

    def _refresh_size_hint(self):
        """
        Spell out what the multiplier actually trades.

        "Lots x3" is not self-explanatory, and a client who types 2 expecting
        two lots and gets six has a very bad day. Show the arithmetic.
        """
        try:
            mult = max(1, int(str(self.ent_lots.get()).strip() or "1"))
        except ValueError:
            self.lbl_size.configure(text="enter a whole number", text_color=RED)
            return
        lots = mult * 3
        lot_size = getattr(self, "_lot_size", 20) or 20
        third = lots // 3
        self.lbl_size.configure(
            text=(f"{mult} × 3 = {lots} lots = {lots * lot_size} qty\n"
                  f"{third} at T1 · {third} at T2 · {third} runner"),
            text_color=ACC)

    def _tick(self):
        self.lbl_clock.configure(text=now_ist().strftime("%H:%M:%S"))
        self._tick_job = self.after(1000, self._tick)

    def destroy(self):
        """Cancel the clock before tearing the window down."""
        job = getattr(self, "_tick_job", None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._tick_job = None
        super().destroy()

    def _log(self, msg):
        ts = now_ist().strftime("%H:%M:%S")
        self.trade_log.configure(state="normal")
        self.trade_log.insert("end", f"[{ts}] {msg}\n"); self.trade_log.see("end")
        self.trade_log.configure(state="disabled")

    # ═══════════ ENGINE EVENTS ═══════════
    def _on_event(self, ev, d):
        self.after(0, self._handle, ev, d)

    def _handle(self, ev, d):
        if ev == "status":
            self.lbl_status.configure(text=f"● {d.get('msg','')}", text_color=YEL)
            self._log(d.get("msg", ""))
        elif ev == "error":
            self.lbl_status.configure(text="● ERROR", text_color=RED)
            self._log(f"ERROR {d.get('msg','')}")
        elif ev == "ws_connected":
            # Deliberately NOT the word "live": that belongs to the mode badge
            # and nothing else, or a paper run reads as a funded one.
            self.lbl_ws.configure(text=f"WS ● {d.get('instruments',0)}", text_color=GRN)
            self.lbl_status.configure(text="● RUNNING", text_color=GRN)
            self._log(f"WS connected — {d.get('instruments',0)} instruments")
            self._clear_candles()
            self._write_candles(f"{'leg':<4}{CANDLE_HEADER}\n{'-'*98}\n")
        elif ev == "ws_disconnected":
            self.lbl_ws.configure(text="WS ✗", text_color=RED)
        elif ev == "strike":
            self.lbl_strike.configure(text=str(d.get("strike", "—")))
            self.lbl_expiry.configure(text=str(d.get("expiry", "—")))
            self.lbl_lot.configure(text=str(d.get("lot_size", "—")))
            try:
                self._lot_size = int(d.get("lot_size") or 20)
                self._refresh_size_hint()
            except (TypeError, ValueError):
                pass
            self._log(f"09:15 open {d.get('open',0):.2f} → strike {d.get('strike')} "
                      f"| exp {d.get('expiry')} | lot {d.get('lot_size')}")
        elif ev == "waiting_for_open":
            self.lbl_status.configure(
                text=f"● MARKET OPENS IN ~{d.get('text','?')}", text_color=YEL)
        elif ev == "seeded":
            note = "converged" if d.get("residual", 1) < 1e-4 else "NOT CONVERGED"
            self._log(f"[{d.get('leg')}] ATR seeded {d.get('bars')} bars → "
                      f"{d.get('atr', 0):.2f}  ({note})")
        elif ev == "bar_latency":
            if d.get("lag", 0) > 30:
                self._log(f"⚠ [{d.get('leg')}] bar {d.get('time')} arrived "
                          f"{d.get('lag', 0):.0f}s late")
        elif ev == "spot_tick":
            self.lbl_spot.configure(text=f"{d.get('spot',0):.2f}")
        elif ev == "candle":
            self._append_candle(d)
        elif ev == "armed":
            self._log(f"🎯 [{d.get('leg')}] ARMED trigger=₹{d.get('trigger',0):.2f} "
                      f"stop=₹{d.get('stop',0):.2f} ATR={d.get('atr',0):.2f} "
                      f"T1=₹{d.get('t1',0):.2f} T2=₹{d.get('t2',0):.2f}")
        elif ev == "order_cancelled":
            self._log(f"⊘ [{d.get('leg')}] Order @₹{d.get('trigger',0):.2f} not taken "
                      f"— leg stands down until a close below VWAP")
        elif ev == "entry":
            icon = "📝 PAPER" if d.get("paper") else "🔴 LIVE"
            self._log(f"{icon} [{d.get('leg')}] ENTRY {d.get('strike')} "
                      f"E=₹{d.get('E',0):.2f} fill=₹{d.get('fill',0):.2f} "
                      f"qty={d.get('qty')} stop=₹{d.get('stop',0):.2f}")
        elif ev == "rung":
            self._log(f"   T{d.get('rung')} @ ₹{d.get('price',0):.2f} "
                      f"→ stop ₹{d.get('stop',0):.2f}")
        elif ev == "scale_out":
            n = d.get("lots_sold", 1)
            self._log(f"   {d.get('tag')} sold {n} lot{'' if n == 1 else 's'} "
                      f"({d.get('qty', 0)} qty) @ ₹{d.get('price',0):.2f} "
                      f"these lots ₹{d.get('pnl',0):+,.0f} "
                      f"({d.get('lots_left')} left)")
        elif ev == "trade_closed":
            pnl = d.get("pnl", 0); icon = "✅" if pnl >= 0 else "❌"
            scaled = d.get("scaled", 0)
            # The per-lot lines above and this one are different units. Left
            # unlabelled, adding the whole column double-counts the scale-outs
            # and inflates the day badly.
            detail = (f"  = {scaled:+,.0f} scaled + {d.get('last_leg_pnl', 0):+,.0f} final"
                      if abs(scaled) > 0.005 else "")
            self._log(f"{icon} [{d.get('leg')}] CLOSED {d.get('reason')} "
                      f"rung=T{d.get('rung',0)}  TRADE TOTAL ₹{pnl:+,.0f}{detail}")
        elif ev == "position_mismatch":
            self._log(f"🚨 MISMATCH [{d.get('leg')}] — broker holds "
                      f"{d.get('broker')} qty, engine has {d.get('engine')}. "
                      f"An unmanaged position has no stop. CHECK THE TERMINAL.")
            self.lbl_status.configure(text="● POSITION MISMATCH", text_color=RED)
        elif ev == "order_unresolved":
            self._log(f"⚠ [{d.get('leg')}] order {d.get('order_id')} outcome "
                      f"unconfirmed — checking the broker's positions.")
        elif ev == "position_adopted":
            self._log(f"✔ [{d.get('leg')}] broker held {d.get('qty')} qty @ "
                      f"₹{d.get('price',0):.2f} — adopted, stop and ladder now "
                      f"manage it.")
        elif ev == "partial_fill":
            self._log(f"◐ [{d.get('leg')}] PARTIAL FILL {d.get('got')} of "
                      f"{d.get('wanted')} qty — managing what was filled.")
        elif ev == "daily_loss_hit":
            self._log(f"🛑 DAILY LOSS LIMIT ₹{d.get('limit',0):,.0f} reached — "
                      f"day at ₹{d.get('total',0):+,.0f} "
                      f"(booked ₹{d.get('realised',0):+,.0f}, "
                      f"open ₹{d.get('unrealised',0):+,.0f}). "
                      f"Closing {d.get('open_legs',0)} position(s) and stopping.")
            self.lbl_status.configure(text="● LOSS LIMIT", text_color=RED)
        elif ev == "lot_size_changed":
            self._log(f"⚠ LOT SIZE CHANGED {d.get('previous')} → {d.get('now')}. "
                      f"Position value moves accordingly — verify against the "
                      f"exchange circular.")
        elif ev == "feed_blind":
            m = d.get("seconds", 0) / 60
            self._log(f"🚫 NO CANDLES FOR {m:.0f} MIN — strategy is blind and "
                      f"cannot signal. See the terminal log for the cause.")
            self.lbl_status.configure(text="● NO DATA", text_color=RED)
        elif ev == "stale_signal":
            self._log(f"⏳ [{d.get('leg')}] signal at {d.get('time')} ignored — "
                      f"bar was {d.get('age',0):.0f}s old, not tradeable")
        elif ev == "suspended":
            self._log(f"⚡ App was not running for {d.get('seconds',0):.0f}s "
                      f"(power cut or sleep). Late bars will be ignored.")
        elif ev == "halted":
            self._log(f"🛑 HALTED — {d.get('reason')}")
            self.lbl_status.configure(text="● HALTED", text_color=RED)
        elif ev == "tick_update":
            self._update_dash(d)

    def _update_dash(self, d):
        total = d.get("total_pnl", 0)
        self.lbl_pnl.configure(text=f"₹{total:+,.0f}",
                               text_color=GRN if total >= 0 else RED)
        r, u = d.get("realised_pnl", 0), d.get("unrealised_pnl", 0)
        self.lbl_pnl_split.configure(
            text=f"booked ₹{r:+,.0f}     open ₹{u:+,.0f}" if (r or u) else "")
        pk = f"Packets: {d.get('packets',0):,}"
        fh = d.get("feed")
        if fh and fh.get("bars"):
            pk += (f"   |   candles: {d.get('data_source','')}  "
                   f"{fh['bars']} bars, lag avg {fh['avg_lag']:.1f}s "
                   f"max {fh['max_lag']:.1f}s ({fh['verdict']})")
        elif d.get("data_source"):
            pk += f"   |   candles: {d['data_source']}"
        self.lbl_pkts.configure(text=pk)
        if d.get("spot"): self.lbl_spot.configure(text=f"{d['spot']:.2f}")

        for leg in d.get("legs", []):
            name = leg["leg"]
            if name not in self.leg_cards: continue
            card = self.leg_cards[name]; c = card["cells"]
            st = leg["state"]
            card["state"].configure(text=st, text_color=STATE_COLORS.get(st, DIM))
            pnl = leg.get("open_pnl", leg["pnl"])
            card["pnl"].configure(text=f"₹{pnl:+,.0f}",
                                  text_color=GRN if pnl >= 0 else RED)

            c["ltp"].configure(text=f"₹{leg['ltp']:.2f}" if leg["ltp"] else "—")
            c["vwap"].configure(text=f"₹{leg['vwap']:.2f}" if leg["vwap"] else "…")
            c["atr"].configure(text=f"{leg['atr']:.2f}" if leg["atr"] else "…")
            c["trig"].configure(text=f"₹{leg['trigger']:.2f}" if leg["trigger"] else "—")
            c["E"].configure(text=f"₹{leg['E']:.2f}" if leg["E"] else "—")
            c["stop"].configure(text=f"₹{leg['stop']:.2f}" if leg["stop"] else "—")
            if leg["E"] and leg["atr"]:
                mult = self._f(self.ent_mult, 2.0)
                a = leg["atr"]
                c["tt"].configure(text=f"{leg['E'] + mult*a:.0f} / {leg['E'] + 2*mult*a:.0f}")
            else:
                c["tt"].configure(text="—")
            c["lots"].configure(text=str(leg["lots_open"]) if leg["lots_open"] else "—")
            u = leg.get("unrealised", 0)
            c["upnl"].configure(text=f"₹{u:+,.0f}" if leg["lots_open"] else "—",
                                text_color=(GRN if u >= 0 else RED)
                                if leg["lots_open"] else TXT)
            c["rung"].configure(text=f"T{leg['rung']}" if leg["rung"] else "—")
            c["att"].configure(text=f"{leg['attempts']} ({leg['trades']} filled)")

            if leg["eligible"]:
                card["elig"].configure(text="● eligible", text_color=GRN)
            else:
                card["elig"].configure(
                    text="● attempt spent — waiting for a close below VWAP",
                    text_color=RED)

            border = {"IN_TRADE": GRN, "ARMED": AMB, "STOOD_DOWN": "#fca5a5"}.get(st, BD)
            card["frame"].configure(border_color=border)

    # ═══════════ START / STOP ═══════════
    def _f(self, e, d=0.0):
        try: return float(e.get().strip())
        except Exception: return d

    def _i(self, e, d=0):
        try:
            return int(str(e.get()).strip())
        except Exception:
            return d

    def _build_config(self):
        return StrategyConfig(
            paper_mode=self.var_paper.get(),
            entry_buffer=self._f(self.ent_buf, 0.20),
            stop_buffer=self._f(self.ent_sl, 1.00),
            atr_period=self._i(self.ent_atrp, 14),
            vwap_field=self.opt_field.get(),
            timeframe_minutes=self._i(self.opt_tf, 2),
            lot_multiplier=max(1, self._i(self.ent_lots, 1)),
            rung_atr_mult=self._f(self.ent_mult, 2.0),
            cost_plus=self._f(self.ent_cost, 2.00),
            last_entry=self.ent_le.get().strip() or "14:45",
            square_off=self.ent_so.get().strip() or "15:00",
            atr_seed_bars=self._i(self.ent_seed, 100),
            min_premium=self._f(self.ent_minp, 0.0),
            daily_loss_limit=self._f(self.ent_dll, 0.0),
            first_candle_can_signal=self.var_first.get(),
            max_slippage=self._f(self.ent_slip, 0.0))

    def _confirm_live(self, cfg):
        """
        One deliberate yes before real money moves.

        Split out so the headless test suite can replace it; nothing else
        should ever call it.
        """
        return messagebox.askyesno(
            "Confirm LIVE trading",
            f"This run will place REAL orders on Dhan.\n\n"
            f"  Size            {cfg.lots} lots per entry\n"
            f"  Candle          {cfg.timeframe_minutes} minute\n"
            f"  Day loss cap    "
            f"{('₹' + format(cfg.daily_loss_limit, ',.0f')) if cfg.daily_loss_limit else 'OFF'}\n\n"
            f"Start in LIVE mode?",
            icon="warning", default="no", parent=self)

    def _on_start(self):
        if self.is_running: return
        cid = self.ent_cid.get().strip(); pin = self.ent_pin.get().strip()
        totp = self.ent_totp.get().strip()
        if not cid or not pin or not totp:
            self._log("ERROR Fill credentials"); return

        # Build the config BEFORE anything else, so what is confirmed, what is
        # saved and what the engine runs are the same object. The old code
        # could start from a _saved_config captured at some earlier SAVE while
        # the panel on screen said something different.
        cfg = self._build_config()
        if not cfg.paper_mode and not self._confirm_live(cfg):
            self._log("Live start cancelled — nothing was sent to the broker.")
            return

        # Starting is an endorsement of these parameters: persist them, so a
        # crash or a restart comes back exactly here.
        self._save_settings(note="  (on start)")

        set_key(str(ENV_FILE), "DHAN_CLIENT_ID", cid)
        set_key(str(ENV_FILE), "DHAN_PIN", pin)
        set_key(str(ENV_FILE), "DHAN_TOTP_SECRET", totp)
        self.btn_start.configure(state="disabled"); self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="● CONNECTING...", text_color=YEL)

        banner = ("📝 PAPER MODE — simulated fills, nothing reaches the broker"
                  if cfg.paper_mode else
                  "🔴 LIVE MODE — REAL ORDERS will be placed")
        self._log("─" * 46)
        self._log(banner)
        self._log(f"   {cfg.lots} lots · {cfg.timeframe_minutes}m candles · "
                  f"VWAP {cfg.vwap_field} · day cap "
                  f"{('₹%.0f' % cfg.daily_loss_limit) if cfg.daily_loss_limit else 'OFF'}")
        self._log("─" * 46)

        def _run():
            try:
                set_credentials(cid, pin, totp, os.getenv("DHAN_ACCESS_TOKEN", ""))
                self.after(0, self._log, "Authenticating...")
                init_credentials(status_cb=lambda m: self.after(0, self._log, f"  {m}"))
                self.after(0, self._log, "Token ready")
                self.engine = SensexVWAPLadderEngine(cfg, gui_callback=self._on_event)
                self.is_running = True
                self.engine.run()
            except Exception as e:
                self.after(0, self._log, f"ERROR Start failed: {e}")
                self.after(0, lambda: self.btn_start.configure(state="normal"))
                self.after(0, lambda: self.btn_stop.configure(state="disabled"))
                self.after(0, lambda: self.lbl_status.configure(text="● ERROR", text_color=RED))
        threading.Thread(target=_run, daemon=True).start()

    def _on_stop(self):
        if self.engine: self.engine.stop()
        self.is_running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.lbl_status.configure(text="● STOPPED", text_color=RED)
        self._log("Engine stopped")

    def _on_save(self):
        cfg = self._build_config()
        ok = self._save_settings()
        mode = "PAPER" if cfg.paper_mode else "LIVE"
        if self.engine and self.is_running:
            self.engine.config = cfg
            self._log(f"💾 Parameters applied to the running engine and "
                      f"{'saved' if ok else 'NOT saved'} to {SETTINGS_FILE.name} "
                      f"(mode {mode})")
        elif ok:
            self._log(f"💾 Parameters saved to {SETTINGS_FILE.name} — mode "
                      f"{mode}, {cfg.lots} lots, {cfg.timeframe_minutes}m "
                      f"candles. They will load automatically next time.")

    def _on_close(self):
        """Save on the way out, so closing the window is not a way to lose work."""
        try:
            self._save_settings(note="  (on exit)")
        except Exception:
            pass
        if self.is_running and self.engine:
            try:
                self.engine.stop()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = SensexApp(); app.mainloop()
