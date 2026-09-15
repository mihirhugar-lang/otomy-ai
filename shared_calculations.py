"""Pure financial arithmetic shared by the cloud and localhost adapters.

No ERP, database, filesystem, clock or publication access belongs here.
Adapters own input parsing, row selection, dates and their existing rounding
boundaries. Keep the packaged localhost copy byte-identical (parity guard).
"""

from datetime import timedelta


def apply_fifo_payments(invoices, payments):
    """Mutate normalized unpaid invoices in caller order, skipping settled rows.

    Preserve the legacy rounding caused by visiting a settled invoice first:
    localhost receipts can have sub-cent precision before their first step.
    No sorting, identity merging, source filtering or receipt aggregation.
    """
    cursor = 0
    for amount in payments:
        remaining = amount
        if cursor and remaining > 0:
            remaining = round(remaining, 2)
        while remaining > 0 and cursor < len(invoices):
            invoice = invoices[cursor]
            applied = min(remaining, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            remaining = round(remaining - applied, 2)
            if invoice["unpaid"] == 0:
                cursor += 1


def credit_due(invoices, payments, target, cutoff):
    """FIFO overdue credit, preserving the adapter's mutable invoice contract."""
    apply_fifo_payments(invoices, payments)
    invoice_unpaid = round(sum(row["unpaid"] for row in invoices), 2)
    older = 0.0
    if target < invoice_unpaid:
        apply_fifo_payments(invoices, [round(invoice_unpaid - target, 2)])
    elif target > invoice_unpaid:
        older = round(target - invoice_unpaid, 2)
    overdue = round(sum(row["unpaid"] for row in invoices if row["date"] <= cutoff) + older, 2)
    return min(max(overdue, 0.0), target)


def exclusive_age_buckets(bills, target, *, round_each=False):
    """Allocate authoritative balance over caller-ordered (age_days, amount).

    Missing history is old debt; negative balances are advances, not overdue.
    Adapters preserve date filtering, tie order and their rounding boundary.
    """
    result = {"age_0_15": 0.0, "age_16_30": 0.0, "age_31_45": 0.0, "age_45_plus": 0.0}
    remaining = target
    iterator = iter(bills)
    while remaining > 0:
        try:
            days, amount = next(iterator)
        except StopIteration:
            break
        unpaid = min(remaining, amount)
        if unpaid <= 0:
            continue
        bucket = "age_0_15" if days <= 15 else "age_16_30" if days <= 30 else "age_31_45" if days <= 45 else "age_45_plus"
        value = result[bucket] + unpaid
        result[bucket] = round(value, 2) if round_each else value
        remaining = round(remaining - unpaid, 2)
    if remaining > 0:
        value = result["age_45_plus"] + remaining
        result["age_45_plus"] = round(value, 2) if round_each else value
    return {key: round(value, 2) for key, value in result.items()}


def customer_balance(opening, sales, receipts, snapshot=None):
    """Keep an authoritative signed snapshot, otherwise calculate the balance."""
    return snapshot if snapshot is not None else opening + sales - receipts


def accumulate_sale_group(group, amount, quantity, mdp, cash, credit, bank):
    """Add every ticket once; MDP is summed, never substituted with net tonnes."""
    group["ticket_count"] += 1
    group["qty_mt"] += quantity
    group["mdp_ton"] += mdp
    group["amount"] += amount
    group["credit_sale_amount"] += credit
    group["cash_received"] += cash
    group["bank_received"] += bank
    group["paid_against_sale"] += cash + bank


def advance_book_balance(balance, incoming, outgoing):
    """Preserve per-movement rounding; round-off metadata is not money."""
    return round(balance + incoming - outgoing, 2)


def rebalance_book_rows(rows, opening, *, number=lambda value: value):
    """Return copied, rebalanced rows in caller order, without sorting or plugs.

    Evidence selection and adjustment creation remain adapter responsibilities.
    The caller supplies its already-established initial rounding boundary.
    """
    shown = [dict(row) for row in rows]
    running = opening
    for row in shown:
        running = advance_book_balance(running, number(row["in"]), number(row["out"]))
        row["balance"] = running
    return shown, running


def cashbook_totals(rows, opening, closing, *, number=lambda value: value):
    """Summarize finished rows; do not replace their calculated closing."""
    return {
        "opening": round(number(opening), 2), "rows": rows,
        "total_in": round(sum(number(row["in"]) for row in rows), 2),
        "total_out": round(sum(number(row["out"]) for row in rows), 2),
        "settlement_roundoff": round(sum(number(row.get("settlement_roundoff", 0)) for row in rows), 2),
        "closing": round(closing, 2),
    }


def daily_ledger_row(day, *, sales, repayment_cash, repayment_bank,
                     expense_cash, expense_bank, internal_transfer,
                     cash_balance, bank_balance, boulder_tonnes, boulder_trips):
    """Build a Daily Ledger row from normalized amounts and verified balances.

    Each sale is (gross, cash, credit, bank, tonnes), with the adapter's existing
    split rounding. Keep summation order and round only at the old boundaries.
    Repayments here are the displayed gross amounts, not netted book movements.
    """
    qty_mt = sum(sale[4] for sale in sales)
    return {
        "date": str(day),
        "sale_trips": len(sales),
        "sale_amount": round(sum(sale[0] for sale in sales), 2),
        "spot_sale_amount": round(sum(sale[1] + sale[3] for sale in sales), 2),
        "spot_sale_cash": round(sum(sale[1] for sale in sales), 2),
        "spot_sale_bank": round(sum(sale[3] for sale in sales), 2),
        "credit_sale_amount": round(sum(sale[2] for sale in sales), 2),
        "qty_mt": round(qty_mt, 2),
        "credit_repayment": round(repayment_cash + repayment_bank, 2),
        "credit_repayment_cash": round(repayment_cash, 2),
        "credit_repayment_bank": round(repayment_bank, 2),
        "expenses": round(expense_cash + expense_bank, 2),
        "expense_cash": round(expense_cash, 2),
        "expense_bank": round(expense_bank, 2),
        "internal_transfer": round(internal_transfer, 2),
        "cash_balance_office": cash_balance,
        "bank_balance": bank_balance,
        "boulder_input_mt": round(boulder_tonnes, 2),
        "boulder_trips": round(boulder_trips, 2),
        "stock_in_plant_mt": round(boulder_tonnes - qty_mt, 2),
    }


def daily_ledger_totals(rows):
    """Sum displayed rounded daily rows; balances are last-day values, not sums."""
    return {
        "sale_trips": sum(row["sale_trips"] for row in rows),
        "sale_amount": round(sum(row["sale_amount"] for row in rows), 2),
        "spot_sale_amount": round(sum(row["spot_sale_amount"] for row in rows), 2),
        "spot_sale_cash": round(sum(row.get("spot_sale_cash", 0) for row in rows), 2),
        "spot_sale_bank": round(sum(row.get("spot_sale_bank", 0) for row in rows), 2),
        "qty_mt": round(sum(row.get("qty_mt", 0) for row in rows), 2),
        "credit_sale_amount": round(sum(row["credit_sale_amount"] for row in rows), 2),
        "credit_repayment": round(sum(row["credit_repayment"] for row in rows), 2),
        "credit_repayment_cash": round(sum(row.get("credit_repayment_cash", 0) for row in rows), 2),
        "credit_repayment_bank": round(sum(row.get("credit_repayment_bank", 0) for row in rows), 2),
        "expenses": round(sum(row["expenses"] for row in rows), 2),
        "expense_cash": round(sum(row.get("expense_cash", 0) for row in rows), 2),
        "expense_bank": round(sum(row.get("expense_bank", 0) for row in rows), 2),
        "boulder_input_mt": round(sum(row["boulder_input_mt"] for row in rows), 2),
        "boulder_trips": round(sum(row["boulder_trips"] for row in rows), 2),
        "stock_in_plant_mt": round(sum(row.get("stock_in_plant_mt", 0) for row in rows), 2),
        "cash_balance_office": rows[-1]["cash_balance_office"] if rows else 0.0,
        "bank_balance": rows[-1]["bank_balance"] if rows else 0.0,
    }


def sale_channels(total, payment_mode, cash, credit, upi):
    """Use captured tender when present; otherwise classify the gross total.

    Inputs are already numeric. Do not round here: cloud dictionaries and local
    database rows historically round at different adapter boundaries.
    """
    if cash + credit + upi > 0:
        return cash, credit, upi
    mode = payment_mode or "Credit"
    if mode.lower() == "credit":
        return 0.0, total, 0.0
    if "CASH" in mode.upper():
        return total, 0.0, 0.0
    return 0.0, 0.0, total


def settlement_roundoff(gross, cash, credit, upi):
    """Informational (cash, bank) difference; never a money movement."""
    difference = round(gross - cash - credit - upi, 2)
    if abs(difference) < 0.005:
        return 0.0, 0.0
    if cash > 0 and upi <= 0:
        return difference, 0.0
    if upi > 0 and cash <= 0:
        return 0.0, difference
    return 0.0, 0.0


def payable_due_aging(invoices, payments, target, as_of):
    """FIFO cumulative age bands anchored to an authoritative payable.

    Adapters supply chronological (ISO day, positive rounded amount) invoices,
    positive rounded payments, a nonnegative rounded target and an as-of date.
    Unknown ERP entry types and query policy remain adapter responsibilities.
    Copies inputs so reports cannot alter source rows or later calculations.
    """
    result = {f"payable_due_{days}_plus": 0.0 for days in (15, 30, 45, 60)}
    result["payable_prior_ledger"] = 0.0
    if target <= 0:
        return result
    unpaid = [{"date": day, "unpaid": amount} for day, amount in invoices]
    apply_fifo_payments(unpaid, payments)
    ledger_unpaid = round(sum(row["unpaid"] for row in unpaid), 2)
    if ledger_unpaid > target:
        reduction = round(ledger_unpaid - target, 2)
        for invoice in unpaid:
            if reduction <= 0:
                break
            applied = min(reduction, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            reduction = round(reduction - applied, 2)
    prior = round(max(target - sum(row["unpaid"] for row in unpaid), 0.0), 2)
    result["payable_prior_ledger"] = prior
    for days in (15, 30, 45, 60):
        cutoff = str(as_of - timedelta(days=days))
        due = prior + sum(row["unpaid"] for row in unpaid if row["date"] <= cutoff)
        result[f"payable_due_{days}_plus"] = round(min(max(due, 0.0), target), 2)
    return result
