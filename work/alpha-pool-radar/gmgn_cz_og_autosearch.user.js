// ==UserScript==
// @name         Alpha Radar - GMGN Twitter Monitor OG Auto Search
// @namespace    alpha-radar
// @version      0.1.0
// @description  Read-only GMGN helper: use GMGN twitter monitor output from selected accounts to find related BSC meme OG tokens.
// @match        https://gmgn.ai/*
// @run-at       document-idle
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_openInTab
// @grant        GM_setClipboard
// @grant        GM_xmlhttpRequest
// @connect      api.dexscreener.com
// ==/UserScript==

(function () {
  "use strict";

  const STORAGE_KEY = "alpha_radar_pending_gmgn_og";
  const FIRED_KEY = "alpha_radar_fired_cz_reply_ids";
  const CHECKED_KEY = "alpha_radar_checked_cz_reply_ids";
  const PENDING_MAX_AGE_MS = 10 * 60 * 1000;
  const NO_MATCH_RETRY_MS = 75 * 1000;
  const DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search";
  const MAX_TERMS = 8;
  const MAX_CANDIDATES = 5;
  const MIN_LIQUIDITY_USD = 1000;
  const MAX_AGE_HOURS = 72;
  const AUTO_OPEN_WHEN_MATCHED = false;
  const GMGN_MONITOR_SCAN_INTERVAL_MS = 2500;

  // Add important Twitter/X accounts here. GMGN must already monitor them.
  // Use lowercase usernames without "@". Example: "heyibinance", "bnbchain".
  const WATCH_ACCOUNTS = [
    "cz_binance",
    "heyibinance",
    "binance",
    "binancewallet",
  ];

  // Source aliases for GMGN monitor cards and Binance Square reposts.
  // `requiresSquare: true` avoids treating every plain "CZ" in token text as a source hit.
  const WATCH_SOURCE_ALIASES = {
    cz_binance: [
      { text: "cz_binance" },
      { text: "@cz_binance" },
      { text: "changpeng zhao" },
      { text: "赵长鹏" },
      { text: "cz", requiresSquare: true },
      { text: "币安广场 cz" },
      { text: "binance square cz" },
    ],
    heyibinance: [
      { text: "heyibinance" },
      { text: "@heyibinance" },
      { text: "heyi" },
      { text: "he yi" },
      { text: "yi he" },
      { text: "何一" },
      { text: "币安广场 何一" },
      { text: "binance square he yi" },
    ],
    binance: [
      { text: "binance" },
      { text: "@binance" },
      { text: "binance official" },
      { text: "币安官方" },
    ],
    binancewallet: [
      { text: "binancewallet" },
      { text: "@binancewallet" },
      { text: "binance wallet" },
      { text: "币安钱包" },
    ],
  };

  const STOP_TERMS = new Set([
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "bnb",
    "bsc",
    "binance",
    "binancewallet",
    "blockchain",
    "buy",
    "ca",
    "chain",
    "crypto",
    "cz",
    "for",
    "from",
    "gmgn",
    "has",
    "have",
    "he",
    "heyi",
    "heyibinance",
    "his",
    "http",
    "https",
    "in",
    "is",
    "it",
    "meme",
    "memes",
    "of",
    "on",
    "or",
    "our",
    "post",
    "reply",
    "she",
    "that",
    "the",
    "their",
    "this",
    "to",
    "token",
    "tweet",
    "was",
    "we",
    "with",
    "you",
    "your",
    "yi",
    "何一",
    "币安广场",
  ]);

  // Optional manual mappings. Use this when the OG token is not a literal text match.
  const RULES = [
    {
      id: "cz_2091566455464792426_og",
      author: "cz_binance",
      statusId: "2091566455464792426",
      symbol: "OG",
      token: "0xb070bb71dd214fcd10a3657ae6e8f8dcdf027777",
      chain: "bsc",
      sourceUrl: "https://x.com/cz_binance/status/2091566455464792426",
      note: "Manual override: CZ reply mapped to OG BSC meme token",
    },
  ];

  const activeDynamicLookups = new Set();

  function gmGet(key, fallback) {
    try {
      if (typeof GM_getValue === "function") return GM_getValue(key, fallback);
    } catch (_error) {
      // Fall through to localStorage.
    }
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (_error) {
      return fallback;
    }
  }

  function gmSet(key, value) {
    try {
      if (typeof GM_setValue === "function") {
        GM_setValue(key, value);
        return;
      }
    } catch (_error) {
      // Fall through to localStorage.
    }
    localStorage.setItem(key, JSON.stringify(value));
  }

  function gmDelete(key) {
    try {
      if (typeof GM_deleteValue === "function") {
        GM_deleteValue(key);
        return;
      }
    } catch (_error) {
      // Fall through to localStorage.
    }
    localStorage.removeItem(key);
  }

  function openTab(url) {
    try {
      if (typeof GM_openInTab === "function") {
        GM_openInTab(url, { active: true, insert: true });
        return;
      }
    } catch (_error) {
      // Fall through to window.open.
    }
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function setClipboard(text) {
    try {
      if (typeof GM_setClipboard === "function") {
        GM_setClipboard(text, "text");
        return;
      }
    } catch (_error) {
      // Fall through to clipboard API.
    }
    if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
  }

  function gmgnTokenUrl(rule) {
    return `https://gmgn.ai/${encodeURIComponent(rule.chain)}/token/${encodeURIComponent(rule.token)}`;
  }

  function gmgnSearchUrl(chain, text) {
    return `https://gmgn.ai/${encodeURIComponent(chain || "bsc")}?q=${encodeURIComponent(text || "")}`;
  }

  function savePending(rule, reason) {
    gmSet(STORAGE_KEY, {
      ...rule,
      reason,
      searchText: rule.searchText || rule.symbol || rule.token,
      tokenUrl: rule.token ? gmgnTokenUrl(rule) : gmgnSearchUrl(rule.chain, rule.searchText || rule.symbol || ""),
      savedAt: Date.now(),
    });
  }

  function firedIds() {
    const value = gmGet(FIRED_KEY, []);
    return Array.isArray(value) ? value : [];
  }

  function rememberFired(id) {
    const next = [...new Set([...firedIds(), id])].slice(-100);
    gmSet(FIRED_KEY, next);
  }

  function hasFired(id) {
    return firedIds().includes(id);
  }

  function checkedMap() {
    const value = gmGet(CHECKED_KEY, {});
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function markChecked(id) {
    const map = checkedMap();
    map[id] = Date.now();
    const entries = Object.entries(map)
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .slice(0, 100);
    gmSet(CHECKED_KEY, Object.fromEntries(entries));
  }

  function recentlyChecked(id) {
    const last = Number(checkedMap()[id] || 0);
    return last > 0 && Date.now() - last < NO_MATCH_RETRY_MS;
  }

  function isVisible(element) {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function nativeSetInputValue(input, value) {
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    if (descriptor && descriptor.set) descriptor.set.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function findSearchInput() {
    const inputs = Array.from(document.querySelectorAll("input, textarea")).filter((input) => {
      if (!(input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)) return false;
      if (input.disabled || input.readOnly || !isVisible(input)) return false;
      const type = (input.getAttribute("type") || "").toLowerCase();
      return !["password", "checkbox", "radio", "submit", "button"].includes(type);
    });
    return (
      inputs.find((input) => {
        const text = [
          input.getAttribute("placeholder") || "",
          input.getAttribute("aria-label") || "",
          input.className || "",
          input.id || "",
        ]
          .join(" ")
          .toLowerCase();
        return text.includes("search") || text.includes("token") || text.includes("address") || text.includes("ca");
      }) || inputs[0]
    );
  }

  function clickLikelySearchButton() {
    const controls = Array.from(document.querySelectorAll("button, [role='button'], a")).filter(isVisible);
    const target = controls.find((node) => {
      const text = [
        node.textContent || "",
        node.getAttribute("aria-label") || "",
        node.getAttribute("title") || "",
        node.className || "",
      ]
        .join(" ")
        .toLowerCase();
      return text.includes("search") || text.includes("token") || text.includes("ca");
    });
    if (target instanceof HTMLElement) target.click();
  }

  function tryFillGmgnSearch(searchText) {
    const input = findSearchInput();
    if (!input) {
      clickLikelySearchButton();
      return false;
    }
    input.focus();
    nativeSetInputValue(input, searchText);
    input.select?.();
    return true;
  }

  function panelStyles() {
    return `
      position: fixed;
      z-index: 2147483647;
      right: 18px;
      top: 84px;
      width: 330px;
      box-sizing: border-box;
      padding: 14px;
      border: 1px solid #f59e0b;
      border-radius: 8px;
      background: #071014;
      color: #e5edf3;
      font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      box-shadow: 0 10px 34px rgba(0,0,0,.38);
    `;
  }

  function createGmgnPanel(rule) {
    const old = document.getElementById("alpha-radar-gmgn-og-panel");
    if (old) old.remove();

    const panel = document.createElement("div");
    panel.id = "alpha-radar-gmgn-og-panel";
    panel.setAttribute("style", panelStyles());
    const candidates = Array.isArray(rule.candidates) ? rule.candidates : [];
    const candidateHtml = candidates.length
      ? `<div style="margin-top:10px;border-top:1px solid #1f2937;padding-top:8px;">
          ${candidates
            .map(
              (candidate, index) => `
                <div style="display:grid;grid-template-columns:18px 1fr auto;gap:6px;align-items:center;margin:5px 0;color:#cbd5e1;">
                  <span style="color:#f59e0b;">${index + 1}</span>
                  <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(
                    `${candidate.symbol || "--"} / ${candidate.name || "--"}`
                  )}</span>
                  <button data-alpha-candidate="${index}" style="cursor:pointer;background:#111827;color:#e5edf3;border:1px solid #334155;border-radius:5px;padding:2px 6px;">open</button>
                </div>`
            )
            .join("")}
        </div>`
      : "";

    panel.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;">
        <strong style="color:#f59e0b;font-size:15px;">CZ OG</strong>
        <button data-alpha-close style="cursor:pointer;background:#111827;color:#9ca3af;border:1px solid #334155;border-radius:6px;padding:2px 7px;">x</button>
      </div>
      <div style="font-size:18px;font-weight:800;margin-bottom:4px;">${escapeHtml(rule.symbol || rule.searchText || "OG")}</div>
      <div style="word-break:break-all;color:#a7f3d0;margin-bottom:8px;">${escapeHtml(rule.token || "No CA matched yet")}</div>
      <div style="color:#94a3b8;margin-bottom:10px;">${escapeHtml(rule.note || "Mapped CZ reply token")}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button data-alpha-fill style="cursor:pointer;background:#f59e0b;color:#0b0f13;border:0;border-radius:6px;padding:7px 9px;font-weight:800;">Fill ${escapeHtml(
          rule.searchText || rule.symbol || "OG"
        )}</button>
        <button data-alpha-open style="cursor:pointer;background:#0f172a;color:#e5edf3;border:1px solid #334155;border-radius:6px;padding:7px 9px;">Open token</button>
        <button data-alpha-copy style="cursor:pointer;background:#0f172a;color:#e5edf3;border:1px solid #334155;border-radius:6px;padding:7px 9px;">Copy CA</button>
      </div>
      ${candidateHtml}
    `;

    panel.querySelector("[data-alpha-close]")?.addEventListener("click", () => panel.remove());
    panel.querySelector("[data-alpha-fill]")?.addEventListener("click", () => tryFillGmgnSearch(rule.searchText || rule.symbol || rule.token));
    panel.querySelector("[data-alpha-open]")?.addEventListener("click", () => window.location.assign(rule.tokenUrl || gmgnSearchUrl(rule.chain, rule.searchText || rule.symbol || "")));
    panel.querySelector("[data-alpha-copy]")?.addEventListener("click", () => setClipboard(rule.token || rule.searchText || ""));
    panel.querySelectorAll("[data-alpha-candidate]").forEach((button) => {
      button.addEventListener("click", () => {
        const index = Number(button.getAttribute("data-alpha-candidate"));
        const candidate = candidates[index];
        if (candidate?.token) window.location.assign(gmgnTokenUrl({ chain: "bsc", token: candidate.token }));
      });
    });
    document.body.appendChild(panel);
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function showXToast(message) {
    const toast = document.createElement("div");
    toast.setAttribute(
      "style",
      [
        "position:fixed",
        "z-index:2147483647",
        "right:18px",
        "bottom:18px",
        "max-width:360px",
        "padding:12px 14px",
        "border:1px solid #f59e0b",
        "border-radius:8px",
        "background:#071014",
        "color:#e5edf3",
        "font:13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        "box-shadow:0 10px 34px rgba(0,0,0,.38)",
      ].join(";")
    );
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 6000);
  }

  function stableHash(text) {
    let hash = 2166136261;
    const input = String(text || "");
    for (let index = 0; index < input.length; index += 1) {
      hash ^= input.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
  }

  function accountVariants(account) {
    const clean = String(account || "").replace(/^@+/, "").toLowerCase();
    return clean ? [clean, `@${clean}`] : [];
  }

  function watchedAccountPattern() {
    const escaped = WATCH_ACCOUNTS.map((account) => String(account || "").replace(/^@+/, "").toLowerCase())
      .filter(Boolean)
      .map((account) => account.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return escaped.length ? escaped.join("|") : "cz_binance";
  }

  function aliasMatchesMonitorText(lower, alias) {
    const text = String(alias?.text || "").toLowerCase();
    if (!text) return false;
    if (alias.requiresSquare && !(lower.includes("binance square") || lower.includes("币安广场"))) return false;
    if (/^[a-z0-9_]+$/i.test(text)) {
      return new RegExp(`(^|[^a-z0-9_@])@?${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}([^a-z0-9_]|$)`, "i").test(lower);
    }
    return lower.includes(text);
  }

  function accountFromMonitorText(text) {
    const value = String(text || "");
    const lower = value.toLowerCase();
    for (const account of WATCH_ACCOUNTS) {
      const clean = String(account || "").replace(/^@+/, "").toLowerCase();
      if (!clean) continue;
      if (lower.includes(clean) || lower.includes(`@${clean}`)) return clean;
      const urlPattern = new RegExp(`(?:x|twitter)\\.com/${clean.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/status/\\d+`, "i");
      if (urlPattern.test(value)) return clean;
    }
    for (const [account, aliases] of Object.entries(WATCH_SOURCE_ALIASES)) {
      if (aliases.some((alias) => aliasMatchesMonitorText(lower, alias))) return account;
    }
    return "";
  }

  function statusIdFromText(text, account = "") {
    const value = String(text || "");
    const accountPattern = account
      ? String(account).replace(/^@+/, "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
      : watchedAccountPattern();
    const direct = value.match(new RegExp(`(?:x|twitter)\\.com/(?:${accountPattern})/status/(\\d+)`, "i"));
    if (direct) return direct[1];
    const generic = value.match(/\/status\/(\d{8,})/i);
    return generic ? generic[1] : "";
  }

  function looksLikeWatchedMonitorText(text) {
    const value = compactText(text);
    if (value.length < 8) return false;
    const lower = value.toLowerCase();
    if (accountFromMonitorText(value)) return true;
    if ((lower.includes("twitter") || lower.includes("推特") || lower.includes("监控")) && WATCH_ACCOUNTS.some((account) => lower.includes(account.toLowerCase()))) return true;
    return new RegExp(`(?:x|twitter)\\.com/(?:${watchedAccountPattern()})/status/\\d+`, "i").test(value);
  }

  function manualRuleFromGmgnText(text) {
    const account = accountFromMonitorText(text);
    const statusId = statusIdFromText(text, account);
    if (!statusId) return null;
    return RULES.find((rule) => rule.statusId === statusId && (!account || String(rule.author || "").toLowerCase() === account)) || null;
  }

  function shortestUsefulMonitorText(text) {
    const source = compactText(text);
    const lines = String(text || "")
      .split(/\r?\n/)
      .map(compactText)
      .filter(Boolean);
    const watchedLines = lines.filter(looksLikeWatchedMonitorText);
    if (watchedLines.length) return watchedLines.sort((a, b) => a.length - b.length)[0];
    return source.slice(0, 1200);
  }

  function compactText(text) {
    return String(text || "")
      .replace(/https?:\/\/\S+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function termClean(value) {
    return String(value || "")
      .replace(/^[$#@]+/, "")
      .replace(/[^\p{L}\p{N}_-]+/gu, "")
      .trim();
  }

  function isUsefulTerm(value) {
    const term = termClean(value);
    if (!term) return false;
    if (/^0x[a-f0-9]{40}$/i.test(term)) return true;
    if (term.length < 2 || term.length > 32) return false;
    if (/^\d+$/.test(term)) return false;
    return !STOP_TERMS.has(term.toLowerCase());
  }

  function extractTerms(text) {
    const source = compactText(text);
    const terms = [];
    const push = (value) => {
      const term = termClean(value);
      if (isUsefulTerm(term) && !terms.some((existing) => existing.toLowerCase() === term.toLowerCase())) {
        terms.push(term);
      }
    };

    for (const match of source.matchAll(/0x[a-fA-F0-9]{40}/g)) push(match[0]);
    for (const match of source.matchAll(/[$#]([A-Za-z][A-Za-z0-9_]{1,20})/g)) push(match[1]);
    for (const match of source.matchAll(/[“"']([^“”"']{2,32})[”"']/g)) push(match[1]);
    for (const match of source.matchAll(/\b[A-Z][A-Z0-9_]{1,12}\b/g)) push(match[0]);
    for (const match of source.matchAll(/\b[A-Za-z][A-Za-z0-9_-]{2,20}\b/g)) push(match[0]);
    for (const match of source.matchAll(/[\u4e00-\u9fff]{2,8}/g)) push(match[0]);

    return terms.slice(0, MAX_TERMS);
  }

  function xhrJson(url) {
    return new Promise((resolve, reject) => {
      try {
        if (typeof GM_xmlhttpRequest === "function") {
          GM_xmlhttpRequest({
            method: "GET",
            url,
            headers: { Accept: "application/json" },
            timeout: 8000,
            onload: (response) => {
              try {
                resolve(JSON.parse(response.responseText || "{}"));
              } catch (error) {
                reject(error);
              }
            },
            onerror: reject,
            ontimeout: () => reject(new Error("timeout")),
          });
          return;
        }
      } catch (_error) {
        // Fall through to fetch.
      }
      fetch(url, { headers: { Accept: "application/json" } })
        .then((response) => response.json())
        .then(resolve, reject);
    });
  }

  function bscLaunchpadBonus(pair) {
    const text = [pair.dexId, pair.labels?.join(" "), pair.url].join(" ").toLowerCase();
    if (text.includes("flap")) return 24;
    if (text.includes("four") || text.includes("4meme")) return 20;
    if (text.includes("pancake")) return 14;
    return 0;
  }

  function scorePair(pair, term) {
    const base = pair.baseToken || {};
    const symbol = String(base.symbol || "");
    const name = String(base.name || "");
    const termLower = term.toLowerCase();
    const symbolLower = symbol.toLowerCase();
    const nameLower = name.toLowerCase();
    const liquidity = Number(pair.liquidity?.usd || 0);
    const fdv = Number(pair.fdv || pair.marketCap || 0);
    const createdAt = Number(pair.pairCreatedAt || 0);
    const ageHours = createdAt > 0 ? (Date.now() - createdAt) / 3600000 : 9999;
    let score = 0;
    if (symbolLower === termLower) score += 80;
    else if (nameLower === termLower) score += 70;
    else if (symbolLower.includes(termLower)) score += 45;
    else if (nameLower.includes(termLower)) score += 35;
    if (ageHours <= 1) score += 24;
    else if (ageHours <= 6) score += 18;
    else if (ageHours <= 24) score += 12;
    else if (ageHours <= MAX_AGE_HOURS) score += 6;
    if (liquidity >= MIN_LIQUIDITY_USD) score += 12;
    if (fdv > 0 && fdv <= 2_000_000) score += 8;
    score += bscLaunchpadBonus(pair);
    return score;
  }

  function normalizePair(pair, term) {
    const base = pair.baseToken || {};
    return {
      term,
      score: scorePair(pair, term),
      token: String(base.address || ""),
      symbol: String(base.symbol || ""),
      name: String(base.name || ""),
      dexId: String(pair.dexId || ""),
      url: String(pair.url || ""),
      liquidityUsd: Number(pair.liquidity?.usd || 0),
      fdv: Number(pair.fdv || pair.marketCap || 0),
      pairCreatedAt: Number(pair.pairCreatedAt || 0),
    };
  }

  async function searchDexscreenerTerm(term) {
    if (/^0x[a-f0-9]{40}$/i.test(term)) {
      return [
        {
          term,
          score: 120,
          token: term,
          symbol: term,
          name: "Direct CA",
          dexId: "manual",
          url: "",
          liquidityUsd: 0,
          fdv: 0,
          pairCreatedAt: 0,
        },
      ];
    }
    const payload = await xhrJson(`${DEXSCREENER_SEARCH_URL}?q=${encodeURIComponent(term)}`);
    const pairs = Array.isArray(payload?.pairs) ? payload.pairs : [];
    return pairs
      .filter((pair) => String(pair.chainId || "").toLowerCase() === "bsc")
      .map((pair) => normalizePair(pair, term))
      .filter((candidate) => candidate.token && candidate.score >= 45)
      .sort((a, b) => b.score - a.score)
      .slice(0, MAX_CANDIDATES);
  }

  async function findRelatedOgFromText(text) {
    const terms = extractTerms(text);
    const candidates = [];
    for (const term of terms) {
      try {
        candidates.push(...(await searchDexscreenerTerm(term)));
      } catch (_error) {
        // Continue with the next term; X should not freeze because one lookup fails.
      }
    }
    const deduped = [];
    const seen = new Set();
    for (const candidate of candidates.sort((a, b) => b.score - a.score)) {
      const key = candidate.token.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      deduped.push(candidate);
    }
    return { terms, candidates: deduped.slice(0, MAX_CANDIDATES) };
  }

  function triggerRule(rule, reason) {
    if (hasFired(rule.id)) return;
    rememberFired(rule.id);
    savePending(rule, reason);
    showXToast(`CZ reply matched: ${rule.symbol} -> GMGN BSC`);
    openTab(gmgnTokenUrl(rule));
  }

  function showGmgnSearchResult(rule, reason) {
    savePending(rule, reason);
    createGmgnPanel({
      ...rule,
      searchText: rule.searchText || rule.symbol || rule.token,
      tokenUrl: rule.token ? gmgnTokenUrl(rule) : gmgnSearchUrl(rule.chain, rule.searchText || rule.symbol || ""),
      savedAt: Date.now(),
    });
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      clickLikelySearchButton();
      if (tryFillGmgnSearch(rule.searchText || rule.symbol || rule.token) || attempts >= 20) {
        window.clearInterval(timer);
      }
    }, 350);
  }

  function activateManualRuleOnGmgn(rule, reason) {
    if (hasFired(rule.id)) return;
    rememberFired(rule.id);
    showGmgnSearchResult(rule, reason);
    showXToast(`GMGN CZ monitor matched: ${rule.symbol} -> BSC OG`);
  }

  async function triggerDynamicStatus(statusId, text, reason) {
    const account = accountFromMonitorText(text) || "watched";
    const dynamicId = `${account}_dynamic_${statusId}`;
    if (hasFired(dynamicId)) return;
    if (recentlyChecked(dynamicId) || activeDynamicLookups.has(dynamicId)) return;
    activeDynamicLookups.add(dynamicId);
    markChecked(dynamicId);
    try {
      const result = await findRelatedOgFromText(text);
      const best = result.candidates[0];
      if (!best) {
        showXToast(`CZ detected, no BSC OG match yet. Terms: ${result.terms.slice(0, 4).join(", ") || "none"}`);
        return;
      }
      rememberFired(dynamicId);
      const rule = {
        id: dynamicId,
        author: account,
        statusId,
        symbol: best.symbol || best.term,
        token: best.token,
        chain: "bsc",
        sourceUrl: `https://x.com/${account}/status/${statusId}`,
        searchText: best.symbol || best.term,
        note: `Auto matched from CZ text: ${best.term} / score ${best.score}`,
        candidates: result.candidates,
      };
      savePending(rule, reason);
      showXToast(`${account} dynamic OG matched: ${rule.symbol} -> GMGN BSC`);
      if (location.hostname === "gmgn.ai") showGmgnSearchResult(rule, reason);
      if (AUTO_OPEN_WHEN_MATCHED) openTab(gmgnTokenUrl(rule));
    } finally {
      activeDynamicLookups.delete(dynamicId);
    }
  }

  async function triggerGmgnMonitorText(rawText, reason) {
    const text = shortestUsefulMonitorText(rawText);
    if (!looksLikeWatchedMonitorText(text)) return;

    const manualRule = manualRuleFromGmgnText(text);
    if (manualRule) {
      activateManualRuleOnGmgn(manualRule, reason);
      return;
    }

    const account = accountFromMonitorText(text) || "watched";
    const statusId = statusIdFromText(text, account) || stableHash(text);
    const dynamicId = `gmgn_${account}_monitor_${statusId}`;
    if (hasFired(dynamicId) || recentlyChecked(dynamicId)) return;
    await triggerDynamicStatus(statusId, text, reason);
  }

  function collectWatchedTextsFromPayload(value, out = []) {
    if (out.length >= 12 || value == null) return out;
    if (typeof value === "string") {
      if (looksLikeWatchedMonitorText(value)) out.push(value);
      return out;
    }
    if (Array.isArray(value)) {
      for (const item of value) collectWatchedTextsFromPayload(item, out);
      return out;
    }
    if (typeof value !== "object") return out;

    const record = value;
    const authorText = compactText(
      [
        record.account,
        record.username,
        record.screen_name,
        record.author,
        record.authorName,
        record.displayName,
        record.nickname,
        record.platform,
        record.source,
        record.user?.username,
        record.user?.screen_name,
        record.user?.name,
        record.user?.displayName,
      ].join(" ")
    );
    const contentText = compactText(
      [
        record.text,
        record.full_text,
        record.content,
        record.title,
        record.description,
        record.url,
        record.link,
        record.tweet_url,
        record.square_url,
        record.post_url,
      ].join(" ")
    );
    const combined = compactText(`${authorText} ${contentText}`);
    if (looksLikeWatchedMonitorText(combined)) out.push(combined);

    for (const item of Object.values(record)) collectWatchedTextsFromPayload(item, out);
    return out;
  }

  function watchedTextsFromRawResponse(raw) {
    const text = String(raw || "");
    const accountPattern = watchedAccountPattern();
    if (
      !accountFromMonitorText(text) &&
      !new RegExp(`${accountPattern}|@(?:${accountPattern})|x\\.com/(?:${accountPattern})|twitter\\.com/(?:${accountPattern})`, "i").test(text)
    ) {
      return [];
    }
    try {
      return collectWatchedTextsFromPayload(JSON.parse(text));
    } catch (_error) {
      return [text.slice(0, 2500)];
    }
  }

  function handleGmgnPayloadText(raw, reason) {
    for (const text of watchedTextsFromRawResponse(raw)) {
      triggerGmgnMonitorText(text, reason);
    }
  }

  function installGmgnNetworkHooks() {
    if (window.__alphaRadarGmgnHooksInstalled) return;
    window.__alphaRadarGmgnHooksInstalled = true;

    const originalFetch = window.fetch;
    if (typeof originalFetch === "function") {
      window.fetch = async function alphaRadarFetch(...args) {
        const response = await originalFetch.apply(this, args);
        try {
          response
            .clone()
            .text()
            .then((text) => handleGmgnPayloadText(text, "gmgn_fetch_response"))
            .catch(() => {});
        } catch (_error) {
          // Some responses cannot be cloned; DOM observer still covers alerts.
        }
        return response;
      };
    }

    const OriginalXHR = window.XMLHttpRequest;
    if (typeof OriginalXHR === "function") {
      const originalOpen = OriginalXHR.prototype.open;
      const originalSend = OriginalXHR.prototype.send;
      OriginalXHR.prototype.open = function alphaRadarXhrOpen(method, url, ...rest) {
        this.__alphaRadarUrl = String(url || "");
        return originalOpen.call(this, method, url, ...rest);
      };
      OriginalXHR.prototype.send = function alphaRadarXhrSend(...args) {
        this.addEventListener("load", () => {
          try {
            if (typeof this.responseText === "string") handleGmgnPayloadText(this.responseText, "gmgn_xhr_response");
          } catch (_error) {
            // Ignore binary/cross-origin response bodies.
          }
        });
        return originalSend.apply(this, args);
      };
    }
  }

  function scanGmgnMonitorDom(root = document) {
    const selector = [
      "[role='dialog']",
      "[class*='toast' i]",
      "[class*='alert' i]",
      "[class*='notify' i]",
      "[class*='notification' i]",
      "[class*='twitter' i]",
      "[class*='tweet' i]",
      "[class*='monitor' i]",
      "[class*='message' i]",
      "article",
      "li",
    ].join(",");
    const nodes = [];
    if (root instanceof Element && root.matches(selector)) nodes.push(root);
    nodes.push(...Array.from(root.querySelectorAll?.(selector) || []));
    for (const node of nodes.slice(-250)) {
      const text = node.innerText || node.textContent || "";
      if (!looksLikeWatchedMonitorText(text)) continue;
      triggerGmgnMonitorText(text, "gmgn_dom_monitor");
    }
  }

  function installGmgnDomWatcher() {
    scanGmgnMonitorDom();
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node instanceof Element) scanGmgnMonitorDom(node);
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    window.setInterval(() => scanGmgnMonitorDom(), GMGN_MONITOR_SCAN_INTERVAL_MS);
  }

  function runGmgnHelper() {
    installGmgnNetworkHooks();
    installGmgnDomWatcher();

    const pending = gmGet(STORAGE_KEY, null);
    if (!pending || Date.now() - Number(pending.savedAt || 0) > PENDING_MAX_AGE_MS) {
      gmDelete(STORAGE_KEY);
      return;
    }
    createGmgnPanel(pending);
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (tryFillGmgnSearch(pending.searchText || pending.symbol || pending.token) || attempts >= 20) {
        window.clearInterval(timer);
      }
    }, 500);

    window.addEventListener("keydown", (event) => {
      if (event.ctrlKey && event.altKey && event.code === "KeyO") {
        triggerRule(RULES[0], "manual_hotkey");
      }
    });
  }

  if (location.hostname === "gmgn.ai") {
    runGmgnHelper();
  }
})();
