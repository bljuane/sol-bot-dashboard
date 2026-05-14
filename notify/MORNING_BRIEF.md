# Morning Brief — overnight build summary

**Author:** assistant
**Window:** ~2026-05-14 16:25 → 17:00 UTC (Ben asleep)

---

## ⚠️ Top of the brief — bot appears to have stopped

The bot wallet (`5YDHipTh…`) has had **zero on-chain activity for 3+ hours**.

```
Last bot tx:                  13:48:37 UTC  (slot 419,698,875, FAIL — V2 InvalidSeeds)
Last [shred] heartbeat shown: ~15:14 UTC    (slot 419,714,458)
                              ──────── 3+ hour gap ────────
Now:                          ~17:00 UTC
```

**Watched-wallet activity DURING that gap (these should have triggered fires):**

| Time UTC | Creator | Mint | Bot fired? |
|---|---|---|---|
| 16:18:19 | `8YcbyX92…` | `AkSJiwtBLEzycvzUWWVLC27zpBH7Eawq6eB1f7h1pump` | ❌ no |
| 16:51:41 | `8YcbyX92…` | `2d91xCLsASrr77nPPpqPPfwu2aojEfMtUTZD3Rjrpump` | ❌ no |

The bot is either:

1. **Not running** — process crashed after the last heartbeat
2. **Running but ShredStream stalled** — gRPC connection died and reconnect didn't recover
3. **Running but newer V2 detection paths broke** — Franklin's `cb3df0f "wip higher sdk"` may have regressed something

**Action when you wake up:** check the bot process. If it's still running, look at logs around 15:14 UTC for errors / crash trace. If it's not, restart and verify the very next watched-wallet launch triggers `[trigger]`.

---

## What got built tonight

Everything in one repo: `github.com/bljuane/sol-bot-dashboard` (the dashboard repo).

### 1. Dashboard upgrade — mark-to-market on open positions

Pushed `?v=5`. Open positions now show:
- **Mark** — current sell-out value in SOL (computed from bonding curve state)
- **Unrealized** — PnL vs entry, color-coded green/red, with %
- **Tokens held** — sub-text under the mark column

How it works: on each refresh, for each open mint, the dashboard reads the bonding curve account directly (PDA captured from the buy tx itself, no client-side derivation needed) and applies the pump.fun sell-curve formula:

```
mtm_sol = virtual_sol * tokens_held / (virtual_token + tokens_held)
```

Graduated tokens show "—" (curve closed). Cached 60s to limit RPC load.

Right now the bot has zero open positions so the column is empty.

### 2. Telegram bot + GitHub Actions notifications

**Bot:** [@solbotboybot](https://t.me/solbotboybot) (renamed to "SolBotBoy", commands + description set)

Three workflows on cron:

| Workflow | Cadence | What it does |
|---|---|---|
| `notify.yml` | every 5 min | New trade alerts, watched-launch alerts (with XSV-caught-it comparison), balance changes, low-balance |
| `daily.yml`  | 00:05 UTC | 24h summary: PnL, win rate, top winners/losers, open positions |
| `health.yml` | every 15 min | Silent-during-peak alerts, missed-watched-creation alerts |

All three are wired up with `TG_BOT_TOKEN` (GH Secret) and repo Variables (`BOT_WALLET`, `XSV_WALLET`, `WATCHED_WALLETS`, `RPC_URL`).

**To activate:** DM `@solbotboybot` any single message ("hi", "/start", whatever). Within 5 minutes the next cron run auto-discovers your chat_id via `getUpdates`, sends a "👋 SolBotBoy is online" welcome, and starts streaming notifications.

You can also add the bot to a group/channel (must be admin in channels with "post messages" permission) — same auto-discovery works.

To force a specific target instead of auto-discovery, set GH Secret `TG_CHANNEL_ID`.

### 3. Watched-wallet audit

Ran an outcome / cadence audit on all 12 watched wallets. Full data saved to `notify/watched_audit.json`. Headline:

| Wallet | Role | Creates in last 50 sigs | Current cadence | Last create UTC |
|---|---|---:|---|---|
| `8YcbyX92…` | active | 15 | 0.7/hr over 20h | **16:51 today** ← just now |
| `D9gQ6Rh…` | active | 11 | 1.3/hr over 8.6h | 14:49 |
| `HiSo5kyk…` | active | 12 | 6.5/hr over 1.9h | 11:51 |
| `7hbtZ1M9…` | Luis dev | 8 | 0.4/hr over 22h | 11:40 |
| `FM1YCKED…` | creator | 5 | 7.2/hr over 0.7h | 06:02 |
| `5htGpHK2…` | creator | 3 | 3.1/hr over 1h | 09:41 |
| `BiCQ7k6a…` | low-freq | 0 | no creates in 50 sigs | yesterday |
| `CyaE1Vxv…` | Cented7 (trader) | 0 | n/a — only trades | n/a |
| `5ZuV8eqk…`, `A8Z1ejQG…`, `EYfdt8cN…`, `GZVSEAaj…` | unknown | 0 | no creates seen | n/a |

**Recommendations:**
- The 4 silent wallets (5ZuV8eqk, A8Z1ejQG, EYfdt8cN, GZVSEAaj) and Cented7 are wasted slots — they won't trigger the bot. Consider trimming.
- The 7 active creators are real volume sources. `8YcbyX92`, `HiSo5kyk`, `D9gQ6Rh` are the heavy hitters.

---

## Files added this session

```
sol-bot-dashboard/
├── index.html                 (?v=5 cache-bust)
├── app.js                     (MTM logic, bondingCurve capture from tx)
├── README.md                  (header now mentions @solbotboybot)
├── notify/
│   ├── README.md              ← morning quickstart
│   ├── MORNING_BRIEF.md       ← this file
│   ├── poll.py                ← 5-min poller
│   ├── daily.py               ← daily summary
│   ├── health.py              ← health monitor
│   ├── setup_bot.py           ← one-shot bot config (already run)
│   ├── state.json             ← persistent state (auto-committed)
│   ├── watched_audit.json     ← creator audit data
│   └── requirements.txt
└── .github/workflows/
    ├── notify.yml             ← 5-min cron
    ├── daily.yml              ← 00:05 UTC
    └── health.yml             ← 15-min cron
```

## What's NOT built yet (deferred)

- **Interactive bot commands** (`/balance`, `/trades`, `/pnl`) — requires either a long-polling daemon on a VPS or a webhook + Cloudflare Worker. Foundation (parsers, formatters) is reusable when you decide which path.
- **Chart screenshots in TG** — generate a PNG of the dashboard chart and attach to daily summary. ~1hr of work.
- **Performance threshold alerts** ("hit +5 SOL today", "dropped below -2 SOL") — trivial to add but didn't want to clog the channel.
- **Per-wallet drill-down** (`/wallet 8YcbyX92`) — needs interactive bot path.

---

## One-line morning checklist

1. ✅ Check why the bot stopped (logs around 15:14 UTC) — **start here**
2. ✅ Open @solbotboybot, send "hi" → next cron will start pushing notifications
3. ✅ Open https://bljuane.github.io/sol-bot-dashboard/ → verify dashboard with MTM works
4. ✅ Once Franklin's bot is back up, confirm a watched-wallet launch triggers both:
   - `[trigger]` in bot logs
   - 🆕 message in the Telegram channel
   - bot wallet tx within 60 slots

If all three light up, the entire stack is verified end-to-end.

Have a good morning.
