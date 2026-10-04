function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
  };
}
function json(status, payload) {
  return new Response(JSON.stringify(payload), { status, headers: corsHeaders() });
}
function cleanPathPart(value) {
  const raw = String(value || "").trim();
  return /^[a-z0-9_-]+$/i.test(raw) ? raw : "";
}
function chainAliases(chain) {
  const raw = String(chain || "").toLowerCase();
  const aliases = {
    bnb: ["bsc"], bsc: ["bsc"], "binance-smart-chain": ["bsc"],
    sol: ["solana"], solana: ["solana"], eth: ["ethereum", "eth"], ethereum: ["ethereum", "eth"],
    base: ["base"], robinhood: ["robinhood", "4663"], 4663: ["robinhood", "4663"],
  };
  return aliases[raw] || [raw];
}
function liquidityUsd(pairData) {
  const value = Number(pairData?.liquidity?.usd);
  return Number.isFinite(value) ? value : 0;
}
function volume24h(pairData) {
  const value = Number(pairData?.volume?.h24);
  return Number.isFinite(value) ? value : 0;
}
function bestPairForToken(pairs, chain) {
  const list = Array.isArray(pairs) ? pairs : [];
  const aliases = new Set(chainAliases(chain));
  const sameChain = list.filter((pair) => aliases.has(String(pair?.chainId || "").toLowerCase()));
  const candidates = sameChain.length ? sameChain : list;
  return candidates.slice().sort((a, b) => liquidityUsd(b) - liquidityUsd(a) || volume24h(b) - volume24h(a))[0];
}
function quoteFromPair(pairData) {
  const marketCap = Number(pairData.marketCap || pairData.fdv);
  const fdv = Number(pairData.fdv || pairData.marketCap);
  return {
    ok: true,
    priceUsd: Number(pairData.priceUsd),
    marketCap, fdv,
    liquidityUsd: liquidityUsd(pairData),
    volume24h: volume24h(pairData),
    changeM5: Number(pairData.priceChange?.m5),
    changeH1: Number(pairData.priceChange?.h1),
    changeH24: Number(pairData.priceChange?.h24),
    pairAddress: pairData.pairAddress,
    dexUrl: pairData.url,
    chainId: pairData.chainId,
    updatedAt: Date.now(),
  };
}
export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}
export async function onRequestGet(context) {
  try {
    const url = new URL(context.request.url);
    const chain = cleanPathPart(url.searchParams.get("chain"));
    const pair = cleanPathPart(url.searchParams.get("pair"));
    const token = cleanPathPart(url.searchParams.get("token"));
    if (!chain || (!pair && !token)) return json(400, { ok: false, error: "missing_chain_and_pair_or_token" });
    const endpoint = token
      ? `https://api.dexscreener.com/latest/dex/tokens/${token}`
      : `https://api.dexscreener.com/latest/dex/pairs/${chain}/${pair}`;
    const response = await fetch(endpoint, {
      cache: "no-store",
      headers: { accept: "application/json", "user-agent": "AlphaRadar/1.0" },
    });
    const payload = await response.json().catch(() => ({}));
    const pairData = token ? bestPairForToken(payload?.pairs, chain) : payload?.pair;
    if (!response.ok || !pairData) {
      return json(response.ok ? 502 : response.status, {
        ok: false,
        step: token ? "dexscreener_token" : "dexscreener_pair",
        status: response.status,
        error: payload?.message || (token ? "empty_token_pairs" : "empty_pair"),
      });
    }
    return json(200, quoteFromPair(pairData));
  } catch (error) {
    return json(503, { ok: false, step: "dex_price_api", error: error.message || String(error) });
  }
}
