import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';

const html=await readFile(new URL('../index.html',import.meta.url),'utf8');
const start=html.indexOf('const DASHBOARD_BLOCK_TITLES=');
const end=html.indexOf('function _isPrintWeekOrMore(',start);
assert(start>0&&end>start,'block range helpers exist');
const context={
  _num:value=>Number(value)||0,
  fmtINR:value=>String(Math.round(Number(value)||0)),
  fmtINR2:value=>(Number(value)||0).toFixed(2),
  fmtNum:(value,digits)=>Number(value||0).toFixed(digits),
  esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;'),
  _reclassifySales:()=>({}),
  _repaymentRowsForDisplay:data=>data.customer_repayments||[],
  _payChannel:mode=>String(mode).toLowerCase().includes('cash')?'cash':'bank',
  _machineRangeRows:()=>[
    {vehicle_type:'Jaw',start_reading:10,end_reading:13,difference:3},
    {vehicle_type:'Cone',start_reading:20,end_reading:24,difference:4},
  ],
  _machineFuelStatsForRange:(_from,_to,vehicle)=>({liters:vehicle==='Jaw'?5:7}),
  _allData:{machines:['original']},
  _page:{machines:3},
};
runInNewContext(html.slice(start,end)+'\nglobalThis.blockTest={dashboardConsolidatedTable,dashboardBookSummaryTable,dashboardFuelSummaryTable,dashboardMachineSummaryTable,withDashboardMachineSummary};',context);
const {dashboardConsolidatedTable,dashboardBookSummaryTable,dashboardFuelSummaryTable,dashboardMachineSummaryTable,withDashboardMachineSummary}=context.blockTest;

const repayments=dashboardConsolidatedTable('dash-credit-repayment-card',{customer_repayments:[
  {customer_name:'Alice',date:'2026-09-21',display_bank_received:100,display_cash_received:0,display_payment_received:100,balance:50},
  {customer_name:'ALICE',date:'2026-09-22',display_bank_received:0,display_cash_received:20,display_payment_received:20,balance:30},
]});
assert.match(repayments,/Total \(1\)/);
assert.match(repayments,/100<\/td><td>20<\/td><td>120<\/td>/);
assert.match(repayments,/30<\/td>/);

const sales=dashboardConsolidatedTable('dash-customer-sales-card',{customer_sales:[
  {customer_name:'Alice',material:'M-Sand',ticket_count:1,qty_mt:2,amount:100,bank_received:60,cash_received:20,credit_sale_amount:20},
  {customer_name:'ALICE',material:'M-Sand',ticket_count:2,qty_mt:3,amount:150,bank_received:0,cash_received:50,credit_sale_amount:100},
]});
assert.match(sales,/Total \(1\)/);
assert.match(sales,/5\.00<\/td><td>250\.00<\/td>/);

const expenses=dashboardConsolidatedTable('dash-expenses-card',{expense_rows:[
  {category:'Diesel',amount:50,payment_mode:'Cash'},
  {category:'DIESEL',amount:70,payment_mode:'UPI'},
  {category:'Drawings',amount:100,is_operating_expense:false},
]});
assert.match(expenses,/Total \(1\)/);
assert.match(expenses,/50<\/td><td>70<\/td><td>120<\/td>/);
assert.doesNotMatch(expenses,/Drawings/);

const book=dashboardBookSummaryTable({bank:{opening:100,closing:160,total_in:80,total_out:20,rows:[
  {particulars:'Spot sale',in:50,out:0},{particulars:'SPOT SALE',in:30,out:0},{particulars:'Expense',in:0,out:20},
]}},'bank');
assert.match(book,/Opening 100 · Closing 160/);
assert.match(book,/80<\/td><td>20<\/td>/);

const fuel=dashboardFuelSummaryTable({opening_litres:100,closing_litres:125,rows:[
  {kind:'opening',balance_litres:120},
  {kind:'receipt',details:'Received from Vendor',received_litres:10,receipt_amount:500},
  {kind:'issue',details:'Issued to Loader',spend_litres:5,spend_amount:250},
]});
assert.match(fuel,/Opening 120\.00 L · Closing 125\.00 L/);
assert.doesNotMatch(fuel,/Opening 100\.00 L/);

const machines=dashboardMachineSummaryTable('2026-09-01','2026-09-22');
assert.match(machines,/Jaw/);
assert.match(machines,/Cone/);
assert.match(machines,/Total Machines \(2\)/);
assert.match(machines,/12\.00<\/td>/);

const before=context._allData.machines;
assert.equal(withDashboardMachineSummary({odometer:[{vehicle_type:'Jaw'}]},()=>context._page.machines),1);
assert.equal(context._allData.machines,before);
assert.equal(context._page.machines,3);
assert.match(html,/if\(el\.dataset\.rangeFrom&&el\.dataset\.rangeTo\)dateRange=/);
assert.match(html,/\[15,30,60,90\]/);
assert.match(html,/<option value="custom">Custom dates<\/option>/);
console.log('Dashboard block ranges: grouping, balances, independent state and PDF date wiring passed.');
