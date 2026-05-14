"""
Poll the bot wallet + watched creators and push notifications to a Telegram channel.

Designed to be run on a 5-minute GitHub Actions cron. State (last seen sigs +
discovered chat id) is committed back to the repo as `notify/state.json` so we
don't re-notify across runs.

Required environment variables:
  TG_BOT_TOKEN     - bot token from @BotFather (NEVER hardcode, NEVER log)

Optional environment variables:
  TG_CHANNEL_ID    - explicit chat id ("@channel" or "-1001234"). If unset, we
                     auto-discover the most recently-active chat via getUpdates.
  BOT_WALLET       - main wallet to monitor (default: the test bot wallet)
  RPC_URL          - Solana RPC endpoint (default: solana-rpc.publicnode.com)
  WATCHED_WALLETS  - comma-separated list of creator wallets to alert on
  XSV_WALLET       - XSV V4 wallet for bot-vs-XSV comparison
                     (default: EgQX9R3Qph1dPHE1Ysou1auSYqRGomCNmLDC28Yg77aq)

Notifications sent:
  - new pump.fun trade landed on the bot wallet (buy/sell/fail)
  - new pump.fun token created by a watched wallet
       + comparison: did XSV catch it? at what slot delta?
  - bot wallet balance change >= configurable threshold
  - low-balance warning when balance drops below threshold
"""
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

# ── Config ─────────────────────────────────────────────────────────────

STATE_PATH = Path(__file__).parent / "state.json"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

DISC_BUY_V1   = bytes.fromhex("66063d1201daebea")
DISC_BUY_V1B  = bytes.fromhex("38fc74089edfcd5f")
DISC_BUY_V2A  = bytes.fromhex("c2ab1c46684d5b2f")
DISC_BUY_V2B  = bytes.fromhex("b817ee6167c5d33d")
DISC_SELL     = bytes.fromhex("33e685a4017f83ad")
DISC_CREATE   = bytes.fromhex("d6904cec5f8b31b4")

BUY_DISCS  = {DISC_BUY_V1, DISC_BUY_V1B, DISC_BUY_V2A, DISC_BUY_V2B}
SELL_DISCS = {DISC_SELL}

TOKEN_2022_PROG = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

LOW_BALANCE_THRESHOLD = float(os.environ.get("LOW_BALANCE_THRESHOLD_SOL", "0.5"))
BALANCE_CHANGE_NOTIFY = float(os.environ.get("BALANCE_CHANGE_NOTIFY_PCT", "5.0")) / 100
MAX_NEW_TRADES_TO_NOTIFY = 20
COMPARE_VS_XSV = os.environ.get("COMPARE_VS_XSV", "true").lower() == "true"

# ── Base58 (no extra deps) ────────────────────────────────────────────

BS58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + BS58_ALPHABET.index(c)
    h = hex(n)[2:]
    if len(h) % 2:
        h = "0" + h
    out = bytes.fromhex(h) if h else b""
    leading = 0
    for c in s:
        if c == "1":
            leading += 1
        else:
            break
    return b"\x00" * leading + out


# ── Telegram ───────────────────────────────────────────────────────────

class Tg:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **params):
        url = f"{self.base}/{method}"
        r = self.session.post(url, json=params, timeout=30)
        if not r.ok:
            print(f"[tg] {method} failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
            return None
        j = r.json()
        if not j.get("ok"):
            print(f"[tg] {method} not ok: {j.get('description')}", file=sys.stderr)
            return None
        return j["result"]

    def send(self, chat_id, text: str, parse_mode="HTML"):
        return self.call("sendMessage",
            chat_id=chat_id, text=text, parse_mode=parse_mode,
            disable_web_page_preview=True, disable_notification=False,
        )

    def get_updates(self, offset=None):
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params) or []

    def set_commands(self, commands):
        return self.call("setMyCommands", commands=commands)


def discover_chat_id(tg: Tg) -> str | None:
    """Auto-discover the most recently active chat_id from getUpdates.

    Picks up:
      - DMs sent to the bot
      - Group messages where the bot is a member
      - Channel posts where the bot is admin
    """
    updates = tg.get_updates()
    candidates = []
    for u in updates:
        for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
            m = u.get(key)
            if m and m.get("chat", {}).get("id"):
                candidates.append((u.get("update_id", 0), m["chat"]["id"], m["chat"].get("type"), m["chat"].get("title") or m["chat"].get("username") or ""))
    if not candidates:
        return None
    # Pick the highest update_id (most recent)
    candidates.sort(reverse=True)
    chat_id, ctype, label = candidates[0][1], candidates[0][2], candidates[0][3]
    print(f"[tg] auto-discovered chat: id={chat_id} type={ctype} label={label!r}")
    return str(chat_id)


# ── Solana RPC ─────────────────────────────────────────────────────────

class Rpc:
    def __init__(self, url):
        self.url = url
        self.id = 0
        self.s = requests.Session()

    def call(self, method, params):
        self.id += 1
        r = self.s.post(self.url, json={
            "jsonrpc": "2.0", "id": self.id, "method": method, "params": params,
        }, timeout=30)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"RPC {method} error: {j['error']}")
        return j["result"]


# ── Trade parsing ──────────────────────────────────────────────────────

def parse_pump_trade(tx, wallet):
    if not tx or not tx.get("meta"):
        return None
    meta = tx["meta"]
    failed = meta.get("err") is not None
    msg = tx.get("transaction", {}).get("message", {})
    static_keys = msg.get("accountKeys", [])
    loaded = meta.get("loadedAddresses") or {}
    all_keys = static_keys + (loaded.get("writable") or []) + (loaded.get("readonly") or [])
    if wallet not in all_keys:
        return None
    if wallet in static_keys:
        idx = static_keys.index(wallet)
        pre = (meta.get("preBalances") or [0])[idx]
        post = (meta.get("postBalances") or [0])[idx]
        sol_delta = (post - pre) / 1e9
    else:
        sol_delta = 0
    for ix in msg.get("instructions", []):
        prog_idx = ix.get("programIdIndex")
        if prog_idx is None or prog_idx >= len(all_keys):
            continue
        if all_keys[prog_idx] != PUMP_PROGRAM:
            continue
        try:
            data = b58decode(ix.get("data", ""))
        except Exception:
            continue
        if len(data) < 8:
            continue
        disc = data[:8]
        kind, is_v2 = None, False
        if disc in BUY_DISCS:
            kind = "buy"
            is_v2 = disc in (DISC_BUY_V2A, DISC_BUY_V2B)
        elif disc in SELL_DISCS:
            kind = "sell"
        else:
            continue
        mint = None
        for ai in ix.get("accounts", []):
            if ai < len(all_keys) and all_keys[ai].endswith("pump"):
                mint = all_keys[ai]
                break
        return {
            "type": kind, "mint": mint or "?",
            "sol_delta": sol_delta, "is_v2": is_v2, "failed": failed,
        }
    return None


def parse_pump_creation(tx, watched_set):
    if not tx or not tx.get("meta"):
        return None
    if tx["meta"].get("err"):
        return None
    msg = tx.get("transaction", {}).get("message", {})
    static_keys = msg.get("accountKeys", [])
    loaded = tx["meta"].get("loadedAddresses") or {}
    all_keys = static_keys + (loaded.get("writable") or []) + (loaded.get("readonly") or [])
    if not static_keys:
        return None
    signer = static_keys[0]
    if signer not in watched_set:
        return None
    for ix in msg.get("instructions", []):
        prog_idx = ix.get("programIdIndex")
        if prog_idx is None or prog_idx >= len(all_keys):
            continue
        if all_keys[prog_idx] != PUMP_PROGRAM:
            continue
        try:
            data = b58decode(ix.get("data", ""))
        except Exception:
            continue
        if len(data) < 8:
            continue
        if data[:8] != DISC_CREATE:
            continue
        is_v2 = any(
            (ai < len(all_keys) and all_keys[ai] == TOKEN_2022_PROG)
            for ai in ix.get("accounts", [])
        )
        mint = None
        for ai in ix.get("accounts", [])[:6]:
            if ai < len(all_keys) and all_keys[ai].endswith("pump"):
                mint = all_keys[ai]
                break
        return {"creator": signer, "mint": mint or "?", "is_v2": is_v2}
    return None


# ── XSV comparison ────────────────────────────────────────────────────

def check_xsv_caught(rpc, mint, creation_slot, xsv_wallet, window_slots=200):
    """Did XSV buy this mint within `window_slots` after creation?

    Returns:
      None  — XSV did NOT buy this mint (we win)
      dict  — { slot, slot_delta, sig, success }
    """
    if not xsv_wallet:
        return None
    # Pull recent sigs for XSV constrained to the slot window
    try:
        # Helius/publicnode don't support slot-window filters cleanly; we just pull recent
        sigs = rpc.call("getSignaturesForAddress", [xsv_wallet, {"limit": 100}])
    except Exception as e:
        print(f"[xsv-check] sig pull failed: {e}", file=sys.stderr)
        return None
    for s in sigs:
        slot = s.get("slot", 0)
        if slot < creation_slot or slot > creation_slot + window_slots:
            continue
        if s.get("err"):
            continue
        # Fetch tx and check if it touched our mint
        try:
            tx = rpc.call("getTransaction", [s["signature"], {
                "encoding": "json", "maxSupportedTransactionVersion": 0,
            }])
        except Exception:
            continue
        if not tx:
            continue
        keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        loaded = tx.get("meta", {}).get("loadedAddresses") or {}
        all_keys = keys + (loaded.get("writable") or []) + (loaded.get("readonly") or [])
        if mint in all_keys:
            return {
                "slot": slot, "slot_delta": slot - creation_slot,
                "sig": s["signature"], "success": not s.get("err"),
            }
    return None


# ── State ──────────────────────────────────────────────────────────────

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {
        "last_bot_sig": None,
        "last_watched_sigs": {},
        "last_balance_sol": None,
        "last_low_balance_alert": None,
        "discovered_chat_id": None,
        "last_update_id": 0,
        "first_run_done": False,
    }


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# ── Format messages ────────────────────────────────────────────────────

def short(s, head=8, tail=4):
    return f"{s[:head]}…{s[-tail:]}" if s and len(s) > head + tail else (s or "?")

def fmt_trade(t, sig, slot, block_time):
    v_badge = "V2" if t["is_v2"] else "V1"
    sol_str = f"{'+' if t['sol_delta'] > 0 else ''}{t['sol_delta']:.4f}"
    icon = "🟢" if t["sol_delta"] > 0 else ("🔴" if t["sol_delta"] < 0 else "⚪")
    ts = datetime.fromtimestamp(block_time, timezone.utc).strftime("%H:%M:%S UTC") if block_time else ""

    if t["failed"]:
        return (
            f"⚠️ <b>Trade FAILED</b> · {v_badge} {t['type'].upper()}\n"
            f"mint <code>{short(t['mint'])}</code>\n"
            f"slot {slot:,} · {ts}\n"
            f"<a href='https://solscan.io/tx/{sig}'>tx</a>"
        )
    return (
        f"{icon} <b>{t['type'].upper()}</b> · {v_badge} · <code>{sol_str} SOL</code>\n"
        f"mint <code>{short(t['mint'])}</code>\n"
        f"slot {slot:,} · {ts}\n"
        f"<a href='https://pump.fun/{t['mint']}'>pump.fun</a> · "
        f"<a href='https://solscan.io/tx/{sig}'>tx</a>"
    )

def fmt_creation(c, sig, slot, block_time, xsv_result=None):
    v_badge = "V2" if c["is_v2"] else "V1"
    ts = datetime.fromtimestamp(block_time, timezone.utc).strftime("%H:%M:%S UTC") if block_time else ""
    body = (
        f"🆕 <b>Watched creator launched</b> · {v_badge}\n"
        f"creator <code>{short(c['creator'])}</code>\n"
        f"mint <code>{short(c['mint'])}</code>\n"
        f"slot {slot:,} · {ts}\n"
    )
    if xsv_result is not None:
        if xsv_result is False:
            body += "⚔️ XSV did NOT catch this one (yet)\n"
        else:
            sd = xsv_result["slot_delta"]
            body += f"⚔️ XSV caught at slot +{sd} · <a href='https://solscan.io/tx/{xsv_result['sig']}'>their tx</a>\n"
    body += f"<a href='https://pump.fun/{c['mint']}'>pump.fun</a> · <a href='https://solscan.io/tx/{sig}'>tx</a>"
    return body

def fmt_balance(prev, cur, pct):
    arrow = "↗️" if cur > prev else "↘️"
    return (
        f"💰 <b>Balance change</b> {arrow}\n"
        f"<code>{prev:.4f} → {cur:.4f} SOL</code> ({pct:+.1f}%)"
    )

def fmt_low_balance(bal):
    return (
        f"🚨 <b>Low balance alert</b>\n"
        f"Bot wallet has only <code>{bal:.4f} SOL</code>.\n"
        f"Refill needed for V2 entries (min ~1.21 SOL per trade)."
    )

def fmt_first_run(bot_wallet, watched_count):
    return (
        f"👋 <b>SolBotBoy is online</b>\n\n"
        f"Monitoring wallet <code>{bot_wallet[:8]}…{bot_wallet[-4:]}</code>\n"
        f"Tracking <b>{watched_count}</b> creator wallets\n"
        f"Polling every 5 minutes via GitHub Actions\n\n"
        f"Notifications you'll receive:\n"
        f"• 🟢/🔴 every bot trade (buy/sell/fail)\n"
        f"• 🆕 every watched-wallet launch + XSV comparison\n"
        f"• 💰 balance changes ≥ 5%\n"
        f"• 🚨 low balance alerts\n"
        f"• 📊 daily PnL summary at 00:05 UTC\n\n"
        f"Dashboard: https://bljuane.github.io/sol-bot-dashboard/"
    )


# ── Main ───────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        print("[poll] TG_BOT_TOKEN missing — exiting", file=sys.stderr)
        return 2

    tg = Tg(token)

    bot_wallet = os.environ.get("BOT_WALLET", "5YDHipThEddE5jr7AcRkcdkUofeiqWBukSwLgZcmFjSP")
    xsv_wallet = os.environ.get("XSV_WALLET", "EgQX9R3Qph1dPHE1Ysou1auSYqRGomCNmLDC28Yg77aq")
    rpc_url = os.environ.get("RPC_URL", "https://solana-rpc.publicnode.com")
    watched_raw = os.environ.get("WATCHED_WALLETS", "")
    watched = {w.strip() for w in watched_raw.split(",") if w.strip()}

    print(f"[poll] bot_wallet={bot_wallet}")
    print(f"[poll] watched_count={len(watched)}")
    print(f"[poll] rpc_url={rpc_url}")

    state = load_state()

    # ── Resolve chat_id (env > state > auto-discover via getUpdates) ──
    chat_id = os.environ.get("TG_CHANNEL_ID") or state.get("discovered_chat_id")
    if not chat_id:
        chat_id = discover_chat_id(tg)
        if chat_id:
            state["discovered_chat_id"] = chat_id
            save_state(state)
        else:
            print("[poll] no TG_CHANNEL_ID and no discoverable chat — "
                  "DM @solbotboybot any message to enable notifications. exiting.")
            return 0

    # ── First-run welcome ─────────────────────────────────────────────
    if not state.get("first_run_done"):
        if tg.send(chat_id, fmt_first_run(bot_wallet, len(watched))):
            state["first_run_done"] = True
            save_state(state)

    rpc = Rpc(rpc_url)
    notifications = 0

    # ── 1. Balance ─────────────────────────────────────────────────────
    try:
        bal_result = rpc.call("getBalance", [bot_wallet])
        cur_bal = bal_result.get("value", 0) / 1e9
        prev = state.get("last_balance_sol")
        if prev is not None and prev > 0:
            pct = (cur_bal - prev) / prev
            if abs(pct) >= BALANCE_CHANGE_NOTIFY:
                if tg.send(chat_id, fmt_balance(prev, cur_bal, pct * 100)):
                    notifications += 1
        if cur_bal < LOW_BALANCE_THRESHOLD:
            last_alert = state.get("last_low_balance_alert") or 0
            if time.time() - last_alert > 86400:
                if tg.send(chat_id, fmt_low_balance(cur_bal)):
                    state["last_low_balance_alert"] = time.time()
                    notifications += 1
        state["last_balance_sol"] = cur_bal
        print(f"[poll] bot balance: {cur_bal:.4f} SOL")
    except Exception as e:
        print(f"[poll] balance failed: {e}", file=sys.stderr)

    # ── 2. Bot trades ──────────────────────────────────────────────────
    try:
        opts = {"limit": 100}
        if state.get("last_bot_sig"):
            opts["until"] = state["last_bot_sig"]
        sigs = rpc.call("getSignaturesForAddress", [bot_wallet, opts])
        print(f"[poll] {len(sigs)} new bot sigs")
        new_msgs = []
        for s in sigs[:MAX_NEW_TRADES_TO_NOTIFY]:
            try:
                tx = rpc.call("getTransaction", [s["signature"], {
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                }])
            except Exception:
                continue
            t = parse_pump_trade(tx, bot_wallet)
            if not t:
                continue
            new_msgs.append(fmt_trade(t, s["signature"], s["slot"], s.get("blockTime")))
        for msg in reversed(new_msgs):
            if tg.send(chat_id, msg):
                notifications += 1
        if sigs:
            state["last_bot_sig"] = sigs[0]["signature"]
    except Exception as e:
        print(f"[poll] bot trades failed: {e}", file=sys.stderr)

    # ── 3. Watched-wallet creations ────────────────────────────────────
    state.setdefault("last_watched_sigs", {})
    for w in watched:
        try:
            opts = {"limit": 30}
            last = state["last_watched_sigs"].get(w)
            if last:
                opts["until"] = last
            sigs = rpc.call("getSignaturesForAddress", [w, opts])
            new_msgs = []
            for s in sigs[:10]:
                try:
                    tx = rpc.call("getTransaction", [s["signature"], {
                        "encoding": "json",
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "confirmed",
                    }])
                except Exception:
                    continue
                c = parse_pump_creation(tx, watched)
                if not c:
                    continue
                xsv_result = None
                if COMPARE_VS_XSV:
                    res = check_xsv_caught(rpc, c["mint"], s["slot"], xsv_wallet)
                    xsv_result = res if res else False
                new_msgs.append(fmt_creation(c, s["signature"], s["slot"], s.get("blockTime"), xsv_result))
            for msg in reversed(new_msgs):
                if tg.send(chat_id, msg):
                    notifications += 1
            if sigs:
                state["last_watched_sigs"][w] = sigs[0]["signature"]
        except Exception as e:
            print(f"[poll] watched {w[:12]}… failed: {e}", file=sys.stderr)

    save_state(state)
    print(f"[poll] done — sent {notifications} notifications, chat_id={chat_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
