# Telegram notifier

Pushes bot wallet activity to a Telegram channel via GitHub Actions cron. No backend, no server, no monthly cost.

**Bot:** [@solbotboybot](https://t.me/solbotboybot) ("SolBotBoy")

## Morning quickstart (60 seconds)

Everything is wired up except the chat target. To activate notifications:

1. Open Telegram → search **`@solbotboybot`** → tap **Start** (or send any message).
2. **That's it.** Within 5 minutes the next cron run will auto-discover your chat_id, send a "👋 SolBotBoy is online" welcome message, and start streaming notifications.

If you'd rather pipe to a channel/group (shared with Luis + Franklin):

1. Create a Telegram channel or group.
2. Add **`@solbotboybot`** to it. For channels, the bot must be an **admin** with "post messages" permission.
3. Send any message into that chat.
4. Wait up to 5 minutes — first cron after that picks up the chat ID.

To force a specific chat target (skip auto-discovery): set GH Secret `TG_CHANNEL_ID` to the chat's numeric id (`-1001234…`) or `@channelusername`.

## What gets pushed

### Real-time (5-min cron)
- 🟢/🔴 every bot wallet trade — buy/sell/fail with SOL delta, slot, tx link
- 🆕 every watched-wallet pump.fun launch — with comparison: did XSV catch it? at what slot?
- 💰 balance change ≥ 5%
- 🚨 low-balance alert when wallet drops below 0.5 SOL (max once/24h)

### Daily (00:05 UTC)
- 📊 24h PnL summary — realized PnL, win rate, avg, top winners/losers, open positions

### Health monitor (15-min)
- ⚠️ silent-during-peak alert (bot hasn't submitted in >1h during 02-07 UTC)
- ⚠️ missed-creation alert (watched wallet created a token, bot didn't submit in slot+60 window)

## Files

```
notify/
  poll.py          ← main 5-min poller (trades, balance, watched-launches)
  daily.py         ← 00:05 UTC summary
  health.py        ← 15-min health monitor
  setup_bot.py     ← one-shot bot config (commands, name, description) — already run
  state.json       ← persistent state (last-seen sigs, discovered chat_id)
  requirements.txt
.github/workflows/
  notify.yml       ← 5-min cron
  daily.yml        ← 00:05 UTC daily
  health.yml       ← 15-min health
```

## Config (GH Secrets + Variables, already set)

**Secrets** (encrypted, never appear in logs):
- `TG_BOT_TOKEN` — bot token from @BotFather
- `TG_CHANNEL_ID` — *(optional)* explicit chat target; auto-discovered if absent

**Repo Variables** (visible in workflow logs):
- `BOT_WALLET` — wallet to monitor for trades
- `XSV_WALLET` — XSV's V4 wallet for "did XSV catch this" comparisons
- `RPC_URL` — Solana RPC endpoint (default: solana-rpc.publicnode.com)
- `WATCHED_WALLETS` — comma-separated list of creator wallets

To change any of these:
```bash
gh variable set WATCHED_WALLETS --body "addr1,addr2,addr3" --repo bljuane/sol-bot-dashboard
gh secret set TG_CHANNEL_ID --body "@my_channel"
```

## Cost

GitHub Actions free tier: 2,000 minutes/month for public repos.

- `notify.yml`: 5-min cron × ~30s/run × 12/hour × 24h × 30d = ~720 min/month
- `daily.yml`: 1× per day × 1min = ~30 min/month
- `health.yml`: 15-min cron × ~20s × 4/hour × 24h × 30d = ~240 min/month

**Total: ~1,000 minutes/month = within free tier.** If you want faster polling (e.g., 1-min cron), it's still under the limit but consider self-hosting on a small VPS for the same money.

## How auto-discovery works

`poll.py` calls Telegram's `getUpdates` on each run. If the bot has been added to a chat and someone has messaged it, the chat appears in the updates list. The script picks the most recent chat and saves the ID to `notify/state.json` (which gets committed back by the workflow).

Once saved, subsequent runs read the chat ID from state — no more `getUpdates` calls needed.

To re-discover (e.g., to switch to a new channel): delete `discovered_chat_id` from `notify/state.json` and commit; next run will re-auto-discover.

## Adding interactive commands (future)

Right now the bot is push-only — it can't respond to `/balance` or `/trades` from users. To make it interactive:

**Option A:** Long-polling daemon on a small VPS ($5/mo)
- Continuously calls `getUpdates`, processes commands, replies inline
- ~30 min to build, real-time responses

**Option B:** Webhook → Cloudflare Worker (free)
- Bot sends each message to a Workers endpoint
- Worker reads bot wallet state via RPC and replies
- Free, but slightly more setup

Either path is a separate evening of work; the foundation here (parsing logic, formatting helpers) is reusable.

## Manual testing

Trigger any workflow on demand:
```bash
gh workflow run notify.yml --repo bljuane/sol-bot-dashboard
gh workflow run daily.yml  --repo bljuane/sol-bot-dashboard
gh workflow run health.yml --repo bljuane/sol-bot-dashboard
```

View recent runs + logs:
```bash
gh run list --workflow notify.yml --repo bljuane/sol-bot-dashboard
gh run view <run-id> --log
```

## Security

The bot token is stored as an encrypted GH Secret. The workflow files reference it via `${{ secrets.TG_BOT_TOKEN }}` — the value never appears in logs (GH masks it automatically). The token only ever gets read by:
1. The Python script inside `requests.post()` calls (sent over HTTPS to Telegram)
2. GitHub's own scheduler when injecting it as an env var

If you ever need to rotate the token (e.g., if it leaks):
1. @BotFather → /mybots → @solbotboybot → API Token → Revoke
2. `gh secret set TG_BOT_TOKEN --body "<new_token>" --repo bljuane/sol-bot-dashboard`
3. Done — next cron run uses the new token.
