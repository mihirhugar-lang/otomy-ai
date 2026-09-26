let _managementLoadSeq=0;
let _managementReports=[];

function _managementUnavailableModel(area,message){
  const issue={level:'danger',title:`${area} source is unavailable`,detail:message||'The source did not load, so this control area cannot be relied on.',report:{title:`${area} source availability`,note:'Read-only management pack source check.',columns:['Source','Error'],rows:[[area,message||'Unavailable']]}};
  return {status:'Source unavailable',issues:[issue],checks:[{name:`${area} source loaded`,passed:false}],critical:1,review:0,passed:0};
}

function _managementGSTModel(compliance,from,to){
  if(!compliance)return _managementUnavailableModel('GST','Canonical compliance data is unavailable.');
  const source=compliance.checks||{},warnings=source.warnings||[];
  const checks=[
    {name:'Daily sales totals reconcile',passed:Boolean(source.daily_sales_reconcile)},
    {name:'Daily GST totals reconcile',passed:Boolean(source.daily_tax_reconcile)},
    {name:'Company GSTIN is valid',passed:Boolean(source.valid_company_gstin)},
  ];
  const issues=[];
  const failed=checks.filter(check=>!check.passed);
  if(failed.length)issues.push({level:'danger',title:'GST canonical controls failed',detail:failed.map(check=>check.name).join(', '),report:{title:'GST canonical control failures',note:`Selected period: ${from} to ${to}.`,columns:['Control','Status'],rows:checks.map(check=>[check.name,check.passed?'Passed':'Failed'])}});
  if(warnings.length)issues.push({level:'warning',title:`${warnings.length} GST or master-data warning${warnings.length===1?' needs':'s need'} review`,detail:'Review the canonical engine warnings before filing or CA handoff.',report:{title:'GST and master-data warnings',note:`Selected period: ${from} to ${to}.`,columns:['No.','Warning'],rows:warnings.map((warning,index)=>[index+1,warning])}});
  const critical=issues.filter(issue=>issue.level==='danger').length,review=issues.filter(issue=>issue.level==='warning').length;
  return {status:critical?'GST controls failed':review?'Review GST warnings':'GST controls clear',issues,checks,critical,review,passed:checks.filter(check=>check.passed).length};
}

function _managementClosingView(model,applicable){
  if(applicable)return model;
  const issues=(model.issues||[]).filter(issue=>!String(issue.title||'').startsWith('Selected range is not'));
  const checks=(model.checks||[]).filter(check=>!String(check.name||'').startsWith('MTD or full'));
  const critical=issues.filter(issue=>issue.level==='danger').length,review=issues.filter(issue=>issue.level==='warning').length;
  return {...model,issues,checks,critical,review,passed:checks.filter(check=>check.passed).length,status:critical?'Source mismatch / unavailable':review?'Review available controls':'Use MTD / Last Month'};
}

function _managementConsolidateModules(modules){
  const unique=new Map();
  modules.forEach(module=>(module.model.issues||[]).forEach(issue=>{
    const key=`${issue.level}|${issue.title}`,current=unique.get(key);
    if(current){if(!current.areas.includes(module.name))current.areas.push(module.name);}
    else unique.set(key,{...issue,areas:[module.name]});
  }));
  const findings=[...unique.values()].sort((a,b)=>(a.level==='danger'?0:1)-(b.level==='danger'?0:1)||a.title.localeCompare(b.title));
  return {findings,critical:findings.filter(issue=>issue.level==='danger').length,review:findings.filter(issue=>issue.level==='warning').length,passed:modules.reduce((sum,module)=>sum+Number(module.model.passed||0),0),checks:modules.reduce((sum,module)=>sum+(module.model.checks||[]).length,0)};
}

function _managementPackModel(input){
  const {control,compliance,book,boulders,machines,errors,from,to}=input;
  const business=control?_dailyControlModel(control):_managementUnavailableModel('Owner dashboard',errors.control);
  business.status=business.critical?'Action required':business.review?'Review business controls':'Business controls clear';
  const gst=_managementGSTModel(compliance,from,to);
  const closingErrors=[];
  if(errors.compliance)closingErrors.push({source:'Canonical compliance',message:errors.compliance});
  if(errors.control)closingErrors.push({source:'Dashboard closing data',message:errors.control});
  if(errors.book)closingErrors.push({source:'Cash and bank books',message:errors.book});
  if(errors.master)closingErrors.push({source:'Customer/vendor closing balances',message:errors.master});
  const closing=_managementClosingView(_caClosingModel(compliance,control,book,closingErrors,from,to),_caClosingRangeState(from,to).passed);
  let audit;
  if(!compliance)audit=_managementUnavailableModel('Audit readiness',errors.compliance);
  else{
    const auditErrors=[];
    if(errors.control)auditErrors.push({source:'Dashboard closing data',message:errors.control});
    if(errors.master)auditErrors.push({source:'Customer/vendor closing balances',message:errors.master});
    audit=_caAuditReadinessModel(compliance,control,auditErrors,from,to);
  }
  const operationErrors=[];
  if(errors.control)operationErrors.push(`Dashboard sales source is unavailable: ${errors.control}`);
  if(errors.boulders)operationErrors.push(`Boulder input source is unavailable: ${errors.boulders}`);
  if(errors.machines)operationErrors.push(`Machinery and fuel source is unavailable: ${errors.machines}`);
  const operationSource=machines?_operationsControlSource(machines,from,to):null;
  const operations=_operationsControlModel(control,boulders,operationSource,operationErrors,from,to);
  const modules=[
    {key:'business',name:'Owner Dashboard',model:business},
    {key:'gst',name:'GST Controls',model:gst},
    {key:'closing',name:'Cash, Bank & Closing',model:closing},
    {key:'audit',name:'Audit Readiness',model:audit},
    {key:'operations',name:'Operations',model:operations},
  ];
  const consolidated=_managementConsolidateModules(modules),summary=control?.summary||{},totals=compliance?.totals||{},op=operations.metrics||{};
  return {...consolidated,modules,from,to,status:consolidated.critical?'Attention required':consolidated.review?'Review required':'Automated controls clear',metrics:{sales:_num(summary.sales),expenses:_num(summary.expenses),profit:_num(summary.selected_period_profit_director_adjusted??summary.profit),cash:_num(summary.cash_balance_office),bank:_num(summary.bank_balance),receivables:_num(summary.receivables),payables:_num(summary.payables),salesTonnes:_num(summary.sales_qty_mt),taxableSales:_num(totals.taxable_sales),outputGST:_num(totals.output_tax),boulderTonnes:_num(op.boulderTonnes),soldInputPct:op.soldInputPct,machineHours:_num(op.measuredHours),fuelLitres:_num(op.fuelLitres),fuelPerHour:op.litresPerHour,inputLessSold:_num(op.inputLessSold)},closing,audit,operations};
}

function printManagementFinding(index){
  const report=_managementReports[index];
  if(!report){flash('Management finding details are not ready',false);return;}
  const columns=report.columns||[],heading=columns.map(value=>`<th>${esc(value)}</th>`).join(''),rows=(report.rows||[]).map(row=>`<tr>${row.map(value=>`<td>${esc(value)}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${Math.max(1,columns.length)}" class="empty">No supporting rows</td></tr>`,landscape=columns.length>5?'<style media="print">@page{size:A4 landscape;margin:5mm}</style>':'';
  runPrint(report.title,`${landscape}<div class="print-ph"><h1>CrusherOps — ${esc(report.title)}</h1><p>${esc(report.note||'Read-only management evidence')} &nbsp;|&nbsp; Printed: ${pdfGeneratedStamp()}</p></div><div class="card"><div class="report-table-wrap"><table class="report-table"><thead><tr>${heading}</tr></thead><tbody>${rows}</tbody></table></div><div class="metric-note">Read-only evidence. Corrections must be made in Loctell ERP or the authoritative external source.</div></div>`,'report-print');
}

function renderManagementPack(model){
  const target=document.getElementById('management-pack');if(!target)return;
  target.dataset.rangeFrom=model.from;target.dataset.rangeTo=model.to;_managementReports=model.findings.map(issue=>issue.report||null);
  const m=model.metrics,statusClass=model.critical?'danger':model.review?'warning':'good',ratio=value=>value==null?'—':fmtNum(value,2);
  const moduleCards=model.modules.map(module=>{const item=module.model,cls=item.critical?'danger':item.review?'warning':'good';return `<div class="management-module ${cls}"><h4>${esc(module.name)}</h4><strong>${esc(item.status||(!item.critical&&!item.review?'Controls clear':'Review'))}</strong><span>${item.critical||0} critical · ${item.review||0} review · ${item.passed||0}/${(item.checks||[]).length} checks</span></div>`;}).join('');
  const findings=model.findings.length?model.findings.map((issue,index)=>`<div class="daily-control-item ${issue.level}${issue.report?' has-report':''}"><span class="daily-control-severity">${issue.level==='danger'?'Critical':'Review'}</span><div class="daily-control-copy"><strong>${esc(issue.title)}</strong><span>${esc(issue.areas.join(', '))} · ${esc(issue.detail)}</span></div>${issue.report?`<button class="print-btn no-print daily-control-report-btn" type="button" onclick="printManagementFinding(${index})">Details PDF</button>`:''}</div>`).join(''):`<div class="daily-control-item good"><span class="daily-control-severity">Clear</span><div class="daily-control-copy"><strong>No exception found in the consolidated automated controls</strong><span>Manual evidence and CA sign-off remain required.</span></div></div>`;
  const bookRows=(model.closing.bookRows||[]).map(row=>`<tr><td>${esc(row.name)}</td><td>${fmtINR(row.opening)}</td><td>${fmtINR(row.totalIn)}</td><td>${fmtINR(row.totalOut)}</td><td>${fmtINR(row.closing)}</td><td class="${row.available&&row.passed?'ca-closing-ok':'ca-closing-review'}">${!row.available?'Unavailable':row.passed?'Matched':fmtINR(row.difference)}</td></tr>`).join('');
  const inventory=model.audit.inventory||{},manual=(model.audit.manual||[]).map(label=>`<div class="audit-manual-item"><strong>${esc(label)}</strong><span>Manual evidence</span></div>`).join('');
  const fuel=model.operations.ledger||{},fuelEquation=fuel.ready?(fuel.matches?'Matched':'Mismatch'):'Unavailable';
  target.innerHTML=`<div class="daily-control-head"><div><h3>Owner &amp; CA Management Control Centre</h3><div class="daily-control-subtitle">One read-only consolidation of existing verified controls for ${esc(model.from)} to ${esc(model.to)}. Nothing is edited, posted or locked here.</div></div><div class="daily-control-actions"><span class="daily-control-readonly">Read only</span><button class="print-btn no-print" type="button" onclick="printPageReport('Owner & CA Management Control Centre','section-management')">Management PDF</button></div></div><div class="daily-control-summary"><div class="daily-control-stat ${statusClass} ca-closing-status"><span>Overall status</span><strong>${esc(model.status)}</strong></div><div class="daily-control-stat danger"><span>Unique critical</span><strong>${model.critical}</strong></div><div class="daily-control-stat warning"><span>Unique review</span><strong>${model.review}</strong></div><div class="daily-control-stat good"><span>Automated checks</span><strong>${model.passed}/${model.checks}</strong></div></div><div class="management-module-grid">${moduleCards}</div><div class="ca-closing-balance-grid"><div class="ca-closing-balance"><span>Gross sales</span><strong>${fmtINR(m.sales)}</strong></div><div class="ca-closing-balance"><span>Operating expenses</span><strong>${fmtINR(m.expenses)}</strong></div><div class="ca-closing-balance"><span>Operating result</span><strong>${fmtINR(m.profit)}</strong></div><div class="ca-closing-balance"><span>Sales tonnes</span><strong>${fmtNum(m.salesTonnes,2)} MT</strong></div><div class="ca-closing-balance"><span>Cash</span><strong>${fmtINR(m.cash)}</strong></div><div class="ca-closing-balance"><span>Bank</span><strong>${fmtINR(m.bank)}</strong></div><div class="ca-closing-balance"><span>Receivables</span><strong>${fmtINR(m.receivables)}</strong></div><div class="ca-closing-balance"><span>Payables</span><strong>${fmtINR(m.payables)}</strong></div><div class="ca-closing-balance"><span>Output GST</span><strong>${fmtINR(m.outputGST)}</strong></div><div class="ca-closing-balance"><span>Boulder input</span><strong>${fmtNum(m.boulderTonnes,2)} MT</strong></div><div class="ca-closing-balance"><span>Measured machine hours</span><strong>${fmtNum(m.machineHours,2)}</strong></div><div class="ca-closing-balance"><span>Diesel issued</span><strong>${fmtNum(m.fuelLitres,2)} L</strong></div></div><div class="management-grid"><div class="ca-closing-panel"><h4>Cash &amp; Bank — Opening + In − Out = Closing</h4><div class="report-table-wrap"><table class="ca-closing-table"><thead><tr><th>Book</th><th>Opening</th><th>In</th><th>Out</th><th>Closing</th><th>Status</th></tr></thead><tbody>${bookRows||'<tr><td colspan="6" class="empty">Cash and bank books unavailable</td></tr>'}</tbody></table></div></div><div class="ca-closing-panel"><h4>GST &amp; Audit evidence</h4><table class="ca-closing-table"><tbody><tr><td>Taxable sales</td><td><strong>${fmtINR(m.taxableSales)}</strong></td></tr><tr><td>Output GST</td><td><strong>${fmtINR(m.outputGST)}</strong></td></tr><tr><td>Sales vouchers</td><td><strong>${inventory.sales??'—'}</strong></td></tr><tr><td>Expense vouchers</td><td><strong>${inventory.expenses??'—'}</strong></td></tr><tr><td>Money-movement receipts</td><td><strong>${inventory.receipts??'—'}</strong></td></tr><tr><td>ERP snapshot anchors excluded</td><td><strong>${inventory.excludedSnapshots??'—'}</strong></td></tr></tbody></table></div><div class="ca-closing-panel"><h4>Operations movement &amp; fuel</h4><table class="ca-closing-table"><tbody><tr><td>Sold output ÷ boulder input</td><td><strong>${m.soldInputPct==null?'—':`${fmtNum(m.soldInputPct,2)}%`}</strong></td></tr><tr><td>Input less sold output</td><td><strong>${fmtNum(m.inputLessSold,2)} MT</strong></td></tr><tr><td>Fuel / measured machine hour</td><td><strong>${m.fuelPerHour==null?'—':`${ratio(m.fuelPerHour)} L`}</strong></td></tr><tr><td>Fuel opening</td><td><strong>${fuel.opening==null?'—':`${fmtNum(fuel.opening,2)} L`}</strong></td></tr><tr><td>Fuel received / issued</td><td><strong>${fmtNum(fuel.received,2)} / ${fmtNum(fuel.issued,2)} L</strong></td></tr><tr><td>Fuel equation</td><td class="${fuel.matches?'ca-closing-ok':'ca-closing-review'}"><strong>${fuelEquation}</strong></td></tr></tbody></table></div><div class="ca-closing-panel"><h4>Manual evidence before owner / CA sign-off</h4><div class="audit-manual-list">${manual||'<div class="empty">Checklist unavailable</div>'}</div></div></div><div class="ops-control-boundary"><strong>System boundary:</strong> This page consolidates existing control models; it does not recalculate or override their source values. Credit sales remain receivables, ERP Snapshot anchors remain excluded from money movement, sold/input is not physical production recovery, and bank-statement/portal/physical evidence remains manual until connected.</div><details class="daily-control-details" ${model.findings.length?'open':''}><summary>${model.findings.length?`Review ${model.findings.length} unique management finding${model.findings.length===1?'':'s'}`:'Show consolidated result'}</summary><div class="daily-control-list">${findings}</div></details>`;
}

async function loadManagement(){
  const target=document.getElementById('management-pack'),from=document.getElementById('mgmt-from')?.value,to=document.getElementById('mgmt-to')?.value;
  if(!target)return;if(!from||!to||from>to){target.innerHTML='<div class="empty">Choose a valid management date range.</div>';return;}
  const seq=++_managementLoadSeq;target.innerHTML='<div class="empty">Loading owner, CA, GST, cash/bank and operations controls…</div>';
  const settle=promise=>promise.then(value=>({value})).catch(error=>({error})),machineQuery=!isStaticSnapshotHost()?`?from_date=${encodeURIComponent(from)}&to_date=${encodeURIComponent(to)}`:'';
  const [controlResult,complianceResult,bookResult,boulderResult,machineResult]=await Promise.all([
    settle(cachedJson(_dashCache.control,`${from}|${to}`,`/api/dashboard/control?from_date=${from}&to_date=${to}`)),
    settle(_complianceDataset(from,to)),settle(_buildCashBook(from,to)),settle(api(`/api/boulders/?from_date=${from}&to_date=${to}`)),settle(api(`/api/machines/summary${machineQuery}`)),
  ]);
  if(seq!==_managementLoadSeq)return;
  let control=controlResult.value?.summary?controlResult.value:null,compliance=complianceResult.value||null,book=bookResult.value?.cash&&bookResult.value?.bank?bookResult.value:null,boulders=Array.isArray(boulderResult.value)?boulderResult.value:null,machines=machineResult.value||null;
  const message=result=>result.error?.message||'Source unavailable',errors={control:control?null:message(controlResult),compliance:compliance?null:message(complianceResult),book:book?null:message(bookResult),boulders:boulders?null:message(boulderResult),machines:machines?null:message(machineResult),master:null};
  if(control){try{await _attachDashboardMasterRows(control,from,to);}catch(error){errors.master=error.message||String(error);}}
  if(seq!==_managementLoadSeq)return;
  renderManagementPack(_managementPackModel({control,compliance,book,boulders,machines,errors,from,to}));
}
