import { list, put } from "@vercel/blob";
import { gunzipSync } from "node:zlib";
import { staticReportUrl } from "./static-report-origin.mjs";

const REPORT_PATH = process.env.ALPHA_REPORT_BLOB_PATH || "alpha-radar-report-latest.json";
let volatileReport = null;
let volatileReportUpdatedAt = null;

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const body = Buffer.concat(chunks);
  const encoding = String(req.headers["content-encoding"] || "").toLowerCase();
  return (encoding === "gzip" ? gunzipSync(body) : body).toString("utf8");
}

function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Authorization, Content-Type");
  res.end(JSON.stringify(payload));
}

async function latestBlobUrl() {
  const result = await list({ prefix: REPORT_PATH, limit: 20 });
  const exact = result.blobs.find((blob) => blob.pathname === REPORT_PATH) || result.blobs[0];
  return exact?.url || null;
}

async function staticReport() {
  const url = staticReportUrl(process.env.ALPHA_REPORT_STATIC_ORIGIN);
  if (!url) return null;
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) return null;
  return response.text();
}

function sendRawReport(res, text) {
  res.statusCode = 200;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.end(text);
}

export default async function handler(req, res) {
  try {
    if (req.method === "OPTIONS") {
      sendJson(res, 204, {});
      return;
    }

    if (req.method === "GET") {
      if (volatileReport) {
        sendRawReport(res, volatileReport);
        return;
      }
      if (process.env.ALPHA_REPORT_USE_BLOB !== "1") {
        const fallback = await staticReport();
        if (fallback) {
          sendRawReport(res, fallback);
          return;
        }
      }
      let url = null;
      try {
        url = await latestBlobUrl();
      } catch {
        const fallback = await staticReport();
        if (fallback) {
          sendRawReport(res, fallback);
          return;
        }
      }
      if (!url) {
        const fallback = await staticReport();
        if (fallback) {
          res.statusCode = 200;
          res.setHeader("Content-Type", "application/json; charset=utf-8");
          res.setHeader("Cache-Control", "no-store");
          res.setHeader("Access-Control-Allow-Origin", "*");
          res.end(fallback);
          return;
        }
        sendJson(res, 503, { ok: false, step: "report_lookup", error: "report not uploaded" });
        return;
      }
      const response = await fetch(url, { cache: "no-store" });
      const text = await response.text();
      res.statusCode = response.ok ? 200 : response.status;
      res.setHeader("Content-Type", "application/json; charset=utf-8");
      res.setHeader("Cache-Control", "no-store");
      res.setHeader("Access-Control-Allow-Origin", "*");
      res.end(text);
      return;
    }

    if (req.method === "POST") {
      const expected = process.env.ALPHA_REPORT_WRITE_TOKEN;
      const actual = (req.headers.authorization || "").replace(/^Bearer\s+/i, "");
      if (!expected || actual !== expected) {
        sendJson(res, 401, { ok: false, step: "auth", error: "unauthorized" });
        return;
      }
      const body = await readBody(req);
      JSON.parse(body);
      volatileReport = body;
      volatileReportUpdatedAt = new Date().toISOString();
      try {
        const blob = await put(REPORT_PATH, body, {
          access: "public",
          allowOverwrite: true,
          contentType: "application/json; charset=utf-8",
        });
        sendJson(res, 200, { ok: true, persisted: true, pathname: blob.pathname, url: blob.url, updated_at: volatileReportUpdatedAt });
      } catch {
        sendJson(res, 503, { ok: false, persisted: false, mode: "memory", error: "report_not_persisted", updated_at: volatileReportUpdatedAt });
      }
      return;
    }

    sendJson(res, 405, { ok: false, error: "method_not_allowed" });
  } catch (error) {
    sendJson(res, 503, { ok: false, step: "cloud_report_api", error: error.message || String(error) });
  }
}
