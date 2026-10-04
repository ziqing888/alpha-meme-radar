const MONITOR_KEY = "alpha-monitor-v3-latest.json";
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

function looksLikeMonitorEnvelope(text) {
  const trimmed = text.trim();
  return trimmed.startsWith("{") && trimmed.includes('"monitor_v3"') && trimmed.endsWith("}");
}

async function gunzipToText(raw) {
  const ds = new DecompressionStream("gzip");
  const stream = new Blob([raw]).stream().pipeThrough(ds);
  return await new Response(stream).text();
}

async function readMonitorText(env) {
  if (env.MONITOR_R2) {
    const obj = await env.MONITOR_R2.get(MONITOR_KEY);
    if (obj) return await obj.text();
  }
  // legacy fallback while migrating off KV
  if (env.REPORT_KV) {
    const direct = await env.REPORT_KV.get(MONITOR_KEY);
    if (direct) return direct;
  }
  return null;
}

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}

export async function onRequestGet(context) {
  const { env } = context;
  try {
    const text = await readMonitorText(env);
    if (text) {
      return new Response(text, {
        status: 200,
        headers: corsHeaders({ "Content-Type": "application/json; charset=utf-8" }),
      });
    }
    return json(503, { ok: false, step: "monitor_lookup", error: "monitor not uploaded" });
  } catch (error) {
    return json(503, { ok: false, step: "monitor_api", error: error.message || String(error) });
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
    if (!env.MONITOR_R2) {
      return json(503, { ok: false, step: "r2_binding", error: "MONITOR_R2 not bound; enable R2 and redeploy" });
    }
    const encoding = String(request.headers.get("content-encoding") || "").toLowerCase();
    const raw = await request.arrayBuffer();
    const text = encoding === "gzip" ? await gunzipToText(raw) : new TextDecoder().decode(raw);
    if (!looksLikeMonitorEnvelope(text)) {
      return json(400, { ok: false, step: "validate", error: "invalid_monitor_shape" });
    }
    const bytes = new TextEncoder().encode(text).byteLength;
    if (bytes > MAX_PLAIN_BYTES) {
      return json(413, { ok: false, step: "validate", error: "monitor_too_large", bytes, max: MAX_PLAIN_BYTES });
    }
    // skip identical payload to save Class A writes
    const existing = await env.MONITOR_R2.get(MONITOR_KEY);
    if (existing) {
      const prev = await existing.text();
      if (prev === text) {
        return json(200, { ok: true, persisted: false, unchanged: true, pathname: MONITOR_KEY, bytes });
      }
    }
    await env.MONITOR_R2.put(MONITOR_KEY, text, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
    });
    return json(200, {
      ok: true,
      persisted: true,
      storage: "r2",
      pathname: MONITOR_KEY,
      updated_at: new Date().toISOString(),
      bytes,
    });
  } catch (error) {
    return json(503, { ok: false, step: "monitor_api", error: error.message || String(error) });
  }
}
