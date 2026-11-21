#!/usr/bin/env python3
"""Report only guard names, never financial details, on a diagnostic branch."""
import contextlib
import io
import os
import sys
import traceback
from pathlib import Path
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
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            check()
    except BaseException:
        Path(os.environ['RUNNER_TEMP'], 'otomy-private-guard-error.txt').write_text(
            captured.getvalue() + '\n' + traceback.format_exc(), encoding='utf-8')
        print('FAILED_CHECK=' + check.__name__, flush=True)
        sys.exit(1)
    print('PASSED_CHECK=' + check.__name__, flush=True)
sys.argv = ['verify_r2_readback.py', 'data', 'data']
try:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        code = readback.main()
except BaseException:
    Path(os.environ['RUNNER_TEMP'], 'otomy-private-guard-error.txt').write_text(
        captured.getvalue() + '\n' + traceback.format_exc(), encoding='utf-8')
    print('FAILED_CHECK=verify_r2_readback', flush=True)
    sys.exit(1)
if code:
    Path(os.environ['RUNNER_TEMP'], 'otomy-private-guard-error.txt').write_text(captured.getvalue(), encoding='utf-8')
    print('FAILED_CHECK=verify_r2_readback', flush=True)
    sys.exit(1)
print('PASSED_CHECK=verify_r2_readback', flush=True)
