function corsHeaders(extra = {}) {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "public, max-age=3600",
    ...extra,
  };
}

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: corsHeaders() });
}

export async function onRequestGet() {
  return new Response(JSON.stringify({ ok: false, error: "monitor_stream_retired" }), {
    status: 410,
    headers: corsHeaders({ "Content-Type": "application/json; charset=utf-8" }),
  });
}
