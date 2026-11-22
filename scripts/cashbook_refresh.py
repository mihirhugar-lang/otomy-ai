"""Refresh affected canonical Cash/Bank Book snapshots after an ERP sync."""
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from r2_working_set import snapshot_url


def affected_cashbook_ranges(sync_start, today, archive_start, snapshot_keys, expired_names=()):
    """Map affected daily and previously published ranges to their exact URLs.

    A movement can also change the opening of a later range, so every existing
    book ending on or after the refreshed window must be rebuilt. Never rebuild
    a range if its underlying source archive has not been loaded.
    """
    if sync_start > today:
        raise ValueError('cashbook sync start is after today')
    result = {}
    for i in range((today - sync_start).days + 1):
        day = sync_start + timedelta(days=i)
        result[(day, day)] = {f'/api/sync/erp/cashbook?from_date={day}&to_date={day}'}
    for key in snapshot_keys:
        if Path(str(key)).name in expired_names:
            continue
        url = snapshot_url(str(key))
        if not url:
            continue
        parts = urlsplit(url)
        if parts.path != '/api/sync/erp/cashbook':
            continue
        query = parse_qs(parts.query)
        if len(query.get('from_date', [])) != 1 or len(query.get('to_date', [])) != 1:
            raise ValueError('invalid published cashbook range')
        try:
            start = date.fromisoformat(query['from_date'][0])
            end = date.fromisoformat(query['to_date'][0])
        except ValueError as exc:
            raise ValueError('invalid published cashbook date') from exc
        if start > end:
            raise ValueError('published cashbook range is reversed')
        if end < sync_start or end > today:
            continue
        if start < archive_start:
            raise ValueError('affected cashbook needs earlier archive history')
        result.setdefault((start, end), set()).add(url)
    return result


def verify_expense_movements(book, expenses, start, end, channel_for, number):
    """Fail before publish if any source expense is missing or duplicated."""
    expected = {'cash': Counter(), 'bank': Counter()}
    for row in expenses or []:
        day = str(row.get('date', ''))[:10]
        if not str(start) <= day <= str(end):
            continue
        amount = number(row.get('amount'))
        if amount <= 0:
            continue
        label = (row.get('category') or row.get('description') or 'Expense').strip()
        party = (row.get('description') or row.get('notes') or '').strip()
        key = (day, f'Expense: {label}', party, row.get('notes') or '', round(amount, 2))
        expected['cash' if channel_for(row) == 'cash' else 'bank'][key] += 1

    for channel in ('cash', 'bank'):
        rows = (book.get(channel) or {}).get('rows') or []
        actual = Counter((str(row.get('date', ''))[:10], row.get('particulars') or '',
                          row.get('party') or '', row.get('remarks') or '',
                          round(number(row.get('out')), 2))
                         for row in rows if row.get('kind') == 'expense')
        if actual != expected[channel]:
            raise ValueError(f'{channel} book expense rows do not match source for {start}..{end}')


def verify_adjustment_sources(book, overlay):
    """Never introduce a balance adjustment without a dated source."""
    cash_dates = {str(row.get('date', ''))[:10] for row in overlay.get('anchors', [])
                  if row.get('cash') is not None}
    bank_dates = {str(row.get('date', ''))[:10] for row in overlay.get('stmt_rows', [])}
    for channel, dates, label in (
        ('cash', cash_dates, 'Verified balance adjustment (physical cash count)'),
        ('bank', bank_dates, 'Verified balance adjustment (bank statement)'),
    ):
        for row in (book.get(channel) or {}).get('rows') or []:
            if row.get('kind') != 'adjustment' and not row.get('adjustment'):
                continue
            if row.get('particulars') != label or str(row.get('date', ''))[:10] not in dates:
                raise ValueError(
                    f'{channel} book {book.get("from")}..{book.get("to")} contains '
                    f'an adjustment without a dated source on {row.get("date")}'
                )
