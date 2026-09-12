// Serve /data/* live from the R2 bucket (binding OTOMY_DATA) instead of from
// bundled static files. Keeps dashboard data always-current (the 10-min sync writes
// R2) with no site redeploy. Frontend keeps fetching /data/... unchanged.
//
// Security: only serve on the real, Cloudflare-Access-protected hostnames. The
// per-deployment *.pages.dev preview URLs are NOT behind Access, so serving live
// financial data there would leak it — refuse those.
import { AUDIENCES, verifyAccess } from "../api/report/pdf.js";

const ALLOWED_HOSTS = new Set(Object.keys(AUDIENCES));

export async function onRequestGet(context) {
  const { params, env, request } = context;
  const url = new URL(request.url);

  const audience = AUDIENCES[url.hostname];
  if (!ALLOWED_HOSTS.has(url.hostname) ||
      !(await verifyAccess(request.headers.get("cf-access-jwt-assertion"), audience))) {
    return new Response("Forbidden", { status: 403 });
  }

  const segments = params.path; // catch-all: array of path segments after /data/
  const key = Array.isArray(segments) ? segments.join("/") : String(segments || "");
  if (!key) return new Response("Not found", { status: 404 });
  // Engine controls and rollback packs are operational records, never
  // dashboard data.  Keep them unreachable even to an authenticated browser.
  if (key.startsWith("control/") || key.startsWith("recovery/")) {
    return new Response("Not found", { status: 404 });
  }

  const object = await env.OTOMY_DATA.get(key);
  if (object === null) {
    return new Response("Not found", { status: 404 });
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  // Data changes every sync; never let a browser, proxy, or edge cache reuse an old object.
  headers.set("cache-control", "no-store, no-cache, max-age=0, must-revalidate");
  headers.set("pragma", "no-cache");
  headers.set("expires", "0");
  if (key.endsWith(".json")) headers.set("content-type", "application/json");

  return new Response(object.body, { headers });
}
