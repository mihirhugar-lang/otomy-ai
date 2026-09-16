# Private Otomy PDF renderer

The installed app posts the existing website report HTML/CSS to
`/api/report/pdf`. Pages verifies the signed Cloudflare Access JWT, hostname,
and same-origin POST, then invokes the `OTOMY_PDF` service binding. The Worker
has no public route, workers.dev endpoint, or version-preview URL.

The browser prints A4 landscape with CSS page size/margins and print colours.
There is no screenshot stitching or object-URL handoff. JavaScript, external
requests, navigation elements, and executable attributes in submitted reports
are disabled/removed. Reports and generated PDFs are not stored in R2, browser
storage, application logs, or caches. PDFs are held in the requesting app's
memory until its share dialog is closed. Cloudflare still handles normal
operational request metadata; the receiving app controls shared copies.

## Deployment

The Pages project's production service binding must be:
`OTOMY_PDF -> otomy-private-pdf`. Its existing `OTOMY_DATA` R2 binding is
unchanged. A Pages redeployment is needed after initially adding the binding.

`Deploy private PDF renderer` deploys Worker changes independently of the
frontend and ERP/R2 sync. The existing Cloudflare deployment secret needs
Workers Scripts Edit permission for this workflow. No Mac is required at
runtime or for GitHub deployments. Run `npm ci --prefix pdf_worker` followed
by `npm run check --prefix pdf_worker` for a local bundle check.

## Free-plan behaviour

The free browser allowance is 10 minutes/day, with one new browser every
20 seconds. One launch retry waits 21 seconds without an open browser. A
session closes in `finally` and has a 45-second deadline. Quota, authentication,
network and rendering failures show an in-app message, not a blank page.
No paid plan is enabled by this code. Limits are Cloudflare-controlled.

An iOS share request requires user activation. Fast preparation may open the
sheet immediately; otherwise the app presents a fresh `Share PDF` button.
Cancelling the sheet leaves the generated file ready to share again.

## Tests and acceptance

### Dependency security

Wrangler is pinned to 4.132.0. Its locked Miniflare tooling uses Undici 7.29.0
and Sharp 0.35.4, resolving the six reviewed tooling advisories as of
2026-09-16. These build/development dependencies are not PDF request handlers.

Two upstream `extract-zip` 2.0.1 advisories remain open:
[GHSA-jmr9-qjv8-65gv](https://github.com/advisories/GHSA-jmr9-qjv8-65gv) and
[GHSA-7pqw-9j4j-h8q3](https://github.com/advisories/GHSA-7pqw-9j4j-h8q3).
No patched release was available during this review. The dependency arrives
through `@cloudflare/puppeteer -> @puppeteer/browsers`, whose ZIP browser
installer is not used by this hosted Browser Run implementation. Do not use
it to extract untrusted archives, run a browser-download CLI from this tree,
or force npm's suggested downgrade to the obsolete Puppeteer 0.0.11 release.
The advisories are not dismissed or represented as patched.

`npm run security-check --prefix pdf_worker` checks the reviewed version floors,
installed/locked version agreement, and the actual Wrangler dry-run bundle.
It rejects runtime inclusion or external imports of `extract-zip`, the browser
installer, Sharp or Undici. Both pull-request CI and the PDF deployment workflow
run this check before deployment. It uses no Browser Run sessions or financial
input. New advisories still require review; this is not a universal security
guarantee or a replacement for Dependabot.

The 2026-09-16 upgrade produced byte-identical Worker JavaScript before/after;
renderer source, layout settings, authentication, service bindings, visibility,
and free-tier limits were unchanged.

`node scripts/test_pdf_route.mjs` verifies signatures, expiry, audience, issuer,
host/origin restrictions, private error responses and credential stripping.
`python3 scripts/verify_dashboard_parity.py --code-only` checks report routing
and byte-identical root/static HTML. Synthetic multipage visual checks cover
the report, dashboard, weekly-ticket, Daily Book and Cash Profit style families.
An actual Home Screen iPhone check remains required for native share-sheet and
real-report acceptance. Fonts and browser versions can differ across systems;
pixel-perfect equivalence with every desktop browser is not promised.

`node scripts/test_pdf_render_contract.mjs` separately verifies the existing
A4 landscape rendering options and script/network blocking with a mock page.
It is not an actual iPhone acceptance test.
