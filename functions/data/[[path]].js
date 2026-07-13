// Serve /data/* live from the R2 bucket (binding OTOMY_DATA) instead of from
// bundled static files. Keeps the dashboard data always-current (the 10-min sync
// writes R2) with no site redeploy, and stays behind Cloudflare Access (Access runs
// at the edge before this Function). Frontend keeps fetching /data/... unchanged.
export async function onRequestGet(context) {
  const { params, env } = context;
  const segments = params.path; // catch-all: array of path segments after /data/
  const key = Array.isArray(segments) ? segments.join("/") : String(segments || "");
  if (!key) return new Response("Not found", { status: 404 });

  const object = await env.OTOMY_DATA.get(key);
  if (object === null) {
    return new Response("Not found", { status: 404 });
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  // Data changes every ~10 min; never let a stale copy be cached.
  headers.set("cache-control", "no-store");
  if (key.endsWith(".json")) headers.set("content-type", "application/json");

  return new Response(object.body, { headers });
}
