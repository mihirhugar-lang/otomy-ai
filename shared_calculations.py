"""Pure financial arithmetic shared by the cloud and localhost adapters.

No ERP, database, filesystem, clock or publication access belongs here.
Adapters own input parsing, row selection, dates and their existing rounding
boundaries. Keep the packaged localhost copy byte-identical (parity guard).
"""

from datetime import timedelta


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
    for amount in payments:
        remaining = amount
        for invoice in unpaid:
            if remaining <= 0:
                break
            applied = min(remaining, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            remaining = round(remaining - applied, 2)
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
