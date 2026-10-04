function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "public, max-age=3600",
    "Content-Type": "application/json; charset=utf-8",
  };
}

function json(status, payload) {
  return new Response(JSON.stringify(payload), { status, headers: corsHeaders() });
}

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}

export async function onRequestGet() {
  return json(410, { ok: false, error: "batch_quote_endpoint_retired" });
}
