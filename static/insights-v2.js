/* Read-only Director Analytics renderer with explicit, unit-aware axes. */
(()=>{
  const COLORS=['#245b96','#c37a10','#2f805d','#8a4f96','#ca4b45','#3182a6','#76624c','#4d6f93'];
  const N=v=>{v=Number(v||0);return Number.isFinite(v)?v:0;};
  const defined=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
  const E=(tag,attrs={},text='')=>{const e=document.createElement(tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));if(text)e.textContent=text;return e;};
  const S=(tag,attrs={},text='')=>{const e=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,String(v)));if(text)e.textContent=text;return e;};
  const value=(v,kind='money')=>kind==='tonne'?N(v).toFixed(1)+' MT':kind==='pct'?N(v).toFixed(1)+'%':kind==='days'?N(v).toFixed(1)+' days':'₹'+Math.round(N(v)).toLocaleString('en-IN');
  const tick=(v,kind='money')=>{const a=Math.abs(N(v));if(kind==='tonne')return a>=1000?(N(v)/1000).toFixed(1)+'k':String(Math.round(N(v)));if(kind==='pct')return Math.round(N(v))+'%';if(kind==='days')return N(v).toFixed(N(v)%1?1:0);if(a>=100000)return '₹'+(N(v)/100000).toFixed(a>=1000000?0:1)+'L';if(a>=1000)return '₹'+(N(v)/1000).toFixed(0)+'k';return '₹'+Math.round(N(v));};
  const ordered=rows=>rows.map(r=>Object.assign({},r,{date:String(r.date||'')})).filter(r=>r.date).sort((a,b)=>a.date.localeCompare(b.date));
  const legend=series=>{const e=E('div',{class:'insight-legend'});series.forEach((s,i)=>{const z=E('span'),dot=E('i',{style:'background:'+(s.color||COLORS[i%COLORS.length])});z.append(dot,document.createTextNode(s.name));e.append(z);});return e;};
  const card=(host,title,caption,series,rows)=>{const article=E('article',{class:'insight-card'}),head=E('h3',{},title),note=E('p',{},caption),chart=E('div',{class:'insight-chart'});article.append(head,note,legend(series),chart);host.append(article);drawLine(chart,rows,series);};
  const domain=(series,kind)=>{const a=series.flatMap(s=>s.values).filter(defined).map(N);let lo=Math.min(0,...a),hi=Math.max(0,...a);if(kind==='pct')lo=0;if(hi===lo){hi=hi||1;lo=lo===0?0:lo-1;}const p=(hi-lo)*.08;return {lo:lo-p,hi:hi+p};};
  function drawLine(host,rows,series){
    if(!rows.length||!series.some(s=>s.values.some(defined))){host.append(E('div',{class:'insight-empty'},'Chart data is not available for this period.'));return;}
    const w=640,h=218,l=58,r=58,t=12,b=38,iw=w-l-r,ih=h-t-b,left=series.filter(s=>s.axis!=='right'),right=series.filter(s=>s.axis==='right'),lk=(left[0]||{}).kind||'money',rk=(right[0]||{}).kind||'money',ld=domain(left,lk),rd=right.length?domain(right,rk):null;
    const svg=S('svg',{viewBox:'0 0 '+w+' '+h,role:'img','aria-label':series.map(s=>s.name).join(', ')+' trend'}),scale=(v,d)=>t+(1-(N(v)-d.lo)/(d.hi-d.lo))*ih;
    const axis=(d,kind,side)=>{for(let i=0;i<5;i++){const p=i/4,val=d.hi-(d.hi-d.lo)*p,y=t+ih*p;if(side==='left')svg.append(S('line',{class:'insight-axis',x1:l,x2:w-r,y1:y,y2:y}));svg.append(S('text',{class:'insight-label',x:side==='left'?l-7:w-r+7,y:y+3,'text-anchor':side==='left'?'end':'start'},tick(val,kind)));}};
    axis(ld,lk,'left');if(rd)axis(rd,rk,'right');
    const count=Math.min(rows.length,6),used=new Set();
    for(let k=0;k<count;k++){const i=Math.round(k*(rows.length-1)/Math.max(count-1,1));if(used.has(i))continue;used.add(i);const d=rows[i].date,label=rows.length>120?d.slice(0,7):d.slice(5);svg.append(S('text',{class:'insight-label',x:l+i*iw/Math.max(rows.length-1,1),y:207,'text-anchor':'middle'},label));}
    series.forEach((s,si)=>{const d=s.axis==='right'?rd:ld,pts=s.values.map((v,i)=>defined(v)?[l+i*iw/Math.max(s.values.length-1,1),scale(v,d)]:null),color=s.color||COLORS[si%COLORS.length];let segment=[];const appendSegment=()=>{if(!segment.length)return;svg.append(S('polyline',{points:segment.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' '),fill:'none',stroke:color,'stroke-width':2.5,'stroke-linejoin':'round','stroke-linecap':'round'}));segment=[];};pts.forEach((p,i)=>{if(!p){appendSegment();return;}segment.push(p);const c=S('circle',{class:'insight-tip',cx:p[0],cy:p[1],r:s.values.length>90?1.4:2.3,fill:color});c.append(S('title',{},rows[i].date+' · '+s.name+': '+value(s.values[i],s.kind)));svg.append(c);});appendSegment();});
    host.append(svg);
  }
  function bars(host,title,caption,items,kind='money'){
    const rows=items.filter(r=>N(r.value)>0).slice(0,8),article=E('article',{class:'insight-card'}),chart=E('div',{class:'insight-chart'});article.append(E('h3',{},title),E('p',{},caption),chart);host.append(article);
    if(!rows.length){chart.append(E('div',{class:'insight-empty'},'Chart data is not available for this period.'));return;}
    const w=640,h=218,l=168,r=65,t=10,row=Math.min(25,180/rows.length),max=Math.max(...rows.map(r=>N(r.value))),svg=S('svg',{viewBox:'0 0 '+w+' '+h,role:'img','aria-label':title});
    rows.forEach((it,i)=>{const y=t+i*row,bw=(w-l-r)*N(it.value)/max;svg.append(S('text',{class:'insight-bar-label',x:l-8,y:y+row*.67,'text-anchor':'end'},String(it.label).slice(0,24)));const rect=S('rect',{x:l,y:y+3,width:bw,height:Math.max(row-7,5),rx:4,fill:COLORS[i%COLORS.length]});rect.append(S('title',{},it.label+': '+value(it.value,kind)));svg.append(rect,S('text',{class:'insight-label',x:Math.min(l+bw+5,w-r+2),y:y+row*.67},tick(it.value,kind)));});chart.append(svg);
  }
  const rolling=(a,days)=>a.map((_,i)=>a.slice(Math.max(0,i-days+1),i+1).reduce((s,v)=>s+N(v),0)/Math.max(Math.min(days,i+1),1));
  async function ledgerRows(from,to){
    const a=new Date(from+'T00:00:00'),b=new Date(to+'T00:00:00'),calls=[];let y=a.getFullYear(),m=a.getMonth()+1;
    while(y<b.getFullYear()||(y===b.getFullYear()&&m<=b.getMonth()+1)){calls.push(api('/api/dashboard/ledger-view?year='+y+'&month='+m));m++;if(m===13){m=1;y++;}}
    return ordered((await Promise.all(calls)).flatMap(p=>(p&&p.rows)||[]).filter(r=>String(r.date||'')>=from&&String(r.date||'')<=to));
  }
  window.loadInsights=async function(){
    const host=document.getElementById('insights-grid'),fromEl=document.getElementById('insights-from'),toEl=document.getElementById('insights-to'),period=document.getElementById('insights-period-label');if(!host||!fromEl||!toEl)return;
    fromEl.min='2026-04-01';toEl.min='2026-04-01';const from=fromEl.value||'2026-04-01',to=toEl.value||today();if(period)period.textContent=from+' to '+to+' · verified Daily Book visual analysis';
    if(from<'2026-04-01'||from>to){host.replaceChildren(E('div',{class:'empty'},'Choose a valid period from 1 April 2026 onwards.'));return;}
    host.replaceChildren(E('div',{class:'empty'},'Building visual analysis…'));
    try{
      const result=await Promise.all([api('/api/dashboard/control?from_date='+from+'&to_date='+to),ledgerRows(from,to),api('/api/customers/?active_only=false&from_date='+from+'&to_date='+to+'&as_of='+to).catch(()=>[])]);
      const control=result[0]||{},ledger=result[1]||[],customers=result[2]||[],trend=ordered(control.trend||[]),mix=control.mix||{};
      const daily=ledger.map(r=>Object.assign({},r,{repay:N(r.credit_repayment_cash)+N(r.credit_repayment_bank),cashIn:N(r.spot_sale_cash)+N(r.credit_repayment_cash),bankIn:N(r.spot_sale_bank)+N(r.credit_repayment_bank),liquid:N(r.cash_balance_office)+N(r.bank_balance)}));
      const profit=r=>N(r.sale_amount)-N(r.expenses),creditNet=r=>N(r.credit_sale_amount)-N(r.repay),perTonne=(r,amount,liquidityOnly=false)=>liquidityOnly&&r.date<'2026-06-01'?null:(N(r.qty_mt)?N(amount)/N(r.qty_mt):null),perBoulder=(r,amount)=>N(r.boulder_input_mt)?N(amount)/N(r.boulder_input_mt):null;
      const locked=[];let running=0;daily.forEach(r=>{running+=N(r.credit_sale_amount)-r.repay;locked.push(running);});
      const rates=trend.map(r=>N(r.qty_mt)?N(r.sales)/N(r.qty_mt):0),profitMt=trend.map(r=>N(r.qty_mt)?N(r.profit)/N(r.qty_mt):0),expenseMt=trend.map(r=>N(r.qty_mt)?N(r.expenses)/N(r.qty_mt):0),coverage=daily.map((r,i)=>r.liquid/Math.max(rolling(daily.map(x=>N(x.expenses)),7)[i],1)),recovery=daily.map(r=>N(r.boulder_input_mt)?100*N(r.qty_mt)/N(r.boulder_input_mt):0);
      const positives=customers.filter(r=>N(r.total_outstanding??r.outstanding??r.balance)>0).map(r=>Object.assign({},r,{name:r.name||'Customer',value:N(r.total_outstanding??r.outstanding??r.balance)})).sort((a,b)=>b.value-a.value);
      const ages=[['0–15 days','age_0_15'],['16–30 days','age_16_30'],['31–45 days','age_31_45'],['45+ days','age_45_plus']].map(([label,key])=>({label,value:positives.reduce((s,r)=>s+N(r[key]),0)}));
      host.replaceChildren();
      // Four curves, one for each Owner Control Room column. The x-axis is
      // the selected date range; all y-axes retain their true numeric units.
      card(host,'Owner Control · Column 1','Gross Sale, Operating Expenses and Selected Period Profit use the left ₹ axis. Cash Profit ₹/T uses the right ₹/T axis (from 1 Jun).',[{name:'Gross sale',values:daily.map(r=>r.sale_amount),kind:'money'},{name:'Operating expenses',values:daily.map(r=>r.expenses),kind:'money',color:COLORS[4]},{name:'Selected period profit',values:daily.map(profit),kind:'money',color:COLORS[2]},{name:'Cash profit ₹/T',values:daily.map(r=>perTonne(r,profit(r)-creditNet(r),true)),kind:'money',axis:'right',color:COLORS[3]}],daily);
      card(host,'Owner Control · Column 2','Sales MT and Boulder Input use the left MT axis. Expense/Boulder Input and Credit Locked ₹/T use the right ₹/T axis (credit from 1 Jun).',[{name:'Sales MT',values:daily.map(r=>r.qty_mt),kind:'tonne'},{name:'Boulder input',values:daily.map(r=>r.boulder_input_mt),kind:'tonne',color:COLORS[2]},{name:'Expenses / boulder input',values:daily.map(r=>perBoulder(r,r.expenses)),kind:'money',axis:'right',color:COLORS[1]},{name:'Credit locked ₹/T',values:daily.map(r=>perTonne(r,creditNet(r),true)),kind:'money',axis:'right',color:COLORS[3]}],daily);
      card(host,'Owner Control · Column 3','All curves use the ₹/T numeric axis. Credit Sold ₹/T begins where tender-split data is available (1 Jun).',[{name:'Avg rate / tonne',values:daily.map(r=>perTonne(r,r.sale_amount)),kind:'money'},{name:'Expenses / tonne',values:daily.map(r=>perTonne(r,r.expenses)),kind:'money',color:COLORS[4]},{name:'Profit / tonne',values:daily.map(r=>perTonne(r,profit(r))),kind:'money',color:COLORS[2]},{name:'Credit sold ₹/T',values:daily.map(r=>perTonne(r,r.credit_sale_amount,true)),kind:'money',color:COLORS[3]}],daily);
      card(host,'Owner Control · Column 4','Bank Balance, Cash in Office and Credit Repayment use the left ₹ axis. Credit Repaid ₹/T uses the right ₹/T axis (from 1 Jun).',[{name:'Bank balance',values:daily.map(r=>r.bank_balance),kind:'money'},{name:'Cash in office',values:daily.map(r=>r.cash_balance_office),kind:'money',color:COLORS[1]},{name:'Credit repayment',values:daily.map(r=>r.repay),kind:'money',color:COLORS[2]},{name:'Credit repaid ₹/T',values:daily.map(r=>perTonne(r,r.repay,true)),kind:'money',axis:'right',color:COLORS[3]}],daily);
      card(host,'Daily Sales & Production','₹ sales on the left axis; sold tonnes on the right.',[{name:'Sales',values:daily.map(r=>r.sale_amount),kind:'money'},{name:'Sold tonnes',values:daily.map(r=>r.qty_mt),kind:'tonne',axis:'right',color:COLORS[2]}],daily);
      card(host,'Unit Economics Trend','All lines are ₹ per sold tonne.',[{name:'Rate / MT',values:rates,kind:'money'},{name:'Profit / MT',values:profitMt,kind:'money',color:COLORS[2]},{name:'Expense / MT',values:expenseMt,kind:'money',color:COLORS[4]}],trend);
      card(host,'Operational Throughput','Boulder input, sold tonnes and stock in plant — all in MT.',[{name:'Boulder input',values:daily.map(r=>r.boulder_input_mt),kind:'tonne'},{name:'Sold tonnes',values:daily.map(r=>r.qty_mt),kind:'tonne',color:COLORS[2]},{name:'Stock in plant',values:daily.map(r=>r.stock_in_plant_mt),kind:'tonne',color:COLORS[1]}],daily);
      card(host,'Cash & Bank Closing Balance','Closing balances from the Daily Book.',[{name:'Office cash',values:daily.map(r=>r.cash_balance_office),kind:'money'},{name:'Bank balance',values:daily.map(r=>r.bank_balance),kind:'money',color:COLORS[0]}],daily);
      card(host,'Cash & Bank Collections','Cash and bank collections against cash and bank expenses.',[{name:'Cash collected',values:daily.map(r=>r.cashIn),kind:'money'},{name:'Bank collected',values:daily.map(r=>r.bankIn),kind:'money',color:COLORS[0]},{name:'Cash expense',values:daily.map(r=>r.expense_cash),kind:'money',color:COLORS[1]},{name:'Bank expense',values:daily.map(r=>r.expense_bank),kind:'money',color:COLORS[4]}],daily);
      card(host,'Daily Revenue versus Expense','Operating revenue against total recorded expense.',[{name:'Sales',values:daily.map(r=>r.sale_amount),kind:'money'},{name:'Expense',values:daily.map(r=>r.expenses),kind:'money',color:COLORS[4]}],daily);
      card(host,'Credit Created versus Repaid','New customer credit compared with older credit released.',[{name:'Credit sold',values:daily.map(r=>r.credit_sale_amount),kind:'money'},{name:'Credit repaid',values:daily.map(r=>r.repay),kind:'money',color:COLORS[2]}],daily);
      card(host,'Credit Lock Movement','Net customer-credit movement inside the selected period.',[{name:'Net credit locked',values:locked,kind:'money'}],daily);
      card(host,'Liquidity Coverage Trend','Closing liquid money expressed as recent operating days.',[{name:'Operating-day coverage',values:coverage,kind:'days'}],daily);
      card(host,'Boulder Recovery Trend','Sold tonnes as a share of boulder input on operating days.',[{name:'Recovery',values:recovery,kind:'pct'}],daily);
      bars(host,'Sales Mix','Sales contribution by material in the selected period.',(mix.materials||[]).map(r=>({label:r.material||'Material',value:r.amount})));
      bars(host,'Expense Composition','Operating-expense distribution by category.',(mix.expenses||[]).map(r=>({label:r.category||'Expense',value:r.amount})));
      bars(host,'Customer Concentration','Largest positive customer exposures as at the selected end date.',positives);
      bars(host,'Receivable Aging Shape','Positive customer balances only.',ages);
    }catch(err){host.replaceChildren(E('div',{class:'empty'},'Insights could not load: '+(err&&err.message||err)));};
  };
})();
