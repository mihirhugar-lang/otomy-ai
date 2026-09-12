import puppeteer from '@cloudflare/puppeteer';

const MAX_BYTES = 4 * 1024 * 1024;
const HEADERS = {
  'Cache-Control': 'no-store, private',
  'X-Content-Type-Options': 'nosniff',
  'Content-Type': 'application/json',
};
const error = (status, message) => new Response(JSON.stringify({error: message}), {status, headers: HEADERS});

// Also used by the local browser tests. No cookies, site navigation, scripts,
// remote resources, saved sessions, report logs, or PDF storage are involved.
export async function renderPdf(page, html) {
  await page.setJavaScriptEnabled(false);
  await page.setRequestInterception(true);
  page.on('request', request => request.abort());
  await page.setViewport({width: 1440, height: 1000, deviceScaleFactor: 1});
  await page.emulateMediaType('print');
  page.setDefaultTimeout(20000);
  await page.setContent(html, {waitUntil: 'load', timeout: 20000});
  return page.pdf({format: 'A4', landscape: true, preferCSSPageSize: true,
    printBackground: true, displayHeaderFooter: false, scale: 1, timeout: 30000});
}

export async function cleanDocument(html) {
  const clean = new HTMLRewriter()
    .on('script, iframe, frame, frameset, object, embed, base, link, meta, form, input, button, textarea, select', {
      element(element) { element.remove(); },
    })
    .on('*', {element(element) {
      for (const [name] of element.attributes) {
        if (/^on/i.test(name) || ['src','srcset','href','action','formaction','poster','ping','background','http-equiv'].includes(name.toLowerCase())) {
          element.removeAttribute(name);
        }
      }
    }})
    .transform(new Response(html)).text();
  // A separate outer document keeps the restrictive policy ahead of any input.
  return '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; font-src \'none\'; base-uri \'none\'; form-action \'none\'"></head><body>'+await clean+'</body></html>';
}

export default {
  async fetch(request, env) {
    if (request.method !== 'POST' || new URL(request.url).pathname !== '/render') return error(404, 'Not found');
    if (!request.headers.get('content-type')?.startsWith('text/html')) return error(415, 'HTML report required');
    if (Number(request.headers.get('content-length')) > MAX_BYTES) return error(413, 'Report is too large. Choose a shorter date range.');
    let browser;
    let expiry;
    try {
      const reader = request.body?.getReader();
      if (!reader) return error(400, 'Empty report');
      let size = 0;
      const chunks = [];
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) {await reader.cancel();return error(413, 'Report is too large. Choose a shorter date range.');}
        chunks.push(value);
      }
      const html = await new Blob(chunks).text();
      if (html.length < 30) return error(400, 'Empty report');
      const document = await cleanDocument(html);
      try {
        browser = await puppeteer.launch(env.BROWSER);
      } catch (cause) {
        // Free accounts permit one new browser every 20 seconds. A single
        // bounded retry waits without holding an open, billable browser.
        const message=String(cause?.message);
        if (!/429|rate.?limit|too many/i.test(message) || /today|daily|time limit/i.test(message)) throw cause;
        await new Promise(resolve=>setTimeout(resolve,21000));
        browser = await puppeteer.launch(env.BROWSER);
      }
      // A hard session deadline bounds free browser-time usage on failures.
      expiry = setTimeout(() => {browser.close().catch(() => {});}, 45000);
      const page = await browser.newPage();
      const pdf = await renderPdf(page, document);
      return new Response(pdf, {headers: {...HEADERS, 'Content-Type': 'application/pdf',
        'Content-Disposition': 'attachment; filename="Otomy-report.pdf"'}});
    } catch (cause) {
      // Never expose browser diagnostics: they can contain report content.
      if (/today|daily|time limit/i.test(String(cause?.message))) {
        return error(429, 'The free daily PDF allowance is exhausted. It resets at midnight UTC. No paid upgrade has been made.');
      }
      if (/rate.?limit|too many|429/i.test(String(cause?.message))) {
        return error(429, 'PDF service is busy. Wait one minute, then tap Print / PDF again.');
      }
      return error(503, 'PDF rendering is temporarily unavailable or the free daily allowance is exhausted. Please try later.');
    } finally {
      clearTimeout(expiry);
      if (browser) {try {await browser.close();} catch {}}
    }
  },
};
