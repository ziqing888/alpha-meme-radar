const REPORT_KEY = "alpha-radar-report-latest.json";
const MAX_PLAIN_BYTES = 20 * 1024 * 1024;

function corsHeaders(extra = {}) {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Content-Encoding",
    "Cache-Control": "no-store",
    ...extra,
  };
}

function json(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: corsHeaders({ "Content-Type": "application/json; charset=utf-8" }),
  });
}

function looksLikeJsonObject(text) {
  const trimmed = text.trim();
  return trimmed.startsWith("{") && trimmed.endsWith("}");
}

async function gunzipToText(raw) {
  const ds = new DecompressionStream("gzip");
  const stream = new Blob([raw]).stream().pipeThrough(ds);
  return await new Response(stream).text();
}

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}

export async function onRequestGet(context) {
  const { env } = context;
  try {
    const text = await env.REPORT_KV.get(REPORT_KEY);
    if (text) {
      return new Response(text, {
        status: 200,
        headers: corsHeaders({ "Content-Type": "application/json; charset=utf-8" }),
      });
    }
    try {
      const fallback = await env.ASSETS.fetch(new URL("/report.json", context.request.url));
      if (fallback.ok) {
        return new Response(await fallback.arrayBuffer(), {
          status: 200,
          headers: corsHeaders({ "Content-Type": "application/json; charset=utf-8" }),
        });
      }
    } catch (_) {}
    return json(503, { ok: false, step: "report_lookup", error: "report not uploaded" });
  } catch (error) {
    return json(503, { ok: false, step: "cloud_report_api", error: error.message || String(error) });
  }
}

export async function onRequestPost(context) {
  const { request, env } = context;
  try {
    const expected = env.ALPHA_REPORT_WRITE_TOKEN;
    const actual = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
    if (!expected || actual !== expected) {
      return json(401, { ok: false, step: "auth", error: "unauthorized" });
    }

    const encoding = String(request.headers.get("content-encoding") || "").toLowerCase();
    const raw = await request.arrayBuffer();
    const updated_at = new Date().toISOString();

    let text;
    if (encoding === "gzip") {
      text = await gunzipToText(raw);
    } else {
      text = new TextDecoder().decode(raw);
    }
    if (!looksLikeJsonObject(text)) {
      return json(400, { ok: false, step: "validate", error: "invalid_report_shape" });
    }
    const bytes = new TextEncoder().encode(text).byteLength;
    if (bytes > MAX_PLAIN_BYTES) {
      return json(413, { ok: false, step: "validate", error: "report_too_large_for_pages_kv", bytes, max: MAX_PLAIN_BYTES });
    }
    await env.REPORT_KV.put(REPORT_KEY, text);
    return json(200, { ok: true, persisted: true, pathname: REPORT_KEY, updated_at, bytes });
  } catch (error) {
    return json(503, { ok: false, step: "cloud_report_api", error: error.message || String(error) });
  }
}
