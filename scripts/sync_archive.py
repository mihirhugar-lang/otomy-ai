"""Archive identities, merge rules and persisted source rows.

Runtime settings and helper callbacks are explicit keyword-only dependencies.
The gha_sync entry point supplies them, preserving its existing public interface
and isolated importlib loaders used by localhost and repair tools.
"""

from datetime import date, datetime, timedelta
import base64, json, re, html as htmllib, os, sys, time


def _bank_amount_key(row, *, _num):
    return (
        str(row.get("date", ""))[:10],
        round(_num(row.get("credit")), 2),
        round(_num(row.get("debit")), 2),
    )


def _erp_credit_ref(row):
    text = " ".join(str(row.get(key) or "") for key in ("id", "description", "reference", "notes"))
    match = re.search(r"ERP-CREDIT-(\d+)-\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(1)
    match = re.search(r"\breceipt-(\d+)-\d{4}-\d{2}-\d{2}\b", text)
    if match and match.group(1) != "1":
        return match.group(1)
    return ""


def _bank_dedupe_key(row, *, _bank_amount_key):
    source = str(row.get("source") or "").strip()
    date_value, credit, debit = _bank_amount_key(row)
    # Two independently-recorded bank expenses can legitimately have the same
    # date, amount and visible description.  Their stable expense id is the
    # only safe way to collapse an archive copy with its regenerated copy
    # without dropping a real payment (for example the two 21-Apr ₹15,000
    # farmer payments).
    if source == "Expense" and row.get("id"):
        return ("expense", str(row["id"]))
    if source == "Credit Payment" and row.get("id"):
        # Repayments are aggregated per customer/day/channel.  Different
        # customers can legitimately pay the same amount on the same date;
        # collapsing only by date and amount drops a real bank credit.
        return ("credit-payment", str(row["id"]))
    return (
        "bank",
        source,
        date_value,
        str(row.get("description") or ""),
        credit,
        debit,
        str(row.get("bank_name") or ""),
    )


def _bank_row_quality(row):
    text = " ".join(str(row.get(key) or "") for key in ("id", "description", "reference", "notes"))
    score = 0
    if "ERP-CREDIT-" in text:
        score += 10
    if re.search(r"\breceipt-(?!1-)\d+-\d{4}-\d{2}-\d{2}\b", text):
        score += 5
    if " - Customer" in str(row.get("description") or ""):
        score -= 2
    if row.get("id"):
        score += 1
    if row.get("description"):
        score += 1
    return score


def dedupe_bank_rows(
    rows,
    *,
    _bank_dedupe_key,
    _bank_row_quality,
    _is_excluded_customer_receipt_bank_row,
    _is_vendor_payment_bank_row,
):
    merged = {}
    for row in rows or []:
        if _is_vendor_payment_bank_row(row):
            continue
        if _is_excluded_customer_receipt_bank_row(row):
            continue
        key = _bank_dedupe_key(row)
        if key in merged:
            merged[key] = row if _bank_row_quality(row) >= _bank_row_quality(merged[key]) else merged[key]
        else:
            merged[key] = row
    return sorted(merged.values(), key=lambda row: (row.get("date", ""), str(row.get("id", ""))), reverse=True)


def _archive_key(section, row, *, _bank_dedupe_key):
    if section == "sales":
        ticket_no = str(row.get("ticket_no") or "").strip()
        if ticket_no:
            return "sales-ticket:" + "|".join(str(part) for part in (
                row.get("date", ""),
                ticket_no,
            ))
        return "sales:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("vehicle_no", ""),
            row.get("customer_name", ""),
            row.get("material", ""),
            row.get("amount", ""),
        ))
    if section == "expenses":
        return "expenses:" + "|".join(str(part) for part in (
            row.get("erp_key") or "",
            row.get("date", ""),
            row.get("category", ""),
            row.get("description", ""),
            row.get("amount", ""),
            row.get("payment_mode", ""),
            row.get("notes", ""),
        ))
    if section == "receipts":
        reference = str(row.get("reference") or "").strip()
        if reference:
            return "receipts-ref:" + "|".join(str(part) for part in (
                row.get("date", ""),
                row.get("mode", ""),
                reference,
            ))
        return "receipts:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("customer_id", row.get("customer_name", "")),
            row.get("amount", ""),
            row.get("payment_received", ""),
            row.get("reference", ""),
        ))
    if section == "balances":
        return "balances:" + str(row.get("date", ""))
    if section == "boulders":
        return "boulders:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("source", ""),
        ))
    if section == "bank":
        return "bank:" + "|".join(str(part) for part in _bank_dedupe_key(row))
    if section == "cash":
        return "cash:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("ledger", ""),
            row.get("description", ""),
            row.get("received", ""),
            row.get("paid", ""),
        ))
    if section == "vendor_payments":
        reference = str(row.get("reference") or "").strip()
        if reference:
            return "vendor-payments-ref:" + "|".join(str(part) for part in (
                row.get("date", ""),
                row.get("mode", ""),
                reference,
            ))
        return "vendor-payments:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("vendor_id", row.get("vendor_name", "")),
            row.get("amount", ""),
            row.get("mode", ""),
        ))
    if row.get("id"):
        return f"{section}:id:{row['id']}"
    parts = [
        row.get("date", ""),
        row.get("ticket_no", ""),
        row.get("description", ""),
        row.get("customer_name", ""),
        row.get("amount", row.get("received", row.get("credit", ""))),
        row.get("paid", row.get("debit", "")),
    ]
    return f"{section}:" + "|".join(str(part) for part in parts)


def _is_vendor_payment_expense(row):
    text = " ".join(str(row.get(key, "")) for key in ("id", "category", "description", "notes")).upper()
    return row.get("category") == "Vendor Payment" or "VENDOR PAYMENT" in text or "ERP-SUP-" in text


def _is_vendor_payment_bank_row(row):
    text = " ".join(str(row.get(key, "")) for key in ("id", "bank_name", "source", "description")).upper()
    return (
        row.get("source") == "Vendor Payment"
        or row.get("bank_name") == "UPI/Bank Vendor Payment"
        or "VENDOR PAYMENT" in text
        or "VENDOR-PAYMENT-" in text
    )


def _row_quality(section, row):
    if section == "sales":
        score = 0
        if str(row.get("id") or "") not in ("", "0"):
            score += 10
        if row.get("customer_id"):
            score += 4
        if row.get("material") and row.get("material") != "6mm":
            score += 2
        return score
    if section == "receipts":
        score = 0
        if str(row.get("id") or "") not in ("", "0"):
            score += 10
        if row.get("customer_id"):
            score += 4
        if row.get("customer_name"):
            score += 2
        if row.get("payment_received") is not None:
            score += 2
        if row.get("balance") is not None:
            score += 1
        return score
    if section == "balances":
        score = 0
        sample_receivable = (row.get("receivables_rows") or row.get("top_receivables") or [{}])[0] if isinstance(row, dict) else {}
        sample_payable = (row.get("payables_rows") or row.get("top_payables") or [{}])[0] if isinstance(row, dict) else {}
        if isinstance(sample_receivable, dict) and sample_receivable.get("id") is not None:
            score += 5
        if isinstance(sample_payable, dict) and sample_payable.get("id") is not None:
            score += 5
        score += min(len(row.get("receivables_rows") or []), 100) / 100
        score += min(len(row.get("payables_rows") or []), 100) / 100
        return score
    return 0


def _prefer_archive_row(section, existing, incoming, *, _row_quality):
    if section == "sales":
        merged = dict(existing)
        merged.update(incoming)
        if str(incoming.get("id") or "") in ("", "0") and existing.get("id"):
            merged["id"] = existing["id"]
        if not incoming.get("customer_id") and existing.get("customer_id"):
            merged["customer_id"] = existing["customer_id"]
        return merged
    if section == "balances":
        return incoming if _row_quality(section, incoming) >= _row_quality(section, existing) else existing
    if section in {"sales", "receipts"}:
        return incoming if _row_quality(section, incoming) >= _row_quality(section, existing) else existing
    return incoming


def _historical_existing_dates(rows, *, IST, MERGE_PROTECT_BEFORE_DATE):
    cutoff = MERGE_PROTECT_BEFORE_DATE or datetime.now(IST).date().isoformat()
    return {
        str(row.get("date", ""))[:10]
        for row in rows or []
        if str(row.get("date", ""))[:10] and str(row.get("date", ""))[:10] < cutoff
    }


def _expense_content_key(row):
    return "|".join(str(part) for part in (
        row.get("date", ""),
        row.get("category", ""),
        row.get("description", ""),
        row.get("amount", ""),
        row.get("payment_mode", ""),
        row.get("notes", ""),
    ))


def _merge_archive_rows(
    existing,
    incoming,
    section,
    *,
    drop_current_window=True,
    IST,
    MERGE_PROTECT_BEFORE_DATE,
    _archive_key,
    _expense_content_key,
    _historical_existing_dates,
    _is_vendor_payment_bank_row,
    _is_vendor_payment_expense,
    _prefer_archive_row,
):
    if section == "expenses":
        existing = [row for row in existing if not _is_vendor_payment_expense(row)]
        incoming = [row for row in incoming if not _is_vendor_payment_expense(row)]
    if drop_current_window and section in {"sales", "expenses", "internal_transfers", "cash", "bank", "receipts", "vendor_payments"}:
        # Fresh fetch is authoritative for its window. Every section we re-pull in full over
        # [sync_start, today] drops its archived rows on/after the sync cutoff, so an ERP row
        # later edited (remark/amount changed) or reordered can't linger as a stale duplicate
        # beside its refreshed version — otomy reconciles to the live ERP exactly like the local
        # DB does. Older (protected) dates keep their archive untouched. Receipts are included so
        # a re-derived window drops repayment rows the fresh ERP derivation no longer produces —
        # mirroring localhost's import_customer_credit_receipts, which deletes the range and
        # re-imports. (Incoming all_repayments covers June-archive + fresh last-month + fresh MTD,
        # so the dropped [cutoff, today] window is always fully re-supplied.)
        _cutoff = MERGE_PROTECT_BEFORE_DATE or datetime.now(IST).date().isoformat()
        existing = [row for row in existing if str(row.get("date", ""))[:10] < _cutoff]
    protected_dates = _historical_existing_dates(existing) if section in {"sales", "expenses", "internal_transfers", "receipts", "bank", "cash", "vendor_payments"} else set()
    merged = {}
    existing_expense_keys = set()
    for idx, row in enumerate(existing):
        if section == "bank" and _is_vendor_payment_bank_row(row):
            continue
        key = _archive_key(section, row)
        if section == "expenses":
            existing_expense_keys.add(_expense_content_key(row))
            if key in merged:
                key = f"{key}|archive-row:{row.get('id') or idx}"
        merged[key] = _prefer_archive_row(section, merged[key], row) if key in merged else row
    for row in incoming:
        if section == "bank" and _is_vendor_payment_bank_row(row):
            continue
        key = _archive_key(section, row)
        row_date = str(row.get("date", ""))[:10]
        if section == "expenses" and _expense_content_key(row) in existing_expense_keys:
            continue
        if row_date in protected_dates and key not in merged:
            continue
        merged[key] = _prefer_archive_row(section, merged[key], row) if key in merged else row
    return sorted(merged.values(), key=lambda row: (row.get("date", ""), str(row.get("id", ""))))


def _bank_key(row):
    # Same-day expense payments with the same amount and rendered description
    # are distinct unless their stable source expense is the same.  This key
    # is used before dedupe_bank_rows(), so it must preserve the expense id
    # here as well (21-Apr farmer payments are the regression case).
    if str(row.get("source") or "") == "Expense" and row.get("id"):
        return f"expense|{row['id']}"
    return "|".join(str(row.get(k, "")) for k in ("date", "description", "credit", "debit", "bank_name"))


def derive_bank_transactions(
    sales,
    expenses,
    repayments,
    existing=None,
    *,
    _bank_key,
    _is_excluded_customer_receipt,
    _is_excluded_customer_receipt_bank_row,
    _is_vendor_payment_bank_row,
    _num,
    _payment_channel,
    _sale_channels,
):
    # Drop archived "Sale" rows so they re-derive fresh with the current split amount
    # (otherwise a sale whose UPI portion changed shows twice — old full + new split).
    # Other derived sources (Expense / Credit Payment) are unaffected by
    # the split and are preserved to avoid dropping rows the recent window can't re-derive.
    rows = [dict(row, source=row.get("source", "ERP Bank")) for row in (existing or [])
            if row.get("source") != "Sale" and not _is_vendor_payment_bank_row(row)]
    rows = [row for row in rows if not _is_excluded_customer_receipt_bank_row(row)]
    seen = {_bank_key(r) for r in rows}
    for sale in sales:
        # Only the UPI/bank portion of the sale belongs on the bank page (SPLIT-aware).
        _s_cash, _s_credit, s_upi = _sale_channels(sale)
        if s_upi <= 0:
            continue
        r = {
            "id": f"sale-{sale.get('id') or sale.get('ticket_no') or ''}-{sale.get('date')}",
            "date": sale.get("date"),
            "description": (
                f"Sale received by bank/UPI - {sale.get('customer_name') or 'Customer'}"
                f" - Ticket {sale.get('ticket_no') or '-'} - {sale.get('vehicle_no') or '-'}"
            ),
            "credit": round(s_upi, 2),
            "debit": 0.0,
            "bank_name": "UPI/Bank Sale",
            "source": "Sale",
        }
        if _bank_key(r) not in seen:
            seen.add(_bank_key(r))
            rows.append(r)
    for expense in expenses:
        if _payment_channel(expense.get("payment_mode") or "") == "cash":
            continue
        r = {
            "id": f"expense-{expense.get('id') or ''}-{expense.get('date')}-{expense.get('amount')}",
            "date": expense.get("date"),
            "description": f"Expense paid by bank/UPI - {expense.get('category') or 'Expense'} - {expense.get('description') or ''}",
            "credit": 0.0,
            "debit": _num(expense.get("amount")),
            "bank_name": "UPI/Bank Expense",
            "source": "Expense",
        }
        if _bank_key(r) not in seen:
            seen.add(_bank_key(r))
            rows.append(r)
    for idx, receipt in enumerate(repayments or []):
        if _is_excluded_customer_receipt(receipt):
            continue
        bank_received = _num(receipt.get("bank_received"))
        if bank_received <= 0 and _payment_channel(receipt.get("mode") or "") != "cash":
            bank_received = _num(receipt.get("payment_received", receipt.get("amount")))
        if bank_received <= 0:
            continue
        r = {
            "id": f"receipt-{receipt.get('erp_customer_id') or idx}-{receipt.get('date')}",
            "date": str(receipt.get("date", ""))[:10],
            "description": f"Credit payment received by bank/UPI - {receipt.get('customer_name') or 'Customer'}",
            "credit": bank_received,
            "debit": 0.0,
            "bank_name": "UPI/Bank Credit Payment",
            "source": "Credit Payment",
        }
        if _bank_key(r) not in seen:
            seen.add(_bank_key(r))
            rows.append(r)
    rows.sort(key=lambda row: (row.get("date", ""), str(row.get("id", ""))), reverse=True)
    return rows


def write_archive_updates(
    today,
    all_sales,
    all_expenses,
    internal_transfers,
    cash_rows,
    bank_rows,
    boulder_rows,
    repayments,
    vendor_payments,
    local_seed,
    balance_snapshots=None,
    ledger_by_month=None,
    *,
    ARCHIVE_DIR,
    IST,
    _is_excluded_customer_receipt,
    _is_excluded_customer_receipt_bank_row,
    _merge_archive_rows,
    _num,
):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for section, rows in (
        ("sales", all_sales),
        ("expenses", all_expenses),
        ("internal_transfers", internal_transfers),
        ("vendor_payments", vendor_payments),
        ("cash", cash_rows),
        ("bank", bank_rows),
        ("boulders", boulder_rows),
    ):
        for row in rows:
            month = str(row.get("date", ""))[:7]
            if not month:
                continue
            by_month.setdefault(month, {}).setdefault(section, []).append(row)

    for idx, row in enumerate(repayments or []):
        if _is_excluded_customer_receipt(row):
            continue
        day = str(row.get("date", ""))[:10]
        month = day[:7]
        if not month:
            continue
        by_month.setdefault(month, {}).setdefault("receipts", []).append({
            "id": f"gha-{day}-{idx}",
            "date": day,
            # Persist the FULL repayment identity so archived receipts net exactly like
            # localhost's build_cashbook: customer_name enables same-day spot<->repayment
            # netting, and payment_received (gross) is what the cash/bank book uses (the bare
            # `amount` is already net of the ledger sale-adjustment and must NOT be used as the
            # movement). Reader (archive_receipts_to_repayments) + _row_quality already expect
            # these fields; the writer just wasn't populating them.
            "customer_id": row.get("erp_customer_id"),
            "customer_name": row.get("customer_name"),
            "amount": row.get("amount", 0.0),
            "payment_received": row.get("payment_received", row.get("amount", 0.0)),
            "cash_received": row.get("cash_received", 0.0),
            "bank_received": row.get("bank_received", 0.0),
            "sale_adjusted": row.get("sale_adjusted", 0.0),
            "balance": row.get("balance"),
            "mode": row.get("mode", "Cash"),
            "reference": row.get("reference", ""),
            "notes": (
                "ERP credit balance repayment; "
                f"payment_received={row.get('payment_received', row.get('amount', 0.0))}; "
                f"sale_adjusted={row.get('sale_adjusted', 0.0)}"
            ),
        })

    for as_of, snapshot in (balance_snapshots or {}).items():
        day = str(as_of)[:10]
        month = day[:7]
        if not month:
            continue
        if not snapshot.get("debtors") or not snapshot.get("creditors"):
            continue
        receivables = [
            {"name": row.get("name"), "balance": round(_num(row.get("outstanding", row.get("balance", 0.0))), 2)}
            for row in (snapshot.get("debtors") or [])
            if _num(row.get("outstanding", row.get("balance", 0.0))) > 0
        ]
        payables = [
            {"name": row.get("name"), "balance": round(_num(row.get("payable", row.get("balance", 0.0))), 2)}
            for row in (snapshot.get("creditors") or [])
            if _num(row.get("payable", row.get("balance", 0.0))) > 0
        ]
        receivables.sort(key=lambda row: row["balance"], reverse=True)
        payables.sort(key=lambda row: row["balance"], reverse=True)
        by_month.setdefault(month, {}).setdefault("balances", []).append({
            "date": day,
            "receivables": round(sum(row["balance"] for row in receivables), 2),
            "payables": round(sum(row["balance"] for row in payables), 2),
            "receivables_rows": receivables,
            "payables_rows": payables,
            "top_receivables": receivables[:5],
            "top_payables": payables[:5],
        })

    for month, sections in by_month.items():
        path = ARCHIVE_DIR / f"{month}.json"
        if path.exists():
            with open(path, "r") as f:
                payload = json.load(f)
        else:
            payload = {
                "month": month,
                "sales": [],
                "expenses": [],
                "internal_transfers": [],
                "receipts": [],
                "vendor_payments": [],
                "bank": [],
                "cash": [],
                "boulders": [],
                "labour": [],
                "parts": [],
                "machines": [],
                "balances": [],
                "ledger": [],
                "ledger_totals": {},
            }
        payload["receipts"] = [
            row for row in payload.get("receipts", [])
            if not _is_excluded_customer_receipt(row)
        ]
        payload["bank"] = [
            row for row in payload.get("bank", [])
            if not _is_excluded_customer_receipt_bank_row(row)
        ]
        for section, rows in sections.items():
            if section == "receipts":
                payload["receipts"] = [
                    row for row in payload.get("receipts", [])
                    if not _is_excluded_customer_receipt(row)
                ]
                rows = [
                    row for row in rows
                    if not _is_excluded_customer_receipt(row)
                ]
            if section == "bank":
                rows = [
                    row for row in rows
                    if not _is_excluded_customer_receipt_bank_row(row)
                ]
            payload[section] = _merge_archive_rows(payload.get(section, []), rows, section)
        canonical_ledger = (ledger_by_month or {}).get(month)
        if canonical_ledger is not None:
            payload["ledger"] = canonical_ledger.get("rows", [])
            payload["ledger_totals"] = canonical_ledger.get("totals", {})
        with open(path, "w") as f:
            json.dump(payload, f, default=str, separators=(",", ":"))

    manifest_path = ARCHIVE_DIR / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    else:
        manifest = {"from": "2025-02-14", "months": []}
    months = sorted({*(manifest.get("months") or []), *by_month.keys()})
    seed_config = ((local_seed.get("endpoints") or {}).get("exports_config") or {}) if isinstance(local_seed, dict) else {}
    manifest.update({
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": "github-actions archive merge",
        "from": manifest.get("from") or "2025-02-14",
        "months": months,
        "operating_balance_opening": seed_config.get("operating_balance_opening") or manifest.get("operating_balance_opening", {}),
    })
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


def load_archive_manifest(*, ARCHIVE_DIR):
    try:
        with open(ARCHIVE_DIR / "manifest.json") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _date_months(from_d, to_d):
    months = []
    cur = from_d.replace(day=1)
    end = to_d.replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months


def load_archive_window(from_d, to_d, *, ARCHIVE_DIR, _date_months):
    out = {
        "sales": [],
        "expenses": [],
        "internal_transfers": [],
        "receipts": [],
        "vendor_payments": [],
        "cash": [],
        "bank": [],
        "boulders": [],
        "iot": [],
        "labour": [],
        "parts": [],
        "machines": [],
        "balances": [],
    }
    fs, ts = str(from_d), str(to_d)
    for month in _date_months(from_d, to_d):
        path = ARCHIVE_DIR / f"{month}.json"
        if not path.exists():
            continue
        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except Exception as e:
            print(f"  archive read error ({month}): {e}")
            continue
        for section in out:
            out[section].extend([
                row for row in payload.get(section, [])
                if fs <= str(row.get("date", ""))[:10] <= ts
            ])
    return out


def merge_rows_by_archive_key(
    archive_rows,
    fresh_rows,
    section,
    *,
    drop_current_window=True,
    _merge_archive_rows,
):
    return _merge_archive_rows(
        archive_rows or [], fresh_rows or [], section,
        drop_current_window=drop_current_window,
    )


