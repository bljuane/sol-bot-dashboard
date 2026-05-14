"""
One-shot bot setup — register commands, name, description.

Run manually with TG_BOT_TOKEN set:
    TG_BOT_TOKEN=... python notify/setup_bot.py
"""
import os
import sys
import requests

TOKEN = os.environ.get("TG_BOT_TOKEN")
if not TOKEN:
    print("TG_BOT_TOKEN required", file=sys.stderr)
    sys.exit(2)

BASE = f"https://api.telegram.org/bot{TOKEN}"

def call(method, **params):
    r = requests.post(f"{BASE}/{method}", json=params, timeout=30)
    print(f"  {method}: {r.status_code} — {r.json().get('description') or 'ok'}")
    return r.json()

# Set commands so users see a nice menu
COMMANDS = [
    {"command": "start",   "description": "Activate notifications in this chat"},
    {"command": "status",  "description": "(coming soon) Current bot status snapshot"},
    {"command": "balance", "description": "(coming soon) Wallet balance + recent PnL"},
    {"command": "trades",  "description": "(coming soon) Last 10 trades"},
    {"command": "pnl",     "description": "(coming soon) 24h / 7d / all-time PnL"},
    {"command": "help",    "description": "Show bot help + dashboard URL"},
]

print("=== Setting commands ===")
call("setMyCommands", commands=COMMANDS)

print("=== Setting description ===")
call("setMyDescription", description=(
    "SolBotBoy monitors a Solana pump.fun sniper bot wallet — "
    "pushes every trade, watched-wallet launch, and daily PnL summary to this chat. "
    "Dashboard: https://bljuane.github.io/sol-bot-dashboard/"
))

print("=== Setting short description ===")
call("setMyShortDescription", short_description=(
    "Live PnL + trade alerts for a Solana pump.fun sniper bot."
))

print("=== Setting name ===")
call("setMyName", name="SolBotBoy")

print("Done.")
