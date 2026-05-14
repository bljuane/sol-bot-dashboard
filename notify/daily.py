"""
Daily PnL summary — runs once per day at 00:05 UTC via GitHub Actions.

Pulls the bot wallet's last ~24h of pump.fun activity, computes realized
PnL using the same on-chain-balance-first logic as the dashboard, and
posts a structured summary to the Telegram channel.

Same env vars as poll.py.
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Pull shared helpers from poll.py
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from poll import (  # noqa: E402
    Rpc, Tg, b58decode, discover_chat_id,
    parse_pump_trade,
    DISC_BUY_V1, DISC_BUY_V1B, DISC_BUY_V2A, DISC_BUY_V2B, DISC_SELL,
    TOKEN_2022_PROG, PUMP_PROGRAM,
    BUY_DISCS, SELL_DISCS,
    short, load_state, save_state, STATE_PATH,
)

LAMPORTS_PER_SOL = 1_000_000_000


def fetch_window_trades(rpc, wallet, since_ts):
    """Pull pump.fun trades that happened after `since_ts` (unix seconds).

    Returns a list of dicts: {sig, slot, time, type, mint, sol_delta, is_v2, failed}
    """
    trades = []
    before = None
    while True:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        sigs = rpc.call("getSignaturesForAddress", [wallet, opts])
        if not sigs:
            break
        oldest_ts = sigs[-1].get("blockTime") or 0
        for s in sigs:
            bt = s.get("blockTime") or 0
            if bt < since_ts:
                continue
            try:
                tx = rpc.call("getTransaction", [s["signature"], {
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                }])
            except Exception as e:
                print(f"[daily] tx fetch failed for {s['signature'][:14]}: {e}", file=sys.stderr)
                continue
            t = parse_pump_trade(tx, wallet)
            if not t:
                continue
            trades.append({
                "sig": s["signature"], "slot": s["slot"], "time": bt,
                **t,
            })
        before = sigs[-1]["signature"]
        if oldest_ts < since_ts or len(sigs) < 1000:
            break
    return trades


def get_token_balances(rpc, wallet):
    """Return {mint -> uiAmount} for non-zero balances across V1 and V2 programs."""
    balances = {}
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", TOKEN_2022_PROG):
        try:
            res = rpc.call("getTokenAccountsByOwner", [
                wallet, {"programId": prog}, {"encoding": "jsonParsed"},
            ])
            for a in res.get("value", []):
                info = a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                mint = info.get("mint")
                amt = info.get("tokenAmount", {}).get("uiAmount") or 0
                if mint and amt > 0:
                    balances[mint] = amt
        except Exception as e:
            print(f"[daily] token balance fetch failed ({prog[:12]}): {e}", file=sys.stderr)
    return balances


def compute_summary(trades, token_balances):
    """Aggregate trades into per-mint and total stats."""
    by_mint = defaultdict(list)
    for t in trades:
        if t.get("failed"):
            continue
        by_mint[t["mint"]].append(t)

    closed = []
    open_positions = []
    for mint, mints_trades in by_mint.items():
        spent = sum(max(0, -x["sol_delta"]) for x in mints_trades if x["type"] == "buy")
        received = sum(max(0, x["sol_delta"]) for x in mints_trades if x["type"] == "sell")
        buys = sum(1 for x in mints_trades if x["type"] == "buy")
        sells = sum(1 for x in mints_trades if x["type"] == "sell")
        on_chain = token_balances.get(mint, 0)
        if on_chain > 0:
            open_positions.append({
                "mint": mint, "spent": spent, "received": received,
                "buys": buys, "sells": sells, "balance": on_chain,
            })
        elif buys > 0 or sells > 0:
            closed.append({
                "mint": mint, "spent": spent, "received": received,
                "buys": buys, "sells": sells, "pnl": received - spent,
                "last_ts": max(x["time"] for x in mints_trades),
            })

    fails = sum(1 for t in trades if t.get("failed"))
    total_buys  = sum(1 for t in trades if t["type"] == "buy"  and not t.get("failed"))
    total_sells = sum(1 for t in trades if t["type"] == "sell" and not t.get("failed"))

    closed_pnl = sum(c["pnl"] for c in closed)
    winners = sum(1 for c in closed if c["pnl"] > 0)
    losers  = sum(1 for c in closed if c["pnl"] < 0)
    avg = (closed_pnl / len(closed)) if closed else 0
    wr = (winners / len(closed)) if closed else 0

    # Sort for top winners / losers
    closed_sorted = sorted(closed, key=lambda c: -c["pnl"])
    top_winners = closed_sorted[:3]
    top_losers  = sorted(closed_sorted, key=lambda c: c["pnl"])[:3]

    return {
        "trades": len(trades),
        "buys": total_buys,
        "sells": total_sells,
        "fails": fails,
        "closed": closed,
        "open": open_positions,
        "closed_pnl": closed_pnl,
        "winners": winners,
        "losers": losers,
        "wr": wr,
        "avg": avg,
        "top_winners": top_winners,
        "top_losers": top_losers,
    }


def fmt_summary(s, cur_balance, window_label):
    pnl_str = f"{'+' if s['closed_pnl'] > 0 else ''}{s['closed_pnl']:.4f}"
    pnl_icon = "🟢" if s["closed_pnl"] > 0 else ("🔴" if s["closed_pnl"] < 0 else "⚪")
    wr_pct = (s["wr"] * 100) if s["closed"] else 0

    lines = [
        f"📊 <b>Daily summary</b> ({window_label})",
        "",
        f"<b>Wallet balance:</b> <code>{cur_balance:.4f} SOL</code>",
        f"<b>Realized PnL:</b> {pnl_icon} <code>{pnl_str} SOL</code>",
        f"<b>Closed trades:</b> {len(s['closed'])} ({s['winners']}W / {s['losers']}L · {wr_pct:.0f}% win rate)",
        f"<b>Avg PnL/trade:</b> <code>{s['avg']:+.4f} SOL</code>",
        f"<b>Open positions:</b> {len(s['open'])}",
        f"<b>Total attempts:</b> {s['trades']} ({s['buys']} buys, {s['sells']} sells, {s['fails']} failed)",
    ]

    if s["top_winners"] and s["top_winners"][0]["pnl"] > 0:
        lines.append("")
        lines.append("<b>Top winners</b>")
        for t in s["top_winners"]:
            if t["pnl"] <= 0:
                break
            lines.append(f"  • <code>{short(t['mint'])}</code> {t['pnl']:+.4f} SOL")

    if s["top_losers"] and s["top_losers"][0]["pnl"] < 0:
        lines.append("")
        lines.append("<b>Top losers</b>")
        for t in s["top_losers"]:
            if t["pnl"] >= 0:
                break
            lines.append(f"  • <code>{short(t['mint'])}</code> {t['pnl']:+.4f} SOL")

    if s["open"]:
        lines.append("")
        lines.append("<b>Open positions</b>")
        for p in s["open"][:5]:
            lines.append(f"  • <code>{short(p['mint'])}</code> {p['spent']:.4f} SOL deployed · {p['balance']:,.0f} tokens")
        if len(s["open"]) > 5:
            lines.append(f"  … and {len(s['open']) - 5} more")

    lines.append("")
    lines.append("Dashboard: https://bljuane.github.io/sol-bot-dashboard/")
    return "\n".join(lines)


def main():
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        print("[daily] TG_BOT_TOKEN missing", file=sys.stderr)
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
            print("[daily] no chat_id available — skipping")
            return 0

    bot_wallet = os.environ.get("BOT_WALLET", "5YDHipThEddE5jr7AcRkcdkUofeiqWBukSwLgZcmFjSP")
    rpc_url = os.environ.get("RPC_URL", "https://solana-rpc.publicnode.com")
    window_hours = int(os.environ.get("DAILY_WINDOW_HOURS", "24"))

    rpc = Rpc(rpc_url)
    since_ts = int(time.time()) - window_hours * 3600

    print(f"[daily] window {window_hours}h, since_ts={since_ts}")

    try:
        bal_result = rpc.call("getBalance", [bot_wallet])
        cur_balance = bal_result.get("value", 0) / LAMPORTS_PER_SOL
    except Exception as e:
        print(f"[daily] balance fetch failed: {e}", file=sys.stderr)
        cur_balance = 0

    trades = fetch_window_trades(rpc, bot_wallet, since_ts)
    print(f"[daily] {len(trades)} pump.fun trades in window")

    token_balances = get_token_balances(rpc, bot_wallet)
    print(f"[daily] {len(token_balances)} non-zero token balances")

    summary = compute_summary(trades, token_balances)

    label = f"last {window_hours}h"
    msg = fmt_summary(summary, cur_balance, label)

    ok = tg.send(chat_id, msg)
    print(f"[daily] sent: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
