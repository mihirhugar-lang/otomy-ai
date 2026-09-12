const ISSUER = 'https://otomy.cloudflareaccess.com';
// Public Access audience identifiers, not credentials. Bound to each host.
export const AUDIENCES = {
  'otomy.ai': '86f6440c52f26a2def8939fe79069246b7025050ce486ecb3441c9278e01a274',
  'www.otomy.ai': '86f6440c52f26a2def8939fe79069246b7025050ce486ecb3441c9278e01a274',
  'otomy-ai.pages.dev': '863d5216bf6b59e121db3381270a827febae6c6f2ed46db7bed31f0f7cf31efa',
};
let cachedKeys;
let keysUntil = 0;
const decode = value => Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0));
const jsonPart = value => JSON.parse(new TextDecoder().decode(decode(value)));
const deny = (status, message) => new Response(JSON.stringify({error:message}), {
  status, headers:{'Content-Type':'application/json','Cache-Control':'no-store, private','X-Content-Type-Options':'nosniff'},
});

export async function verifyAccess(token, audience, getKeys = async () => {
  if (!cachedKeys || Date.now() >= keysUntil) {
    const response = await fetch(ISSUER+'/cdn-cgi/access/certs');
    if (!response.ok) throw new Error('Access unavailable');
    cachedKeys = (await response.json()).keys;
    keysUntil = Date.now()+300000;
  }
  return cachedKeys;
}) {
  try {
    if (!token || token.length > 16384) return false;
    const parts = token.split('.');
    if (parts.length !== 3) return false;
    const header = jsonPart(parts[0]), claims = jsonPart(parts[1]);
    const now = Date.now()/1000;
    if (header.alg !== 'RS256' || claims.iss !== ISSUER || !Number.isFinite(claims.exp) || claims.exp <= now ||
        (claims.nbf !== undefined && (!Number.isFinite(claims.nbf) || claims.nbf > now)) ||
        !(Array.isArray(claims.aud) ? claims.aud : [claims.aud]).includes(audience)) return false;
    const jwk = (await getKeys()).find(key => key.kid === header.kid && key.kty === 'RSA');
    if (!jwk) {keysUntil=0;return false;}
    const key = await crypto.subtle.importKey('jwk',jwk,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
    return await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,decode(parts[2]),new TextEncoder().encode(parts[0]+'.'+parts[1]));
  } catch {return false;}
}

export async function onRequest(context) {
  const {request,env} = context;
  const url = new URL(request.url), audience = AUDIENCES[url.hostname];
  if (!audience) return deny(403,'Forbidden');
  if (request.method !== 'POST') return deny(405,'Use Print / PDF inside Otomy.');
  if (request.headers.get('origin') !== url.origin) return deny(403,'Forbidden');
  if (!request.headers.get('content-type')?.startsWith('text/html')) return deny(415,'HTML report required');
  if (!(await verifyAccess(request.headers.get('cf-access-jwt-assertion'),audience))) return deny(401,'Please sign in to Otomy again, then retry.');
  if (!env.OTOMY_PDF) return deny(503,'PDF renderer is not configured yet.');
  // Deliberately strip cookies and credentials before invoking the private Worker.
  try {
    const headers = new Headers({'Content-Type':'text/html; charset=utf-8'});
    const length = request.headers.get('content-length');
    if (length) headers.set('content-length',length);
    return await env.OTOMY_PDF.fetch(new Request('https://private-pdf/render', {method:'POST',headers,body:request.body,duplex:'half'}));
  } catch {return deny(503,'PDF renderer is temporarily unavailable. Please retry.');}
}
