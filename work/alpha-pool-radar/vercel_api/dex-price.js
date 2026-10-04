function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  res.end(JSON.stringify(payload));
}

function cleanPathPart(value) {
  const raw = String(value || "").trim();
  if (!/^[a-z0-9_-]+$/i.test(raw)) return "";
  return raw;
}

function chainAliases(chain) {
  const raw = String(chain || "").toLowerCase();
  const aliases = {
    bnb: ["bsc"],
    bsc: ["bsc"],
    "binance-smart-chain": ["bsc"],
    sol: ["solana"],
    solana: ["solana"],
    eth: ["ethereum", "eth"],
    ethereum: ["ethereum", "eth"],
    base: ["base"],
    robinhood: ["robinhood", "4663"],
    4663: ["robinhood", "4663"],
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
  return candidates
    .slice()
    .sort((a, b) => liquidityUsd(b) - liquidityUsd(a) || volume24h(b) - volume24h(a))[0];
}

function quoteFromPair(pairData) {
  const marketCap = Number(pairData.marketCap || pairData.fdv);
  const fdv = Number(pairData.fdv || pairData.marketCap);
  return {
    ok: true,
    priceUsd: Number(pairData.priceUsd),
    marketCap,
    fdv,
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

export default async function handler(req, res) {
  try {
    if (req.method === "OPTIONS") {
      sendJson(res, 204, {});
      return;
    }
    if (req.method !== "GET") {
      sendJson(res, 405, { ok: false, error: "method_not_allowed" });
      return;
    }

    const chain = cleanPathPart(req.query.chain);
    const pair = cleanPathPart(req.query.pair);
    const token = cleanPathPart(req.query.token);
    if (!chain || (!pair && !token)) {
      sendJson(res, 400, { ok: false, error: "missing_chain_and_pair_or_token" });
      return;
    }

    const url = token
      ? `https://api.dexscreener.com/latest/dex/tokens/${token}`
      : `https://api.dexscreener.com/latest/dex/pairs/${chain}/${pair}`;
    const response = await fetch(url, {
      cache: "no-store",
      headers: {
        accept: "application/json",
        "user-agent": "AlphaRadar/1.0",
      },
    });
    const payload = await response.json().catch(() => ({}));
    const pairData = token ? bestPairForToken(payload?.pairs, chain) : payload?.pair;
    if (!response.ok || !pairData) {
      sendJson(res, response.ok ? 502 : response.status, {
        ok: false,
        step: token ? "dexscreener_token" : "dexscreener_pair",
        status: response.status,
        error: payload?.message || (token ? "empty_token_pairs" : "empty_pair"),
      });
      return;
    }

    sendJson(res, 200, quoteFromPair(pairData));
  } catch (error) {
    sendJson(res, 503, { ok: false, step: "dex_price_api", error: error.message || String(error) });
  }
}
