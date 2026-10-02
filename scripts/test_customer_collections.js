const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(process.argv[2]||'index.html','utf8');
const helpers=source.slice(source.indexOf('function _num('),source.indexOf('// Localhost keys spot sales'));
const archive=source.slice(source.indexOf('function _archiveCustomerRows('),source.indexOf('// The customers/vendors PAGE'));
const ctx=vm.createContext({_archiveCreditDue15Plus:()=>({}),_formatArchiveMaterials:()=>'',today:()=> '2026-10-01'});
vm.runInContext(helpers+'\n'+archive,ctx);
function check(sales,receipts,expected){
  const row=ctx._archiveCustomerRows({},sales,receipts)[0];
  assert.equal(Math.round(ctx._customerCollection(row)*100)/100,expected);
  return row;
}
const sale={customer_name:'Split Customer',date:'2026-10-01',amount:16546,cash_amount:15000,upi_amount:1546.4,credit_amount:0};
const receipt=(amount,mode,adjusted,date='2026-10-01')=>({customer_name:'Split Customer',date,mode,amount,
  sale_adjusted:adjusted,notes:`payment_received=${amount}; sale_adjusted=${adjusted}`});
const row=check([sale],[receipt(15000,'Cash',15000),receipt(1546,'Bank',1546)],16546);
assert.equal(ctx._customerCreditReceived(row),0);
check([sale],[receipt(20000,'Cash',15000),receipt(1546,'Bank',1546)],21546);
check([{...sale,amount:1000,cash_amount:0,upi_amount:0,credit_amount:1000}],[receipt(1000,'Cash',1000)],1000);
const cashSale={...sale,amount:100,cash_amount:100,upi_amount:0};
check([cashSale],[receipt(100,'Bank',100)],200);
check([cashSale],[receipt(100,'Cash',100,'2026-10-02')],200);
check([cashSale],[receipt(50,'Cash',0)],150);
check([sale],[receipt(20000,'Cash',Math.round(16546*20000/21546*100)/100),receipt(1546,'Bank',Math.round(16546*1546/21546*100)/100)],21546);
check([{...cashSale,erp_customer_id:11}],[{...receipt(100,'Cash',100),erp_customer_id:22}],200);
check([cashSale],[{...receipt(100,'Cash',100),erp_customer_id:11}],100);
check([cashSale],[{...receipt(100,'Cash',100),erp_customer_id:11},{...receipt(100,'Cash',100),erp_customer_id:22}],300);
assert.equal(ctx._spotReceiptOverlap([{...cashSale,customer_name:'  split customer '}],[receipt(100,'Cash',100)]).get('SPLIT CUSTOMER'),100);
assert.equal((source.match(/const customerCollection=_customerCollection;/g)||[]).length,2);
console.log('Customer collections: split, credit, old-debt, date, channel and manual-receipt regressions passed');
