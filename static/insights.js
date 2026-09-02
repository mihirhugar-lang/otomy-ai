/* Director Analytics is deliberately read-only.  It visualises existing dashboard
 * and Daily Book responses and never writes a balance, receipt, or adjustment. */
(()=>{
  const C=['#245b96','#c37a10','#2f805d','#8a4f96','#ca4b45','#3182a6','#76624c','#4d6f93'];
  const n=v=>{v=Number(v||0);return Number.isFinite(v)?v:0};
  const x=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=v=>`₹${Math.round(n(v)).toLocaleString('en-IN')}`;
  const tonne=v=>`${n(v).toFixed(1)} MT`;
  const plain=v=>n(v).toFixed(1);
  const point=(label,value,kind='money')=>`<title>${x(label)}: ${kind==='tonne'?tonne(value):kind==='pct'?`${plain(value)}%`:kind==='plain'?plain(value):money(value)}</title>`;
  const shell=(title,caption,body,legend='')=>`<article class="insight-card"><h3>${x(title)}</h3><p>${x(caption)}</p>${legend?`<div class="insight-legend">${legend}</div>`:''}<div class="insight-chart">${body}</div></article>`;
  const noData=()=>'<div class="insight-empty">Chart data is not available yet.</div>';
  const legend=series=>series.map((s,i)=>`<span><i style="background:${s.color||C[i%C.length]}"></i>${x(s.name)}</span>`).join('');
  const ticks=(rows,w=600)=>{
    const wanted=[]; let last='';
    rows.forEach((r,i)=>{const d=String(r.date||'');const key=d.slice(0,7);if((key!==last||i===rows.length-1)&&d){wanted.push([i,d]);last=key;}});
    return wanted.slice(-6).map(([i,d])=>`<text class="insight-label" x="${34+i*(w-52)/Math.max(rows.length-1,1)}" y="205" text-anchor="middle">${x(d.slice(5))}</text>`).join('');
  };
  function line(rows,series){
    if(!rows.length||!series.some(s=>s.values.some(v=>n(v))))return noData();
    const w=600,h=214,l=34,r=14,t=12,b=28,iw=w-l-r,ih=h-t-b;
    const grid=[.2,.5,.8].map(p=>`<line class="insight-axis" x1="${l}" x2="${w-r}" y1="${t+ih*p}" y2="${t+ih*p}"/>`).join('');
    const paths=series.map((s,si)=>{
      const values=s.values.map(n),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1;
      const pts=values.map((v,i)=>[l+i*iw/Math.max(values.length-1,1),t+(1-(v-lo)/span)*ih]);
      const d=pts.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
      const dots=pts.map((p,i)=>`<circle class="insight-tip" cx="${p[0]}" cy="${p[1]}" r="${values.length>90?1.4:2.3}" fill="${s.color||C[si]}">${point(`${rows[i].date} · ${s.name}`,values[i],s.kind)}</circle>`).join('');
      return `<path d="${d}" fill="none" stroke="${s.color||C[si]}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${dots}`;
    }).join('');
    return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${x(series.map(s=>s.name).join(', '))} trend">${grid}${paths}${ticks(rows,w)}</svg>`;
  }
  function bars(items,kind='money',colors=C){
    const clean=items.filter(r=>n(r.value)>0).slice(0,8);if(!clean.length)return noData();
    const w=600,h=214,l=148,r=18,t=10,row=Math.min(25,176/clean.length),max=Math.max(...clean.map(r=>n(r.value)))||1;
    return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="ranking chart">${clean.map((it,i)=>{const y=t+i*row,bw=(w-l-r)*n(it.value)/max;return `<text class="insight-bar-label" x="${l-8}" y="${y+row*.67}" text-anchor="end">${x(String(it.label).slice(0,21))}</text><rect x="${l}" y="${y+3}" width="${bw}" height="${Math.max(row-7,5)}" rx="4" fill="${colors[i%colors.length]}"><title>${x(it.label)}: ${kind==='tonne'?tonne(it.value):kind==='pct'?`${plain(it.value)}%`:money(it.value)}</title></rect>`;}).join('')}</svg>`;
  }
  function donut(items){
    const clean=items.filter(r=>n(r.value)>0).slice(0,7),total=clean.reduce((s,r)=>s+n(r.value),0);if(!total)return noData();
    const cx=300,cy=103,R=70,ri=43;let a=-Math.PI/2;
    const paths=clean.map((it,i)=>{const next=a+Math.PI*2*n(it.value)/total,large=next-a>Math.PI?1:0;const p1=[cx+R*Math.cos(a),cy+R*Math.sin(a)],p2=[cx+R*Math.cos(next),cy+R*Math.sin(next)],q2=[cx+ri*Math.cos(next),cy+ri*Math.sin(next)],q1=[cx+ri*Math.cos(a),cy+ri*Math.sin(a)];const path=`M${p1} A${R} ${R} 0 ${large} 1 ${p2} L${q2} A${ri} ${ri} 0 ${large} 0 ${q1} Z`;a=next;return `<path d="${path}" fill="${C[i%C.length]}"><title>${x(it.label)}: ${money(it.value)}</title></path>`;}).join('');
    const labels=clean.map((it,i)=>`<text class="insight-label" x="${i<4?18:405}" y="${36+(i%4)*28}"><tspan fill="${C[i%C.length]}">●</tspan> ${x(String(it.label).slice(0,22))}</text>`).join('');
    return `<svg viewBox="0 0 600 214" role="img" aria-label="expense composition">${paths}<circle cx="${cx}" cy="${cy}" r="${ri-1}" fill="#fffdf8"/>${labels}</svg>`;
  }
  function bridge(items){
    const total=Math.max(...items.map(r=>Math.abs(n(r.value))),1),w=600,h=214,base=180,bw=80,gap=55;
    return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="profit bridge"><line class="insight-axis" x1="28" x2="572" y1="${base}" y2="${base}"/>${items.map((it,i)=>{const v=n(it.value),bh=138*Math.abs(v)/total,y=v>=0?base-bh:base;const bx=46+i*(bw+gap);return `<rect x="${bx}" y="${y}" width="${bw}" height="${bh}" rx="8" fill="${it.color||C[i]}"><title>${x(it.label)}: ${money(v)}</title></rect><text class="insight-bar-label" x="${bx+bw/2}" y="201" text-anchor="middle">${x(it.label)}</text>`;}).join('')}</svg>`;
  }
  function monthlyRows(rows){return rows.map(r=>({...r,date:String(r.date||'')})).filter(r=>r.date).sort((a,b)=>a.date.localeCompare(b.date));}
  function rolling(values,days){return values.map((v,i)=>{const a=Math.max(0,i-days+1),slice=values.slice(a,i+1);return slice.reduce((s,x)=>s+n(x),0)/Math.max(slice.length,1);});}
  async function ledgerRows(from,to){
    const start=new Date(`${from}T00:00:00`),end=new Date(`${to}T00:00:00`),requests=[];let y=start.getFullYear(),m=start.getMonth()+1;
    while(y<end.getFullYear()||(y===end.getFullYear()&&m<=end.getMonth()+1)){requests.push(api(`/api/dashboard/ledger-view?year=${y}&month=${m}`));m++;if(m===13){m=1;y++;}}
    const pages=await Promise.all(requests);return monthlyRows(pages.flatMap(p=>p?.rows||[]).filter(r=>String(r.date||'')>=from&&String(r.date||'')<=to));
  }
  function forecast(rows){
    const history=rows.slice(-28),values=history.map(r=>n(r.sale_amount)),mean=values.reduce((s,v)=>s+v,0)/Math.max(values.length,1),spread=Math.sqrt(values.reduce((s,v)=>s+(v-mean)**2,0)/Math.max(values.length,1));
    const out=history.map(r=>({date:r.date,actual:n(r.sale_amount),base:null,low:null,high:null}));let d=new Date(`${history.at(-1)?.date||today()}T00:00:00`);
    for(let i=0;i<30;i++){d.setDate(d.getDate()+1);out.push({date:localISO(d),actual:null,base:mean,low:Math.max(0,mean-spread*.8),high:mean+spread*.8});}return out;
  }
  window.loadInsights=async function(){
    const host=document.getElementById('insights-grid');if(!host)return;
    const from=document.getElementById('insights-from')?.value||'2026-04-01',to=document.getElementById('insights-to')?.value||today();
    if(from>to){host.innerHTML='<div class="empty">Choose a valid date range.</div>';return;}
    host.innerHTML='<div class="empty">Building visual analysis…</div>';
    try{
      const [control,ledger,customers]=await Promise.all([
        api(`/api/dashboard/control?from_date=${from}&to_date=${to}`),
        ledgerRows(from,to),
        api(`/api/customers/?active_only=false&from_date=${from}&to_date=${to}&as_of=${to}`).catch(()=>[]),
      ]);
      const trend=monthlyRows(control.trend||[]),sum=control.summary||{},mix=control.mix||{};
      const daily=ledger.map(r=>({...r,repay:n(r.credit_repayment_cash)+n(r.credit_repayment_bank),spot:n(r.spot_sale_cash)+n(r.spot_sale_bank),liquid:n(r.cash_balance_office)+n(r.bank_balance)}));
      const locked=[];let lock=0;daily.forEach(r=>{lock+=n(r.credit_sale_amount)-r.repay;locked.push(lock);});
      const positives=(customers||[]).filter(r=>n(r.total_outstanding??r.outstanding??r.balance)>0).map(r=>({...r,value:n(r.total_outstanding??r.outstanding??r.balance)}));
      const ages=[['0–15 days','age_0_15'],['16–30 days','age_16_30'],['31–45 days','age_31_45'],['45+ days','age_45_plus']].map(([label,key])=>({label,value:positives.reduce((s,r)=>s+n(r[key]),0)}));
      const pmt=trend.map(r=>n(r.qty_mt)?n(r.profit)/n(r.qty_mt):0),emt=trend.map(r=>n(r.qty_mt)?n(r.expenses)/n(r.qty_mt):0),rate=trend.map(r=>n(r.qty_mt)?n(r.sales)/n(r.qty_mt):0);
      const coverage=daily.map((r,i)=>r.liquid/Math.max(rolling(daily.map(x=>n(x.expenses)),7)[i],1));
      const recovery=daily.map(r=>n(r.boulder_input_mt)?100*n(r.qty_mt)/n(r.boulder_input_mt):0);
      const netCredit=n(sum.net_credit_change_for_liquidity);
      const fc=forecast(daily),forecastRows=fc.map(r=>({date:r.date}));
      host.innerHTML=[
        shell('Sales & Production Trend','Relative movement of sales and sold tonnes.',line(trend,[{name:'Sales',values:trend.map(r=>r.sales),kind:'money'},{name:'Sold tonnes',values:trend.map(r=>r.qty_mt),color:C[2],kind:'tonne'}]),legend([{name:'Sales'},{name:'Sold tonnes',color:C[2]}])),
        shell('Unit Economics Trend','Realised rate, profit per tonne and expense per tonne.',line(trend,[{name:'Rate / MT',values:rate},{name:'Profit / MT',values:pmt,color:C[2]},{name:'Expense / MT',values:emt,color:C[4]}]),legend([{name:'Rate / MT'},{name:'Profit / MT',color:C[2]},{name:'Expense / MT',color:C[4]}])),
        shell('Sales Mix','Relative FYTD sales contribution by material.',bars((mix.materials||[]).map(r=>({label:r.material||'Material',value:r.amount}))),''),
        shell('Profit to Liquidity Bridge','Economic profit compared with credit money still locked outside.',bridge([{label:'Sales',value:sum.sales,color:C[0]},{label:'Expense',value:-n(sum.expenses),color:C[4]},{label:'Profit',value:sum.profit,color:C[2]},{label:'Credit locked',value:-netCredit,color:C[1]}])),
        shell('Liquid Money Movement','Office cash and bank balance from the Daily Book.',line(daily,[{name:'Office cash',values:daily.map(r=>r.cash_balance_office),kind:'money'},{name:'Bank balance',values:daily.map(r=>r.bank_balance),color:C[0],kind:'money'}]),legend([{name:'Office cash'},{name:'Bank balance',color:C[0]}])),
        shell('Collections versus Operating Expense','Daily cash/UPI collections against money paid for operations.',line(daily,[{name:'Spot + credit collection',values:daily.map(r=>r.spot+r.repay),kind:'money'},{name:'Operating expense',values:daily.map(r=>r.expenses),color:C[4],kind:'money'}]),legend([{name:'Collections'},{name:'Operating expense',color:C[4]}])),
        shell('Liquidity Coverage Trend','Liquid money measured against the recent operating run-rate.',line(daily,[{name:'Operating-day coverage',values:coverage,kind:'plain'}]),legend([{name:'Operating-day coverage'}])),
        shell('Credit Created versus Repaid','New customer credit compared with older credit released.',line(daily,[{name:'Credit sold',values:daily.map(r=>r.credit_sale_amount),kind:'money'},{name:'Credit repaid',values:daily.map(r=>r.repay),color:C[2],kind:'money'}]),legend([{name:'Credit sold'},{name:'Credit repaid',color:C[2]}])),
        shell('Credit Locked Movement','Cumulative movement of funds still locked with customers.',line(daily,[{name:'Net credit locked',values:locked,kind:'money'}]),legend([{name:'Net credit locked'}])),
        shell('Receivable Aging Shape','Positive customer balances only; credit balances never reduce this view.',bars(ages),''),
        shell('Customer Concentration','The largest positive receivable exposures.',bars(positives.sort((a,b)=>b.value-a.value).slice(0,8).map(r=>({label:r.name,value:r.value}))),''),
        shell('Expense Composition','FYTD operating-expense distribution by category.',donut((mix.expenses||[]).map(r=>({label:r.category||'Expense',value:r.amount}))),''),
        shell('Boulder Recovery Trend','Sold tonnes as a share of boulder input on operating days.',line(daily,[{name:'Recovery',values:recovery,kind:'pct'}]),legend([{name:'Recovery'}])),
        shell('Forward Sales Outlook','Observed recent run-rate with a conservative and upside band.',line(forecastRows,[{name:'Observed sales',values:fc.map(r=>r.actual??r.base),kind:'money'},{name:'Conservative',values:fc.map(r=>r.low??r.actual),color:C[1],kind:'money'},{name:'Upside',values:fc.map(r=>r.high??r.actual),color:C[2],kind:'money'}]),legend([{name:'Observed run-rate'},{name:'Conservative',color:C[1]},{name:'Upside',color:C[2]}])),
      ].join('');
    }catch(e){host.innerHTML=`<div class="empty">Insights could not load: ${x(e?.message||e)}</div>`;}
  };
})();
