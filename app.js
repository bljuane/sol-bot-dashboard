/* Sniper-bot wallet dashboard — pure client-side, no backend.
 * Reads bot wallet's pump.fun activity directly from Solana RPC,
 * computes per-mint cost-basis FIFO PnL, renders KPIs and chart.
 */

// ── Configuration ─────────────────────────────────────────────

const DEFAULT_WALLET = "5YDHipThEddE5jr7AcRkcdkUofeiqWBukSwLgZcmFjSP";
const DEFAULT_RPC    = "https://solana-rpc.publicnode.com";
const REFRESH_INTERVAL_MS = 30_000;
const MAX_SIGS_TO_FETCH = 1000;
const PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const LAMPORTS_PER_SOL = 1_000_000_000;

// Pump.fun instruction discriminators (first 8 bytes, hex)
const PUMP_DISCS = {
  buy_v1: "66063d1201daebea",
  buy_v2: "c2ab1c46684d5b2f",
  buy_v3: "b817ee6167c5d33d",
  buy_v1b: "38fc74089edfcd5f",
  sell:   "33e685a4017f83ad",
};

// Token program IDs (for V1/V2 detection)
const TOKEN_PROGRAM_V1 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
const TOKEN_PROGRAM_V2 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";

// SOL price cache (refreshed every refresh cycle)
let SOL_PRICE_USD = 84; // fallback

// ── State ──────────────────────────────────────────────────────

let state = {
  wallet:  "",
  rpcUrl:  "",
  trades:  [],        // [{ sig, slot, time, type, mint, solDelta, isV2, err }]
  balance: 0,         // SOL
  positions: new Map(),  // mint -> { mint, totalSpent, lastBuyTs, lastBuySlot, tokenBalance }
  closed: [],         // [{ mint, spent, received, pnl, firstBuyTs, lastTs, buys, sells }]
  tokenBalances: new Map(),  // mint -> on-chain ui amount
  loading: false,
  chart: null,
};

// ── Utilities ──────────────────────────────────────────────────

const fmtSol = (n, sign=true) => {
  const s = (sign && n > 0 ? "+" : "") + n.toFixed(4);
  return s + " SOL";
};
const fmtUsd = (n) => {
  const v = n * SOL_PRICE_USD;
  return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
};
const fmtTime = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toISOString().replace("T"," ").substring(5, 19) + " UTC";
};
const fmtAge = (ts) => {
  if (!ts) return "—";
  const sec = Math.floor((Date.now()/1000) - ts);
  if (sec < 60)    return `${sec}s ago`;
  if (sec < 3600)  return `${Math.floor(sec/60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
  return `${Math.floor(sec/86400)}d ago`;
};
const shortAddr = (s, n=6) => s ? `${s.slice(0,n)}…${s.slice(-4)}` : "";
const log = (...a) => console.log("[dash]", ...a);

// Hex helpers (base58 → hex for discriminator matching)
const BS58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function base58ToBytes(s) {
  const bytes = [0];
  for (const c of s) {
    const v = BS58_ALPHABET.indexOf(c);
    if (v < 0) throw new Error("invalid base58 char: " + c);
    let carry = v;
    for (let j = 0; j < bytes.length; j++) {
      const x = bytes[j] * 58 + carry;
      bytes[j] = x & 0xff;
      carry    = x >> 8;
    }
    while (carry) {
      bytes.push(carry & 0xff);
      carry >>= 8;
    }
  }
  // leading 1s = leading zero bytes
  for (let i = 0; i < s.length && s[i] === "1"; i++) bytes.push(0);
  return new Uint8Array(bytes.reverse());
}
function bytesToHex(b) {
  return Array.from(b).map(x => x.toString(16).padStart(2, "0")).join("");
}
function base58DiscriminatorHex(b58) {
  try {
    const bytes = base58ToBytes(b58);
    return bytesToHex(bytes.slice(0, 8));
  } catch { return ""; }
}

// ── RPC ────────────────────────────────────────────────────────

let rpcId = 0;
async function rpc(method, params) {
  let res;
  try {
    res = await fetch(state.rpcUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params }),
    });
  } catch (err) {
    throw new Error(`RPC unreachable (${err.message}). Try a different RPC URL — the default public Solana RPC blocks browser requests. Recommended: https://solana-rpc.publicnode.com`);
  }
  if (!res.ok) {
    if (res.status === 403) {
      throw new Error(`RPC ${method} returned 403 Forbidden — this endpoint blocks browsers. Switch to https://solana-rpc.publicnode.com or supply your own Helius/Triton URL in the RPC box above.`);
    }
    if (res.status === 429) {
      throw new Error(`RPC ${method} rate-limited (429). Supply your own dedicated RPC URL for smoother operation.`);
    }
    throw new Error(`RPC ${method} HTTP ${res.status}`);
  }
  const j = await res.json();
  if (j.error) throw new Error(`RPC ${method} error: ${j.error.message ?? JSON.stringify(j.error)}`);
  return j.result;
}

async function rpcBatch(reqs) {
  // Many public RPCs reject batch; we just sequence with low concurrency
  const out = [];
  for (let i = 0; i < reqs.length; i += 4) {
    const slice = reqs.slice(i, i + 4);
    const results = await Promise.all(slice.map(([method, params]) =>
      rpc(method, params).catch(e => ({ __err: e.message }))
    ));
    out.push(...results);
  }
  return out;
}

// ── Trade extraction ──────────────────────────────────────────

function parseTxForPumpTrade(tx, walletAddr) {
  // Returns { type: "buy"|"sell"|null, mint, solDelta, isV2 }
  if (!tx || !tx.meta || tx.meta.err) {
    return { failed: !!(tx && tx.meta && tx.meta.err), type: null };
  }
  const msg = tx.transaction?.message;
  if (!msg) return { type: null };

  const staticKeys = (msg.accountKeys || []).map(k => typeof k === "string" ? k : (k.pubkey ?? k));
  const loaded = tx.meta.loadedAddresses || {};
  const allKeys = [...staticKeys, ...(loaded.writable || []), ...(loaded.readonly || [])];

  const walletIdx = allKeys.indexOf(walletAddr);
  if (walletIdx < 0) return { type: null };

  const pre  = tx.meta.preBalances[walletIdx] ?? 0;
  const post = tx.meta.postBalances[walletIdx] ?? 0;
  const fee  = tx.meta.fee ?? 0;
  // SOL delta net of fee (so a buy of 1 SOL with 0.005 fee = -1.0 SOL)
  const solDeltaLamports = post - pre; // negative = spent, positive = received
  const solDelta = solDeltaLamports / LAMPORTS_PER_SOL;

  // Walk top-level instructions; identify pump.fun program + discriminator
  let foundType = null;
  let foundMint = null;
  let isV2 = false;
  const ixs = msg.instructions || [];

  for (const ix of ixs) {
    const pi = ix.programIdIndex;
    if (pi == null || pi >= allKeys.length) continue;
    if (allKeys[pi] !== PUMP_PROGRAM) continue;

    const discHex = base58DiscriminatorHex(ix.data || "");
    let kind = null;
    if (discHex === PUMP_DISCS.buy_v1 || discHex === PUMP_DISCS.buy_v1b) kind = "buy";
    else if (discHex === PUMP_DISCS.buy_v2 || discHex === PUMP_DISCS.buy_v3) { kind = "buy"; isV2 = true; }
    else if (discHex === PUMP_DISCS.sell) kind = "sell";
    else continue;

    foundType = kind;

    // Extract mint pubkey from the ix's account list
    // V1 buy: mint at idx 2; V2 buy: mint at idx 1; sell: mint at idx 2 (V1) or 1 (V2)
    const acctIdxs = ix.accounts || [];
    const candidates = isV2 ? [1, 2] : [2, 1];
    for (const ai of candidates) {
      if (ai < acctIdxs.length) {
        const keyIdx = acctIdxs[ai];
        if (keyIdx < allKeys.length) {
          const addr = allKeys[keyIdx];
          if (addr && addr.endsWith("pump")) { foundMint = addr; break; }
        }
      }
    }
    // Fallback: scan all ix accounts for "pump" suffix
    if (!foundMint) {
      for (const ai of acctIdxs) {
        if (ai < allKeys.length && allKeys[ai].endsWith("pump")) {
          foundMint = allKeys[ai];
          break;
        }
      }
    }
    break;
  }

  return { type: foundType, mint: foundMint, solDelta, isV2 };
}

// ── Main fetch ────────────────────────────────────────────────

async function refresh() {
  if (state.loading) { log("refresh skipped, already loading"); return; }
  state.loading = true;
  setStatus("loading");

  try {
    // 1. SOL price (CoinGecko, best-effort, falls back to cached)
    fetchSolPrice().catch(() => {});

    // 2. Balance
    const balanceResult = await rpc("getBalance", [state.wallet]);
    state.balance = (balanceResult?.value ?? 0) / LAMPORTS_PER_SOL;

    // 2b. Current on-chain token balances — needed for accurate open-position detection
    //     Trojan/BullX multi-ix txs can buy AND sell in the same tx; the parser sees only
    //     the buy. The ONLY way to know if the wallet still holds the token is to ask chain.
    try {
      const [v1Accts, v2Accts] = await Promise.all([
        rpc("getTokenAccountsByOwner", [state.wallet, { programId: TOKEN_PROGRAM_V1 }, { encoding: "jsonParsed" }]),
        rpc("getTokenAccountsByOwner", [state.wallet, { programId: TOKEN_PROGRAM_V2 }, { encoding: "jsonParsed" }]),
      ]);
      state.tokenBalances = new Map();
      for (const accts of [v1Accts?.value || [], v2Accts?.value || []]) {
        for (const a of accts) {
          const info = a.account?.data?.parsed?.info;
          const mint = info?.mint;
          const amt = info?.tokenAmount?.uiAmount || 0;
          if (mint && amt > 0) state.tokenBalances.set(mint, amt);
        }
      }
    } catch (err) {
      log("token balance fetch failed:", err.message);
      state.tokenBalances = new Map();
    }

    // 3. Recent signatures (paginate up to MAX_SIGS_TO_FETCH)
    let sigs = [];
    let before = null;
    while (sigs.length < MAX_SIGS_TO_FETCH) {
      const opts = { limit: 1000 };
      if (before) opts.before = before;
      const batch = await rpc("getSignaturesForAddress", [state.wallet, opts]);
      if (!batch || batch.length === 0) break;
      sigs = sigs.concat(batch);
      if (batch.length < 1000) break;
      before = batch[batch.length - 1].signature;
    }
    log(`Fetched ${sigs.length} signatures`);

    // 4. Fetch tx detail for each sig (only the ones we haven't seen)
    const knownSigs = new Set(state.trades.map(t => t.sig));
    const newSigs = sigs.filter(s => !knownSigs.has(s.signature));
    log(`${newSigs.length} new signatures to fetch`);

    const txReqs = newSigs.map(s => [
      "getTransaction",
      [s.signature, { encoding: "json", maxSupportedTransactionVersion: 0, commitment: "confirmed" }],
    ]);

    const txs = await rpcBatch(txReqs);

    // 5. Parse trades
    const newTrades = [];
    for (let i = 0; i < newSigs.length; i++) {
      const sig = newSigs[i];
      const tx  = txs[i];
      if (!tx || tx.__err) continue;

      const parsed = parseTxForPumpTrade(tx, state.wallet);
      if (!parsed.type) continue;

      newTrades.push({
        sig:      sig.signature,
        slot:     sig.slot,
        time:     sig.blockTime,
        type:     parsed.type,
        mint:     parsed.mint || "?",
        solDelta: parsed.solDelta,
        isV2:     parsed.isV2,
        err:      !!sig.err,
      });
    }
    log(`Parsed ${newTrades.length} new pump.fun trades`);

    // 6. Merge with state and sort
    state.trades = [...state.trades, ...newTrades].sort((a, b) => b.time - a.time);
    // Dedupe just in case
    const seen = new Set();
    state.trades = state.trades.filter(t => seen.has(t.sig) ? false : (seen.add(t.sig), true));

    // 7. Recompute positions + closed PnL (FIFO per mint)
    recomputePnl();

    // 8. Render
    render();
    setStatus("ok");
    document.getElementById("last-refresh").textContent =
      `last refresh: ${new Date().toISOString().substring(11, 19)} UTC`;

  } catch (err) {
    log("REFRESH ERROR:", err.message);
    setStatus("error", err.message);
  } finally {
    state.loading = false;
  }
}

async function fetchSolPrice() {
  try {
    const r = await fetch("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd");
    const j = await r.json();
    if (j?.solana?.usd) SOL_PRICE_USD = j.solana.usd;
  } catch {}
}

// ── PnL computation ───────────────────────────────────────────

function recomputePnl() {
  // Group trades by mint
  const byMint = new Map();
  for (const t of state.trades) {
    if (t.err) continue;
    if (!byMint.has(t.mint)) byMint.set(t.mint, []);
    byMint.get(t.mint).push(t);
  }

  state.positions = new Map();
  state.closed = [];

  for (const [mint, trades] of byMint.entries()) {
    trades.sort((a, b) => a.time - b.time);

    let totalSpent = 0;
    let totalReceived = 0;
    let firstBuyTs = null;
    let lastBuyTs  = null;
    let lastBuySlot = null;
    let buys = 0, sells = 0;

    for (const t of trades) {
      if (t.type === "buy") {
        if (!firstBuyTs) firstBuyTs = t.time;
        lastBuyTs = t.time;
        lastBuySlot = t.slot;
        totalSpent += Math.max(0, -t.solDelta);
        buys++;
      } else if (t.type === "sell") {
        totalReceived += Math.max(0, t.solDelta);
        sells++;
      }
    }

    // The CORRECT open/closed classifier: does the wallet still hold any tokens of this mint?
    // This is the ONLY way to handle multi-ix Trojan/BullX txs where buy+sell happen in one tx
    // (parser sees the buy, misses the embedded sell). On-chain balance is ground truth.
    const onChainBalance = state.tokenBalances.get(mint) || 0;
    const isOpen = onChainBalance > 0;

    if (isOpen) {
      // Real open position — wallet still holds tokens of this mint
      state.positions.set(mint, {
        mint,
        totalSpent,
        totalReceived,
        buys, sells,
        firstBuyTs, lastBuyTs, lastBuySlot,
        tokenBalance: onChainBalance,
      });
    } else if (buys > 0 || sells > 0) {
      // Closed — wallet holds no tokens of this mint, PnL is realized
      state.closed.push({
        mint,
        spent: totalSpent,
        received: totalReceived,
        pnl: totalReceived - totalSpent,
        firstBuyTs: firstBuyTs || (trades[0] && trades[0].time),
        lastTs: trades[trades.length - 1].time,
        buys, sells,
      });
    }
  }
}

// ── Render ────────────────────────────────────────────────────

function render() {
  // ── KPI cards ──
  const totalPnl = state.closed.reduce((a, c) => a + c.pnl, 0);
  const winners = state.closed.filter(c => c.pnl > 0).length;
  const losers  = state.closed.filter(c => c.pnl < 0).length;
  const totalClosed = state.closed.length;
  const wr = totalClosed > 0 ? winners / totalClosed : 0;
  const avgPnl = totalClosed > 0 ? totalPnl / totalClosed : 0;
  const openSpent = [...state.positions.values()].reduce((a, p) => a + p.totalSpent, 0);
  const buys = state.trades.filter(t => t.type === "buy" && !t.err).length;
  const sells = state.trades.filter(t => t.type === "sell" && !t.err).length;

  setText("kpi-balance", state.balance.toFixed(4) + " SOL");
  setText("kpi-balance-sub", fmtUsd(state.balance));

  const pnlEl = document.getElementById("kpi-pnl");
  pnlEl.textContent = fmtSol(totalPnl) + "";
  pnlEl.className = "kpi-value mono " + (totalPnl > 0 ? "green" : totalPnl < 0 ? "red" : "");
  setText("kpi-pnl-sub", `across ${totalClosed} closed trades`);

  const wrEl = document.getElementById("kpi-wr");
  wrEl.textContent = totalClosed > 0 ? (wr * 100).toFixed(0) + "%" : "—%";
  wrEl.className = "kpi-value mono " + (wr >= 0.5 ? "green" : wr < 0.4 && totalClosed > 0 ? "red" : "");
  setText("kpi-wr-sub", `${winners} winners / ${losers} losers`);

  const avgEl = document.getElementById("kpi-avg");
  avgEl.textContent = totalClosed > 0 ? fmtSol(avgPnl) : "— SOL";
  avgEl.className = "kpi-value mono " + (avgPnl > 0 ? "green" : avgPnl < 0 ? "red" : "");

  setText("kpi-open", state.positions.size);
  setText("kpi-open-sub", fmtSol(openSpent, false) + " deployed");

  setText("kpi-trades", state.trades.length);
  setText("kpi-trades-sub", `${buys} buys / ${sells} sells`);

  // ── Chart ──
  renderChart();

  // ── Recent trades table ──
  renderTradesTable();

  // ── Open positions table ──
  renderOpenTable();

  setText("trades-count", `${state.trades.length} shown`);
  setText("open-count", `${state.positions.size}`);
}

function renderChart() {
  const ctx = document.getElementById("pnl-chart");
  const series = [];
  let running = 0;
  // Use closed trades sorted by lastTs
  const closed = [...state.closed].sort((a, b) => a.lastTs - b.lastTs);
  for (const c of closed) {
    running += c.pnl;
    series.push({ x: c.lastTs * 1000, y: running });
  }

  if (state.chart) state.chart.destroy();
  if (series.length === 0) {
    state.chart = null;
    return;
  }

  setText("chart-range", `${series.length} closed trades`);

  state.chart = new Chart(ctx, {
    type: "line",
    data: {
      datasets: [{
        label: "Cumulative realized PnL (SOL)",
        data: series,
        borderColor: "#58a6ff",
        backgroundColor: "rgba(88, 166, 255, 0.1)",
        fill: true,
        tension: 0.1,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => new Date(items[0].parsed.x).toISOString().replace("T"," ").substring(0, 19) + " UTC",
            label: (item) => `${item.parsed.y > 0 ? "+" : ""}${item.parsed.y.toFixed(4)} SOL`,
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          ticks: {
            color: "#8b97a8",
            callback: (val) => new Date(val).toISOString().substring(5, 16).replace("T"," "),
          },
          grid: { color: "rgba(35, 44, 58, 0.5)" },
        },
        y: {
          ticks: {
            color: "#8b97a8",
            callback: (val) => (val > 0 ? "+" : "") + val.toFixed(2),
          },
          grid: { color: "rgba(35, 44, 58, 0.5)" },
        },
      },
    },
  });
}

function renderTradesTable() {
  const tbody = document.querySelector("#trades-table tbody");
  if (state.trades.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No pump.fun trades found for this wallet</td></tr>`;
    return;
  }

  const rows = state.trades.slice(0, 100).map(t => {
    const typeBadge = t.err ? `<span class="badge fail">FAIL</span>` :
      t.type === "buy" ? `<span class="badge buy">BUY</span>` : `<span class="badge sell">SELL</span>`;
    const versionBadge = t.isV2 ? `<span class="badge v2">V2</span>` : `<span class="badge v1">V1</span>`;
    const solClass = t.solDelta > 0 ? "green" : "red";
    const solSign = t.solDelta > 0 ? "+" : "";
    const mintShort = t.mint && t.mint !== "?" ?
      `<span class="mint" title="${t.mint}">${t.mint.slice(0, 8)}…${t.mint.slice(-4)}</span>` :
      `<span class="dim">unknown</span>`;
    const explorerUrl = `https://solscan.io/tx/${t.sig}`;
    return `
      <tr>
        <td><div>${fmtTime(t.time)}</div><div class="sig dim">${fmtAge(t.time)}</div></td>
        <td>${typeBadge} ${versionBadge}</td>
        <td>${mintShort}</td>
        <td class="right ${solClass}">${solSign}${t.solDelta.toFixed(4)}</td>
        <td class="right dim">${t.slot.toLocaleString()}</td>
        <td><a href="${explorerUrl}" target="_blank">${t.sig.slice(0, 8)}…</a></td>
      </tr>`;
  }).join("");
  tbody.innerHTML = rows;
}

function renderOpenTable() {
  const tbody = document.querySelector("#open-table tbody");
  const opens = [...state.positions.values()].sort((a, b) => b.lastBuyTs - a.lastBuyTs);

  if (opens.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No open positions</td></tr>`;
    return;
  }

  tbody.innerHTML = opens.map(p => {
    const mintShort = p.mint !== "?" ?
      `<a href="https://pump.fun/${p.mint}" target="_blank" title="${p.mint}">${p.mint.slice(0, 8)}…${p.mint.slice(-4)}</a>` :
      `<span class="dim">unknown</span>`;
    const tokFmt = p.tokenBalance >= 1000000 ?
      (p.tokenBalance / 1000000).toFixed(2) + "M" :
      p.tokenBalance >= 1000 ?
      (p.tokenBalance / 1000).toFixed(1) + "K" :
      p.tokenBalance.toFixed(2);
    return `
      <tr>
        <td>${mintShort}</td>
        <td class="right">${p.totalSpent.toFixed(4)} SOL</td>
        <td class="right">${tokFmt}</td>
        <td class="right dim">${fmtAge(p.lastBuyTs)}</td>
      </tr>`;
  }).join("");
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setStatus(state, msg) {
  const el = document.getElementById("status-indicator");
  if (state === "loading") {
    el.classList.remove("offline");
    el.innerHTML = `<span class="dot"></span>Sniper Bot Monitor <span class="dim" style="font-size:12px;margin-left:8px;">refreshing…</span>`;
  } else if (state === "error") {
    el.classList.add("offline");
    // truncate long error messages for the header; full message in the banner
    const short = msg && msg.length > 60 ? msg.slice(0, 60) + "…" : (msg || "");
    el.innerHTML = `<span class="dot"></span>Sniper Bot Monitor <span class="red" style="font-size:12px;margin-left:8px;" title="${(msg||'').replace(/"/g,'&quot;')}">error: ${short}</span>`;
    showErrorBanner(msg);
  } else {
    el.classList.remove("offline");
    el.innerHTML = `<span class="dot"></span>Sniper Bot Monitor`;
    hideErrorBanner();
  }
}

function showErrorBanner(msg) {
  let b = document.getElementById("error-banner");
  if (!b) {
    b = document.createElement("div");
    b.id = "error-banner";
    b.style.cssText = "background:rgba(248,81,73,0.12);border:1px solid rgba(248,81,73,0.35);color:#f85149;padding:12px 16px;margin:0 24px 16px;border-radius:8px;font-size:13px;line-height:1.5;";
    const main = document.querySelector("main");
    main.insertBefore(b, main.firstChild);
  }
  b.innerHTML = `<strong>Couldn't reach RPC.</strong> ${msg || ""}`;
}
function hideErrorBanner() {
  const b = document.getElementById("error-banner");
  if (b) b.remove();
}

// ── URL params + settings ─────────────────────────────────────

// URLs that block browsers — automatically replace with the working default
const BLOCKED_RPC_HOSTS = ["api.mainnet-beta.solana.com", "api.devnet.solana.com"];

function isBlockedRpc(url) {
  if (!url) return false;
  return BLOCKED_RPC_HOSTS.some(host => url.includes(host));
}

function loadSettings() {
  const params = new URLSearchParams(location.search);

  // Pull from URL → localStorage → default, in that order
  let rpc = params.get("rpc") || localStorage.getItem("dash.rpc") || "";

  // Force-replace any blocked endpoint
  if (isBlockedRpc(rpc)) {
    console.warn("[dash] migrating blocked RPC", rpc, "→", DEFAULT_RPC);
    rpc = "";
    localStorage.removeItem("dash.rpc");
  }
  if (!rpc) rpc = DEFAULT_RPC;

  state.wallet = params.get("wallet") || localStorage.getItem("dash.wallet") || DEFAULT_WALLET;
  state.rpcUrl = rpc;

  document.getElementById("wallet").value = state.wallet;
  document.getElementById("rpc").value    = state.rpcUrl;

  // Update URL to reflect the (possibly migrated) settings
  const newUrl = new URL(location.href);
  if (state.wallet !== DEFAULT_WALLET) newUrl.searchParams.set("wallet", state.wallet);
  else newUrl.searchParams.delete("wallet");
  if (state.rpcUrl !== DEFAULT_RPC) newUrl.searchParams.set("rpc", state.rpcUrl);
  else newUrl.searchParams.delete("rpc");
  history.replaceState({}, "", newUrl);
}

function saveSettings() {
  state.wallet = document.getElementById("wallet").value.trim() || DEFAULT_WALLET;
  state.rpcUrl = document.getElementById("rpc").value.trim()    || DEFAULT_RPC;
  localStorage.setItem("dash.wallet", state.wallet);
  localStorage.setItem("dash.rpc",    state.rpcUrl);
  // Update URL so it's shareable
  const url = new URL(location.href);
  url.searchParams.set("wallet", state.wallet);
  if (state.rpcUrl !== DEFAULT_RPC) url.searchParams.set("rpc", state.rpcUrl);
  history.replaceState({}, "", url);
  // Reset state and reload
  state.trades = [];
  state.closed = [];
  state.positions = new Map();
  refresh();
}

// ── Bootstrap ─────────────────────────────────────────────────

window.addEventListener("DOMContentLoaded", () => {
  loadSettings();
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("settings").addEventListener("click", saveSettings);
  document.getElementById("wallet").addEventListener("keypress", e => { if (e.key === "Enter") saveSettings(); });
  document.getElementById("rpc").addEventListener("keypress",    e => { if (e.key === "Enter") saveSettings(); });

  refresh();
  setInterval(refresh, REFRESH_INTERVAL_MS);
});
