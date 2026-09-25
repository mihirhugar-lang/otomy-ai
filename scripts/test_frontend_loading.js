#!/usr/bin/env node
// Exercise real HTML helpers with delayed/out-of-order responses, without private data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const paths = process.argv.slice(2);
if (!paths.length) paths.push(path.join(__dirname, '..', 'index.html'));
function part(source, from, to) {
  const start = source.indexOf(from), end = source.indexOf(to, start + from.length);
  assert(start >= 0 && end > start, `Missing helper ${from}`);
  return source.slice(start, end);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
async function verify(file) {
  const source = fs.readFileSync(file, 'utf8');
  for (const script of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
    if (script[1].trim()) new vm.Script(script[1], {filename:file});
  }
  // Phase 3 GST Control Centre stays read-only and never invents ITC/net tax.
  assert.match(source,/id="gst-monthly-control"/);
  assert.match(source,/Input tax credit and net GST payable are deliberately not estimated/);
  const gstCtx=vm.createContext({Map,Set,Date,Number,String,Math,
    fyStart:()=>'2026-04-01',today:()=>'2026-09-24',monthNow:()=>'2026-09',
    fmtINR2:v=>'INR '+Number(v||0).toFixed(2),esc:v=>String(v??''),
    _num:v=>Number(v||0),
    _complianceRound:(v,p=2)=>{const m=10**p;return Math.round(Number(v||0)*m)/m;},
    _complianceInRange:(row,f,t)=>String(row?.date||'').slice(0,10)>=f&&String(row?.date||'').slice(0,10)<=t});
  vm.runInContext(part(source,'function _validComplianceGSTIN(', 'function _warningHTML('),gstCtx);
  assert.equal(vm.runInContext('JSON.stringify(_gstMonthPeriod("2026-09"))',gstCtx),
    '{"value":"2026-09","from":"2026-09-01","to":"2026-09-24","calendarEnd":"2026-09-30","closed":false}');
  assert.equal(vm.runInContext('_gstInternalTarget("2026-12",10)',gstCtx),'2027-01-10');
  const gstFixture={totals:{sales_count:1,gross_sales:105,taxable_sales:100,igst:0,cgst:2.5,sgst:2.5,output_tax:5},
    checks:{daily_sales_reconcile:true,daily_tax_reconcile:true,valid_company_gstin:true,invalid_customer_gstin:1,missing_hsn:1,duplicate_invoice_keys:0,warnings:['No customer GSTIN is mapped; review classification.']},
    sales:[{date:'2026-08-15',invoice_no:'INV-1',customer_name:'Fixture',customer_gstin:'BADGSTIN',hsn_code:'',gross_value:105}]};
  gstCtx.gstFixture=gstFixture;
  assert.equal(vm.runInContext('_gstExceptionRows(gstFixture).length',gstCtx),2);
  const gstHtml=vm.runInContext('_gstMonthlyControlHTML(gstFixture,_gstMonthPeriod("2026-08"))',gstCtx);
  assert.match(gstHtml,/Review items<\/span><strong>3/);
  assert.match(gstHtml,/Source advisories:/);
  assert.match(gstHtml,/Needs portal 2B \+ ledgers/);
  assert.match(gstHtml,/Net GST payable<\/td><td style="text-align:right">—/);
  const snapshotSource={company:{gstin:'29AAICV4284G1ZV'},period:{fy_start:'2026-04-01'},sales:[],expenses:[],
    receipts:[{id:1,date:'2026-09-01',amount:100,mode:'Bank',reference:'UTR-1'},{id:2,date:'2026-09-01',amount:999,mode:'ERP Snapshot',reference:'Anchor'},{id:3,date:'2026-09-01',amount:0,mode:'Cash',reference:'Same-sale adjustment'}],
    vendor_payments:[],daily:[{date:'2026-09-01',gross_sales:0,output_tax:0}]};
  gstCtx.snapshotSource=snapshotSource;
  assert.equal(vm.runInContext('_sliceComplianceDataset(snapshotSource,"2026-09-01","2026-09-01").receipts.length',gstCtx),1);
  assert.equal(vm.runInContext('_sliceComplianceDataset(snapshotSource,"2026-09-01","2026-09-01").totals.receipts',gstCtx),100);
  assert.equal(vm.runInContext('_sliceComplianceDataset(snapshotSource,"2026-09-01","2026-09-01").audit_exclusions.erp_snapshot_receipts.length',gstCtx),1);
  assert.equal(vm.runInContext('_sliceComplianceDataset(snapshotSource,"2026-09-01","2026-09-01").audit_exclusions.zero_value_receipts.length',gstCtx),1);

  // Phase 6 is a read-only audit evidence check. Blank party GSTINs are valid
  // audit inputs; only populated invalid values become findings.
  assert.match(source,/id="ca-audit-readiness"/);
  assert.match(source,/The ERP dataset has no scanned-bill or attachment field/);
  const auditCtx=vm.createContext({Map,Set,Date,Number,String,Math,
    _num:v=>Number(v||0),_validComplianceGSTIN:v=>/^[0-9]{2}[A-Z0-9]{13}$/.test(String(v||'').trim().toUpperCase()),
    fmtINR:v=>'INR '+Number(v||0).toFixed(2)});
  vm.runInContext(part(source,'function _caAuditMode(', 'function printCAAuditFinding('),auditCtx);
  auditCtx.auditFixture={company:{name:'Fixture',gstin:'29AAICV4284G1ZV'},
    checks:{daily_sales_reconcile:true,daily_tax_reconcile:true,valid_company_gstin:true,duplicate_invoice_keys:0},
    sales:[{id:1,date:'2026-09-01',invoice_no:'INV-1',customer_name:'Cash Buyer',customer_gstin:'',gross_value:100}],
    expenses:[{id:2,date:'2026-09-01',category:'Fuel',description:'Diesel',vendor_name:'Supplier',vendor_gstin:'',amount:20,payment_mode:'Cash',erp_key:'EXP-2'}],
    receipts:[{id:3,date:'2026-09-01',customer_name:'Cash Buyer',customer_gstin:'',amount:100,mode:'Cash',reference:''}],
    vendor_payments:[{id:4,date:'2026-09-01',vendor_name:'Supplier',vendor_gstin:'',amount:20,mode:'Cash',reference:''}],
    daily:[{date:'2026-09-01'}],audit_exclusions:{erp_snapshot_receipts:[{id:9,date:'2026-09-01'}],zero_value_receipts:[{id:10,date:'2026-09-01'}]}};
  assert.equal(vm.runInContext('_caAuditReadinessModel(auditFixture,{},[],"2026-09-01","2026-09-01").issues.length',auditCtx),0);
  assert.equal(vm.runInContext('_caAuditReadinessModel(auditFixture,{},[],"2026-09-01","2026-09-01").passed',auditCtx),11);
  assert.equal(vm.runInContext('_caAuditReadinessModel(auditFixture,{},[],"2026-09-01","2026-09-01").inventory.excludedSnapshots',auditCtx),1);
  assert.equal(vm.runInContext('_caAuditReadinessModel(auditFixture,{},[],"2026-09-01","2026-09-01").inventory.excludedZeroReceipts',auditCtx),1);
  vm.runInContext('auditFixture.sales[0].gross_value=0;auditFixture.sales[0].customer_gstin="BADGSTIN";auditFixture.receipts[0].mode="Bank";auditFixture.expenses[0].category="Other";auditFixture.expenses[0].description="Default Ledger"',auditCtx);
  assert.equal(vm.runInContext('_caAuditReadinessModel(auditFixture,{},[],"2026-09-01","2026-09-01").status',auditCtx),'Evidence review required');
  assert(vm.runInContext('_caAuditReadinessModel(auditFixture,{},[],"2026-09-01","2026-09-01").issues.some(x=>x.title.includes("GSTIN"))',auditCtx));
  assert(vm.runInContext('_caAuditReadinessModel(auditFixture,{},[],"2026-09-01","2026-09-01").issues.some(x=>x.title.includes("bank or UPI"))',auditCtx));
  const requests = [];
  const ctx = vm.createContext({assert, Map, Set, Date, console,
    fetch: () => { const d=deferred(); requests.push(d); return d.promise; },
    fiveMinuteCacheBust:()=>1});
  vm.runInContext(part(source, 'const _archiveCache=', 'function _rowsInRange('), ctx);
  const first = vm.runInContext('Promise.all([_loadArchiveMonth("2026-08"),_loadArchiveMonth("2026-08"),_loadArchiveMonth("2026-08")])',ctx);
  assert.equal(requests.length,1,'duplicate archive GETs');
  requests[0].resolve(new Response(JSON.stringify({sales:[{id:1,amount:12}]})));
  const values=await first;
  assert.equal(values[0],values[1]);
  assert.equal(values[0].sales[0].amount,12);
  // A failed request is retried, never remembered as an empty financial range.
  const failed=vm.runInContext('_loadArchiveMonth("2026-07")',ctx);
  requests[1].resolve(new Response('',{status:503}));
  await assert.rejects(failed,/temporarily unavailable/);
  const retry=vm.runInContext('_loadArchiveMonth("2026-07")',ctx);
  requests[2].resolve(new Response('{"generation":"retry"}'));
  assert.equal((await retry).generation,'retry');
  // Clear during a slow GET; only the post-clear generation can refill memory.
  const old=vm.runInContext('_loadArchiveMonth("2026-06")',ctx);
  vm.runInContext('delete _archivePending["2026-06"]',ctx);
  const fresh=vm.runInContext('_loadArchiveMonth("2026-06")',ctx);
  requests[4].resolve(new Response('{"generation":"new"}'));
  await fresh;
  requests[3].resolve(new Response('{"generation":"old"}'));
  await old;
  assert.equal(vm.runInContext('_archiveCache["2026-06"].generation',ctx),'new');

  let rangeCalls=0;
  const rangeCtx=vm.createContext({Map,_dateMonths:()=>['2026-08'],
    _loadArchiveRange:async()=>{ if(++rangeCalls===1)throw Error('offline'); return [{sales:[{amount:12}]}]; }});
  vm.runInContext(part(source,'const OtomyDataEngine=','  clear(){')+'clear(){this._ranges.clear();}};',rangeCtx);
  await assert.rejects(vm.runInContext('OtomyDataEngine.range("a","b")',rangeCtx));
  assert.equal((await vm.runInContext('OtomyDataEngine.range("a","b")',rangeCtx))[0].sales[0].amount,12);
  assert.equal(rangeCalls,2);

  const apiRequests=[];
  const cacheCtx=vm.createContext({Map,Date,DASH_CACHE_MS:60000,isStaticSnapshotHost:()=>true,
    api:()=>{const d=deferred();apiRequests.push(d);return d.promise;}});
  vm.runInContext('const cache=new Map();'+part(source,'async function cachedJson(', 'function prefetchDashRange('),cacheCtx);
  const a=vm.runInContext('cachedJson(cache,"range","/api/fixture")',cacheCtx);
  vm.runInContext('cache.clear()',cacheCtx);
  const b=vm.runInContext('cachedJson(cache,"range","/api/fixture")',cacheCtx);
  apiRequests[1].resolve({generation:2});await b;
  apiRequests[0].resolve({generation:1});await a;
  assert.equal(vm.runInContext('cache.get("range").data.generation',cacheCtx),2);
  vm.runInContext('cache.clear()',cacheCtx);
  const c=vm.runInContext('cachedJson(cache,"range","/api/fixture")',cacheCtx);
  vm.runInContext('cache.clear()',cacheCtx);
  const d=vm.runInContext('cachedJson(cache,"range","/api/fixture")',cacheCtx);
  apiRequests[3].resolve({generation:4});await d;
  apiRequests[2].reject(Error('old error'));await assert.rejects(c);
  assert.equal(vm.runInContext('cache.get("range").data.generation',cacheCtx),4);

  // Badge + freshness polling share reads. A network failure must not masquerade
  // as a data generation or code deployment and reload the user's page.
  let time=1000000, fail=false, modified='v1';
  const urls=[];
  const badge={};
  class Clock extends Date { static now(){return time;} }
  const statusCtx=vm.createContext({Date:Clock,Map,API:'',OTOMY_APP_VERSION:'fallback',
    document:{hidden:false,getElementById:()=>badge},navigator:{onLine:true},
    isStaticSnapshotHost:()=>true,_rememberStaticSyncStamp:()=>{},flash:()=>{},
    _reloadStaticPageForSync:()=>{throw Error('unexpected reload');},
    _reloadActiveSectionAfterSync:()=>{throw Error('unexpected refresh');},
    _fetchT:async(url)=>{urls.push(url);if(fail)throw Error('offline');return url.startsWith('/?')
      ? new Response(null,{headers:{'last-modified':modified}})
      : new Response(JSON.stringify({generated_at:'2026-09-13T00:00:00Z',last_sync:'2026-09-13T00:00:00Z',version:'test'}));}});
  vm.runInContext('let _lastSeenSyncStamp=null,_syncRefreshBusy=false;'+
    part(source,'const _syncMarkerCache=','function startSyncAutoRefresh(')+
    part(source,'let _engineStatusTimer=','function startEngineStatusRefresh('),statusCtx);
  await vm.runInContext('Promise.all([checkForFreshSync(),loadEngineStatus()])',statusCtx);
  assert.equal(urls.length,3);
  assert(urls.every(url=>!url.includes('control/')&&!url.includes('${')));
  const goodStamp=vm.runInContext('_lastSeenSyncStamp',statusCtx);
  time+=15000;await vm.runInContext('checkForFreshSync()',statusCtx);
  assert.equal(urls.length,5,'HTML HEAD repeated inside one minute');
  fail=true;time+=15000;await vm.runInContext('checkForFreshSync()',statusCtx);
  assert.equal(vm.runInContext('_lastSeenSyncStamp',statusCtx),goodStamp);
  const before=urls.length;
  vm.runInContext('document.hidden=true',statusCtx);
  await vm.runInContext('Promise.all([checkForFreshSync(),loadEngineStatus()])',statusCtx);
  assert.equal(urls.length,before,'hidden tab polled');
  vm.runInContext('document.hidden=false;navigator.onLine=false',statusCtx);
  await vm.runInContext('Promise.all([checkForFreshSync(),loadEngineStatus()])',statusCtx);
  assert.equal(urls.length,before,'offline tab polled');
  fail=false;time+=60000;modified='v2';let reloaded=false;
  statusCtx._reloadStaticPageForSync=()=>{reloaded=true;return true;};
  vm.runInContext('navigator.onLine=true',statusCtx);
  await vm.runInContext('checkForFreshSync()',statusCtx);
  assert(reloaded,'Last-Modified-only deployment was not detected');

  // A Dashboard tab left open overnight keeps yesterday in its date inputs.
  // A fresh sync must advance an active relative preset (especially Today),
  // while preserving an intentionally selected custom/historical range.
  let requestedPreset='';
  const dates={
    'dash-from':{value:'2026-09-22'},
    'dash-to':{value:'2026-09-22'},
    'dash-date':{value:'2026-09-22'},
  };
  const todayButton={getAttribute:()=>"preset('dashboard','today',this)"};
  const presetBar={querySelector:()=>todayButton};
  dates['dash-from'].closest=()=>presetBar;
  const rolloverCtx=vm.createContext({document:{getElementById:id=>dates[id]},
    calcPreset:p=>{requestedPreset=p;return ['2026-09-23','2026-09-23'];}});
  vm.runInContext(part(source,'function _refreshDashboardPresetForSync(){','async function _reloadActiveSectionAfterSync(){'),rolloverCtx);
  assert.equal(vm.runInContext('_refreshDashboardPresetForSync()',rolloverCtx),true);
  assert.equal(requestedPreset,'today');
  assert.deepEqual([dates['dash-from'].value,dates['dash-to'].value,dates['dash-date'].value],
    ['2026-09-23','2026-09-23','2026-09-23']);
  presetBar.querySelector=()=>null;
  dates['dash-from'].value='2026-07-01';dates['dash-to'].value='2026-07-31';dates['dash-date'].value='2026-07-31';
  assert.equal(vm.runInContext('_refreshDashboardPresetForSync()',rolloverCtx),false);
  assert.deepEqual([dates['dash-from'].value,dates['dash-to'].value,dates['dash-date'].value],
    ['2026-07-01','2026-07-31','2026-07-31']);
  assert.match(source,/if\(section==='dashboard'\)\{_refreshDashboardPresetForSync\(\);return loadDash\(\);\}/);

  // The selected range may change while master rows are still loading.
  const attaches=[], renders=[];
  const dashCtx=vm.createContext({document:{getElementById:()=>({value:'2026-09-13'})},
    _dashCache:{control:new Map()},mtdStart:()=>'',today:()=>'',
    cachedJson:async()=>({summary:{}}),_dashUnavailable:()=>{throw Error('unavailable');},
    _attachDashboardMasterRows:()=>{const d=deferred();attaches.push(d);return d.promise;},
    renderDashSummary:()=>renders.push('render'),renderDailyControlCentre:()=>{},renderControlRoom:async()=>{}});
  vm.runInContext('let _dashLoadSeq=0,_dashRetried=false;'+part(source,'async function loadDash(){','async function loadControlRoom(){'),dashCtx);
  const older=vm.runInContext('loadDash()',dashCtx);await new Promise(setImmediate);
  const newer=vm.runInContext('loadDash()',dashCtx);await new Promise(setImmediate);
  attaches[1].resolve();await newer;attaches[0].resolve();await older;
  assert.equal(renders.length,1,'old range rendered over new selection');
  console.log(path.basename(path.dirname(file))+' frontend: syntax, archive deduplication, retry, invalidation races, polling, offline, deployment and range guards passed');
}
(async()=>{for(const file of paths)await verify(file);})().catch(e=>{console.error(e);process.exitCode=1;});
