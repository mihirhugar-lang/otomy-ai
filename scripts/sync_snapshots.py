"""Dashboard/API snapshot and compliance output generation.

Runtime settings and helper callbacks are explicit keyword-only dependencies.
The gha_sync entry point supplies them, preserving its existing public interface
and isolated importlib loaders used by localhost and repair tools.
"""

from datetime import date, datetime, timedelta
from shared_compliance import build_audit_ca as build_compliance_audit_ca
from shared_compliance import build_gstr1 as build_compliance_gstr1
from shared_compliance import build_gstr2b_reconciliation as build_compliance_gstr2b
from shared_compliance import build_gstr3b as build_compliance_gstr3b
from shared_compliance import build_tally_xml as build_compliance_tally_xml
import base64, json, re, html as htmllib, os, sys, time


def write_compliance_snapshots(dataset, from_date, to_date, *, write_snapshot):
    """Publish GST and AUDIT CA from the same full archived FY dataset.

    The regular ERP sync may run in recent mode, but these pages are FY-to-date
    views.  Building them from the full archive prevents a recent-window run from
    silently replacing April-June rows with zeros.
    """
    query = f"from_date={from_date}&to_date={to_date}"
    audit = build_compliance_audit_ca(dataset)
    write_snapshot(f"/api/exports/compliance/dataset?{query}", dataset)
    write_snapshot(
        f"/api/exports/compliance/summary?{query}",
        {
            "engine": dataset["engine"],
            "period": dataset["period"],
            "company": dataset["company"],
            "totals": dataset["totals"],
            "daily": dataset["daily"],
            "checks": dataset["checks"],
            "audit": audit,
        },
    )
    write_snapshot(f"/api/exports/audit-ca/summary?{query}", audit)
    write_snapshot(
        f"/api/exports/audit-ca/tally.xml?{query}",
        {
            "content_type": "application/xml",
            "content": build_compliance_tally_xml(dataset),
        },
    )

    cursor = from_date.replace(day=1)
    while cursor <= to_date:
        year, month = cursor.year, cursor.month
        write_snapshot(
            f"/api/exports/gst/gstr1?year={year}&month={month}",
            build_compliance_gstr1(dataset, year, month),
        )
        write_snapshot(
            f"/api/exports/gst/gstr3b?year={year}&month={month}",
            build_compliance_gstr3b(dataset, year, month),
        )
        write_snapshot(
            f"/api/exports/gst/gstr2b?year={year}&month={month}",
            build_compliance_gstr2b(dataset, year, month),
        )
        if month == 12:
            cursor = cursor.replace(year=year + 1, month=1)
        else:
            cursor = cursor.replace(month=month + 1)


def build_gstr1(sales_rows, name_to_gstin, exports_config, year, month, *, _num):
    """Compute a GSTR-1 payload for one month from sale rows, mirroring the live
    backend (routers/exports.py:export_gstr1). Amounts are GST-inclusive, so the
    taxable value is amount / (1 + rate/100). B2B when the customer has a valid
    15-char GSTIN, else rolled into the B2C summary. Keeps otomy's static snapshot
    identical to what localhost returns instead of shipping an empty stub."""
    gstin = (exports_config or {}).get("gstin", "") or ""
    state_code = (exports_config or {}).get("state_code", "29") or "29"
    fp = f"{month:02d}{year}"
    prefix = f"{year}-{month:02d}-"
    b2b = {}
    b2cs_taxable = b2cs_cgst = b2cs_sgst = 0.0
    total_taxable = 0.0
    total_qty = 0.0
    for s in sales_rows:
        d = str(s.get("date") or "")
        if not d.startswith(prefix):
            continue
        rate = _num(s.get("gst_rate")) or 5.0
        amount = _num(s.get("amount")) + _num(s.get("transport_charge"))
        taxable = round(amount / (1 + rate / 100), 2)
        cgst = round(taxable * (rate / 2) / 100, 2)
        sgst = round(taxable * (rate / 2) / 100, 2)
        total_taxable += taxable
        total_qty += _num(s.get("qty_mt"))
        cust_gstin = (name_to_gstin.get((s.get("customer_name") or "").strip().lower(), "") or "").strip()
        if len(cust_gstin) == 15:
            entry = b2b.setdefault(cust_gstin, {"ctin": cust_gstin, "inv": []})
            try:
                idt = datetime.strptime(d[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
            except ValueError:
                idt = d
            entry["inv"].append({
                "inum": s.get("ticket_no") or f"INV{s.get('id')}",
                "idt": idt,
                "val": round(amount, 2),
                "pos": state_code,
                "rchrg": "N",
                "itms": [{"num": 1, "itm_det": {
                    "txval": taxable, "rt": rate, "igst": 0,
                    "cgst": cgst, "sgst": sgst, "cess": 0,
                }}],
            })
        else:
            b2cs_taxable += taxable
            b2cs_cgst += cgst
            b2cs_sgst += sgst
    gstr1 = {
        "gstin": gstin,
        "fp": fp,
        "gt": round(total_taxable, 2),
        "cur_gt": round(total_taxable, 2),
    }
    if b2b:
        gstr1["b2b"] = list(b2b.values())
    gstr1["b2cs"] = [{
        "sply_tp": "INTRA", "pos": state_code, "typ": "OE", "rt": 5,
        "txval": round(b2cs_taxable, 2), "igst": 0,
        "cgst": round(b2cs_cgst, 2), "sgst": round(b2cs_sgst, 2), "cess": 0,
    }] if b2cs_taxable > 0 else []
    b2b_cgst = sum(itm["itm_det"]["cgst"] for c in b2b.values() for inv in c["inv"] for itm in inv["itms"])
    b2b_sgst = sum(itm["itm_det"]["sgst"] for c in b2b.values() for inv in c["inv"] for itm in inv["itms"])
    gstr1["hsn"] = {"data": [{
        "num": 1, "hsn_sc": "2517", "desc": "Crushed Stone / Aggregate", "uqc": "MT",
        "qty": round(total_qty, 3), "val": round(total_taxable, 2), "txval": round(total_taxable, 2),
        "igst": 0, "cgst": round(b2cs_cgst + b2b_cgst, 2), "sgst": round(b2cs_sgst + b2b_sgst, 2), "cess": 0,
    }]} if total_taxable > 0 else {"data": []}
    return gstr1


def write_snapshot_bundle(
    today,
    yesterday,
    month_start,
    financial_year_start,
    all_sales,
    all_expenses,
    internal_transfers,
    labour_rows,
    parts_rows,
    machines_rows,
    odometer_readings,
    odometer_history,
    vmi_loader_fuel_issues,
    fuel_received_rows,
    fuel_balance,
    boulder_rows,
    iot_rows,
    cash_rows,
    bank_rows,
    cash_balance,
    bank_net,
    bank_balance_book,
    cash_balance_office_book,
    customers_full,
    customers_outstanding,
    vendors_full,
    vendors_payables,
    vendor_ledgers,
    vendor_payments,
    repayments,
    local_seed,
    controls,
    balance_snapshots,
    archive_balances,
    customer_ledgers_full=None,
    historical_start=None,
    aging_sales=None,
    aging_repayments=None,
    *,
    DATA_DIR,
    ERP_BASE,
    ERP_ORG,
    ERP_USER,
    ErpFetchError,
    IST,
    _balance_overlay,
    _num,
    _overlay_balance,
    _vendor_identity,
    apply_seed_control_overrides,
    archived_vendor_balances_as_of,
    build_control,
    build_customer_ledgers,
    build_customer_range_rows,
    build_gstr1,
    build_ledger_view,
    empty_ledger,
    historical_vendor_master_rows,
    latest_seed_control,
    load_archive_manifest,
    load_book_balance_accounts,
    vendor_rows_as_of,
    write_snapshot,
):
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start - timedelta(days=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    ranges = [
        (today, today),
        (yesterday, yesterday),
        (week_start, today),
        (last_week_start, last_week_end),
        (month_start, today),
        (last_month_start, last_month_end),
        # The FYTD dashboard is a canonical current view, not a historical
        # one-off.  A recent ERP ingest therefore must refresh it too; leaving
        # this range to full-only runs makes the FYTD dashboard silently freeze
        # while Today and MTD continue to advance.
        (financial_year_start, today),
    ]
    # These rolling periods are first-class dashboard choices in Otomy.  The
    # static site cannot calculate an absent dashboard snapshot on demand, so
    # generate the exact ranges selected by the UI as part of every engine
    # refresh.  They replace the prior day's derived snapshots through the
    # retention pass below; this does not grow R2 storage over time.
    def calendar_month_range_start(months: int) -> date:
        # Last 2 Months = previous full calendar month + current MTD; Last 3
        # Months also includes the full month before that.  Match the UI's
        # month-boundary semantics exactly rather than a rolling 60/90 days.
        month_index = today.year * 12 + (today.month - 1) - (months - 1)
        target_year, target_month_index = divmod(month_index, 12)
        target_month = target_month_index + 1
        return date(target_year, target_month, 1)

    rolling_ranges = [
        (calendar_month_range_start(2), today),
        (calendar_month_range_start(3), today),
        *((today - timedelta(days=days - 1), today) for days in (7, 15, 30, 45, 60, 90)),
    ]
    for rolling_range in rolling_ranges:
        if rolling_range not in ranges:
            ranges.append(rolling_range)
    # Completed FY months are selectable dashboard periods too.  Without
    # explicit snapshots April falls through to a different client archive
    # path while May onward may happen to exist from prior runs.
    completed_month = financial_year_start
    while completed_month < month_start:
        completed_end = (completed_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        completed_range = (completed_month, completed_end)
        if completed_range not in ranges:
            ranges.append(completed_range)
        completed_month = completed_end + timedelta(days=1)
    if historical_start is not None:
        historical_end = min(yesterday, last_month_end)
        ranges.extend([
            (historical_start, historical_end),
            (historical_start, yesterday),
            (historical_start, today),
        ])
    for start_day in range(1, today.day + 1):
        start = today.replace(day=start_day)
        for end_day in range(start_day, today.day + 1):
            end = today.replace(day=end_day)
            if (start, end) not in ranges:
                ranges.append((start, end))
    # Regenerate single-day control snapshots back to the balance anchor. A cash/bank book's opening
    # is control(previous-day); if that historical single-day snapshot is stale (written months ago
    # before the receipts were reconciled) the book range opens on a wrong figure. Rewriting one
    # snapshot per day from the anchor to today keeps every historical range opening self-computed
    # and correct — cheap (~a day's worth per day since the anchor).
    try:
        _hist_anchors = _balance_overlay().get("anchors", [])
        if _hist_anchors:
            _ad = date.fromisoformat(str(_hist_anchors[-1]["date"]))
            _dd = _ad
            while _dd <= today:
                if (_dd, _dd) not in ranges:
                    ranges.append((_dd, _dd))
                _dd += timedelta(days=1)
    except Exception as _e:
        print(f"  historical single-day snapshot backfill skipped: {_e}")

    control_by_range = {
        (today, today): controls["today"],
        (yesterday, yesterday): controls["yesterday"],
        (week_start, today): controls.get("week"),
        (month_start, today): controls["mtd"],
    }

    def rows_between(rows, start, end):
        fs, ts = str(start), str(end)
        return [row for row in rows if fs <= row.get("date", "") <= ts]

    def debtors_as_of(as_of):
        rows = balance_snapshots.get(str(as_of), {}).get("debtors") or []
        return [{"name": row.get("name"), "outstanding": row.get("outstanding", row.get("balance", 0.0))} for row in rows]

    def creditors_as_of(as_of):
        rows = balance_snapshots.get(str(as_of), {}).get("creditors") or []
        # Supplier Balance may contain same-name masters.  The vendor page is
        # keyed by its Loctell supplier-ledger ID, so retain that ID when a
        # dated snapshot is rebuilt.  Dropping it makes every ID-backed vendor
        # miss its balance and appear as ₹0 in the dated Vendor-page view.
        return [{
            "name": row.get("name"),
            "payable": row.get("payable", row.get("balance", 0.0)),
            "erp_supplier_id": row.get("erp_supplier_id"),
        } for row in rows]

    def archive_balance_rows(balance, rows_key, amount_key):
        """Use archived end balances only when a fresh ERP point snapshot is unavailable."""
        return [
            {"name": row.get("name"), amount_key: _num(row.get("balance"))}
            for row in ((balance or {}).get(rows_key) or [])
            if str(row.get("name") or "").strip()
        ]

    def positive_balance_rows(rows, amount_key):
        result = []
        for row in rows or []:
            amount = round(_num(row.get(amount_key, row.get("balance", 0.0))), 2)
            if not row.get("active", True) or amount <= 0:
                continue
            result.append({
                "id": row.get("id"), "name": row.get("name"), "balance": amount,
                # Vendor page's payable endpoint uses `payable`; Dashboard top
                # lists use `balance`. Carry both names for one exact amount.
                **({"payable": amount} if amount_key == "payable" else {}),
            })
        return sorted(result, key=lambda row: (-row["balance"], str(row.get("name") or "")))

    def overlay_anchor_date(as_of):
        anchors = [
            row for row in _balance_overlay().get("anchors", [])
            if str(row.get("date")) <= str(as_of)
        ]
        return str(anchors[-1].get("date") or "") if anchors else ""

    seed_endpoints = local_seed.get("endpoints", {}) if isinstance(local_seed, dict) else {}
    seed_customer_ledgers = local_seed.get("customer_ledgers", {}) if isinstance(local_seed, dict) else {}
    seed_vendor_ledgers = local_seed.get("vendor_ledgers", {}) if isinstance(local_seed, dict) else {}
    seed_bank_statements = local_seed.get("bank_statements", {}) if isinstance(local_seed, dict) else {}
    bank_accounts = seed_endpoints.get("bank_accounts") or load_book_balance_accounts() or [
        {
            "id": 1,
            "name": "Operating Bank",
            "account_no": "",
            "bank_name": "ERP Bank",
            "branch": "",
            "ifsc": "",
            "initial_balance": bank_net,
            "initial_balance_date": str(today),
            "active": True,
            "current_balance": bank_net,
        }
    ]
    archive_manifest = load_archive_manifest()
    exports_config = seed_endpoints.get("exports_config") or {"company_name": "ValliMuruga Industires pvt ltd", "gstin": "", "state_code": "29"}
    if not exports_config.get("operating_balance_opening") and archive_manifest.get("operating_balance_opening"):
        exports_config = {
            **exports_config,
            "operating_balance_opening": archive_manifest["operating_balance_opening"],
        }
    opening = exports_config.get("operating_balance_opening") or {}
    try:
        opening_as_of = datetime.fromisoformat(str(opening.get("as_of"))).date()
    except Exception:
        opening_as_of = today - timedelta(days=1)
    movement_start = opening_as_of + timedelta(days=1)

    write_snapshot("/api/me", {"username": "otomy", "can_write": False})
    write_snapshot("/api/dashboard/latest-date", {"latest_date": str(today)})
    write_snapshot("/api/machines/odometer", odometer_readings)
    write_snapshot("/api/machines/odometer-history", odometer_history)
    write_snapshot("/api/machines/fuel-issued", vmi_loader_fuel_issues)
    write_snapshot("/api/machines/fuel-received", fuel_received_rows)
    write_snapshot("/api/machines/fuel-balance", fuel_balance)
    # The Operations page reads this one static bundle, rather than three
    # browser requests.  Keep the individual snapshots for compatibility.
    write_snapshot("/api/machines/summary", {
        "odometer": odometer_readings,
        "odometer_history": odometer_history,
        "fuel_issued": vmi_loader_fuel_issues,
        "fuel_received": fuel_received_rows,
        "fuel_balance": fuel_balance,
    })
    write_snapshot("/api/customers/", customers_full)
    write_snapshot("/api/customers/?active_only=false", customers_full)
    write_snapshot(f"/api/customers/?active_only=false&as_of={today}", customers_full)
    write_snapshot("/api/customers/outstanding", customers_outstanding)
    write_snapshot(f"/api/customers/outstanding?as_of={today}", customers_outstanding)
    write_snapshot("/api/vendors/", vendors_full)
    write_snapshot("/api/vendors/?active_only=false", vendors_full)
    write_snapshot(f"/api/vendors/?active_only=false&as_of={today}", vendors_full)
    write_snapshot("/api/vendors/payables", vendors_payables)
    write_snapshot(f"/api/vendors/payables?as_of={today}", vendors_payables)
    # The Vendor page groups this complete, already-synced ledger source by
    # its selected date range.  Publishing it once avoids a browser request
    # for every supplier ledger while leaving the individual ledger snapshots
    # intact for the on-demand detail view.
    write_snapshot("/api/vendors/ledger-summary", {"ledgers": vendor_ledgers})
    write_snapshot("/api/bank/accounts", bank_accounts)
    for account in bank_accounts:
        write_snapshot(f"/api/bank/accounts/{account['id']}/statement", seed_bank_statements.get(str(account["id"]), []))
    write_snapshot("/api/emi/", seed_endpoints.get("emi", []))
    write_snapshot("/api/workers/", [row for row in seed_endpoints.get("workers", []) if row.get("active", True)])
    write_snapshot("/api/workers/?active_only=false", seed_endpoints.get("workers", []))
    write_snapshot("/api/exports/config", exports_config)
    write_snapshot("/api/sync/erp/config", {"erp_base": ERP_BASE, "erp_org": ERP_ORG, "erp_username": ERP_USER, "last_sync": datetime.now(IST).isoformat(timespec="seconds")})
    write_snapshot("/api/sync/erp/status", {"last_sync": datetime.now(IST).isoformat(timespec="seconds"), "source": "github-actions"})

    customer_ledgers = build_customer_ledgers(customers_full, all_sales, repayments, today, customer_ledgers_full)
    for row in customers_full:
        write_snapshot(
            f"/api/customers/ledger/{row['id']}",
            customer_ledgers.get(str(row["id"]))
            or seed_customer_ledgers.get(str(row["id"]), empty_ledger(row["name"], row.get("outstanding", 0.0))),
        )
    for row in vendors_full:
        write_snapshot(
            f"/api/vendors/ledger/{row['id']}",
            vendor_ledgers.get(str(row["id"])) or seed_vendor_ledgers.get(str(row["id"]), empty_ledger(row["name"], row.get("payable", 0.0))),
        )

    for start, end in ranges:
        control = control_by_range.get((start, end))
        if control is None:
            range_boulders = rows_between(boulder_rows, start, end)
            control = build_control(
                rows_between(all_sales, start, end),
                rows_between(all_expenses, start, end),
                start,
                end,
                boulders={
                    "total_tonnes": sum(_num(row.get("total_tonnes")) for row in range_boulders),
                    "total_trips": sum(_num(row.get("trips")) for row in range_boulders),
                    "materials": [],
                    "suppliers": [],
                },
                debtors=debtors_as_of(end) or [{"name": row["name"], "outstanding": row.get("outstanding", 0.0)} for row in customers_full],
                creditors=creditors_as_of(end) or [{"name": row["name"], "payable": row.get("payable", 0.0)} for row in vendors_full],
                cash_balance=cash_balance,
                bank_net=bank_net,
                labour=rows_between(labour_rows, start, end),
                parts=rows_between(parts_rows, start, end),
                machines=rows_between(machines_rows, start, end),
                vendor_payments=rows_between(vendor_payments, start, end),
                bank_balance_book=bank_balance_book,
                cash_balance_office_book=cash_balance_office_book,
                repayments=rows_between(repayments, start, end),
            )
        archive_balance = archive_balances.get(str(end)) if end < today and isinstance(archive_balances, dict) else None
        end_debtors = debtors_as_of(end) or archive_balance_rows(
            archive_balance, "receivables_rows", "outstanding"
        )
        end_creditors = creditors_as_of(end)
        payable_source_rows = end_creditors
        used_mapped_creditors = False
        # Older saved balance snapshots have the same name-only shape as the
        # archive.  Never send either form directly to the ID-backed vendor
        # renderer: resolve it first or stop the publish.
        if end_creditors and not all(str(row.get("erp_supplier_id") or "").strip() for row in end_creditors):
            used_mapped_creditors = True
        elif not end_creditors and archive_balance:
            payable_source_rows = archive_balance.get("payables_rows") or []
            end_creditors = payable_source_rows
            used_mapped_creditors = True
        # A supplier can be removed from today's Loctell master after a real
        # historical payable existed.  Keep it on that dated page only; never
        # reintroduce it into the live Vendor view.
        range_vendor_master = historical_vendor_master_rows(vendors_full, end_creditors)
        if used_mapped_creditors:
            # Resolve name-only archive balances against the same dated master
            # that will render them, including a legitimate retired supplier.
            end_creditors = archived_vendor_balances_as_of(
                payable_source_rows, range_vendor_master
            )
        customer_rows = build_customer_range_rows(
            customers_full,
            all_sales,
            rows_between(all_sales, start, end),
            rows_between(repayments, start, end),
            archive_balance,
            ending_debtors=end_debtors,
            as_of=end,
            all_repayments=repayments,
            aging_sales=aging_sales,
            aging_repayments=aging_repayments,
        )
        vendor_rows = vendor_rows_as_of(range_vendor_master, end_creditors, vendor_ledgers, str(end))
        receivable_rows = positive_balance_rows(customer_rows, "total_outstanding")
        payable_rows = positive_balance_rows(vendor_rows, "payable")

        # The control-room payment blocks must mirror the Customer/Vendor
        # pages exactly.  They remain deliberately separate from Credit
        # Repayment, whose display removes same-period spot-sale settlements.
        control["customer_page_rows"] = customer_rows
        paid_by_vendor = {}
        for payment in rows_between(vendor_payments, start, end):
            identity = _vendor_identity(payment)
            paid_by_vendor[identity] = paid_by_vendor.get(identity, 0.0) + _num(payment.get("amount"))
        vendor_page_rows = []
        for row in vendor_rows:
            page_row = dict(row)
            entries = (vendor_ledgers.get(str(row.get("id"))) or {}).get("entries") or []
            page_row["range_purchased"] = round(sum(
                _num(entry.get("credit", entry.get("amount")))
                for entry in entries
                if entry.get("type") == "purchase" and str(start) <= str(entry.get("date") or "")[:10] <= str(end)
            ), 2)
            page_row["range_paid"] = round(paid_by_vendor.get(_vendor_identity(row), 0.0), 2)
            vendor_page_rows.append(page_row)
        control["vendor_page_rows"] = vendor_page_rows

        if used_mapped_creditors:
            expected_payables = round(sum(
                max(0.0, _num(row.get("balance", row.get("payable", 0.0))))
                for row in payable_source_rows
            ), 2)
            actual_payables = round(sum(_num(row.get("payable")) for row in payable_rows), 2)
            if abs(actual_payables - expected_payables) > 0.01:
                raise ErpFetchError(
                    f"historical vendor payable parity failed for {end}: "
                    f"archive={expected_payables:.2f} snapshot={actual_payables:.2f}"
                )

        # The Dashboard must not have independent balance math. Its tiles and
        # top-five lists come directly from the selected-date Customer/Vendor
        # rows written below.
        control = apply_seed_control_overrides(control, local_seed, start, end)
        summary = control.setdefault("summary", {})
        summary["receivables"] = round(sum(row["balance"] for row in receivable_rows), 2)
        summary["payables"] = round(sum(row["balance"] for row in payable_rows), 2)
        control["top_receivables"] = receivable_rows[:5]
        control["top_payables"] = payable_rows[:5]
        control["internal_transfers"] = rows_between(internal_transfers, start, end)
        overlay_balance = _overlay_balance(str(end), all_sales, all_expenses, repayments, internal_transfers)
        if overlay_balance:
            summary = control.setdefault("summary", {})
            summary["bank_balance"] = overlay_balance[0]
            summary["cash_balance_office"] = overlay_balance[1]
            summary["operating_balance_from"] = overlay_anchor_date(end)
        write_snapshot(f"/api/dashboard/control?from_date={start}&to_date={end}", control)
        write_snapshot(
            f"/api/customers/?active_only=false&from_date={start}&to_date={end}&as_of={end}",
            customer_rows,
        )
        write_snapshot(f"/api/vendors/?active_only=false&as_of={end}", vendor_rows)
        write_snapshot(f"/api/vendors/payments/?from_date={start}&to_date={end}", rows_between(vendor_payments, start, end))
        write_snapshot(f"/api/vendors/payables?as_of={end}", payable_rows)
        write_snapshot(f"/api/sales/?from_date={start}&to_date={end}", rows_between(all_sales, start, end))
        write_snapshot(f"/api/expenses/?from_date={start}&to_date={end}", rows_between(all_expenses, start, end))
        write_snapshot(f"/api/boulders/?from_date={start}&to_date={end}", rows_between(boulder_rows, start, end))
        write_snapshot(f"/api/machines/?from_date={start}&to_date={end}", rows_between(machines_rows, start, end))
        write_snapshot(f"/api/labour/?from_date={start}&to_date={end}", rows_between(labour_rows, start, end))
        write_snapshot(f"/api/parts/?from_date={start}&to_date={end}", rows_between(parts_rows, start, end))
        write_snapshot(f"/api/sync/erp/bank?from_date={start}&to_date={end}", rows_between(bank_rows, start, end))
        write_snapshot(f"/api/sync/erp/cash?from_date={start}&to_date={end}", rows_between(cash_rows, start, end))

    ledger_current = build_ledger_view(
        all_sales,
        all_expenses,
        vendor_payments,
        boulder_rows,
        (controls.get("mtd") or {}).get("customer_repayments", []),
        today.year,
        today.month,
        opening.get("bank_balance", 0.0),
        opening.get("cash_balance_office", 0.0),
        movement_start,
        today,
        overlay_repayments=repayments,
    )
    latest_summary = (latest_seed_control(local_seed) or {}).get("summary") or {}
    if ledger_current.get("rows") and "bank_balance" in latest_summary and "cash_balance_office" in latest_summary:
        ledger_current["rows"][-1]["bank_balance"] = latest_summary["bank_balance"]
        ledger_current["rows"][-1]["cash_balance_office"] = latest_summary["cash_balance_office"]
        ledger_current["totals"]["bank_balance"] = latest_summary["bank_balance"]
        ledger_current["totals"]["cash_balance_office"] = latest_summary["cash_balance_office"]
    write_snapshot(f"/api/dashboard/ledger-view?year={today.year}&month={today.month}", ledger_current)

    write_snapshot(
        f"/api/dashboard/monthly?year={today.year}&month={today.month}",
        {"year": today.year, "month": today.month, "sales": {}, "expenses": {}, "pnl": {}},
    )
    # GSTR-1: compute a real payload per month that has sales (mirrors the live
    # backend export) so otomy no longer serves an empty stub. Match sale -> customer
    # by name because sale rows always carry customer_name.
    name_to_gstin = {(c.get("name") or "").strip().lower(): (c.get("gstin") or "").strip() for c in customers_full}
    gstr1_months = {(today.year, today.month)}
    for s in all_sales:
        d = str(s.get("date") or "")
        if len(d) >= 7:
            gstr1_months.add((int(d[:4]), int(d[5:7])))
    for yr, mo in sorted(gstr1_months):
        write_snapshot(
            f"/api/exports/gstr1?year={yr}&month={mo}",
            build_gstr1(all_sales, name_to_gstin, exports_config, yr, mo),
        )
    (DATA_DIR / "snapshot").mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "snapshot" / "manifest.json", "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
                "source": "github-actions / loctell.com ERP",
                "ranges": [{"from": str(start), "to": str(end)} for start, end in ranges],
            },
            f,
            indent=2,
        )


