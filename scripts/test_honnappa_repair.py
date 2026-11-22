from datetime import date
from sync_loctell import compute_repayments_from_erp

day = date(2026, 11, 2)
debtor = {'erp_customer_id': 135304, 'name': 'HONNAPPA', 'received': 0, 'outstanding': 58232}
payment = [''] * 16
payment[0], payment[12], payment[13] = '02-11-2026', '12285', 'UPI'
sale = [''] * 16
sale[0], sale[11] = '02-11-2026', '12285'
rows = compute_repayments_from_erp(
    None, day, day, [debtor], [debtor], {day: [debtor]},
    ERP_DEBTOR_WORKERS=1, EXCLUDED_CUSTOMER_RECEIPT_REFS=set(),
    _clean=lambda x: x, _clone_sess=lambda x: x,
    _ledger_payment_channel=lambda row: 'bank', _num=lambda x: float(x or 0),
    fetch_customer_ledger_rows=lambda *args: [payment, sale],
    fetch_debtors=lambda *args: [debtor],
)
assert len(rows) == 1 and rows[0]['payment_received'] == 12285
assert rows[0]['bank_received'] == 12285 and rows[0]['amount'] == 0
assert rows[0]['reference'] == 'ERP-CREDIT-135304-2026-11-02-BANK'
print('Same-day equal sale and repayment recovery passed')
