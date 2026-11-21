#!/usr/bin/env python3
"""Report only guard names, never financial details, on a diagnostic branch."""
import contextlib
import io
import sys
import verify_dashboard_parity as parity
import verify_r2_readback as readback

checks = [getattr(parity, name) for name in (
    'verify_frontend_guard', 'verify_sync_tolerance_guard',
    'verify_negative_number_guard', 'verify_credit_aging_guard',
    'verify_archive_balance_guard', 'verify_dashboard_balance_source_guard',
    'verify_payment_split_guard', 'verify_ledger_split_guard',
    'verify_no_vendor_payment_bank_guard', 'verify_customer_page_guard',
    'verify_pdf_export_guard', 'verify_compliance_code_guard',
    'verify_dashboard_balance_page_parity', 'verify_daily_balance_chain',
    'verify_no_balance_adjustment_rows', 'verify_all_dashboard_presets',
    'verify_bank_page_presets', 'verify_fytd_cashbook_parity',
    'verify_customer_page_presets', 'verify_compliance_snapshot',
    'verify_cloud_independence_guard',
)]
for check in checks:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            check()
    except BaseException:
        print('FAILED_CHECK=' + check.__name__, flush=True)
        sys.exit(1)
    print('PASSED_CHECK=' + check.__name__, flush=True)
sys.argv = ['verify_r2_readback.py', 'data', 'data']
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = readback.main()
except BaseException:
    print('FAILED_CHECK=verify_r2_readback', flush=True)
    sys.exit(1)
if code:
    print('FAILED_CHECK=verify_r2_readback', flush=True)
    sys.exit(1)
print('PASSED_CHECK=verify_r2_readback', flush=True)
