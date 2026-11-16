#!/usr/bin/env python3
"""
doctor.py — pre-flight check. Run this first, and any time something is off.

    python doctor.py            environment + credentials only, no network
    python doctor.py --api      also log in to Dhan and probe the data feed

Checks, in order, stopping at the first thing that would make the app fail:

  1. Python version and whether you are inside the venv
  2. Every dependency importable, tkinter included (macOS trips on this)
  3. .env present and which keys are filled
  4. Dhan login
  5. Instrument master, expiry list, SENSEX lot size
  6. Historical data: timestamps, session alignment, feed gaps

Nothing here places an order or touches the trading path.
"""

import sys
import os
import time
import argparse
from pathlib import Path

BASE = Path(__file__).parent
OK, WARN, BAD = "  OK  ", " WARN ", " FAIL "
_fails = []
_warns = []


def line(state, label, detail=""):
    print(f"[{state}] {label}" + (f"\n         {detail}" if detail else ""))
    if state == BAD:
        _fails.append(label)
    elif state == WARN:
        _warns.append(label)


def head(t):
    print(f"\n{'-' * 72}\n{t}\n{'-' * 72}")


def check_python():
    head("1. Python")
    v = sys.version_info
    if v < (3, 9):
        line(BAD, f"Python {v.major}.{v.minor}", "Need 3.9 or newer. Install python.org 3.11.")
    else:
        line(OK, f"Python {v.major}.{v.minor}.{v.micro}")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    venv_dir = BASE / ".venv"
    if in_venv:
        line(OK, "Running inside a virtual environment", sys.prefix)
    elif venv_dir.exists():
        line(WARN, "A .venv exists but you are NOT using it",
             "VS Code: Cmd+Shift+P -> Python: Select Interpreter -> ./.venv/bin/python\n"
             "         Terminal: source .venv/bin/activate")
    else:
        line(WARN, "No virtual environment", "Run ./setup.sh to create one.")


def check_deps():
    head("2. Dependencies")
    mods = [("requests", "requests"), ("pyotp", "pyotp"),
            ("websocket", "websocket-client"), ("dotenv", "python-dotenv"),
            ("customtkinter", "customtkinter")]
    for mod, pkg in mods:
        try:
            __import__(mod)
            line(OK, pkg)
        except ImportError:
            line(BAD, pkg, f"pip install {pkg}")

    try:
        import tkinter  # noqa: F401
        line(OK, "tkinter (GUI toolkit)")
    except ImportError:
        line(BAD, "tkinter missing — the GUI cannot start",
             "macOS: the python.org installer bundles it; Homebrew python does not.\n"
             "         Fix:  brew install python-tk\n"
             "         Or install Python from python.org and rebuild the venv.")

    for f in ("engine.py", "app.py", "indicators.py"):
        if (BASE / f).exists():
            line(OK, f"{f} present")
        else:
            line(BAD, f"{f} MISSING", "Unzip the build into this folder.")


def check_env():
    head("3. Credentials (.env)")
    env = BASE / ".env"
    if not env.exists():
        line(WARN, ".env not found",
             "It is created when you press START in the app, or:  cp .env.example .env")
        return False
    line(OK, ".env found")

    from dotenv import dotenv_values
    vals = dotenv_values(str(env))
    have_all = True
    for key in ("DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET"):
        v = (vals.get(key) or "").strip()
        if v:
            line(OK, key, f"set ({len(v)} chars)")
        else:
            line(BAD, key, "empty")
            have_all = False
    for k in vals:
        if k.count("DHAN_") > 1:
            line(BAD, f"Mangled line in .env: {k[:40]}",
                 "Two settings ran together, probably from an append onto a file\n"
                 "         with no trailing newline. Open .env and split them.")
    tok = (vals.get("DHAN_ACCESS_TOKEN") or "").strip()
    line(OK if tok else WARN, "DHAN_ACCESS_TOKEN",
         "cached" if tok else "empty — will be generated from your TOTP on first login")

    try:
        mode = oct(env.stat().st_mode)[-3:]
        if mode != "600":
            line(WARN, f".env permissions are {mode}", "Tighten with:  chmod 600 .env")
    except Exception:
        pass
    return have_all


def check_network():
    """
    Time the TCP connect to Dhan over IPv4 and over whatever the OS picks.

    A broken IPv6 route shows up as a connect that takes tens of seconds and
    then succeeds. That looks like a slow API but is not one, and it makes the
    strategy unusable because a candle arriving 80s after it closed is no use
    to a rule whose order lives for one candle.
    """
    head("3b. Network path to api.dhan.co")
    import socket as sk
    host, port = "api.dhan.co", 443

    try:
        infos = sk.getaddrinfo(host, port, sk.AF_UNSPEC, sk.SOCK_STREAM)
    except Exception as e:
        line(BAD, "DNS lookup failed", str(e)); return
    n6 = sum(1 for i in infos if i[0] == sk.AF_INET6)
    n4 = sum(1 for i in infos if i[0] == sk.AF_INET)
    fams = {("IPv6" if i[0] == sk.AF_INET6 else "IPv4") for i in infos}
    line(OK, "DNS resolves", f"{host} -> {n4} IPv4, {n6} IPv6 addresses")
    if n6 > 1 and os.getenv("DHAN_FORCE_IPV4", "").strip().lower() \
            not in ("1", "true", "yes"):
        line(WARN, f"{n6} IPv6 addresses",
             f"Python tries each in turn with the FULL timeout, unlike curl\n"
             f"         which races both families. If the IPv6 route is dead, a\n"
             f"         15s timeout becomes {n6 * 15}s before IPv4 is reached.")

    def connect_time(family):
        try:
            infos = sk.getaddrinfo(host, port, family, sk.SOCK_STREAM)
        except Exception as e:
            return 0.0, f"no address ({e})"
        if not infos:
            return 0.0, "no address"
        af, socktype, proto, _, sa = infos[0]
        s_ = None
        t0 = time.time()
        try:
            s_ = sk.socket(af, socktype, proto)
            s_.settimeout(20)
            s_.connect(sa)
            return time.time() - t0, None
        except Exception as e:
            return time.time() - t0, str(e)
        finally:
            if s_ is not None:
                try:
                    s_.close()
                except Exception:
                    pass

    v4, e4 = connect_time(sk.AF_INET)
    v4 = v4 or 0.0
    if e4:
        line(BAD, "IPv4 connect", f"{v4:.1f}s — {e4}")
    else:
        line(OK if v4 < 2 else WARN, "IPv4 connect", f"{v4:.2f}s")

    forced = os.getenv("DHAN_FORCE_IPV4", "").strip().lower() in ("1", "true", "yes")

    if "IPv6" in fams:
        v6, e6 = connect_time(sk.AF_INET6)
        v6 = v6 or 0.0
        stalls = (v6 > 5)

        # Severity depends on whether we actually use IPv6. With
        # DHAN_FORCE_IPV4=1 the path is never attempted, so a broken route is a
        # fact about the network, not a fault in the setup.
        if e6 and v6 < 1.0:
            line(OK, "IPv6 connect", f"refused immediately ({v6:.2f}s) — "
                                     f"falls straight back to IPv4")
        elif e6 or stalls:
            detail = (f"{'failed after' if e6 else 'took'} {v6:.0f}s"
                      + (f" — {e6}" if e6 else ""))
            if forced:
                line(WARN, "IPv6 connect", detail + "  (not used — IPv4 forced)")
            else:
                line(BAD, "IPv6 connect", detail)
        else:
            line(OK, "IPv6 connect", f"{v6:.2f}s")

        if stalls and not e4 and v4 < 2:
            if forced:
                line(OK, "IPv6 is slow, but mitigated",
                     f"IPv6 needs {v6:.0f}s to give up while IPv4 answers in "
                     f"{v4:.2f}s.\n"
                     f"         DHAN_FORCE_IPV4=1 is set, so IPv6 is never "
                     f"tried. Nothing to do.")
            else:
                line(BAD, "IPv6 is the bottleneck",
                     f"IPv4 answers in {v4:.2f}s but IPv6 takes {v6:.0f}s to "
                     f"give up, and Python\n"
                     f"         tries all {n6} of them in turn — "
                     f"{n6} x {v6:.0f}s = {n6 * v6:.0f}s per call.\n"
                     f"         FIX: add this line to .env  ->  "
                     f"DHAN_FORCE_IPV4=1")
    else:
        line(OK, "IPv6", "not advertised — nothing to stall on")

    line(OK if forced else WARN, "DHAN_FORCE_IPV4",
         "set — IPv4 only, IPv6 never attempted"
         if forced else "not set (fine only if IPv6 is healthy)")


def check_api():
    head("4. Dhan login")
    t0 = time.time()
    import engine as E
    from dotenv import load_dotenv
    load_dotenv(str(BASE / ".env"), override=True)
    E.set_credentials(os.getenv("DHAN_CLIENT_ID", ""), os.getenv("DHAN_PIN", ""),
                      os.getenv("DHAN_TOTP_SECRET", ""),
                      os.getenv("DHAN_ACCESS_TOKEN", ""))
    try:
        t0 = time.time()
        E.init_credentials()
        dt = time.time() - t0
        line(OK if dt < 5 else WARN, "Authenticated", f"{dt:.1f}s"
             + ("" if dt < 5 else "  <- slow; see the network section above"))
    except Exception as ex:
        line(BAD, "Login failed", str(ex))
        return

    head("5. Instruments")
    lot = E.fetch_sensex_lot_size()
    line(OK if lot else BAD, "SENSEX lot size",
         f"{lot}" if lot else "scrip master unreachable — the app refuses to trade")

    expiry = E.get_nearest_expiry()
    line(OK if expiry else BAD, "Nearest expiry", expiry or "none returned")
    if not expiry:
        return

    idx = E.get_sensex_open_0915()
    if idx:
        strike = int(round(idx / 100.0) * 100)
        line(OK, "SENSEX 09:15 open", f"{idx:.2f}  ->  strike {strike}")
    else:
        line(WARN, "09:15 open unavailable",
             "Normal outside market hours / before the open.")
        strike = None

    oc = E.fetch_option_chain(expiry)
    if not oc:
        line(BAD, "Option chain", "unavailable"); return
    line(OK, "Option chain", f"spot {oc['spot_price']:.2f}")
    if strike is None:
        strike = int(round(oc["spot_price"] / 100.0) * 100)

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
    line(OK if len(legs) == 2 else BAD, f"Strike {strike} legs",
         ", ".join(f"{k}={v}" for k, v in legs.items()) or "not listed")
    if len(legs) != 2:
        return

    head("6. Historical data")
    from indicators import aggregate
    from datetime import datetime
    anchor = E.session_anchor_epoch()
    for side, sec in legs.items():
        raw = E.fetch_intraday_1m(sec, E.SENSEX["segment"], "OPTIDX", days=10)
        if not raw:
            line(BAD, f"{side} 1-minute history", "empty response"); continue

        ts = [E._normalize_epoch(c["ts"]) for c in raw]
        first = datetime.fromtimestamp(min(ts), tz=E.IST)
        last = datetime.fromtimestamp(max(ts), tz=E.IST)
        line(OK, f"{side} 1-minute bars", f"{len(raw)} bars, "
             f"{first:%d-%b %H:%M} to {last:%d-%b %H:%M}")

        today = sorted(t for t in ts if t >= anchor)
        if today:
            t0 = datetime.fromtimestamp(today[0], tz=E.IST)
            aligned = t0.strftime("%H:%M") == "09:15"
            line(OK if aligned else WARN, f"{side} first bar today",
                 f"{t0:%H:%M}" + ("" if aligned else "  <- expected 09:15"))
            missing = sum(1 for i in range(1, len(today))
                          if today[i] - today[i - 1] != 60)
            line(OK if missing == 0 else WARN, f"{side} feed gaps today",
                 f"{missing} gap(s)" + ("" if missing == 0 else
                 "  <- why the rollup mode matters; keep it on 'count'"))
        else:
            line(WARN, f"{side} bars today", "none yet")

        anch = lambda t: E.session_anchor_epoch(
            datetime.fromtimestamp(E._normalize_epoch(t), tz=E.IST))
        c = aggregate(raw, 2, "count", anch)
        k = aggregate(raw, 2, "clock", anch)
        prior = len([b for b in c if b["ts"] < anchor])
        resid = (1 - 1 / 14) ** prior if prior else 1.0
        line(OK if resid < 1e-4 else WARN, f"{side} ATR warm-up",
             f"{prior} prior 2-min bars, seed influence {resid:.1e}"
             + ("" if resid < 1e-4 else "  <- too short to match the terminal"))
        line(OK if len(c) == len(k) else WARN, f"{side} rollup modes",
             f"count={len(c)} bars, clock={len(k)} bars"
             + ("  (identical — no gaps)" if len(c) == len(k)
                else "  <- they differ, so the mode changes every value"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", action="store_true",
                    help="also log in and probe the data feed")
    args = ap.parse_args()

    # Load .env before ANY check runs. This was previously done only inside
    # the --api section, so the network check could not see DHAN_FORCE_IPV4 and
    # reported a problem the user had already fixed.
    try:
        from dotenv import load_dotenv
        load_dotenv(str(BASE / ".env"), override=True)
    except Exception:
        pass

    print("=" * 72)
    print("SENSEX VWAP Ladder — doctor")
    print("=" * 72)

    check_python()
    check_deps()
    creds = check_env()
    check_network()

    if args.api:
        if not creds:
            head("4. Dhan login")
            line(BAD, "Skipped", "Fill the three credential keys in .env first.")
        elif _fails:
            head("4. Dhan login")
            line(BAD, "Skipped", "Fix the failures above first.")
        else:
            check_api()
    else:
        print("\n(Run with --api to test the login and data feed too.)")

    print("\n" + "=" * 72)
    if _fails:
        print(f"{len(_fails)} FAILURE(S): " + ", ".join(_fails))
    elif _warns:
        print(f"No failures. {len(_warns)} warning(s): " + ", ".join(_warns))
    else:
        print("All checks passed.")
    print("=" * 72)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
