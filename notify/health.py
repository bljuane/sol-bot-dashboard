"""
Bot health monitor — checks for two failure modes:

1. PEAK-HOUR SILENCE
   During XSV's peak hours (02:00-06:00 UTC), the bot should fire at least
   N attempts per hour if it's healthy. If it's silent during peak hours,
   something's wrong (bot crashed, RPC down, parser broken, etc.).

2. WATCHED-WALLET CREATION MISSED
   If a watched wallet created a token in the last hour AND the bot wallet
   has no submission in the [creation_slot, creation_slot + 60] window,
   the bot probably didn't see the creation — alert.

Run on a 15-min cron.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from poll import (  # noqa: E402
    Rpc, Tg, b58decode, discover_chat_id,
    parse_pump_creation, parse_pump_trade,
    DISC_CREATE, PUMP_PROGRAM, TOKEN_2022_PROG,
    short, load_state, save_state,
)

PEAK_HOURS_UTC = (2, 7)  # 02:00 - 06:59 UTC = peak per our research
MIN_PEAK_ATTEMPTS_PER_HOUR = 1

def is_peak_hour():
    h = datetime.now(timezone.utc).hour
    return PEAK_HOURS_UTC[0] <= h < PEAK_HOURS_UTC[1]


def fmt_silence_alert(hours_silent, last_seen):
    last = "never" if not last_seen else datetime.fromtimestamp(last_seen, timezone.utc).strftime("%H:%M UTC")
    return (
        f"⚠️ <b>Bot silent during peak hours</b>\n\n"
        f"Bot wallet has not submitted any tx for <b>{hours_silent:.1f}h</b> during "
        f"peak market activity (02-07 UTC). Last seen: <b>{last}</b>.\n\n"
        f"Possible causes:\n"
        f"• Bot process crashed or hung\n"
        f"• ShredStream connection failed\n"
        f"• RPC issues blocking submissions\n"
        f"• All recent creates filtered out (unlikely during peak)"
    )


def fmt_missed_creation_alert(misses):
    lines = [f"⚠️ <b>Bot may have missed {len(misses)} watched launch(es)</b>", ""]
    for m in misses:
        ts = datetime.fromtimestamp(m['time'], timezone.utc).strftime("%H:%M:%S UTC")
        lines.append(f"  • <code>{short(m['mint'])}</code> · creator <code>{short(m['creator'])}</code> at {ts}")
    lines.append("")
    lines.append("No bot tx detected in slot+60 window after these creations.")
    lines.append("Verify bot is running and that V2 detection is working.")
    return "\n".join(lines)


def main():
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        print("[health] TG_BOT_TOKEN missing", file=sys.stderr)
        return 2

    tg = Tg(token)
    state = load_state()
    chat_id = os.environ.get("TG_CHANNEL_ID") or state.get("discovered_chat_id")
    if not chat_id:
        chat_id = discover_chat_id(tg)
        if chat_id:
            state["discovered_chat_id"] = chat_id
            save_state(state)
        else:
            print("[health] no chat_id available — skipping")
            return 0

    bot_wallet = os.environ.get("BOT_WALLET", "5YDHipThEddE5jr7AcRkcdkUofeiqWBukSwLgZcmFjSP")
    rpc_url = os.environ.get("RPC_URL", "https://solana-rpc.publicnode.com")
    watched_raw = os.environ.get("WATCHED_WALLETS", "")
    watched = {w.strip() for w in watched_raw.split(",") if w.strip()}

    rpc = Rpc(rpc_url)

    state.setdefault("health", {})

    # ── 1. Peak-hour silence check ────────────────────────────────────
    if is_peak_hour():
        try:
            sigs = rpc.call("getSignaturesForAddress", [bot_wallet, {"limit": 20}])
            now = time.time()
            most_recent = sigs[0]["blockTime"] if sigs else None
            hours_since = (now - most_recent) / 3600 if most_recent else 999
            if hours_since > 1.0:
                # Silent for >1h during peak. Don't spam — once per silence period.
                last_alert = state["health"].get("last_silence_alert") or 0
                if now - last_alert > 3600:  # max one alert per hour
                    if tg.send(chat_id, fmt_silence_alert(hours_since, most_recent)):
                        state["health"]["last_silence_alert"] = now
        except Exception as e:
            print(f"[health] silence check failed: {e}", file=sys.stderr)

    # ── 2. Missed-creation check ──────────────────────────────────────
    # Look at watched wallets' creations in last 30 min;
    # for each, check if bot wallet has any tx in slot+60 window.
    misses = []
    now = time.time()
    cutoff_ts = now - 1800  # 30 min
    state["health"].setdefault("alerted_misses", [])

    for w in watched:
        try:
            sigs = rpc.call("getSignaturesForAddress", [w, {"limit": 30}])
        except Exception:
            continue
        for s in sigs:
            bt = s.get("blockTime") or 0
            if bt < cutoff_ts:
                continue
            if s.get("err"):
                continue
            if s["signature"] in state["health"]["alerted_misses"]:
                continue
            # Confirm this is a pump.fun create
            try:
                tx = rpc.call("getTransaction", [s["signature"], {
                    "encoding": "json", "maxSupportedTransactionVersion": 0,
                }])
            except Exception:
                continue
            c = parse_pump_creation(tx, watched)
            if not c:
                continue
            # Did bot wallet submit anything in slot+60?
            try:
                bot_sigs = rpc.call("getSignaturesForAddress", [bot_wallet, {"limit": 100}])
                slot_low = s["slot"]
                slot_high = s["slot"] + 60
                bot_in_window = any(slot_low <= bs.get("slot", 0) <= slot_high for bs in bot_sigs)
            except Exception:
                bot_in_window = True  # be conservative
            if not bot_in_window:
                misses.append({
                    "mint": c["mint"], "creator": c["creator"],
                    "time": bt, "sig": s["signature"],
                })

    # Cap miss alerts to avoid spam
    if misses:
        # Limit to 5 per alert
        misses = misses[:5]
        if tg.send(chat_id, fmt_missed_creation_alert(misses)):
            for m in misses:
                state["health"]["alerted_misses"].append(m.get("sig", ""))
            # Trim memory
            state["health"]["alerted_misses"] = state["health"]["alerted_misses"][-200:]

    save_state(state)
    print(f"[health] done — {len(misses)} miss alerts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
