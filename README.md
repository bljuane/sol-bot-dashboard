# Sniper Bot Wallet Dashboard

Static client-side dashboard for monitoring a Solana pump.fun sniper-bot wallet's PnL and trade activity. Runs entirely in the browser — no backend, no API keys stored anywhere.

**Live dashboard:** [https://bljuane.github.io/sol-bot-dashboard/](https://bljuane.github.io/sol-bot-dashboard/)

## What it shows

- Current SOL balance (with USD valuation from CoinGecko)
- Realized PnL across closed trades (mint-by-mint FIFO match)
- Win rate (winners / losers)
- Open positions (mint, capital deployed, age)
- Recent trades feed (last 100), with V1/V2 type tags
- Cumulative-realized-PnL line chart over time
- Auto-refreshes every 30 seconds

## How to use

Visit the live dashboard. By default it monitors the test bot wallet (`5YDHipTh…`). To monitor a different wallet, either:

1. **Use the URL params:** `?wallet=<address>&rpc=<rpc_url>`
   Example: `https://bljuane.github.io/sol-bot-dashboard/?wallet=5YDHi…ZcmFjSP`

2. **Use the controls in the header** (paste address into the box, click Save). Your settings persist in `localStorage` and the URL gets updated so you can share the configured view.

## RPC endpoint

The dashboard defaults to the public `https://api.mainnet-beta.solana.com` endpoint. This is heavily rate-limited and may show errors during high-load periods. For a smoother experience, use your own Helius/Alchemy/Triton endpoint — paste it into the "RPC URL" box.

**Important:** if you put a private RPC URL into the dashboard, it gets saved in `localStorage` and the URL. Anyone you share the URL with will see your RPC URL. Use a read-only / rate-limited endpoint, not your production hot-path key.

## How PnL is computed

For each pump.fun mint the wallet has interacted with:

1. Sum SOL spent across all buy transactions (`solDelta < 0`)
2. Sum SOL received across all sell transactions (`solDelta > 0`)
3. Realized PnL = received − spent
4. If the mint has only buys → counted as an **open position**
5. If the mint has at least one sell → counted as a **closed trade**, contributing to total realized PnL

PnL uses the wallet's actual SOL balance delta (`postBalance − preBalance`) from each transaction, so transaction fees and Jito tips are correctly debited. Token amounts and bonding-curve prices are not tracked — this is realized-cash PnL, not mark-to-market.

## Known limitations

- Open positions don't show current mark-to-market value (would require an extra RPC call per mint per refresh to read the bonding curve state — TODO)
- Token decimals/amounts aren't shown — only SOL deltas
- Public RPC rate-limits cap the refresh interval at ~30s; with a dedicated endpoint you can refresh more often
- "FIFO match" is naive — if a wallet repeatedly buys and sells the same mint, the position-vs-closed classification is approximate
- Manual trades (e.g., via Trojan/BullX) appear as pump.fun trades because they are — but they'll skew bot-only metrics. Use a clean bot-specific wallet for cleanest readings.

## Customizing

The dashboard is a single `index.html` + `app.js` + (no styles file — embedded). To change refresh interval, edit `REFRESH_INTERVAL_MS` in `app.js`. To change the default wallet, edit `DEFAULT_WALLET`.

## Stack

- Vanilla JS (no build step, no bundler)
- Chart.js 4.4.1 (via CDN) for the PnL chart
- Solana JSON-RPC for all data
- CoinGecko free API for SOL/USD price

## Privacy

All RPC calls go directly from the user's browser to the RPC endpoint they configure. No data is sent to any backend, no analytics, no third-party tracking.

## License

MIT. Fork and adapt as needed.
