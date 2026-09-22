"""Financial books and customer/vendor calculations; arithmetic is unchanged.

Runtime settings and helper callbacks are explicit keyword-only dependencies.
The gha_sync entry point supplies them, preserving its existing public interface
and isolated importlib loaders used by localhost and repair tools.
"""

from datetime import date, datetime, timedelta
from shared_calculations import accumulate_sale_group
from shared_calculations import advance_book_balance
from shared_calculations import cashbook_totals as calculate_cashbook_totals
from shared_calculations import credit_due as calculate_credit_due
from shared_calculations import credit_liquidity_metrics
from shared_calculations import customer_sales_totals
from shared_calculations import daily_ledger_row as calculate_daily_ledger_row
from shared_calculations import daily_ledger_totals as calculate_daily_ledger_totals
from shared_calculations import exclusive_age_buckets
from shared_calculations import payable_due_aging as calculate_payable_due_aging
from shared_calculations import rebalance_book_rows


def build_control(
    sales,
    expenses,
    from_d,
    to_d,
    boulders=None,
    debtors=None,
    creditors=None,
    cash_balance=0.0,
    bank_net=0.0,
    repayments=None,
    labour=None,
    parts=None,
    machines=None,
    vendor_payments=None,
    bank_balance_book=0.0,
    cash_balance_office_book=0.0,
    *,
    _balance_overlay,
    _is_director_payment,
    _num,
    _overlay_mode,
    _payment_channel,
    _sale_channels,
    _sale_total,
):
    days        = (to_d - from_d).days + 1
    total_sales = sum(_sale_total(s) for s in sales)
    total_qty   = sum(_num(s["qty_mt"]) for s in sales)
    cash_collected = sum(_sale_total(s) for s in sales if s["payment_mode"] != "Credit")
    credit_sales   = total_sales - cash_collected
    labour = labour or []
    parts = parts or []
    machines = machines or []
    vendor_payments = vendor_payments or []
    expense_direct = sum(_num(e["amount"]) for e in expenses)
    labour_total = sum(_num(row.get("amount")) for row in labour)
    parts_total = sum(_num(row.get("total_amount")) for row in parts)
    total_exp = expense_direct + labour_total + parts_total
    director_expense_total = (
        sum(
            _num(e.get("amount"))
            for e in expenses
            if _is_director_payment(e.get("category"), e.get("description"), e.get("payment_mode"), e.get("notes"), when=e.get("date"))
        )
        + sum(
            _num(row.get("amount"))
            for row in labour
            if _is_director_payment(row.get("worker_name"), row.get("worker_type"), row.get("notes"), when=row.get("date"))
        )
        + sum(
            _num(row.get("total_amount"))
            for row in parts
            if _is_director_payment(row.get("machine_name"), row.get("part_name"), row.get("supplier"), row.get("notes"), when=row.get("date"))
        )
    )
    operating_total_exp = total_exp - director_expense_total
    # Only ERP expense rows have a recorded cash/bank mode.  Do not fabricate
    # a channel for legacy labour/parts rows that remain in total expenses.
    operating_expense_cash = 0.0
    operating_expense_bank = 0.0
    # The dashboard tiles must use the same reviewed corrections as the
    # cashbook and balance overlay.  Otherwise an ERP row corrected from cash
    # to bank appears in the right book but in the wrong dashboard tile.
    mode_corrections = _balance_overlay().get("corrections", [])
    for expense in expenses:
        if _is_director_payment(
            expense.get("category"), expense.get("description"), expense.get("payment_mode"),
            expense.get("notes"), when=expense.get("date"),
        ):
            continue
        channel = _overlay_mode(mode_corrections, expense) or _payment_channel(
            expense.get("payment_mode") or "Cash"
        )
        if channel == "cash":
            operating_expense_cash += _num(expense.get("amount"))
        else:
            operating_expense_bank += _num(expense.get("amount"))
    profit = total_sales - operating_total_exp

    # material mix
    by_material = {}
    for s in sales:
        k = s["material"] or "Unknown"
        if k not in by_material:
            by_material[k] = {"material": k, "qty_mt": 0.0, "amount": 0.0, "tickets": 0}
        by_material[k]["qty_mt"]  += _num(s["qty_mt"])
        by_material[k]["amount"]  += _sale_total(s)
        by_material[k]["tickets"] += 1

    # expense mix
    by_expense = {}
    for e in expenses:
        k = e["category"] or "General"
        by_expense[k] = by_expense.get(k, 0.0) + _num(e["amount"])
    if labour_total:
        by_expense["Labour"] = by_expense.get("Labour", 0.0) + labour_total
    if parts_total:
        by_expense["Parts"] = by_expense.get("Parts", 0.0) + parts_total

    # customer sales breakdown
    by_customer = {}
    for s in sales:
        c  = s["customer_name"] or "Cash Sale"
        m  = s["material"]      or "Mixed"
        k  = (c, m)
        g  = by_customer.setdefault(k, {
            "customer_name": c, "material": m,
            "ticket_count": 0, "qty_mt": 0.0, "amount": 0.0, "mdp_ton": 0.0,
            "bank_received": 0.0, "cash_received": 0.0,
            "paid_against_sale": 0.0, "credit_sale_amount": 0.0, "tickets": [],
        })
        amt = _sale_total(s)
        pm  = s["payment_mode"]
        # Split each sale into its real channels (handles SPLIT payments).
        s_cash, s_credit, s_upi = _sale_channels(s)
        accumulate_sale_group(g, amt, _num(s["qty_mt"]), _num(s.get("mdp_ton")), s_cash, s_credit, s_upi)
        g["tickets"].append({
            "date": s["date"], "ticket_no": s.get("ticket_no", "—"),
            "qty_mt": round(_num(s["qty_mt"]), 2),
            "amount": round(amt, 2), "payment_mode": pm,
        })

    csr = []
    for g in by_customer.values():
        csr.append({
            "customer_name": g["customer_name"], "material": g["material"],
            "ticket_count":  g["ticket_count"],
            "ticket_nos":    [t["ticket_no"] for t in g["tickets"]],
            "tickets":       g["tickets"],
            "qty_mt":               round(g["qty_mt"], 2),
            "amount":               round(g["amount"], 2),
            "mdp_ton":              round(g["mdp_ton"], 3),
            "bank_received":        round(g["bank_received"], 2),
            "cash_received":        round(g["cash_received"], 2),
            "paid_against_sale":    round(g["paid_against_sale"], 2),
            "credit_sale_amount":   round(g["credit_sale_amount"], 2),
        })
    csr.sort(key=lambda r: r["amount"], reverse=True)

    # expense rows for the detail table
    expense_rows = []
    for e in expenses:
        expense_rows.append({
            "date":         e["date"],
            "type":         "Expense",
            "category":     e["category"] or "Other",
            "description":  e["description"] or e["category"] or "Expense",
            "party":        "",
            "payment_mode": e["payment_mode"] or "",
            "remarks":      e.get("notes") or "",
            "amount":       round(_num(e["amount"]), 2),
            # Preserve the exact decision behind summary.expenses.  The browser
            # receives no notes field, so it must not try to classify a row again.
            "is_operating_expense": not _is_director_payment(
                e.get("category"), e.get("description"), e.get("payment_mode"),
                e.get("notes"), when=e.get("date"),
            ),
        })
    for row in labour:
        expense_rows.append({
            "date": row.get("date"),
            "type": "Labour",
            "category": row.get("worker_type") or "Labour",
            "description": row.get("worker_name") or "Labour entry",
            "party": row.get("worker_name") or "",
            "payment_mode": "Paid" if row.get("paid") else "Unpaid",
            "remarks": row.get("notes") or "",
            "amount": round(_num(row.get("amount")), 2),
            "is_operating_expense": not _is_director_payment(
                row.get("worker_name"), row.get("worker_type"), row.get("notes"),
                when=row.get("date"),
            ),
        })
    for row in parts:
        expense_rows.append({
            "date": row.get("date"),
            "type": "Part",
            "category": row.get("machine_name") or "Parts",
            "description": row.get("part_name") or "Part / Repair",
            "party": row.get("supplier") or "",
            "payment_mode": "",
            "remarks": row.get("notes") or "",
            "amount": round(_num(row.get("total_amount")), 2),
            "is_operating_expense": not _is_director_payment(
                row.get("machine_name"), row.get("part_name"), row.get("supplier"),
                row.get("notes"), when=row.get("date"),
            ),
        })
    expense_rows.sort(key=lambda r: (r["date"], r["amount"]), reverse=True)

    # trend
    trend = []
    for i in range(days):
        d  = str(from_d + timedelta(days=i))
        ds = sum(_sale_total(s) for s in sales if s["date"] == d)
        de = (
            sum(_num(e["amount"]) for e in expenses if e["date"] == d)
            + sum(_num(row.get("amount")) for row in labour if row.get("date") == d)
            + sum(_num(row.get("total_amount")) for row in parts if row.get("date") == d)
        )
        trend.append({
            "date": d, "sales": round(ds, 2), "expenses": round(de, 2),
            "profit": round(ds - de, 2),
            "qty_mt": round(sum(_num(s["qty_mt"]) for s in sales if s["date"] == d), 2),
        })

    # receivables from debtors
    receivables     = []
    total_receivable = 0.0
    for d in sorted(debtors or [], key=lambda r: r["outstanding"], reverse=True):
        if d["outstanding"] > 0:
            receivables.append({"name": d["name"], "balance": d["outstanding"]})
            total_receivable += d["outstanding"]
    # This tile is a normal customer receivable, not a special hard-coded
    # balance.  Derive it from the same selected-date debtor list as the
    # receivables total so it heals with every Loctell correction.
    kumar_balance = next(
        (
            _num(row.get("outstanding", row.get("balance", 0.0)))
            for row in debtors or []
            if str(row.get("name") or "").strip().upper() == "KUMAR SIR"
        ),
        0.0,
    )

    # payables from creditors
    payables       = []
    total_payable  = 0.0
    for c in sorted(creditors or [], key=lambda r: r["payable"], reverse=True):
        if c["payable"] > 0:
            payables.append({"name": c["name"], "balance": c["payable"]})
            total_payable += c["payable"]

    # repayments
    rp = repayments or []
    rp_total        = round(sum(r["amount"]            for r in rp), 2)
    rp_pay_total    = round(sum(r["payment_received"]  for r in rp), 2)
    rp_bank_total   = round(sum(r["bank_received"]     for r in rp), 2)
    rp_cash_total   = round(sum(r["cash_received"]     for r in rp), 2)

    # Credit-liquidity KPIs are available only for the clean Loctell period.
    # They use the ticket tender split and gross customer cash received, never
    # cashbook overlays or reconciliation adjustments.  A positive net-credit
    # figure is profit/cash that remains with customers at period end.
    credit_sale_total = round(sum(row["credit_sale_amount"] for row in csr), 2)
    credit_recovery_total = rp_pay_total
    credit_liquidity = credit_liquidity_metrics(
        profit, total_qty, credit_sale_total, credit_recovery_total,
        eligible=from_d >= date(2026, 6, 1),
    )

    # alerts
    alerts = []
    if profit < 0:
        alerts.append({"level": "danger", "title": "Loss in selected period",
                       "detail": "Expenses higher than sales."})
    if total_qty > 0 and not (boulders or {}).get("total_tonnes"):
        alerts.append({"level": "warning", "title": "Boulder input missing",
                       "detail": "Sales exist but quarry input was not captured."})
    if not alerts:
        alerts.append({"level": "good", "title": "No major control alert",
                       "detail": "Data looks stable."})

    return {
        "period": {"from": str(from_d), "to": str(to_d), "days": days},
        "summary": {
            "sales":            round(total_sales, 2),
            "cash_collected":   round(cash_collected, 2),
            "credit_sales":     round(credit_sales, 2),
            "expenses":         round(operating_total_exp, 2),
            "operating_expense_cash": round(operating_expense_cash, 2),
            "operating_expense_bank": round(operating_expense_bank, 2),
            "expenses_before_director_adjustment": round(total_exp, 2),
            "profit":           round(profit, 2),
            "margin_pct":       round(profit / total_sales * 100, 1) if total_sales else 0.0,
            "sales_qty_mt":     round(total_qty, 2),
            "avg_rate_per_mt":  round(total_sales / total_qty, 2) if total_qty else 0.0,
            "boulder_input_mt": round((boulders or {}).get("total_tonnes", 0.0), 2),
            "boulder_trips":    round((boulders or {}).get("total_trips",  0.0), 2),
            "recovery_pct":     round(total_qty / (boulders or {}).get("total_tonnes", 0) * 100, 1)
                                if (boulders or {}).get("total_tonnes") else 0.0,
            "machine_hours":     round(sum(_num(row.get("running_hours")) for row in machines), 2),
            "machine_fuel_liters": round(sum(_num(row.get("fuel_liters")) for row in machines), 2),
            "fuel_per_mt":       0.0,
            "bank_balance":            round(bank_net, 2),
            "cash_balance_office":     round(cash_balance, 2),
            "bank_balance_book":       round(bank_balance_book, 2),
            "cash_balance_office_book": round(cash_balance_office_book, 2),
            "operating_balance_from":  str(from_d),
            "kumar_balance":           round(kumar_balance, 2),
            "credit_payment_received": rp_pay_total,
            **credit_liquidity,
            "selected_period_profit_per_tonne":
                round(profit / total_qty, 2) if total_qty else 0.0,
            "selected_period_profit_director_adjusted": round(profit, 2),
            "selected_period_director_adjusted_profit_per_tonne":
                round(profit / total_qty, 2) if total_qty else 0.0,
            "receivables": round(total_receivable, 2),
            "payables":    round(total_payable,    2),
        },
        "mix": {
            "materials": sorted(by_material.values(), key=lambda r: r["amount"], reverse=True),
            "expenses":  [{"category": k, "amount": round(v, 2)}
                          for k, v in sorted(by_expense.items(), key=lambda i: i[1], reverse=True)],
        },
        "input": {
            "source":    "ERP",
            "materials": (boulders or {}).get("materials", []),
            "suppliers": (boulders or {}).get("suppliers", []),
        },
        "customer_sales":        csr,
        "customer_sales_totals": customer_sales_totals(csr),
        "customer_repayments":              rp,
        "customer_repayments_total":        rp_total,
        "customer_repayments_payment_total": rp_pay_total,
        "customer_repayments_bank_total":   rp_bank_total,
        "customer_repayments_cash_total":   rp_cash_total,
        "machine_summary": [],
        "expense_rows":    expense_rows,
        "trend":           trend,
        "top_receivables": receivables[:5],
        "top_payables":    payables[:5],
        "alerts":          alerts,
    }


def _overlay_mode(corrs, e, *, _num):
    amt = _num(e.get("amount"))
    hay = ((e.get("category") or "") + " " + (e.get("description") or "") + " " + (e.get("notes") or "")).upper()
    dt = str(e.get("date", ""))[:10]
    for c in corrs:
        if abs(amt - _num(c.get("amount"))) < 1 \
           and (not c.get("contains") or str(c["contains"]).upper() in hay) \
           and (not c.get("date_from") or dt >= c["date_from"]) \
           and (not c.get("date_to") or dt <= c["date_to"]):
            return c.get("force")
    return None


def _overlay_balance(
    to_iso,
    sales,
    expenses,
    repayments,
    internal_transfers=None,
    *,
    _balance_overlay,
    _num,
    _overlay_mode,
    _payment_channel,
    _sale_channels,
):
    """Bank/cash as of to_iso: latest anchor + statement bank + corrected movements.
    Returns (bank, cash) or None if no overlay. Vendor payments live in expenses (not added again)."""
    ov = _balance_overlay()
    anchors = [a for a in ov["anchors"] if str(a.get("date")) <= to_iso]
    if not anchors:
        return None
    a = anchors[-1]
    bank = _num(a.get("bank"))
    cash = _num(a.get("cash"))
    anchor_date = str(a.get("date"))
    cutoff = None
    if ov["stmt_rows"]:
        le = [r for r in ov["stmt_rows"] if str(r.get("date")) <= to_iso]
        if le:
            bank = _num(le[-1].get("balance"))
            cutoff = ov["stmt_to"]
    frm = (date.fromisoformat(anchor_date) + timedelta(days=1)).isoformat()
    corrs = ov["corrections"]
    # A spot sale's receipt is already captured by the sale's cash/UPI channels below. When that
    # same customer's payment also surfaces as a ledger "repayment" (because the ticket carried
    # any credit/outstanding), subtract the same-day, same-channel overlap so the spot payment is
    # not double-counted in the balance. (Fixes bank/cash over-count vs actual.)
    def _rcust(row):
        return str(row.get("customer_name", "")).strip().upper()
    spot_cash_by, spot_bank_by = {}, {}
    for s in sales:
        d = str(s.get("date", ""))[:10]
        if not (frm <= d <= to_iso):
            continue
        s_cash, _c, s_upi = _sale_channels(s)
        if s_cash:
            spot_cash_by[(_rcust(s), d)] = spot_cash_by.get((_rcust(s), d), 0.0) + s_cash
        if s_upi:
            spot_bank_by[(_rcust(s), d)] = spot_bank_by.get((_rcust(s), d), 0.0) + s_upi
    if to_iso >= frm:
        for s in sales:
            d = str(s.get("date", ""))[:10]
            if not (frm <= d <= to_iso):
                continue
            # Split each sale: cash portion -> cash, UPI/bank portion -> bank (SPLIT-aware),
            # so a part-cash/part-UPI sale lands in the right tile (matches localhost).
            s_cash, _s_credit, s_upi = _sale_channels(s)
            cash += s_cash
            if s_upi and (cutoff is None or d > cutoff):
                bank += s_upi
        for r in repayments:
            d = str(r.get("date", ""))[:10]
            if not (frm <= d <= to_iso):
                continue
            amt = _num(r.get("payment_received", r.get("amount")))
            key = (_rcust(r), d)
            if _payment_channel(r.get("mode")) == "cash":
                overlap = min(amt, spot_cash_by.get(key, 0.0))
                spot_cash_by[key] = spot_cash_by.get(key, 0.0) - overlap
                cash += amt - overlap
            elif cutoff is None or d > cutoff:
                overlap = min(amt, spot_bank_by.get(key, 0.0))
                spot_bank_by[key] = spot_bank_by.get(key, 0.0) - overlap
                bank += amt - overlap
        for e in expenses:
            d = str(e.get("date", ""))[:10]
            if not (frm <= d <= to_iso):
                continue
            ch = _overlay_mode(corrs, e) or _payment_channel(e.get("payment_mode") or "Cash")
            if ch == "cash":
                # Cash paid from the company office is a real cash outflow, including
                # director-share drawings. Classification changes P&L only.
                cash -= _num(e.get("amount"))
            elif cutoff is None or d > cutoff:
                bank -= _num(e.get("amount"))
        for transfer in internal_transfers or []:
            d = str(transfer.get("date", ""))[:10]
            if not (frm <= d <= to_iso):
                continue
            # Contra: cash falls, bank rises; it never affects P&L or GST liability.
            cash -= _num(transfer.get("amount"))
            if cutoff is None or d > cutoff:
                bank += _num(transfer.get("amount"))
    return round(bank, 2), round(cash, 2)


def build_ledger_view(
    sales,
    expenses,
    vendor_payments,
    boulder_rows,
    repayments,
    year,
    month,
    opening_bank,
    opening_cash,
    movement_start,
    today,
    overlay_repayments=None,
    internal_transfers=None,
    *,
    _balance_overlay,
    _num,
    _overlay_balance,
    _overlay_mode,
    _payment_channel,
    _sale_channels,
    _sale_total,
):
    month_start = date(year, month, 1)
    display_end = min(today, (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
    if month_start > today:
        return {"year": year, "month": month, "rows": [], "totals": {}}

    def by_date(rows, date_key="date"):
        out = {}
        for row in rows or []:
            value = row.get(date_key, "")
            if value:
                out.setdefault(value[:10], []).append(row)
        return out

    sales_by_date = by_date(sales)
    expenses_by_date = by_date(expenses)
    vendor_payments_by_date = by_date(vendor_payments)
    boulders_by_date = by_date(boulder_rows)
    repayments_by_date = by_date(repayments)
    transfers_by_date = by_date(internal_transfers or [])

    def repayment_channels(row):
        payment = _num(row.get("payment_received", row.get("amount")))
        cash = _num(row.get("cash_received"))
        bank = _num(row.get("bank_received"))
        if cash > 0 or bank > 0:
            return cash, bank
        return (payment, 0.0) if _payment_channel(row.get("mode") or "") == "cash" else (0.0, payment)

    bank_balance = _num(opening_bank)
    cash_balance = _num(opening_cash)
    rows = []
    current = movement_start
    while current < month_start:
        key = str(current)
        for sale in sales_by_date.get(key, []):
            mode = sale.get("payment_mode") or "Credit"
            if mode.lower() == "credit":
                continue
            if _payment_channel(mode) == "cash":
                cash_balance += _sale_total(sale)
            else:
                bank_balance += _sale_total(sale)
        for receipt in repayments_by_date.get(key, []):
            cash_balance += _num(receipt.get("cash_received"))
            bank_balance += _num(receipt.get("bank_received"))
        for expense in expenses_by_date.get(key, []):
            if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                cash_balance -= _num(expense.get("amount"))
            else:
                bank_balance -= _num(expense.get("amount"))
        for transfer in transfers_by_date.get(key, []):
            cash_balance -= _num(transfer.get("amount"))
            bank_balance += _num(transfer.get("amount"))
        # Vendor payments are already booked as expenses; never subtract the vendor stream
        # again (that double-counts a vendor who is also an expense, e.g. ASHWATH SOLING).
        current += timedelta(days=1)
    current = month_start
    while current <= display_end:
        key = str(current)
        if current >= movement_start:
            for sale in sales_by_date.get(key, []):
                mode = sale.get("payment_mode") or "Credit"
                if mode.lower() == "credit":
                    continue
                if _payment_channel(mode) == "cash":
                    cash_balance += _sale_total(sale)
                else:
                    bank_balance += _sale_total(sale)
            for receipt in repayments_by_date.get(key, []):
                cash_balance += _num(receipt.get("cash_received"))
                bank_balance += _num(receipt.get("bank_received"))
            for expense in expenses_by_date.get(key, []):
                if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                    cash_balance -= _num(expense.get("amount"))
                else:
                    bank_balance -= _num(expense.get("amount"))
            for transfer in transfers_by_date.get(key, []):
                cash_balance -= _num(transfer.get("amount"))
                bank_balance += _num(transfer.get("amount"))
            # Vendor payments are already booked as expenses; never subtract the vendor
            # stream again (that double-counts a vendor who is also an expense).

        if current >= month_start:
            day_sales = sales_by_date.get(key, [])
            day_expenses = expenses_by_date.get(key, [])
            day_boulders = boulders_by_date.get(key, [])
            day_repayments = repayments_by_date.get(key, [])
            credit_repayment_cash = sum(repayment_channels(row)[0] for row in day_repayments)
            credit_repayment_bank = sum(repayment_channels(row)[1] for row in day_repayments)
            expense_cash = 0.0
            expense_bank = 0.0
            corrections = _balance_overlay().get("corrections", [])
            for expense in day_expenses:
                channel = _overlay_mode(corrections, expense) or _payment_channel(expense.get("payment_mode") or "Cash")
                if channel == "cash":
                    expense_cash += _num(expense.get("amount"))
                else:
                    expense_bank += _num(expense.get("amount"))
            # Daily Book expenses come only from the ERP Expense source, where every row has a
            # Cash or Bank payment mode. Legacy Labour and Parts records are intentionally excluded.
            boulder_input_mt = sum(_num(row.get("total_tonnes")) for row in day_boulders)
            # Balance overlay must see the FULL repayment history from the anchor (mirrors the
            # tile), not just month-to-date — else pre-month receipts (e.g. 29-30 Jun) are missed
            # and the ledger cash/bank read low. `repayments` here is only mtd; use all-history.
            _ov = _overlay_balance(key, sales, expenses, overlay_repayments if overlay_repayments is not None else repayments, internal_transfers)
            row_bank = _ov[0] if _ov else round(bank_balance, 2)
            row_cash = _ov[1] if _ov else round(cash_balance, 2)
            rows.append(calculate_daily_ledger_row(
                key,
                sales=[(_sale_total(row), *_sale_channels(row), _num(row.get("qty_mt"))) for row in day_sales],
                repayment_cash=credit_repayment_cash, repayment_bank=credit_repayment_bank,
                expense_cash=expense_cash, expense_bank=expense_bank,
                internal_transfer=sum(_num(row.get("amount")) for row in transfers_by_date.get(key, [])),
                cash_balance=row_cash, bank_balance=row_bank,
                boulder_tonnes=boulder_input_mt,
                boulder_trips=sum(_num(row.get("trips")) for row in day_boulders),
            ))
        current += timedelta(days=1)

    totals = calculate_daily_ledger_totals(rows)
    return {"year": year, "month": month, "rows": rows, "totals": totals}


def build_cashbook_view(
    from_d,
    to_d,
    sales,
    expenses,
    repayments,
    opening,
    internal_transfers=None,
    *,
    ErpFetchError,
    _balance_overlay,
    _num,
    _overlay_balance,
    _overlay_mode,
    _payment_channel,
    _sale_channels,
    _sale_settlement_roundoff,
):
    """Build the canonical cash/bank books from the same rows as the ledger.

    The opening and closing figures come from the verified anchor/statement overlay. The visible
    movement rows must tie to that closing figure. If a verified physical count or bank statement
    re-anchors the balance inside the range, it is shown using the same named source row as
    localhost. An unexplained gap is a sync failure: the engine never invents a residual row.
    """
    from_d = from_d if isinstance(from_d, date) else date.fromisoformat(str(from_d))
    to_d = to_d if isinstance(to_d, date) else date.fromisoformat(str(to_d))

    def _customer_id_key(row):
        value = row.get("customer_id", row.get("erp_customer_id"))
        if value is None or str(value).strip() == "":
            return None
        return "id", str(value).strip()

    def _customer_name_key(row):
        # This is only a cashbook matching key; it does not change the stored
        # customer name or merge customer-master records.  Fresh ListSale rows
        # do not carry an ERP customer id, while the matching ledger repayment
        # does, so exact-name fallback is needed to avoid showing one payment
        # twice on the same day.
        value = row.get("customer_name") or row.get("customer") or row.get("name") or ""
        return "name", " ".join(str(value).split()).upper()

    def _sale_customer_key(row):
        return _customer_id_key(row) or _customer_name_key(row)

    def _repayment_customer_key(row, spot_rows):
        date_key = str(row.get("date", ""))[:10]
        id_key = _customer_id_key(row)
        if id_key is not None and (id_key, date_key) in spot_rows:
            return id_key, date_key
        return _customer_name_key(row), date_key

    def _row(day, particulars, party, kind, incoming, outgoing, ticket_no=None, settlement_roundoff=0.0, remarks=""):
        return {
            "date": str(day)[:10],
            "particulars": particulars,
            "party": party or "",
            "kind": kind,
            "in": round(_num(incoming), 2),
            "out": round(_num(outgoing), 2),
            "ticket_no": str(ticket_no or ""),
            "remarks": remarks or "",
            # Informational only: the running book balance uses in/out above.
            # Negative means Loctell settled less than the gross invoice.
            "settlement_roundoff": round(_num(settlement_roundoff), 2),
        }

    sales_in_range = [
        row for row in sales or []
        if str(from_d) <= str(row.get("date", ""))[:10] <= str(to_d)
    ]
    expenses_in_range = [
        row for row in expenses or []
        if str(from_d) <= str(row.get("date", ""))[:10] <= str(to_d)
    ]
    repayments_in_range = [
        row for row in repayments or []
        if str(from_d) <= str(row.get("date", ""))[:10] <= str(to_d)
    ]
    transfers_in_range = [
        row for row in (internal_transfers or [])
        if str(from_d) <= str(row.get("date", ""))[:10] <= str(to_d)
    ]
    spot_cash, spot_bank = {}, {}
    for sale in sales_in_range:
        s_cash, _s_credit, s_upi = _sale_channels(sale)
        key = (_sale_customer_key(sale), str(sale.get("date", ""))[:10])
        if s_cash:
            spot_cash[key] = spot_cash.get(key, 0.0) + s_cash
        if s_upi:
            spot_bank[key] = spot_bank.get(key, 0.0) + s_upi

    cash_rows, bank_rows = [], []
    for sale in sales_in_range:
        s_cash, _s_credit, s_upi = _sale_channels(sale)
        party = sale.get("customer_name") or "Customer"
        ticket_no = sale.get("ticket_no") or sale.get("ticket") or sale.get("bill_no")
        cash_roundoff, bank_roundoff = _sale_settlement_roundoff(sale)
        if s_cash:
            cash_rows.append(_row(
                sale.get("date"), "Spot sale (cash)", party, "sale", s_cash, 0,
                ticket_no=ticket_no, settlement_roundoff=-cash_roundoff,
            ))
        if s_upi:
            bank_rows.append(_row(
                sale.get("date"), "Spot sale (UPI/Bank)", party, "sale", s_upi, 0,
                ticket_no=ticket_no, settlement_roundoff=-bank_roundoff,
            ))

    for repayment in repayments_in_range:
        amount = _num(repayment.get("payment_received", repayment.get("amount")))
        if amount <= 0:
            continue
        party = repayment.get("customer_name") or "Customer"
        if _payment_channel(repayment.get("mode") or "Cash") == "cash":
            key = _repayment_customer_key(repayment, spot_cash)
            overlap = min(amount, spot_cash.get(key, 0.0))
            spot_cash[key] = spot_cash.get(key, 0.0) - overlap
            net = amount - overlap
            if net > 0.5:
                cash_rows.append(_row(repayment.get("date"), "Customer payment (cash)", party, "receipt", net, 0))
        else:
            key = _repayment_customer_key(repayment, spot_bank)
            overlap = min(amount, spot_bank.get(key, 0.0))
            spot_bank[key] = spot_bank.get(key, 0.0) - overlap
            net = amount - overlap
            if net > 0.5:
                bank_rows.append(_row(repayment.get("date"), "Customer payment (UPI/Bank)", party, "receipt", net, 0))

    overlay = _balance_overlay()
    corrections = overlay.get("corrections", [])
    for expense in expenses_in_range:
        channel = _overlay_mode(corrections, expense) or _payment_channel(expense.get("payment_mode") or "Cash")
        amount = _num(expense.get("amount"))
        if amount <= 0:
            continue
        label = (expense.get("category") or expense.get("description") or "Expense").strip()
        party = (expense.get("description") or expense.get("notes") or "").strip()
        target = cash_rows if channel == "cash" else bank_rows
        target.append(_row(expense.get("date"), f"Expense: {label}", party, "expense", 0, amount, remarks=expense.get("notes") or ""))
    for transfer in transfers_in_range:
        amount = _num(transfer.get("amount"))
        if amount <= 0:
            continue
        cash_rows.append(_row(transfer.get("date"), "Internal transfer to bank", transfer.get("bank_name"), "internal_transfer", 0, amount, remarks=transfer.get("remarks") or ""))
        bank_rows.append(_row(transfer.get("date"), "Internal transfer from office cash", transfer.get("cash_ledger"), "internal_transfer", amount, 0, remarks=transfer.get("remarks") or ""))

    def _balance(as_of):
        verified = _overlay_balance(str(as_of), sales, expenses, repayments, internal_transfers)
        if verified is not None:
            return verified
        return (
            _num(opening.get("bank_balance")),
            _num(opening.get("cash_balance_office")),
        )

    previous = from_d - timedelta(days=1)
    open_bank, open_cash = _balance(previous)
    close_bank, close_cash = _balance(to_d)

    def _sort_key(row):
        # A normal daily reconciliation belongs before that day's movements;
        # a deferred reconciliation/physical anchor belongs after them.  The
        # old generic adjustment-first sort could show an impossible negative
        # intermediate cash balance even when the verified daily close was
        # positive.
        return (row.get("date", ""), row.get("_cashbook_order", 1), -_num(row.get("in")))

    def _finalize(rows, opening_balance, closing_balance, channel):
        shown = [dict(row) for row in rows]
        shown.sort(key=_sort_key)
        # Workbook closings are evidence, not financial transactions.  Never
        # generate a "Verified daily cash reconciliation (workbook)" row.
        running = round(_num(opening_balance), 2)
        reconciled = []
        index = 0
        while index < len(shown):
            day = shown[index].get("date", "")
            day_rows = []
            while index < len(shown) and shown[index].get("date", "") == day:
                day_rows.append(shown[index])
                index += 1
            target = None
            target_particulars = None
            # A same-day physical count is independent evidence.  If one is
            # needed to reconcile the book, publish it with its true source
            # label rather than disguising it as a workbook movement.
            if channel == "cash":
                anchor = next(
                    (a for a in overlay.get("anchors", []) if str(a.get("date") or "")[:10] == str(day)[:10]),
                    None,
                )
                if anchor and anchor.get("cash") is not None:
                    target = _num(anchor.get("cash"))
                    target_particulars = "Verified balance adjustment (physical cash count)"
            deferred_gap = 0.0
            if target is not None:
                gap = round(_num(target) - (running + sum(_num(row.get("in")) - _num(row.get("out")) for row in day_rows)), 2)
                if abs(gap) > 0.5 and running + gap >= 0:
                    row = _row(day, target_particulars, "", "adjustment", max(gap, 0), max(-gap, 0))
                    row["adjustment"] = True
                    row["_cashbook_order"] = 0
                    running = advance_book_balance(running, _num(row.get("in")), _num(row.get("out")))
                    row["balance"] = running
                    reconciled.append(row)
                elif abs(gap) > 0.5:
                    deferred_gap = gap
            for row in day_rows:
                running = advance_book_balance(running, _num(row.get("in")), _num(row.get("out")))
                row["balance"] = running
                reconciled.append(row)
            if deferred_gap:
                row = _row(day, target_particulars, "", "adjustment", 0, max(-deferred_gap, 0))
                row["adjustment"] = True
                row["_cashbook_order"] = 2
                running = round(_num(target), 2)
                row["balance"] = running
                reconciled.append(row)
        shown = reconciled
        gap = round(_num(closing_balance) - running, 2)
        if abs(gap) > 0.5:
            anchor_date = None
            if channel == "cash":
                applicable = [
                    anchor for anchor in overlay.get("anchors", [])
                    if str(anchor.get("date") or "")[:10] <= str(to_d)
                ]
                if applicable:
                    anchor_date = str(applicable[-1].get("date"))[:10]
            adjustment_date = anchor_date if anchor_date and anchor_date >= str(from_d) else str(to_d)
            source = "physical cash count" if channel == "cash" else "bank statement"
            adjustment = _row(
                adjustment_date,
                f"Verified balance adjustment ({source})",
                "",
                "adjustment",
                gap if gap > 0 else 0,
                -gap if gap < 0 else 0,
            )
            adjustment["adjustment"] = True
            adjustment["_cashbook_order"] = 2
            shown.append(adjustment)
            shown.sort(key=_sort_key)
            shown, running = rebalance_book_rows(
                shown, round(_num(opening_balance), 2), number=_num)
            if abs(round(_num(closing_balance) - running, 2)) > 0.5:
                raise ErpFetchError(
                    f"common engine {channel} book does not tie for {from_d}..{to_d}: "
                    f"opening={_num(opening_balance):.2f}, expected_closing={_num(closing_balance):.2f}, "
                    f"gap_after_anchor={_num(closing_balance) - running:.2f}"
                )
        for row in shown:
            row.pop("_cashbook_order", None)
        return calculate_cashbook_totals(shown, opening_balance, running, number=_num)

    return {
        "from": str(from_d),
        "to": str(to_d),
        "opening_as_of": str(previous),
        "cash": _finalize(cash_rows, open_cash, close_cash, "cash"),
        "bank": _finalize(bank_rows, open_bank, close_bank, "bank"),
    }


def empty_ledger(name, closing=0.0):
    return {
        "name": name,
        "opening_balance": 0.0,
        "entries": [],
        "closing_balance": round(closing, 2),
        "received": 0.0,
        "erp_received": 0.0,
        "age_0_15": 0.0,
        "age_16_30": 0.0,
        "age_31_45": 0.0,
        "age_45_plus": round(max(closing, 0.0), 2),
    }


def build_vendor_ledgers(
    vendors_full,
    vendor_payments,
    full_ledgers=None,
    *,
    _num,
    _vendor_identity,
    empty_ledger,
):
    full_ledgers = full_ledgers or {}
    payments_by_identity = {}
    payments_by_name = {}
    for payment in vendor_payments:
        payments_by_identity.setdefault(_vendor_identity(payment), []).append(payment)
        payments_by_name.setdefault(payment.get("vendor_name", ""), []).append(payment)

    ledgers = {}
    for vendor in vendors_full:
        name = vendor.get("name", "")
        payable = round(_num(vendor.get("payable")), 2)
        full_entries = full_ledgers.get(_vendor_identity(vendor))
        if full_entries is not None:
            # Tally view from the full loctell ledger: Purchase=Credit, Payment=Debit, running=payable.
            entries_sorted = sorted(full_entries, key=lambda x: (str(x["date"]), 0 if x["type"] == "purchase" else 1))
            window_net = round(sum(e["credit"] - e["debit"] for e in entries_sorted), 2)
            opening = round(payable - window_net, 2)
            if abs(opening) < 100:
                opening = 0.0  # rounding residual on a fully-captured history, not a real prior balance
            entries = []
            running = opening
            for index, e in enumerate(entries_sorted, start=1):
                running = round(running + e["credit"] - e["debit"], 2)
                entries.append({**e, "id": index,
                                "amount": e.get("credit") or e.get("debit") or 0.0,
                                "running_balance": running, "balance": running})
            ledger = empty_ledger(name, payable)
            ledger.update({
                "vendor_id": vendor.get("id"), "vendor_name": name,
                "opening_balance": opening, "entries": entries,
                "closing_balance": round(running, 2), "source": "erp",
            })
            ledgers[str(vendor.get("id"))] = ledger
            continue

        # Fallback: lightweight payments-only ledger (Tally convention: Payment=Debit).
        payments = payments_by_identity.get(_vendor_identity(vendor), [])
        # Older snapshot rows did not retain a supplier ID.  A name fallback
        # is safe only for an actually ID-less master, never for two suppliers
        # that happen to share a display name.
        if not payments and not str(vendor.get("erp_supplier_id") or "").strip():
            payments = payments_by_name.get(name, [])
        total_payments = round(sum(_num(row.get("amount")) for row in payments), 2)
        opening = round(payable + total_payments, 2)
        entries = []
        running = opening
        for index, payment in enumerate(sorted(payments, key=lambda row: (row.get("date", ""), row.get("reference", ""))), start=1):
            amount = _num(payment.get("amount"))
            running = round(running - amount, 2)
            entries.append({
                "type": "payment", "vch_type": "Payment", "id": index,
                "date": payment.get("date"),
                "description": f"Payment ({payment.get('mode') or 'Payment'})" + (f" Ref: {payment.get('reference')}" if payment.get("reference") else ""),
                "amount": amount, "debit": amount, "credit": 0.0,
                "running_balance": running, "balance": running,
            })
        ledger = empty_ledger(name, payable)
        ledger.update({
            "vendor_id": vendor.get("id"), "vendor_name": name,
            "opening_balance": opening, "entries": entries,
            "closing_balance": payable, "source": "db",
        })
        ledgers[str(vendor.get("id"))] = ledger
    return ledgers


def vendor_payable_due_aging(entries, payable, as_of, *, _num):
    """Adapt ERP ledger rows to the shared FIFO payable calculation."""
    target = round(max(_num(payable), 0.0), 2)
    if target <= 0:
        return calculate_payable_due_aging([], [], target, None)
    invoices, payments = [], []
    for entry in entries or []:
        entry_date = str(entry.get("date") or "")[:10]
        if not entry_date or entry_date > str(as_of):
            continue
        amount = round(_num(entry.get("credit") or entry.get("debit")), 2)
        if amount <= 0:
            continue
        if entry.get("type") == "purchase" or entry.get("vch_type") == "Purchase":
            invoices.append((entry_date, amount))
        else:
            # Preserve the existing ERP adapter's non-purchase classification.
            payments.append(amount)
    invoices.sort(key=lambda row: row[0])
    return calculate_payable_due_aging(
        invoices, payments, target, date.fromisoformat(str(as_of)))


def vendor_payable_age_buckets(entries, payable, as_of, *, _num):
    """Exclusive payable ageing, matching localhost customer-balance logic.

    A purchase creates a supplier bill and a payment clears the oldest bill.
    Once the authoritative Loctell payable is known, the unpaid remainder is
    therefore the newest bills first.  Allocating that closing balance from
    newest to oldest is equivalent to FIFO settlement while retaining the ERP
    balance as the single source of truth.
    """
    target = round(max(_num(payable), 0.0), 2)
    result = {"age_0_15": 0.0, "age_16_30": 0.0, "age_31_45": 0.0, "age_45_plus": 0.0}
    if target <= 0:
        return result
    as_of_date = date.fromisoformat(str(as_of))
    bills = []
    for index, entry in enumerate(entries or []):
        entry_date = str(entry.get("date") or "")[:10]
        if not entry_date or entry_date > str(as_of):
            continue
        if entry.get("type") != "purchase" and entry.get("vch_type") != "Purchase":
            continue
        amount = round(_num(entry.get("credit") or entry.get("amount")), 2)
        if amount > 0:
            bills.append((entry_date, index, amount))
    return exclusive_age_buckets(
        ((max((as_of_date - date.fromisoformat(entry_date)).days, 0), amount)
         for entry_date, _index, amount in sorted(bills, reverse=True)),
        target, round_each=True)


def vendor_rows_as_of(
    master_rows,
    balance_rows,
    vendor_ledgers,
    as_of,
    *,
    _num,
    _vendor_identity,
    vendor_payable_age_buckets,
    vendor_payable_due_aging,
):
    # Loctell can have separate supplier masters whose names differ only by
    # case or punctuation.  Supplier ID, not the display label, owns balance.
    balances = {
        _vendor_identity(row): _num(row.get("payable", row.get("balance", 0.0)))
        for row in balance_rows or []
        if str(row.get("name") or "").strip()
    }
    rows = []
    for source in master_rows:
        row = dict(source)
        name = str(row.get("name") or "").strip()
        payable = round(balances.get(_vendor_identity(row), 0.0), 2)
        ledger = vendor_ledgers.get(str(row.get("id"))) or {}
        entries = [entry for entry in (ledger.get("entries") or []) if str(entry.get("date") or "")[:10] <= str(as_of)]
        row.update({
            "active": row.get("active", True),
            "payable": payable,
            "total_purchases": round(sum(_num(entry.get("credit")) for entry in entries), 2),
            "total_payments": round(sum(_num(entry.get("debit")) for entry in entries), 2),
            **vendor_payable_due_aging(entries, payable, as_of),
            **vendor_payable_age_buckets(entries, payable, as_of),
        })
        rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("name") or "").upper())


def archived_vendor_balances_as_of(archive_rows, master_rows, *, ErpFetchError, _norm_name, _num):
    """Attach immutable supplier IDs to historical archive balances.

    Monthly archive balances predate the supplier-ID field and contain only the
    local master ID plus display name.  The dated Vendor page, however, is
    deliberately ID-backed so same-name Loctell suppliers are never merged.
    Reusing those name-only archive rows therefore makes every ID-backed
    supplier look like it has a zero balance.  Resolve each archive row to one
    current master row before using it; ambiguity is a hard failure rather than
    publishing an apparently-valid zero-payable month.
    """
    masters_by_id = {}
    masters_by_name = {}
    for master in master_rows or []:
        if not str(master.get("name") or "").strip():
            continue
        masters_by_id.setdefault(str(master.get("id") or ""), []).append(master)
        masters_by_name.setdefault(_norm_name(master.get("name")), []).append(master)

    resolved, unresolved = [], []
    for source in archive_rows or []:
        name = str(source.get("name") or "").strip()
        if not name:
            continue
        # The archive's numeric ID is authoritative only when it still names
        # the same supplier; a renamed/reordered master falls back to its
        # normalized display name instead.
        candidates = [
            row for row in masters_by_id.get(str(source.get("id") or ""), [])
            if _norm_name(row.get("name")) == _norm_name(name)
        ]
        if not candidates:
            candidates = masters_by_name.get(_norm_name(name), [])
        candidates = [row for row in candidates if str(row.get("erp_supplier_id") or "").strip()]
        if len(candidates) != 1:
            unresolved.append(name)
            continue
        master = candidates[0]
        resolved.append({
            "name": master.get("name") or name,
            "payable": _num(source.get("payable", source.get("balance", 0.0))),
            "erp_supplier_id": str(master.get("erp_supplier_id")),
        })

    if unresolved:
        raise ErpFetchError(
            "historical vendor balance identity unresolved; refusing to publish "
            f"zero-payable snapshot for: {', '.join(sorted(set(unresolved)))}"
        )
    return resolved


def historical_vendor_master_rows(
    current_rows,
    balance_rows,
    *,
    ErpFetchError,
    _norm_name,
    _vendor_identity,
    load_vendor_master,
):
    """Add only retired, historically-balanced suppliers to a dated view.

    Today's Supplier Balance is intentionally the authority for the live
    Vendor page.  A supplier removed from Loctell must not reappear there.
    It can nevertheless have had a real payable at the end of a prior month.
    For that dated snapshot only, recover its stable seed identity so the
    historical payable remains visible and reconcilable.
    """
    result = [dict(row) for row in current_rows or []]
    existing_keys = {_vendor_identity(row) for row in result}
    used_ids = {str(row.get("id") or "") for row in result}
    seed_rows = load_vendor_master()
    seed_by_id = {}
    seed_by_name = {}
    seed_by_erp = {}
    for row in seed_rows:
        if not str(row.get("name") or "").strip():
            continue
        seed_by_id.setdefault(str(row.get("id") or ""), []).append(row)
        seed_by_name.setdefault(_norm_name(row.get("name")), []).append(row)
        if str(row.get("erp_supplier_id") or "").strip():
            seed_by_erp.setdefault(str(row.get("erp_supplier_id")), []).append(row)

    unresolved = []
    for source in balance_rows or []:
        name = str(source.get("name") or "").strip()
        source_erp = str(source.get("erp_supplier_id") or source.get("supplier_id") or "").strip()
        if not name:
            continue
        if source_erp and f"erp:{source_erp}" in existing_keys:
            continue
        if not source_erp and any(_norm_name(row.get("name")) == _norm_name(name) for row in result):
            continue
        candidates = []
        if source_erp:
            candidates = seed_by_erp.get(source_erp, [])
        if not candidates:
            candidates = [
                row for row in seed_by_id.get(str(source.get("id") or ""), [])
                if _norm_name(row.get("name")) == _norm_name(name)
            ]
        if not candidates:
            candidates = seed_by_name.get(_norm_name(name), [])
        if len(candidates) != 1 or not str(candidates[0].get("erp_supplier_id") or "").strip():
            unresolved.append(name)
            continue
        row = dict(candidates[0])
        if str(row.get("id") or "") in used_ids:
            row["id"] = f"historical-{row['erp_supplier_id']}"
        result.append(row)
        used_ids.add(str(row.get("id") or ""))
        existing_keys.add(_vendor_identity(row))

    if unresolved:
        raise ErpFetchError(
            "historical supplier master unresolved; refusing to publish "
            f"incomplete payable snapshot for: {', '.join(sorted(set(unresolved)))}"
        )
    return result


def canonical_vendor_master(
    seed_rows,
    creditors,
    *,
    source_master=None,
    _norm_name,
    _vendor_identity,
    load_vendor_master,
):
    """Merge the checked-in full master with ERP rows by supplier ID.

    The explicit master makes a zero-balance supplier visible.  Creditors are
    still merged so a supplier newly created in Loctell is never hidden while
    waiting for a deliberate master-file update.
    """
    # A legacy name-only row is an approximation from before supplier IDs were
    # retained.  If Loctell now reports that name, its distinct ID-backed rows
    # replace the approximation rather than being merged into one supplier.
    current_names = {
        _norm_name(row.get("name"))
        for row in creditors or []
        if str(row.get("name") or "").strip() and str(row.get("erp_supplier_id") or "").strip()
    }
    master_sources = list(source_master) if source_master is not None else (list(load_vendor_master()) + list(seed_rows or []))
    by_identity = {}
    max_id = 0
    for source in master_sources:
        row = dict(source or {})
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if (not str(row.get("erp_supplier_id") or "").strip()
                and _norm_name(name) in current_names):
            continue
        key = _vendor_identity(row)
        current = by_identity.get(key, {})
        merged = {**current, **row, "name": name, "active": row.get("active", current.get("active", True))}
        if not merged.get("id"):
            max_id += 1
            merged["id"] = max_id
        by_identity[key] = merged
        max_id = max(max_id, int(merged.get("id") or 0))
    for creditor in creditors or []:
        name = str(creditor.get("name") or "").strip()
        supplier_id = str(creditor.get("erp_supplier_id") or "").strip()
        if not name or not supplier_id:
            continue
        key = _vendor_identity(creditor)
        current = by_identity.get(key)
        if current is None:
            max_id += 1
            current = {
                "id": max_id, "name": name, "gstin": "", "phone": "", "address": "",
                "opening_balance": 0.0, "notes": "", "active": True,
            }
        # When the Home > Suppliers master was fetched successfully, it owns
        # the display spelling.  ListSupplierBalance contributes the payable
        # and the ledger-link ID only.
        display_name = current.get("name") if source_master is not None and current.get("name") else name
        by_identity[key] = {
            **current, "name": display_name, "erp_supplier_id": supplier_id,
            "active": current.get("active", True),
        }
    return sorted(by_identity.values(), key=lambda row: (str(row.get("name") or "").upper(), _vendor_identity(row)))


def build_customer_ledgers(
    customers_full,
    all_sales,
    repayments,
    today,
    full_ledgers=None,
    *,
    LEDGER_HISTORY_START,
    _norm_name,
    _num,
    _prev_customer_ledgers_by_name,
    _sale_total,
    empty_ledger,
    load_archive_window,
):
    """Per-customer ledger from sales + receipts, linked by NAME (sale customer_id does NOT
    match the customer list id). When a reconciling FULL loctell ledger (sales + receipts incl.
    same-day spot receipts) is available it is used and reconciles to the ERP outstanding; else
    it falls back to the archive-based build (whose running balance can be inflated because spot
    receipts are missing). Mirrors localhost's ledger shape."""
    full_ledgers = full_ledgers or {}
    prev_ledgers_by_name = _prev_customer_ledgers_by_name()
    hist = load_archive_window(LEDGER_HISTORY_START, today)
    sales_src = hist.get("sales") or all_sales or []
    reps_src = hist.get("receipts") or repayments or []
    sales_by_name = {}
    for s in sales_src:
        sales_by_name.setdefault(str(s.get("customer_name", "")).strip().upper(), []).append(s)
    reps_by_name = {}
    for r in reps_src:
        reps_by_name.setdefault(str(r.get("customer_name", "")).strip().upper(), []).append(r)

    ledgers = {}
    for cust in customers_full:
        name = cust.get("name", "")
        key = str(name).strip().upper()
        closing = round(_num(cust.get("outstanding", cust.get("balance"))), 2)

        # Prefer the FULL reconciling loctell ledger (Sale=Debit, Receipt=Credit incl. spot receipts).
        full_entries = full_ledgers.get(_norm_name(name))
        if not full_entries:
            # No fresh fetch this run: reuse the customer's OWN last reconciling snapshot rather than
            # overwriting it with the receipt-sparse archive build. Look it up by NAME (not by the
            # positional id, which shifts between syncs) — reusing by id would graft another customer's
            # ledger onto this one (wrong material) or drop to the archive build (missing recent
            # receipts). Keyed by name, each customer keeps its own full receipt history across id shifts.
            prev = prev_ledgers_by_name.get(_norm_name(name))
            if isinstance(prev, dict) and prev.get("source") == "erp" and prev.get("entries"):
                ledgers[str(cust.get("id"))] = prev
                continue
        if full_entries:
            es = sorted(full_entries, key=lambda x: (str(x["date"]), 0 if x["type"] == "sale" else 1))
            window_net = round(sum(e["debit"] - e["credit"] for e in es), 2)
            opening = round(closing - window_net, 2)
            if abs(opening) < 100:
                opening = 0.0  # rounding residual on a fully-captured history, not a real prior balance
            received = round(sum(e["credit"] for e in es), 2)
            erp_entries = []
            running = opening
            for idx, e in enumerate(es, start=1):
                running = round(running + e["debit"] - e["credit"], 2)
                erp_entries.append({**e, "id": idx, "amount": e.get("debit") or e.get("credit") or 0.0,
                                    "balance": running})
            ledger = empty_ledger(name, closing)
            ledger.update({
                "customer_id": cust.get("id"), "customer_name": name,
                "opening_balance": opening, "entries": erp_entries,
                "closing_balance": round(running, 2), "received": received,
                "erp_received": received, "source": "erp",
            })
            ledgers[str(cust.get("id"))] = ledger
            continue

        entries = []
        for s in sales_by_name.get(key, []):
            total = _sale_total(s)
            entries.append({
                "type": "sale",
                "id": s.get("id"),
                "date": s.get("date"),
                "description": (f"{s.get('material') or 'Sale'} — {s.get('vehicle_no') or ''}").strip(" —"),
                "debit": total, "credit": 0.0, "amount": total,
                "transport_charge": _num(s.get("transport_charge")),
                "ticket_no": s.get("ticket_no") or "",
                "vehicle_no": s.get("vehicle_no") or "",
                "material": s.get("material") or "",
                "qty_mt": s.get("qty_mt") or 0,
                "mdp_ton": s.get("mdp_ton"),
                "rate_per_mt": s.get("rate_per_mt") or 0,
                "payment_mode": s.get("payment_mode") or "",
                "gst_rate": s.get("gst_rate") or 0,
            })
        received = 0.0
        for r in reps_by_name.get(key, []):
            amt = _num(r.get("payment_received", r.get("amount")))
            received = round(received + amt, 2)
            entries.append({
                "type": "receipt",
                "id": None,
                "date": str(r.get("date", ""))[:10],
                "description": f"Receipt ({r.get('mode') or 'Payment'})" + (f" Ref: {r.get('reference')}" if r.get("reference") else ""),
                "debit": 0.0, "credit": amt, "amount": amt,
            })
        entries.sort(key=lambda x: (str(x.get("date") or ""), x.get("type") or ""))
        window_net = round(sum(_num(e.get("debit")) - _num(e.get("credit")) for e in entries), 2)
        opening = round(closing - window_net, 2)
        # Show an opening line only when it's non-negative (carried-forward dues). A negative
        # opening means credit repayments are undercounted in the data (the known estimation
        # gap) — in that case start at 0 like localhost rather than display a misleading
        # negative. The true balance is always shown via closing_balance (the ERP snapshot).
        result_entries = []
        if opening > 0.01:
            result_entries.append({
                "type": "opening", "id": None, "date": None,
                "description": "Opening Balance (before synced window)",
                "debit": opening, "credit": 0.0, "balance": opening,
            })
            running = opening
        else:
            running = 0.0
        for e in entries:
            running = round(running + _num(e.get("debit")) - _num(e.get("credit")), 2)
            e["balance"] = running
            result_entries.append(e)
        ledger = empty_ledger(name, closing)
        ledger.update({
            "customer_id": cust.get("id"),
            "customer_name": name,
            "opening_balance": opening,
            "entries": result_entries,
            "closing_balance": closing,
            "received": received,
            "erp_received": round(_num(cust.get("received", cust.get("erp_received", received))), 2),
            "source": "db",
        })
        ledgers[str(cust.get("id"))] = ledger
    return ledgers


def _format_material_sold(materials, *, _num):
    rows = sorted(
        materials.items(),
        key=lambda item: (_num(item[1].get("qty")), _num(item[1].get("amount"))),
        reverse=True,
    )
    if not rows:
        return "No sale"
    parts = []
    for material, totals in rows[:4]:
        qty = _num(totals.get("qty"))
        label = material or "Material"
        parts.append(f"{label} {qty:,.2f} MT" if qty else label)
    if len(rows) > 4:
        parts.append(f"+{len(rows) - 4} more")
    return ", ".join(parts)


def _credit_due_15_plus_by_name(
    customers,
    all_sales,
    all_repayments,
    as_of,
    days=15,
    *,
    IST,
    _norm_name,
    _num,
    _sale_channels,
):
    """Calculate unpaid credit material aged ``days`` days or more per customer."""
    as_of = str(as_of or datetime.now(IST).date())[:10]
    cutoff = (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()
    sales_by_name = {}
    for index, sale in enumerate(all_sales or []):
        name = str(sale.get("customer_name") or "").strip()
        customer_key = _norm_name(name)
        sale_date = str(sale.get("date") or "")[:10]
        if not customer_key or not sale_date or sale_date > as_of:
            continue
        _cash, credit, _upi = _sale_channels(sale)
        credit = round(max(credit, 0.0), 2)
        if credit > 0:
            sales_by_name.setdefault(customer_key, []).append({"date": sale_date, "unpaid": credit, "index": index})
    receipts_by_name = {}
    for index, repayment in enumerate(all_repayments or []):
        name = str(repayment.get("customer_name") or "").strip()
        customer_key = _norm_name(name)
        repayment_date = str(repayment.get("date") or "")[:10]
        amount = round(max(_num(repayment.get("payment_received", repayment.get("amount"))), 0.0), 2)
        if customer_key and repayment_date and repayment_date <= as_of and amount > 0:
            receipts_by_name.setdefault(customer_key, []).append({"date": repayment_date, "amount": amount, "index": index})

    result = {}
    for customer in customers or []:
        name = str(customer.get("name") or "").strip()
        customer_key = _norm_name(name)
        invoices = sorted(sales_by_name.get(customer_key, []), key=lambda row: (row["date"], row["index"]))
        receipts = sorted(receipts_by_name.get(customer_key, []), key=lambda row: (row["date"], row["index"]))
        target = round(max(_num(customer.get("outstanding", customer.get("balance", 0.0))), 0.0), 2)
        result[name] = round(calculate_credit_due(
            invoices, [receipt["amount"] for receipt in receipts], target, cutoff), 2)
    return result


def build_customer_range_rows(
    customers_full,
    all_sales,
    range_sales,
    range_repayments,
    archive_balance=None,
    ending_debtors=None,
    as_of=None,
    all_repayments=None,
    aging_sales=None,
    aging_repayments=None,
    *,
    _credit_due_15_plus_by_name,
    _format_material_sold,
    _norm_name,
    _num,
    _sale_channels,
    _sale_total,
):
    metrics = {}
    for sale in all_sales or []:
        name = str(sale.get("customer_name") or "").strip()
        if not name:
            continue
        metric = metrics.setdefault(name, {
            "material_totals": {},
            "range_total_sales": 0.0,
            "range_credit_sales": 0.0,
            "range_payment_received": 0.0,
            "range_latest_sale_date": "",
            "latest_sale_date": "",
        })
        sale_date = str(sale.get("date", ""))[:10]
        if sale_date > metric["latest_sale_date"]:
            metric["latest_sale_date"] = sale_date
    for sale in range_sales or []:
        name = str(sale.get("customer_name") or "").strip()
        if not name:
            continue
        metric = metrics.setdefault(name, {
            "material_totals": {},
            "range_total_sales": 0.0,
            "range_credit_sales": 0.0,
            "range_payment_received": 0.0,
            "range_latest_sale_date": "",
            "latest_sale_date": "",
        })
        sale_date = str(sale.get("date", ""))[:10]
        if sale_date > metric["range_latest_sale_date"]:
            metric["range_latest_sale_date"] = sale_date
        if sale_date > metric["latest_sale_date"]:
            metric["latest_sale_date"] = sale_date
        amount = _sale_total(sale)
        _sale_cash, sale_credit, _sale_upi = _sale_channels(sale)
        material = str(sale.get("material") or "Material").strip() or "Material"
        mat = metric["material_totals"].setdefault(material, {"qty": 0.0, "amount": 0.0})
        mat["qty"] += _num(sale.get("qty_mt"))
        mat["amount"] += amount
        metric["range_total_sales"] += amount
        metric["range_credit_sales"] += sale_credit
    for repayment in range_repayments or []:
        name = str(repayment.get("customer_name") or "").strip()
        if not name:
            continue
        metric = metrics.setdefault(name, {
            "material_totals": {},
            "range_total_sales": 0.0,
            "range_credit_sales": 0.0,
            "range_payment_received": 0.0,
            "range_latest_sale_date": "",
            "latest_sale_date": "",
        })
        metric["range_payment_received"] += _num(repayment.get("payment_received", repayment.get("amount")))

    outstanding_by_name = {}
    use_exact_end_balance = ending_debtors is not None
    if use_exact_end_balance:
        # Use the selected date's Loctell debtor list. A customer absent from
        # that historical list must not inherit today's outstanding balance.
        for row in ending_debtors or []:
            name = str(row.get("name") or "").strip()
            if name:
                outstanding_by_name[_norm_name(name)] = _num(
                    row.get("outstanding", row.get("balance", 0.0))
                )
    elif archive_balance:
        for row in archive_balance.get("receivables_rows") or archive_balance.get("top_receivables") or []:
            name = str(row.get("name") or "").strip()
            if name:
                outstanding_by_name[_norm_name(name)] = _num(row.get("balance"))

    due_15_plus = _credit_due_15_plus_by_name(
        customers_full,
        aging_sales if aging_sales is not None else all_sales,
        aging_repayments if aging_repayments is not None
        else (all_repayments if all_repayments is not None else range_repayments),
        as_of,
        days=16,
    ) if as_of else {}
    due_30_plus = _credit_due_15_plus_by_name(
        customers_full,
        aging_sales if aging_sales is not None else all_sales,
        aging_repayments if aging_repayments is not None
        else (all_repayments if all_repayments is not None else range_repayments),
        as_of,
        days=31,
    ) if as_of else {}
    due_45_plus = _credit_due_15_plus_by_name(
        customers_full,
        aging_sales if aging_sales is not None else all_sales,
        aging_repayments if aging_repayments is not None
        else (all_repayments if all_repayments is not None else range_repayments),
        as_of,
        days=45,
    ) if as_of else {}
    rows = []
    consumed_end_balance_keys = set()
    for customer in customers_full or []:
        row = dict(customer)
        name = str(row.get("name") or "").strip()
        metric = metrics.get(name, {})
        balance_key = _norm_name(name)
        if use_exact_end_balance and balance_key in consumed_end_balance_keys:
            outstanding = 0.0
        else:
            outstanding = outstanding_by_name.get(
                balance_key,
                0.0 if use_exact_end_balance else _num(row.get("outstanding", row.get("balance", 0.0))),
            )
            if use_exact_end_balance and balance_key in outstanding_by_name:
                consumed_end_balance_keys.add(balance_key)
        row.update({
            "balance": round(outstanding, 2),
            "outstanding": round(outstanding, 2),
            "total_outstanding": round(outstanding, 2),
            "material_sold": _format_material_sold(metric.get("material_totals", {})),
            "range_total_sales": round(_num(metric.get("range_total_sales")), 2),
            "range_credit_sales": round(_num(metric.get("range_credit_sales")), 2),
            "range_payment_received": round(_num(metric.get("range_payment_received")), 2),
            "credit_due_15_plus": due_15_plus.get(name, round(max(_num(row.get("credit_due_15_plus")), 0.0), 2)),
            "credit_due_30_plus": due_30_plus.get(name, round(max(_num(row.get("credit_due_30_plus")), 0.0), 2)),
            "credit_due_45_plus": due_45_plus.get(name, round(max(_num(row.get("credit_due_45_plus")), 0.0), 2)),
            "range_latest_sale_date": metric.get("range_latest_sale_date") or None,
            "latest_sale_date": metric.get("latest_sale_date") or None,
        })
        rows.append(row)
    def _date_sort_value(value):
        return int(str(value or "").replace("-", "") or "0")
    rows.sort(key=lambda row: (
        not row.get("active", True),
        -_date_sort_value(row.get("range_latest_sale_date") or row.get("latest_sale_date")),
        -_num(row.get("total_outstanding")),
        str(row.get("name") or ""),
    ))
    return rows


