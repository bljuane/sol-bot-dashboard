"""
Bot log watcher — polls Franklin's log endpoint and pushes important
events to the Telegram channel.

Designed to run on a 2-minute cron. State (last-seen log id + most-recent
heartbeat metrics) lives in `notify/state.json` alongside the other notify
scripts.

Required env vars (same as poll.py):
  TG_BOT_TOKEN
  TG_CHANNEL_ID    (optional — auto-discovered)

Optional:
  BOT_LOG_URL      (default: http://195.201.21.198:3100)
  RPC_URL          (for catch-rate canary; default: publicnode)

What gets pushed:
  - ⚠️ any [sim] would FAIL — bot's pre-flight caught a bug
  - 🔥 [trigger] events that hit a WATCHED creator (real-time visibility on intended buys)
  - 💀 [abandon] when the bot gives up on a position (after a sim/on-chain failure)
  - 📉 catch-rate canary: extract creates-per-min from shred heartbeats,
       compare to pump.fun's actual create rate, alert if <50% catch
  - 🛑 bot lifecycle: startup, [shred] Stream ended, restart loops

What's filtered OUT (too noisy):
  - [shred] heartbeats themselves (we only USE them for catch-rate)
  - [skip-mint] (the bot's correct behavior — skipping non-watched mints)
  - [build] / [jito-http] / [rpc] (mechanical, just precede [OK])
"""
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from poll import Tg, Rpc, discover_chat_id, load_state, save_state, short  # noqa

BOT_LOG_URL = os.environ.get("BOT_LOG_URL", "http://195.201.21.198:3100").rstrip("/")
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MAX_NEW_EVENTS_TO_PUSH = 25   # cap per run; prevent backfill spam

# Heartbeat fields we extract: "msgs=N entries=N txs=N | pumpTxs=N createDisc=N creates=N leads=N | altFallback=N broken=N | slot=N"
HEARTBEAT_RE = re.compile(
    r"\[shred\]\s+msgs=(?P<msgs>\d+)\s+entries=(?P<entries>\d+)\s+txs=(?P<txs>\d+)"
    r"(?:\s*\|\s*pumpTxs=(?P<pumpTxs>\d+))?"
    r"(?:\s+createDisc=(?P<createDisc>\d+))?"
    r"(?:\s+creates=(?P<creates>\d+))?"
    r"(?:\s+leads=(?P<leads>\d+))?"
    r"(?:\s*\|\s*altFallback=(?P<altFallback>\d+))?"
    r"(?:\s+broken=(?P<broken>\d+))?"
    r"(?:\s*\|\s*slot=(?P<slot>\d+))?"
)


# ── Pull logs from the bot endpoint ───────────────────────────────────

def fetch_logs(since_id: int | None = None, limit: int = 500):
    params = {"limit": str(limit), "offset": "0"}
    try:
        r = requests.get(f"{BOT_LOG_URL}/logs", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        entries = data.get("entries", [])
        # Sort by id ascending so we process chronologically
        entries.sort(key=lambda e: e.get("id", 0))
        if since_id is not None:
            entries = [e for e in entries if e.get("id", 0) > since_id]
        return entries
    except Exception as e:
        print(f"[logs-watch] fetch failed: {e}", file=sys.stderr)
        return []


# ── Heartbeat → canary metrics ────────────────────────────────────────

def parse_heartbeat(msg: str) -> dict | None:
    m = HEARTBEAT_RE.search(msg)
    if not m:
        return None
    out = {}
    for k, v in m.groupdict().items():
        out[k] = int(v) if v is not None else None
    return out


def fetch_pumpfun_create_rate(rpc_url: str) -> float | None:
    """Sample pump.fun's recent program activity, return creates-per-minute."""
    import base58
    try:
        rpc = Rpc(rpc_url)
        sigs = rpc.call("getSignaturesForAddress", [PUMP_PROGRAM, {"limit": 500}])
        if not sigs:
            return None
        # Sample the first 50 sigs and count creates
        from base64 import b64decode
        CREATE_DISC = "d6904cec5f8b31b4"
        creates_seen = 0
        oldest_bt = sigs[0].get("blockTime") or 0
        newest_bt = sigs[0].get("blockTime") or 0

        for s in sigs[:80]:
            bt = s.get("blockTime") or 0
            newest_bt = max(newest_bt, bt)
            oldest_bt = min(oldest_bt, bt) if oldest_bt else bt
            if s.get("err"): continue
            tx = rpc.call("getTransaction", [s["signature"], {
                "encoding": "json", "maxSupportedTransactionVersion": 0,
            }])
            if not tx: continue
            msg = tx.get("transaction", {}).get("message", {})
            keys = msg.get("accountKeys", [])
            loaded = tx.get("meta", {}).get("loadedAddresses") or {}
            all_keys = keys + (loaded.get("writable") or []) + (loaded.get("readonly") or [])
            for ix in msg.get("instructions", []):
                pi = ix.get("programIdIndex")
                if pi is None or pi >= len(all_keys): continue
                if all_keys[pi] != PUMP_PROGRAM: continue
                try:
                    data = base58.b58decode(ix.get("data", ""))
                    if len(data) >= 8 and data[:8].hex() == CREATE_DISC:
                        creates_seen += 1
                        break
                except: pass

        span = max(1, newest_bt - oldest_bt)
        rate = creates_seen / (span / 60.0)
        return rate
    except Exception as e:
        print(f"[logs-watch] pump.fun rate fetch failed: {e}", file=sys.stderr)
        return None


# ── Filters: what's pushable ──────────────────────────────────────────

def classify(entry: dict) -> tuple[str, str] | None:
    """Return (category, formatted_message) for events we want to push, or None."""
    msg = entry.get("message", "")
    level = entry.get("level", "log")

    # Pre-flight sim failures — gold for protocol drift diagnosis
    if "[sim]" in msg and "would FAIL" in msg:
        return ("sim-fail", fmt_sim_fail(msg, entry))

    # Watched-creator triggers — real-time intended-buy visibility
    if "[trigger]" in msg:
        return ("trigger", fmt_trigger(msg, entry))

    # Bot gave up on a position
    if "[abandon]" in msg:
        return ("abandon", fmt_abandon(msg, entry))

    # ShredStream disconnects / loop exits
    if "[shred] Stream ended" in msg or "[shred] Stream loop exited" in msg:
        return ("disconnect", fmt_disconnect(msg, entry))

    # Bot startup
    if "[main] Config loaded" in msg or "[shred] First entry received" in msg:
        return ("startup", fmt_startup(msg, entry))

    # Generic warn/error levels we haven't matched yet
    if level in ("warn", "error") and "[shred]" not in msg:
        return ("warning", fmt_generic_warn(msg, entry, level))

    return None


def fmt_sim_fail(msg: str, entry: dict) -> str:
    # Extract the mint, error code, and key log line
    mint_m = re.search(r"\[sim\]\s+(\w+)…", msg)
    err_m  = re.search(r'"InstructionError"\s*:\s*\[\s*\d+\s*,\s*\{\s*"Custom"\s*:\s*(\d+)', msg)
    mint = mint_m.group(1) if mint_m else "?"
    err  = err_m.group(1) if err_m else "?"

    # The line after "logs:" often contains the offending account pubkey
    extra = ""
    logs_m = re.search(r"logs:\s+Program log:\s+([1-9A-HJ-NP-Za-km-z]{32,44})", msg)
    if logs_m:
        extra = f"\n      account flagged: <code>{short(logs_m.group(1))}</code>"

    error_label = lookup_anchor_error(int(err)) if err.isdigit() else err
    return (
        f"⚠️ <b>Pre-flight sim FAILED</b>\n"
        f"mint <code>{short(mint)}</code>\n"
        f"error: <b>Custom: {err}</b> ({error_label}){extra}"
    )


def fmt_trigger(msg: str, entry: dict) -> str:
    # [trigger] MINT… | creator=X… | creatorBuy=N SOL | tier=S (X SOL) | slot=N
    mint_m    = re.search(r"\[trigger\]\s+(\S+)", msg)
    creator_m = re.search(r"creator=(\S+)", msg)
    buy_m     = re.search(r"creatorBuy=([\d.]+)", msg)
    tier_m    = re.search(r"tier=(\S+)\s*\(([\d.]+)", msg)
    slot_m    = re.search(r"slot=(\d+)", msg)

    mint = mint_m.group(1) if mint_m else "?"
    creator = creator_m.group(1).rstrip("…") if creator_m else "?"
    buy = float(buy_m.group(1)) if buy_m else 0
    tier_label = tier_m.group(1) if tier_m else "?"
    tier_size = tier_m.group(2) if tier_m else "?"
    slot = slot_m.group(1) if slot_m else "?"

    return (
        f"🔥 <b>Bot triggered on watched launch</b>\n"
        f"mint <code>{short(mint)}</code>\n"
        f"creator <code>{short(creator)}</code>\n"
        f"creator buy: <code>{buy:.4f} SOL</code>\n"
        f"our entry: <b>{tier_label}</b> ({tier_size} SOL)\n"
        f"slot {slot} · <a href='https://pump.fun/{mint}'>pump.fun</a>"
    )


def fmt_abandon(msg: str, entry: dict) -> str:
    mint_m = re.search(r"\[abandon\]\s+(\w+)…", msg)
    mint = mint_m.group(1) if mint_m else "?"
    reason = ""
    if "tx may have reverted" in msg:
        reason = " (tx reverted on-chain)"
    elif "bundle not included" in msg:
        reason = " (bundle dropped)"
    elif "buy never landed" in msg:
        reason = " (buy never landed)"
    return f"💀 <b>Position abandoned</b>\nmint <code>{short(mint)}</code>{reason}"


def fmt_disconnect(msg: str, entry: dict) -> str:
    return f"🛑 <b>ShredStream disconnected</b>\nbot will reconnect; missing this window of events"


def fmt_startup(msg: str, entry: dict) -> str:
    if "[main] Config loaded" in msg:
        return f"🟢 <b>Bot started</b>\nConfig loaded, beginning ShredStream connect"
    if "[shred] First entry received" in msg:
        slot_m = re.search(r"slot\s+(\d+)", msg)
        slot = slot_m.group(1) if slot_m else "?"
        return f"🟢 <b>Bot online — first shred received</b>\nslot {slot}"
    return f"🟢 <b>Bot lifecycle</b>: {msg[:140]}"


def fmt_generic_warn(msg: str, entry: dict, level: str) -> str:
    icon = "⚠️" if level == "warn" else "🚨"
    return f"{icon} <b>Bot {level}</b>\n<code>{msg[:300]}</code>"


def lookup_anchor_error(code: int) -> str:
    # Common Anchor error codes
    if 6000 <= code < 6100:
        return "pump.fun custom error"
    KNOWN = {
        2000: "AccountDiscriminatorAlreadySet",
        2001: "AccountDiscriminatorNotFound",
        2002: "AccountDiscriminatorMismatch",
        2003: "AccountDidNotDeserialize",
        2004: "AccountDidNotSerialize",
        2005: "AccountNotEnoughKeys",
        2006: "AccountNotMutable",      # ← our V2 writable bug
        2007: "AccountOwnedByWrongProgram",
        2008: "InvalidProgramId",
        2009: "InvalidProgramExecutable",
        2010: "AccountNotSigner",
        2011: "AccountNotSystemOwned",
        2012: "AccountNotInitialized",
        2013: "AccountNotProgramData",
        2014: "AccountNotAssociatedTokenAccount",
        2015: "AccountSysvarMismatch",
        2: "InvalidOwner",              # ATA program
    }
    return KNOWN.get(code, f"Anchor error {code}")


# ── Canary: catch-rate diagnostic ─────────────────────────────────────

def canary_alert_if_drifting(state: dict, latest_hb: dict, tg: Tg, chat_id: str) -> bool:
    """Compare bot's create-detection rate to pump.fun's actual rate.

    Returns True if an alert was sent.
    """
    creates = latest_hb.get("creates")
    if creates is None or creates < 5:
        return False  # not enough sample yet

    # Each heartbeat is a 5K-msgs window; we estimate elapsed time from msg count.
    # But simpler: bot logs creates per heartbeat window, and we know heartbeat
    # was at this slot. Use slot to compute time elapsed since last canary check.

    last_canary = state.setdefault("canary", {})
    last_creates = last_canary.get("last_creates_seen")
    last_ts = last_canary.get("last_ts")

    if last_creates is not None and last_ts is not None:
        delta_creates = max(0, creates - last_creates)
        delta_secs = time.time() - last_ts
        if delta_secs > 60 and delta_creates >= 0:
            bot_rate = delta_creates / (delta_secs / 60)  # creates/min
            pump_rate = fetch_pumpfun_create_rate(os.environ.get("RPC_URL", "https://solana-rpc.publicnode.com"))
            if pump_rate and pump_rate > 1:
                catch_rate = bot_rate / pump_rate
                print(f"[canary] bot={bot_rate:.1f}/min pump={pump_rate:.1f}/min catch={catch_rate*100:.0f}%")
                # Alert if catch < 50%, max once per 30 min
                last_alert = last_canary.get("last_alert", 0)
                if catch_rate < 0.5 and time.time() - last_alert > 1800:
                    if tg.send(chat_id, fmt_canary_alert(bot_rate, pump_rate, catch_rate)):
                        last_canary["last_alert"] = time.time()
                        return True

    last_canary["last_creates_seen"] = creates
    last_canary["last_ts"] = time.time()
    return False


def fmt_canary_alert(bot_rate: float, pump_rate: float, catch_rate: float) -> str:
    return (
        f"📉 <b>Catch-rate canary alert</b>\n\n"
        f"Bot detecting only <b>{bot_rate:.1f} creates/min</b>\n"
        f"Pump.fun actual: <b>{pump_rate:.1f} creates/min</b>\n"
        f"Catch rate: <b>{catch_rate*100:.0f}%</b>\n\n"
        f"This is the pattern that caught the ATL bug (PR #6) and may indicate "
        f"a new pump.fun protocol drift. Inspect the bot logs for parser failures."
    )


# ── Main ──────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        print("[logs-watch] TG_BOT_TOKEN missing", file=sys.stderr)
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
            print("[logs-watch] no chat_id — DM @solbotboybot")
            return 0

    # Pull new log entries since last seen id
    log_state = state.setdefault("logs_watch", {})
    last_id = log_state.get("last_seen_id")

    print(f"[logs-watch] last_seen_id={last_id}")
    new_entries = fetch_logs(since_id=last_id, limit=500)
    print(f"[logs-watch] {len(new_entries)} new log entries")

    if not new_entries:
        # Still update canary on whatever's latest
        save_state(state)
        return 0

    # Classify and push
    pushed = 0
    seen_categories = Counter()
    most_recent_heartbeat = None

    for entry in new_entries[:MAX_NEW_EVENTS_TO_PUSH * 3]:  # process more than we'll push
        msg = entry.get("message", "")
        # Track latest heartbeat for canary
        hb = parse_heartbeat(msg)
        if hb:
            most_recent_heartbeat = hb
            continue

        # Classify
        result = classify(entry)
        if not result: continue
        category, formatted = result

        # Throttle some categories to prevent spam (cap per run)
        cap = {"sim-fail": 5, "abandon": 5, "trigger": 10, "disconnect": 2, "warning": 5, "startup": 2}.get(category, 3)
        if seen_categories[category] >= cap:
            continue
        seen_categories[category] += 1

        if tg.send(chat_id, formatted):
            pushed += 1

        if pushed >= MAX_NEW_EVENTS_TO_PUSH:
            print(f"[logs-watch] capped at {MAX_NEW_EVENTS_TO_PUSH} pushes this run")
            break

    # Update last_seen_id to the maximum id we processed
    max_id = max(e.get("id", 0) for e in new_entries)
    log_state["last_seen_id"] = max_id

    # Canary check on the latest heartbeat
    if most_recent_heartbeat:
        canary_alert_if_drifting(state, most_recent_heartbeat, tg, chat_id)

    save_state(state)
    print(f"[logs-watch] done — pushed {pushed} (seen: {dict(seen_categories)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
