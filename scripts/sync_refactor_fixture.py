"""Public synthetic inputs for before/after sync-engine contract checks.

No ERP or R2 connection is permitted. All output goes to a caller-owned
temporary directory; every financial field and transaction row is hashed.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import requests

TODAY = date(2026, 9, 21)
RANGES = {
    'today': (TODAY, TODAY),
    'mtd': (date(2026, 9, 1), TODAY),
    'fytd': (date(2026, 4, 1), TODAY),
    'current_month': (date(2026, 9, 1), date(2026, 9, 30)),
    'previous_month': (date(2026, 8, 1), date(2026, 8, 31)),
    'historical': (date(2026, 5, 31), date(2026, 7, 2)),
}


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 21, 12, 0, 0, tzinfo=tz)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


def fixture_rows():
    customers = [dict(id=i, name=name, outstanding=balance, active=True, gstin='')
                 for i, name, balance in [(1, 'Fixture Alpha', 350.25),
                                          (2, 'Fixture Bravo', -10), (3, 'Fixture Delta', 40)]]
    vendors = [dict(id=1, name='Fixture Quarry', erp_supplier_id='101_2', payable=450.75, active=True)]
    sales, expenses, receipts, transfers, payments, boulders = [], [], [], [], [], []
    days = ['2026-03-15', '2026-04-01', '2026-04-16', '2026-05-01', '2026-05-31',
            '2026-06-01', '2026-06-30', '2026-07-02', '2026-08-01', '2026-08-15',
            '2026-08-31', '2026-09-01', '2026-09-06', '2026-09-20', '2026-09-21']
    for i, day in enumerate(days):
        for customer, mode, amounts in [(customers[0], 'Cash', (40.005, 20, 40)),
                                        (customers[1], 'Credit', (0, 100.005, 0)),
                                        (customers[2], 'UPI', (0, 0, 100.005))]:
            sales.append(dict(date=day, customer_id=customer['id'], customer_name=customer['name'],
                              ticket_no=str(customer['id']), qty_mt=2.345, material='40mm',
                              amount=95.005, transport_charge=5, payment_mode=mode, mdp_ton=1.25,
                              cash_amount=amounts[0], credit_amount=amounts[1], upi_amount=amounts[2],
                              rate_per_mt=40.5, hsn_code='2517', gst_rate=5, erp_synced=True))
        for channel, amount in [('Cash', 12.345), ('UPI', 7.655)]:
            expenses.append(dict(date=day, category='Fuel', description='Fixture diesel',
                                 notes='', payment_mode=channel, amount=amount, erp_synced=True,
                                 erp_key=f'fixture-{i}-{channel}'))
        expenses.append(dict(date=day, category='DIRECTOR 1', description='Fixture director',
                             notes='Kumar', payment_mode='Cash', amount=2.5, erp_synced=True))
        for channel, amount in [('Cash', 60.005), ('UPI', 50.0)]:
            receipts.append(dict(date=day, customer_id=1, customer_name=customers[0]['name'],
                                 mode=channel, amount=amount, payment_received=amount,
                                 cash_received=amount if channel=='Cash' else 0,
                                 bank_received=amount if channel!='Cash' else 0,
                                 sale_adjusted=0, reference=f'fixture-receipt-{i}-{channel}'))
        transfers.append(dict(date=day, amount=3.125, reference=f'fixture-contra-{i}'))
        payments.append(dict(date=day, vendor_name=vendors[0]['name'], erp_supplier_id='101_2',
                             amount=5.5, mode='Bank', reference=f'fixture-vendor-{i}'))
        boulders.append(dict(date=day, total_tonnes=15.25, trips=2, supplier_name='Fixture Quarry'))
    return customers, vendors, sales, expenses, receipts, transfers, payments, boulders


def capture_fetches(engine):
    """Exercise HTTP retry, row parsing, dates and source IDs without a network."""
    class Response:
        def __init__(self, body):
            self.body = body
            self.text = body if isinstance(body,str) else json.dumps(body)
        def raise_for_status(self): pass
        def json(self): return self.body

    class Session:
        def __init__(self): self.calls = []
        def get(self, url, **kwargs):
            self.calls.append((url,kwargs))
            if len(self.calls)==1:
                raise requests.ConnectionError('synthetic interrupted read')
            if 'ListCustomerWiseReport' in url:
                cells=['1','T-1','21-09-2026','10:15 AM','FIXTURE-1','40 mm','40','2.5','100','CASH','105']
                return Response('Party Name :Fixture Alpha<tr>'+''.join('<td>'+x+'</td>' for x in cells)+'</tr>')
            if 'ListCustomerBalance' in url:
                return Response({'recordsTotal':2,'data':[
                    ['Fixture Alpha<span>status</span>','','1,000.50','600.25','viewLedgerTransactions?customerId=11'],
                    ['Fixture Advance','','10','30','viewLedgerTransactions?customerId=12']]})
            if 'ListSupplierBalance' in url:
                return Response({'data':[['Fixture Quarry','50','450.75','viewSupplierLedgerTransactions?supplierId=101_2']]})
            raise AssertionError('Unexpected ERP fixture request')

    session=Session()
    with patch.object(engine,'ERP_RETRY_DELAY_SECONDS',0):
        rows={
            'sales':engine._fetch_sales_window(session,TODAY,TODAY),
            'debtors':engine.fetch_debtors(session,TODAY),
            'creditors':engine.fetch_creditors(session,TODAY),
            'calls':session.calls,
        }
        with patch.object(session,'get',side_effect=requests.ConnectionError('synthetic outage')) as get:
            try:
                engine._fetch_sales_window(session,TODAY,TODAY)
            except engine.ErpFetchError as error:
                rows['failure']={'type':type(error).__name__,'message':str(error),'attempts':get.call_count}
            else:
                raise AssertionError('An exhausted source read must block publishing')
    return fingerprint(rows)


def capture(engine, root):
    root = Path(root)
    data, seed = root/'data', root/'seed'
    data.mkdir(parents=True)
    seed.mkdir()
    customers, vendors, sales, expenses, receipts, transfers, payments, boulders = fixture_rows()
    opening = {'as_of':'2026-03-01', 'bank_balance':2000, 'cash_balance_office':1000}
    overlay = {'anchors':[{'date':opening['as_of'], 'bank':2000, 'cash':1000}],
               'corrections':[], 'cash_daily_closings':{}, 'stmt_rows':[], 'stmt_to':None}
    local_seed = {'endpoints':{'exports_config':{'company_name':'Synthetic fixture',
                                              'operating_balance_opening':opening}}}
    (seed/'vendor_master.json').write_text(json.dumps(vendors))
    configured = dict(DATA_DIR=data, PRIVATE_SEED_DIR=seed, SNAPSHOT_API_DIR=data/'snapshot/api',
                      ARCHIVE_DIR=data/'archive', LOCAL_SEED_PATH=data/'local_seed.json',
                      VENDOR_MASTER_PATH=seed/'vendor_master.json',
                      BOOK_BALANCE_ACCOUNTS_PATH=seed/'book_balance_accounts.json',
                      BANK_STATEMENT_PATH=seed/'bank_statement.json',
                      CUSTOMER_MASTER_OVERRIDES_PATH=data/'customer_overrides.json',
                      ERP_BASE='https://erp.invalid', ERP_ORG='fixture', ERP_USER='fixture',
                      ERP_FETCH_RETRIES=3,
                      _BALANCE_OVERLAY=overlay, _PREV_LEDGER_CACHE={}, _WRITTEN_SNAPSHOT_FILES=set(),
                      MERGE_PROTECT_BEFORE_DATE='2026-09-01', datetime=FrozenDatetime)
    results = {}
    def between(rows, start, end):
        return deepcopy([r for r in rows if str(start) <= r['date'] <= str(end)])
    with ExitStack() as stack:
        import r2_working_set
        stack.enter_context(patch.dict(os.environ, {'OTOMY_WORKING_SET_STATE':''}))
        stack.enter_context(patch.object(r2_working_set, '_CONTEXT', None))
        stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')))
        for name, value in configured.items():
            stack.enter_context(patch.object(engine, name, value))
        for name in ('sync_loctell', 'sync_finance', 'sync_archive', 'sync_snapshots'):
            module = sys.modules.get(name)
            if module is not None and hasattr(module, 'datetime'):
                stack.enter_context(patch.object(module, 'datetime', FrozenDatetime))
        results['fetches'] = capture_fetches(engine)
        vendor_ledgers = engine.build_vendor_ledgers(vendors, payments)
        results['vendor_ledgers'] = fingerprint(vendor_ledgers)
        controls = {}
        for label, (start, end) in RANGES.items():
            bank, cash = engine._overlay_balance(str(start-timedelta(days=1)), sales, expenses, receipts, transfers)
            book = engine.build_cashbook_view(start, end, sales, expenses, receipts,
                    {'as_of':str(start-timedelta(days=1)), 'bank_balance':bank, 'cash_balance_office':cash},
                    internal_transfers=transfers)
            control = engine.build_control(between(sales,start,end), between(expenses,start,end), start,end,
                        debtors=customers, creditors=vendors, repayments=between(receipts,start,end),
                        vendor_payments=between(payments,start,end), bank_balance_book=2000,
                        cash_balance_office_book=1000)
            parties = engine.build_customer_range_rows(customers,sales,between(sales,start,end),
                        between(receipts,start,end),ending_debtors=customers,as_of=end,all_repayments=receipts,
                        aging_sales=sales,aging_repayments=receipts)
            results[label] = {'book':fingerprint(book),'control':fingerprint(control),
                              'customers':fingerprint(parties),'cash_rows':len(book['cash']['rows']),
                              'bank_rows':len(book['bank']['rows'])}
            controls[label] = control
        controls['yesterday'] = engine.build_control(between(sales,TODAY-timedelta(days=1),TODAY-timedelta(days=1)),
            between(expenses,TODAY-timedelta(days=1),TODAY-timedelta(days=1)),TODAY-timedelta(days=1),TODAY-timedelta(days=1))
        balances = {str(day): {'debtors':customers,'creditors':vendors}
                    for day in [TODAY,TODAY-timedelta(days=1),date(2026,8,31)]}
        engine.write_archive_updates(TODAY,sales,expenses,transfers,[],[],boulders,receipts,payments,
                                     local_seed,balance_snapshots=balances)
        results['archive_window'] = fingerprint(engine.load_archive_window(date(2026,3,1),TODAY))
        # Reuse the same ticket number on different dates; correct and remove
        # current rows while retaining genuine older history.
        prior = deepcopy(sales)
        prior[-1]['amount'] = 9999
        prior.append({**prior[-1],'ticket_no':'deleted-fixture'})
        results['merge_edits_deletions'] = fingerprint(engine._merge_archive_rows(prior,sales,'sales'))
        engine.write_snapshot_bundle(today=TODAY,yesterday=TODAY-timedelta(days=1),month_start=TODAY.replace(day=1),
            financial_year_start=date(2026,4,1),all_sales=sales,all_expenses=expenses,internal_transfers=transfers,
            labour_rows=[],parts_rows=[],machines_rows=[],odometer_readings=[],odometer_history=[],
            vmi_loader_fuel_issues=[],fuel_received_rows=[],fuel_balance={},boulder_rows=boulders,iot_rows=[],
            cash_rows=[],bank_rows=[],cash_balance=1000,bank_net=2000,bank_balance_book=2000,
            cash_balance_office_book=1000,customers_full=customers,customers_outstanding=customers,
            vendors_full=vendors,vendors_payables=vendors,vendor_ledgers=vendor_ledgers,vendor_payments=payments,
            repayments=receipts,local_seed=local_seed,controls=deepcopy(controls),balance_snapshots=balances,
            archive_balances={},historical_start=date(2026,3,1),aging_sales=sales,aging_repayments=receipts)
        dataset=engine.build_compliance_dataset(sales,expenses,receipts,customers,vendors,payments,
                    config=local_seed['endpoints']['exports_config'],from_date=date(2026,4,1),to_date=TODAY)
        engine.write_compliance_snapshots(dataset,date(2026,4,1),TODAY)
        files={str(path.relative_to(data)):hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(data.rglob('*.json'))}
        results['generated_files']={'count':len(files),'sha256':fingerprint(files)}
        results['snapshot_ranges']=fingerprint(json.loads((data/'snapshot/manifest.json').read_text())['ranges'])
    return results
