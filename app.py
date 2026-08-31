"""
SENSEX VWAP Reclaim + ATR Ladder — GUI Application v1.1
════════════════════════════════════════════════════════
Two legs (CE + PE) of one strike | Light Theme

v1.1 adds the RECONCILE panel: every closed 2-minute candle with its per-bar
volume, VWAP and ATR — live during the session, or pulled from history on
demand with no engine and no market hours. Diff against Kite without leaving
the app.

Balfund Trading Pvt Ltd
"""

import os, sys, csv, logging, threading
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
from dotenv import load_dotenv, set_key

import engine as E
from engine import (
    SensexVWAPLadderEngine, StrategyConfig,
    init_credentials, set_credentials, now_ist, ENV_FILE
)
from indicators import compute_series

if getattr(sys, 'frozen', False):
    _APP_DIR = Path(sys.executable).parent
else:
    _APP_DIR = Path(__file__).parent if "__file__" in globals() else Path.cwd()

LOG_DIR = _APP_DIR / "logs"; LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("SNX_GUI")

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
                 f"{'volume':>10} {'VWAP':>9} {'ATR':>8} {'above?':>7}")


class SensexApp(ctk.CTk):
    VERSION = "1.1"

    def __init__(self):
        super().__init__()
        load_dotenv(str(ENV_FILE), override=True)
        self.title(f"SENSEX VWAP Ladder v{self.VERSION} — Balfund Trading")
        self.geometry("1560x980"); self.minsize(1280, 820)
        self.configure(fg_color=BG)
        self.engine = None; self.is_running = False
        self._saved_config = None
        self.candle_rows = []          # everything shown in the reconcile panel
        self._build_ui(); self._load_env(); self._tick()

    # ═══════════ LAYOUT ═══════════
    def _build_ui(self):
        top = ctk.CTkFrame(self, fg_color=CARD, height=52, corner_radius=0,
                           border_width=1, border_color=BD)
        top.pack(fill="x"); top.pack_propagate(False)
        ctk.CTkLabel(top, text="📈 SENSEX VWAP Ladder", font=FT, text_color=ACC
                     ).pack(side="left", padx=16)
        ctk.CTkLabel(top, text=f"v{self.VERSION}", font=FN, text_color=DIM
                     ).pack(side="left", padx=4, pady=(4, 0))
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
                      progress_color=YEL, text_color=TXT).pack(anchor="w", padx=14, pady=4)

        self._sec(p, "🎯 ENTRY & STOP")
        g = ctk.CTkFrame(p, fg_color="transparent"); g.pack(fill="x", padx=14, pady=2)
        self.ent_buf = self._gentry(g, "Entry buffer ₹", "0.20", 0)
        self.ent_sl = self._gentry(g, "Stop buffer ₹", "1.00", 1)
        self.ent_lots = self._gentry(g, "Lots", "3", 2)
        self.ent_slip = self._gentry(g, "Max slip ₹", "0", 3)

        self._sec(p, "📐 STUDIES  (must match his chart)")
        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(f, text="VWAP field", font=FM, text_color=TXT, width=100,
                     anchor="w").pack(side="left")
        self.opt_field = ctk.CTkOptionMenu(f, values=["ohlc4", "hlc3", "hl2", "close"],
                                           font=FS, width=100, height=26,
                                           fg_color=ACC, button_color=ACC)
        self.opt_field.set("ohlc4"); self.opt_field.pack(side="left")
        f = ctk.CTkFrame(p, fg_color="transparent"); f.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(f, text="ATR method", font=FM, text_color=TXT, width=100,
                     anchor="w").pack(side="left")
        self.opt_atrm = ctk.CTkOptionMenu(f, values=["wilder", "sma"], font=FS,
                                          width=100, height=26, fg_color=ACC,
                                          button_color=ACC)
        self.opt_atrm.set("wilder"); self.opt_atrm.pack(side="left")
        ctk.CTkLabel(p, text="ATR runs continuously across sessions. Use the "
                             "RECONCILE panel to check both against Kite before "
                             "trusting them.",
                     font=FT2, text_color=DIM, wraplength=250, justify="left"
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
        self.ent_dll = self._gentry(g, "Daily loss ₹", "0", 1)
        ctk.CTkLabel(p, text="Both are open items — off until the client decides.",
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
        ctk.CTkLabel(pnl_card, text="TOTAL DAY P&L", font=FL, text_color=DIM).pack(pady=(8, 0))
        self.lbl_pnl = ctk.CTkLabel(pnl_card, text="₹ 0", font=FB, text_color=TXT)
        self.lbl_pnl.pack()

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

        self._build_reconcile(c)

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
                ("Lots open", "lots"), ("Rung", "rung"), ("Attempts", "att")]
        for i, (lbl, key) in enumerate(rows):
            ctk.CTkLabel(grid, text=lbl, font=FL, text_color=DIM, width=80, anchor="w"
                         ).grid(row=i, column=0, sticky="w", pady=1)
            v = ctk.CTkLabel(grid, text="—", font=FS, text_color=TXT, anchor="e", width=110)
            v.grid(row=i, column=1, sticky="e", pady=1)
            cells[key] = v

        elig = ctk.CTkLabel(cd, text="eligible", font=FT2, text_color=GRN)
        elig.pack(pady=(0, 6))
        return {"frame": cd, "state": st, "pnl": pnl, "cells": cells, "elig": elig}

    # ═══════════ RECONCILE PANEL ═══════════
    def _build_reconcile(self, c):
        wrap = ctk.CTkFrame(c, fg_color=CARD, corner_radius=8, border_width=1,
                            border_color=BD)
        wrap.pack(fill="both", expand=True, pady=(4, 4))

        bar = ctk.CTkFrame(wrap, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(bar, text="🔍 RECONCILE — candles, VWAP & ATR vs Kite",
                     font=FH, text_color=ACC).pack(side="left")
        self.lbl_recon = ctk.CTkLabel(bar, text="", font=FT2, text_color=DIM)
        self.lbl_recon.pack(side="left", padx=12)

        ctk.CTkButton(bar, text="Export CSV", font=FL, width=88, height=28,
                      fg_color="#e5e7eb", text_color=TXT, hover_color="#d1d5db",
                      command=self._export_csv).pack(side="right", padx=3)
        ctk.CTkButton(bar, text="Clear", font=FL, width=58, height=28,
                      fg_color="#e5e7eb", text_color=TXT, hover_color="#d1d5db",
                      command=self._clear_candles).pack(side="right", padx=3)
        self.btn_recon = ctk.CTkButton(bar, text="⟳  LOAD FROM HISTORY", font=FM,
                                       width=176, height=28, fg_color=ACC,
                                       hover_color="#025a8c", text_color="white",
                                       command=self._on_reconcile)
        self.btn_recon.pack(side="right", padx=3)
        self.ent_recon_strike = ctk.CTkEntry(bar, font=FS, width=78, height=28,
                                             fg_color="#fafbfc", border_color=BD,
                                             text_color=TXT, placeholder_text="strike")
        self.ent_recon_strike.pack(side="right", padx=3)
        self.opt_recon_leg = ctk.CTkOptionMenu(bar, values=["BOTH", "CE", "PE"],
                                               font=FS, width=78, height=28,
                                               fg_color=ACC, button_color=ACC,
                                               text_color="white")
        self.opt_recon_leg.set("BOTH"); self.opt_recon_leg.pack(side="right", padx=3)

        self.candle_box = ctk.CTkTextbox(wrap, fg_color="#fafbfc", font=FMONO,
                                         text_color=TXT, border_width=1,
                                         border_color=BD, corner_radius=4,
                                         wrap="none", state="disabled")
        self.candle_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._write_candles(
            "Closed 2-minute candles appear here live once the engine is running.\n"
            "Or press LOAD FROM HISTORY to pull today's bars right now — no engine,\n"
            "no market hours needed — and compare them against the chart.\n\n"
            "Check in this order:  timestamps -> volume -> VWAP -> ATR.\n"
            "The 09:15 ATR is the one the overnight gap moves, so start there.\n")

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
            above = "YES" if c > vwap else ("=" if c == vwap else "no")
        return (f"{leg:<4}{t:>6} {o:>9.2f} {h:>9.2f} {l:>9.2f} {c:>9.2f} "
                f"{vol:>10.0f} {('%.2f' % vwap) if vwap is not None else '-':>9} "
                f"{('%.2f' % atr) if atr is not None else '-':>8} {above:>7}\n")

    def _append_candle(self, d):
        """A candle just closed on the live feed."""
        self._write_candles(self._fmt_row(
            d.get("leg", ""), d.get("time", ""), d.get("open", 0), d.get("high", 0),
            d.get("low", 0), d.get("close", 0), d.get("volume", 0),
            d.get("vwap"), d.get("atr")))
        self.candle_rows.append({
            "leg": d.get("leg"), "time": d.get("time"), "open": d.get("open"),
            "high": d.get("high"), "low": d.get("low"), "close": d.get("close"),
            "volume": d.get("volume"), "vwap": d.get("vwap"), "atr": d.get("atr"),
            "source": "live"})
        self.lbl_recon.configure(text=f"{len(self.candle_rows)} bars (live)")

    def _export_csv(self):
        if not self.candle_rows:
            self._log("Nothing to export yet"); return
        path = LOG_DIR / f"reconcile_{now_ist().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(self.candle_rows[0].keys()))
                w.writeheader(); w.writerows(self.candle_rows)
            self._log(f"Exported {len(self.candle_rows)} rows -> {path.name}")
        except Exception as ex:
            self._log(f"ERROR Export failed: {ex}")

    def _on_reconcile(self):
        cid = self.ent_cid.get().strip(); pin = self.ent_pin.get().strip()
        totp = self.ent_totp.get().strip()
        if not cid or not pin or not totp:
            self._log("ERROR Fill credentials before loading history"); return
        self.btn_recon.configure(state="disabled", text="Loading...")
        threading.Thread(target=self._reconcile_worker,
                         args=(cid, pin, totp), daemon=True).start()

    def _reconcile_worker(self, cid, pin, totp):
        ui = lambda fn, *a: self.after(0, fn, *a)
        try:
            if not E.HEADERS.get("access-token"):
                set_credentials(cid, pin, totp, os.getenv("DHAN_ACCESS_TOKEN", ""))
                ui(self._log, "Authenticating for reconcile...")
                init_credentials()

            field = self.opt_field.get()
            method = self.opt_atrm.get()
            period = self._i(self.ent_atrp, 14)

            expiry = E.get_nearest_expiry()
            if not expiry:
                ui(self._log, "ERROR No expiry found"); return

            txt = self.ent_recon_strike.get().strip()
            if txt.isdigit():
                strike = int(txt)
            else:
                idx_open = E.get_sensex_open_0915()
                if not idx_open:
                    ui(self._log, "ERROR Could not read the 09:15 index open"); return
                strike = int(round(idx_open / 100.0) * 100)
                ui(self._log, f"09:15 open {idx_open:.2f} -> strike {strike}")

            oc = E.fetch_option_chain(expiry)
            if not oc:
                ui(self._log, "ERROR Option chain unavailable"); return

            legs = {}
            for sk, sd in oc["oc"].items():
                try:
                    if abs(float(sk) - strike) > 0.01:
                        continue
                except ValueError:
                    continue
                for side, key in (("CE", "ce"), ("PE", "pe")):
                    if key in sd:
                        legs[side] = str(sd[key]["security_id"])

            sel = self.opt_recon_leg.get()
            wanted = ["CE", "PE"] if sel == "BOTH" else [sel]
            anchor = E.session_anchor_epoch()

            ui(self._clear_candles)
            total = 0
            for side in wanted:
                sec = legs.get(side)
                if not sec:
                    ui(self._write_candles, f"\n{side}: strike {strike} not listed\n")
                    continue

                raw = E.fetch_intraday_1m(sec, E.SENSEX["segment"], "OPTIDX", days=10)
                c2 = E.aggregate_2m(raw)
                prior = [c for c in c2 if c["ts"] < anchor]
                rows = compute_series(c2, anchor, field, period, method)
                sess = [r for r in rows if r["ts"] >= anchor]

                resid = (1 - 1 / period) ** len(prior) if prior else 1.0
                gaps = [i for i in range(1, len(sess))
                        if sess[i]["ts"] - sess[i - 1]["ts"] != 120]

                hdr = (f"\n{'='*98}\n"
                       f"SENSEX {strike} {side}   secId={sec}   expiry={expiry}\n"
                       f"VWAP field={field}   ATR({period},{method}) continuous\n"
                       f"Warm-up {len(prior)} prior bars, seed influence {resid:.2e} "
                       f"{'(converged)' if resid < 1e-4 else '(NOT CONVERGED)'}\n"
                       f"Session bars {len(sess)}")
                if sess and sess[0]["atr"] is not None:
                    hdr += f"   ATR at 09:15 = {sess[0]['atr']:.2f}"
                hdr += "\n"
                if gaps:
                    hdr += (f"** {len(gaps)} gap(s): bars with no trades are absent from "
                            f"the feed. ATR is bar-count sensitive, so check whether the "
                            f"chart draws them. First at "
                            f"{E.epoch_to_ist(sess[gaps[0]]['ts'], '%H:%M')}\n")
                hdr += f"{'-'*98}\n{'leg':<4}{CANDLE_HEADER}\n{'-'*98}\n"
                ui(self._write_candles, hdr)

                for r in sess:
                    ui(self._write_candles, self._fmt_row(
                        side, E.epoch_to_ist(r["ts"], "%H:%M"), r["open"], r["high"],
                        r["low"], r["close"], r.get("volume", 0), r["vwap"], r["atr"]))
                    self.candle_rows.append({
                        "leg": side, "time": E.epoch_to_ist(r["ts"], "%H:%M"),
                        "open": r["open"], "high": r["high"], "low": r["low"],
                        "close": r["close"], "volume": r.get("volume", 0),
                        "vwap": r["vwap"], "atr": r["atr"], "source": "history"})
                total += len(sess)

            ui(lambda: self.lbl_recon.configure(text=f"{total} bars (history)"))
            ui(self._log, f"Reconcile loaded {total} bars for {strike}")
        except Exception as ex:
            ui(self._log, f"ERROR Reconcile failed: {ex}")
        finally:
            ui(lambda: self.btn_recon.configure(state="normal",
                                                text="⟳  LOAD FROM HISTORY"))

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

    # ═══════════ HELPERS ═══════════
    def _load_env(self):
        self.ent_cid.insert(0, os.getenv("DHAN_CLIENT_ID", ""))
        self.ent_pin.insert(0, os.getenv("DHAN_PIN", ""))
        self.ent_totp.insert(0, os.getenv("DHAN_TOTP_SECRET", ""))

    def _tick(self):
        self.lbl_clock.configure(text=now_ist().strftime("%H:%M:%S"))
        self.after(1000, self._tick)

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
            self.lbl_ws.configure(text=f"WS ● {d.get('instruments',0)}", text_color=GRN)
            self.lbl_status.configure(text="● LIVE", text_color=GRN)
            self._log(f"WS connected — {d.get('instruments',0)} instruments")
            self._clear_candles()
            self._write_candles(f"{'leg':<4}{CANDLE_HEADER}\n{'-'*98}\n")
        elif ev == "ws_disconnected":
            self.lbl_ws.configure(text="WS ✗", text_color=RED)
        elif ev == "strike":
            self.lbl_strike.configure(text=str(d.get("strike", "—")))
            self.lbl_expiry.configure(text=str(d.get("expiry", "—")))
            self.lbl_lot.configure(text=str(d.get("lot_size", "—")))
            self._log(f"09:15 open {d.get('open',0):.2f} → strike {d.get('strike')} "
                      f"| exp {d.get('expiry')} | lot {d.get('lot_size')}")
        elif ev == "seeded":
            note = "converged" if d.get("residual", 1) < 1e-4 else "NOT CONVERGED"
            self._log(f"[{d.get('leg')}] ATR seeded {d.get('bars')} bars → "
                      f"{d.get('atr', 0):.2f}  ({note})")
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
            icon = "📝" if d.get("paper") else "🔴"
            self._log(f"{icon} [{d.get('leg')}] ENTRY {d.get('strike')} "
                      f"E=₹{d.get('E',0):.2f} fill=₹{d.get('fill',0):.2f} "
                      f"qty={d.get('qty')} stop=₹{d.get('stop',0):.2f}")
        elif ev == "rung":
            self._log(f"   T{d.get('rung')} @ ₹{d.get('price',0):.2f} "
                      f"→ stop ₹{d.get('stop',0):.2f}")
        elif ev == "scale_out":
            self._log(f"   {d.get('tag')} sold 1 lot @ ₹{d.get('price',0):.2f} "
                      f"PnL=₹{d.get('pnl',0):+,.0f} ({d.get('lots_left')} left)")
        elif ev == "trade_closed":
            pnl = d.get("pnl", 0); icon = "✅" if pnl >= 0 else "❌"
            self._log(f"{icon} [{d.get('leg')}] CLOSED {d.get('reason')} "
                      f"rung=T{d.get('rung',0)} PnL=₹{pnl:+,.0f}")
        elif ev == "halted":
            self._log(f"🛑 HALTED — {d.get('reason')}")
            self.lbl_status.configure(text="● HALTED", text_color=RED)
        elif ev == "tick_update":
            self._update_dash(d)

    def _update_dash(self, d):
        total = d.get("total_pnl", 0)
        self.lbl_pnl.configure(text=f"₹{total:+,.0f}", text_color=GRN if total >= 0 else RED)
        self.lbl_pkts.configure(text=f"Packets: {d.get('packets',0):,}")
        if d.get("spot"): self.lbl_spot.configure(text=f"{d['spot']:.2f}")

        for leg in d.get("legs", []):
            name = leg["leg"]
            if name not in self.leg_cards: continue
            card = self.leg_cards[name]; c = card["cells"]
            st = leg["state"]
            card["state"].configure(text=st, text_color=STATE_COLORS.get(st, DIM))
            pnl = leg["pnl"]
            card["pnl"].configure(text=f"₹{pnl:+,.0f}", text_color=GRN if pnl >= 0 else RED)

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
        try: return int(e.get().strip())
        except Exception: return d

    def _build_config(self):
        return StrategyConfig(
            paper_mode=self.var_paper.get(),
            entry_buffer=self._f(self.ent_buf, 0.20),
            stop_buffer=self._f(self.ent_sl, 1.00),
            atr_period=self._i(self.ent_atrp, 14),
            vwap_field=self.opt_field.get(),
            atr_method=self.opt_atrm.get(),
            rung_atr_mult=self._f(self.ent_mult, 2.0),
            cost_plus=self._f(self.ent_cost, 2.00),
            lots=self._i(self.ent_lots, 3),
            last_entry=self.ent_le.get().strip() or "14:45",
            square_off=self.ent_so.get().strip() or "15:00",
            atr_seed_bars=self._i(self.ent_seed, 100),
            min_premium=self._f(self.ent_minp, 0.0),
            daily_loss_limit=self._f(self.ent_dll, 0.0),
            first_candle_can_signal=self.var_first.get(),
            max_slippage=self._f(self.ent_slip, 0.0))

    def _on_start(self):
        if self.is_running: return
        cid = self.ent_cid.get().strip(); pin = self.ent_pin.get().strip()
        totp = self.ent_totp.get().strip()
        if not cid or not pin or not totp:
            self._log("ERROR Fill credentials"); return
        set_key(str(ENV_FILE), "DHAN_CLIENT_ID", cid)
        set_key(str(ENV_FILE), "DHAN_PIN", pin)
        set_key(str(ENV_FILE), "DHAN_TOTP_SECRET", totp)
        self.btn_start.configure(state="disabled"); self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="● CONNECTING...", text_color=YEL)

        def _run():
            try:
                set_credentials(cid, pin, totp, os.getenv("DHAN_ACCESS_TOKEN", ""))
                self.after(0, self._log, "Authenticating...")
                init_credentials(status_cb=lambda m: self.after(0, self._log, f"  {m}"))
                self.after(0, self._log, "Token ready")
                cfg = self._saved_config or self._build_config()
                self._saved_config = None
                if not cfg.paper_mode:
                    self.after(0, self._log, "⚠ LIVE MODE — real orders will be placed")
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
        if self.engine and self.is_running:
            self.engine.config = cfg
            self._log("💾 Parameters saved to running engine")
        else:
            self._saved_config = cfg
            self._log("💾 Parameters saved — will apply on next START")


if __name__ == "__main__":
    app = SensexApp(); app.mainloop()
