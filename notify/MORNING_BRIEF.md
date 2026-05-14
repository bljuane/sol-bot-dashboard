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

### 3. Watched-wallet audit — outcomes are bleak

Ran a **deep audit** on all 12 watched wallets (cadence + creation totals + graduation rates over full ~1000-sig history). Full data saved to `notify/watched_audit.json`.

**Volume / cadence per creator:**

| Wallet | Total creates (recent ~1000 sigs) | V2 % | Most recent | Cadence |
|---|---:|---:|---|---|
| `8YcbyX92…` | **252** | 100% | 16:51 today | spray, high-volume |
| `HiSo5kyk…` | 163 | 100% | 11:51 | bursts |
| `D9gQ6Rh…` | 165 | 100% | 14:49 | steady |
| `5htGpHK2…` | 126 | 100% | 09:41 | quieter |
| `BiCQ7k6a…` | 50 | 100% | yesterday | low-freq bursts |
| `7hbtZ1M9…` (Luis) | 36 | 100% | 11:40 | dev wallet |
| `FM1YCKED…` | 31 | 100% | 06:02 | lighter |
| `CyaE1Vxv…` (Cented7) | 0 (trader, not creator) | — | — | trades only |
| `5ZuV8eqk…`, `A8Z1ejQG…`, `EYfdt8cN…`, `GZVSEAaj…` | 0 visible | — | — | quiet / unknown |

**Two important takeaways:**

**1. Every watched creator is 100% Token-2022 (V2).** Not a single V1 launch across 823 total creations spanning all 7 active creators. **The bot's V2 path is the entire game** — if it doesn't work for V2, the bot fires zero times on this watched set.

**2. Graduation rate on these creators is essentially zero.** Of the 20 most-recent mints sampled per creator (140 mints total), **zero have graduated**. Per-creator outcomes (alive on curve, avg real_sol_reserves):

| Wallet | 20 sample mints alive | Avg real_sol_reserves |
|---|---:|---:|
| `D9gQ6Rh…` | 20/20 still on curve | **0.46 SOL** ← best |
| `BiCQ7k6a…` | 20/20 | 0.23 SOL |
| `5htGpHK2…` | 20/20 | 0.19 SOL |
| `FM1YCKED…` | 20/20 | 0.08 SOL |
| `8YcbyX92…` | 20/20 | **0.01 SOL** ← duds |
| `HiSo5kyk…` | 20/20 | 0.00 SOL ← duds |
| `7hbtZ1M9…` (Luis) | 20/20 | 0.00 SOL |

`D9gQ6Rh` is the only wallet whose mints accumulate meaningful (~0.5 SOL avg) buy interest. `8YcbyX92` and `HiSo5kyk` are spray-launchers — high volume but tokens die on curve immediately. This is consistent with typical pump.fun behavior (median token dies <1 SOL real reserves) and means the bot's per-trade ROI from this watched list will be modest unless the bot's exit logic (quick-dump on 1-5 buyers, ladder on alone/hot) genuinely captures the right side of each launch.

**Recommendations:**
- **Trim the 5 silent slots** — Cented7, 5ZuV8eqk, A8Z1ejQG, EYfdt8cN, GZVSEAaj never trigger the bot. Remove them from `WATCHED_WALLETS` repo variable.
- **Prioritize `D9gQ6Rh`** — best historical outcomes. Consider weighting / boosting tier on this creator if Franklin's bot supports per-creator overrides.
- **De-prioritize `8YcbyX92` / `HiSo5kyk`** for testing — they launch a lot but the tokens die. Use them for "is the bot firing" verification, not for expecting real PnL.
- **Expected reality:** at this watched set's quality, the bot's strategy needs to be defensive — quick-dump fast on weak fields, only ride alone-cases. The momentum ladder + extension logic in BUILD_SPEC_V2 §4 is critical, not optional.

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
