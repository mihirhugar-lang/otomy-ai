"""Shared GST and CA export contract.

This module is deliberately independent of SQLAlchemy, FastAPI, the browser, and
the Mac.  Localhost and the Otomy GitHub/Cloudflare worker pass the same
normalised rows here and therefore derive the same daily totals and export
payloads.  It does not file or submit anything to a portal.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape as xml_escape
import re
from typing import Any, Iterable


ENGINE_NAME = "loctell-common-engine"
ENGINE_VERSION = "2026-08-02.2-compliance-range-v1"
DEFAULT_STATE_CODE = "29"
DEFAULT_COMPANY_GSTIN = "29AAICV4284G1ZV"
DEFAULT_COMPANY_NAME = "Valli Muruga Industries Pvt Ltd"
ALLOWED_GST_RATES = {0.0, 5.0, 12.0, 18.0, 28.0, 40.0}


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _num(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _r2(value: Any) -> float:
    return round(_num(value), 2)


def _r3(value: Any) -> float:
    return round(_num(value), 3)


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")[:10]


def _money_tax(gross: float, rate: float, interstate: bool = False) -> tuple[float, float, float, float]:
    """Return taxable, IGST, CGST, SGST from GST-inclusive ERP amount."""
    gross = _r2(gross)
    rate = max(_num(rate), 0.0)
    taxable = round(gross / (1.0 + rate / 100.0), 2) if rate else gross
    tax = round(gross - taxable, 2)
    if interstate:
        return taxable, tax, 0.0, 0.0
    cgst = round(tax / 2.0, 2)
    sgst = round(tax - cgst, 2)
    return taxable, 0.0, cgst, sgst


def valid_gstin(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9]{2}[A-Z0-9]{13}", str(value or "").strip().upper()))


def _name_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _channel_amounts(row: Any, total: float) -> tuple[float, float, float]:
    cash = _num(_value(row, "cash_amount", 0))
    credit = _num(_value(row, "credit_amount", 0))
    upi = _num(_value(row, "upi_amount", 0))
    if cash + credit + upi > 0:
        return _r2(cash), _r2(credit), _r2(upi)
    mode = str(_value(row, "payment_mode", "Credit") or "Credit").upper()
    if "CASH" in mode:
        return _r2(total), 0.0, 0.0
    if "CREDIT" in mode:
        return 0.0, _r2(total), 0.0
    return 0.0, 0.0, _r2(total)


def _customer_maps(customers: Iterable[Any]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in customers or []:
        item = {
            "id": _value(row, "id"),
            "erp_customer_id": _value(row, "erp_customer_id"),
            "name": str(_value(row, "name", "") or "").strip(),
            "gstin": str(_value(row, "gstin", "") or "").strip().upper(),
            "phone": str(_value(row, "phone", "") or ""),
            "address": str(_value(row, "address", "") or ""),
        }
        if item["id"] is not None:
            by_id[str(item["id"])] = item
        if item["erp_customer_id"] is not None:
            by_id[str(item["erp_customer_id"])] = item
        if item["name"]:
            by_name[_name_key(item["name"])] = item
    return by_id, by_name


def _vendor_maps(vendors: Iterable[Any]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in vendors or []:
        item = {
            "id": _value(row, "id"),
            "erp_supplier_id": _value(row, "erp_supplier_id"),
            "name": str(_value(row, "name", "") or "").strip(),
            "gstin": str(_value(row, "gstin", "") or "").strip().upper(),
        }
        if item["id"] is not None:
            by_id[str(item["id"])] = item
        if item["erp_supplier_id"] is not None:
            by_id[str(item["erp_supplier_id"])] = item
        if item["name"]:
            by_name[_name_key(item["name"])] = item
    return by_id, by_name


def build_compliance_dataset(
    sales: Iterable[Any],
    expenses: Iterable[Any],
    receipts: Iterable[Any] = (),
    customers: Iterable[Any] = (),
    vendors: Iterable[Any] = (),
    vendor_payments: Iterable[Any] = (),
    config: dict | None = None,
    from_date: Any = None,
    to_date: Any = None,
) -> dict:
    """Normalise one date window for every GST, Tally, and audit consumer."""
    cfg = dict(config or {})
    start = _date(from_date) if from_date else ""
    end = _date(to_date) if to_date else "9999-12-31"
    if not start:
        dates = [_date(_value(r, "date")) for r in list(sales or []) + list(expenses or [])]
        start = min([d for d in dates if d] or [date.today().isoformat()])
    if end == "9999-12-31":
        dates = [_date(_value(r, "date")) for r in list(sales or []) + list(expenses or [])]
        end = max([d for d in dates if d] or [date.today().isoformat()])

    customer_by_id, customer_by_name = _customer_maps(customers)
    vendor_by_id, vendor_by_name = _vendor_maps(vendors)

    def in_window(value: Any) -> bool:
        d = _date(value)
        return bool(d and start <= d <= end)

    sales_out: list[dict] = []
    for index, row in enumerate(sales or [], 1):
        d = _date(_value(row, "date"))
        if not in_window(d):
            continue
        customer = customer_by_id.get(str(_value(row, "customer_id"))) or customer_by_name.get(
            _name_key(_value(row, "customer_name", ""))
        ) or {}
        gross = _r2(_num(_value(row, "amount", 0)) + _num(_value(row, "transport_charge", 0)))
        rate = _r2(_value(row, "gst_rate", 5.0) or 5.0)
        customer_gstin = str(
            _value(row, "customer_gstin", "") or customer.get("gstin", "") or ""
        ).strip().upper()
        interstate = valid_gstin(customer_gstin) and not customer_gstin.startswith(
            str(cfg.get("state_code") or DEFAULT_STATE_CODE)
        )
        taxable, igst, cgst, sgst = _money_tax(gross, rate, interstate)
        cash, credit, upi = _channel_amounts(row, gross)
        sales_out.append(
            {
                "id": _value(row, "id", index),
                "date": d,
                "invoice_no": str(_value(row, "ticket_no", "") or f"ERP-SALE-{_value(row, 'id', index)}"),
                "customer_name": str(_value(row, "customer_name", "") or customer.get("name") or "Cash Sale").strip(),
                "customer_gstin": customer_gstin,
                "material": str(_value(row, "material", "Aggregate") or "Aggregate").strip(),
                "hsn_code": str(_value(row, "hsn_code", "2517") or "2517").strip(),
                "qty_mt": _r3(_value(row, "qty_mt", 0)),
                "mdp_ton": _r3(_value(row, "mdp_ton", 0)),
                "rate_per_mt": _r2(_value(row, "rate_per_mt", 0)),
                "gross_value": gross,
                "taxable_value": taxable,
                "gst_rate": rate,
                "igst": igst,
                "cgst": cgst,
                "sgst": sgst,
                "cess": 0.0,
                "supply_type": "INTER" if interstate else "INTRA",
                "place_of_supply": str(cfg.get("state_code") or DEFAULT_STATE_CODE),
                "payment_mode": str(_value(row, "payment_mode", "Credit") or "Credit"),
                "cash_amount": cash,
                "credit_amount": credit,
                "upi_amount": upi,
                "vehicle_no": str(_value(row, "vehicle_no", "") or ""),
                "sale_time": str(_value(row, "sale_time", "") or ""),
                "notes": str(_value(row, "notes", "") or ""),
            }
        )

    expenses_out: list[dict] = []
    for index, row in enumerate(expenses or [], 1):
        d = _date(_value(row, "date"))
        if not in_window(d):
            continue
        vendor = vendor_by_id.get(str(_value(row, "vendor_id"))) or vendor_by_name.get(
            _name_key(_value(row, "vendor_name", ""))
        ) or {}
        expenses_out.append(
            {
                "id": _value(row, "id", index),
                "date": d,
                "category": str(_value(row, "category", "Other") or "Other"),
                "description": str(_value(row, "description", "") or ""),
                "amount": _r2(_value(row, "amount", 0)),
                "payment_mode": str(_value(row, "payment_mode", "Cash") or "Cash"),
                "vendor_id": _value(row, "vendor_id"),
                "vendor_name": str(_value(row, "vendor_name", "") or vendor.get("name") or ""),
                "vendor_gstin": str(_value(row, "vendor_gstin", "") or vendor.get("gstin") or "").upper(),
                "notes": str(_value(row, "notes", "") or ""),
                "erp_key": str(_value(row, "erp_key", "") or ""),
            }
        )

    receipts_out: list[dict] = []
    for index, row in enumerate(receipts or [], 1):
        d = _date(_value(row, "date"))
        if not in_window(d):
            continue
        customer = customer_by_id.get(str(_value(row, "customer_id", _value(row, "erp_customer_id")))) or customer_by_name.get(
            _name_key(_value(row, "customer_name", ""))
        ) or {}
        amount = _num(_value(row, "payment_received", _value(row, "amount", 0)))
        receipts_out.append(
            {
                "id": _value(row, "id", index),
                "date": d,
                "customer_name": str(_value(row, "customer_name", "") or customer.get("name") or "Customer"),
                "customer_gstin": str(_value(row, "customer_gstin", "") or customer.get("gstin") or "").upper(),
                "amount": _r2(amount),
                "mode": str(_value(row, "mode", "Cash") or "Cash"),
                "reference": str(_value(row, "reference", "") or ""),
                "source": str(_value(row, "source", "ERP receipt") or "ERP receipt"),
                "notes": str(_value(row, "notes", "") or ""),
            }
        )

    vendor_payments_out: list[dict] = []
    for index, row in enumerate(vendor_payments or [], 1):
        d = _date(_value(row, "date"))
        if not in_window(d):
            continue
        vendor = vendor_by_id.get(str(_value(row, "vendor_id", _value(row, "erp_supplier_id")))) or vendor_by_name.get(
            _name_key(_value(row, "vendor_name", ""))
        ) or {}
        vendor_payments_out.append(
            {
                "id": _value(row, "id", index),
                "date": d,
                "vendor_name": str(_value(row, "vendor_name", "") or vendor.get("name") or "Vendor"),
                "vendor_gstin": str(_value(row, "vendor_gstin", "") or vendor.get("gstin") or "").upper(),
                "amount": _r2(_value(row, "amount", 0)),
                "mode": str(_value(row, "mode", _value(row, "payment_mode", "Cash")) or "Cash"),
                "reference": str(_value(row, "reference", "") or ""),
                "notes": str(_value(row, "notes", "") or ""),
            }
        )

    daily_map: dict[str, dict] = {}
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        d = current.isoformat()
        daily_map[d] = {
            "date": d,
            "sales_count": 0,
            "gross_sales": 0.0,
            "taxable_sales": 0.0,
            "igst": 0.0,
            "cgst": 0.0,
            "sgst": 0.0,
            "output_tax": 0.0,
            "qty_mt": 0.0,
            "cash_sales": 0.0,
            "bank_sales": 0.0,
            "credit_sales": 0.0,
            "expense_count": 0,
            "expenses": 0.0,
            "receipts": 0.0,
            "vendor_payments": 0.0,
        }
        current += timedelta(days=1)
    for row in sales_out:
        day = daily_map[row["date"]]
        day["sales_count"] += 1
        for field in ("gross_value", "taxable_value", "igst", "cgst", "sgst", "qty_mt", "cash_amount", "upi_amount", "credit_amount"):
            target = {"gross_value": "gross_sales", "taxable_value": "taxable_sales", "cash_amount": "cash_sales", "upi_amount": "bank_sales", "credit_amount": "credit_sales"}.get(field, field)
            day[target] = _r2(day[target] + _num(row[field]))
        day["output_tax"] = _r2(day["igst"] + day["cgst"] + day["sgst"])
    for row in expenses_out:
        day = daily_map[row["date"]]
        day["expense_count"] += 1
        day["expenses"] = _r2(day["expenses"] + row["amount"])
    for row in receipts_out:
        day = daily_map[row["date"]]
        day["receipts"] = _r2(day["receipts"] + row["amount"])
    for row in vendor_payments_out:
        day = daily_map[row["date"]]
        day["vendor_payments"] = _r2(day["vendor_payments"] + row["amount"])

    # The GST workspace carries the verified company GSTIN. A blank legacy
    # dashboard config must not make every new GST file silently unusable.
    # An explicitly configured GSTIN still wins and is validated below.
    company_gstin = str(cfg.get("gstin") or DEFAULT_COMPANY_GSTIN).strip().upper()
    invalid_customer_gstin = sum(bool(row["customer_gstin"]) and not valid_gstin(row["customer_gstin"]) for row in sales_out)
    missing_hsn = sum(not row["hsn_code"] for row in sales_out)
    duplicate_invoices = len(sales_out) - len({(row["date"], row["invoice_no"]) for row in sales_out})
    warnings = []
    if not valid_gstin(company_gstin):
        warnings.append("Company GSTIN is not configured as a valid 15-character GSTIN; GST upload files must be checked before upload.")
    if invalid_customer_gstin:
        warnings.append(f"{invalid_customer_gstin} customer GSTIN values are invalid and were not classified as B2B.")
    if missing_hsn:
        warnings.append(f"{missing_hsn} sales rows have no HSN code.")
    if duplicate_invoices:
        warnings.append(f"{duplicate_invoices} duplicate date/invoice keys need review before GST upload.")
    if sales_out and not any(row["customer_gstin"] for row in sales_out):
        warnings.append("No customer GSTIN is mapped in the canonical sales rows; all sales are currently classified as B2C.")
    if vendor_payments_out and not any(row["vendor_gstin"] for row in vendor_payments_out):
        warnings.append("Vendor GSTINs are not mapped; GSTR-2B reconciliation cannot claim ITC from ERP rows alone.")

    totals = {
        "sales_count": len(sales_out),
        "gross_sales": _r2(sum(row["gross_value"] for row in sales_out)),
        "taxable_sales": _r2(sum(row["taxable_value"] for row in sales_out)),
        "igst": _r2(sum(row["igst"] for row in sales_out)),
        "cgst": _r2(sum(row["cgst"] for row in sales_out)),
        "sgst": _r2(sum(row["sgst"] for row in sales_out)),
        "output_tax": _r2(sum(row["igst"] + row["cgst"] + row["sgst"] for row in sales_out)),
        "qty_mt": _r3(sum(row["qty_mt"] for row in sales_out)),
        "expenses": _r2(sum(row["amount"] for row in expenses_out)),
        "receipts": _r2(sum(row["amount"] for row in receipts_out)),
        "vendor_payments": _r2(sum(row["amount"] for row in vendor_payments_out)),
    }
    checks = {
        "daily_sales_reconcile": totals["gross_sales"] == _r2(sum(row["gross_sales"] for row in daily_map.values())),
        "daily_tax_reconcile": totals["output_tax"] == _r2(sum(row["output_tax"] for row in daily_map.values())),
        "valid_company_gstin": valid_gstin(company_gstin),
        "invalid_customer_gstin": invalid_customer_gstin,
        "missing_hsn": missing_hsn,
        "duplicate_invoice_keys": duplicate_invoices,
        "warnings": warnings,
    }
    dataset = {
        "engine": {"name": ENGINE_NAME, "version": ENGINE_VERSION},
        "period": {"from": start, "to": end, "fy_start": start[:4] + "-04-01"},
        "company": {
            "name": str(cfg.get("company_name") or DEFAULT_COMPANY_NAME),
            "gstin": company_gstin,
            "state_code": str(cfg.get("state_code") or DEFAULT_STATE_CODE),
        },
        "sales": sales_out,
        "expenses": expenses_out,
        "receipts": receipts_out,
        "vendor_payments": vendor_payments_out,
        "daily": list(daily_map.values()),
        "totals": totals,
        "checks": checks,
    }
    # The browser may select a smaller reporting range, but it must never
    # calculate Tally vouchers independently.  These fragments are generated
    # here by the canonical engine and are only packaged by the UI for download.
    dataset["tally_vouchers"] = build_tally_voucher_records(dataset)
    return dataset


def _period_rows(dataset: dict, year: int, month: int) -> list[dict]:
    prefix = f"{int(year):04d}-{int(month):02d}-"
    return [row for row in dataset.get("sales", []) if str(row.get("date", "")).startswith(prefix)]


def _tax_item(row: dict) -> dict:
    return {
        "txval": _r2(row.get("taxable_value")),
        "rt": _r2(row.get("gst_rate")),
        "iamt": _r2(row.get("igst")),
        "camt": _r2(row.get("cgst")),
        "samt": _r2(row.get("sgst")),
        "csamt": _r2(row.get("cess")),
    }


def build_gstr1(dataset: dict, year: int, month: int) -> dict:
    """Build the GSTN offline-utility-shaped GSTR-1 JSON body for one month.

    The FY view remains the control/reporting view, but GSTN returns are monthly.
    Keep this payload aligned with the GST workspace's known GST3.0.4 offline
    utility shape so the CA can validate it in the current GSTN utility before
    upload.  The engine never submits the file.
    """
    rows = _period_rows(dataset, year, month)
    company = dataset.get("company") or {}
    state_code = str(company.get("state_code") or DEFAULT_STATE_CODE)
    b2b: dict[str, dict] = {}
    b2cs: dict[tuple, dict] = {}
    hsn: dict[tuple, dict] = {}
    invoice_numbers: list[str] = []
    for row in rows:
        invoice_no = str(row.get("invoice_no") or "").strip()
        invoice_numbers.append(invoice_no)
        gstin = str(row.get("customer_gstin") or "").upper()
        item = _tax_item(row)
        pos = str(row.get("place_of_supply") or state_code)
        supply_type = "INTER" if row.get("supply_type") == "INTER" else "INTRA"
        if valid_gstin(gstin):
            record = b2b.setdefault(gstin, {"ctin": gstin, "cfs": "N", "inv": []})
            record["inv"].append(
                {
                    "inum": invoice_no,
                    "idt": datetime.strptime(row["date"], "%Y-%m-%d").strftime("%d-%m-%Y"),
                    "val": _r2(row.get("gross_value")),
                    "pos": pos,
                    "rchrg": "N",
                    "inv_typ": "R",
                    "itms": [{"num": 1, "itm_det": item}],
                }
            )
        key = (supply_type, pos, _r2(row.get("gst_rate")))
        summary = b2cs.setdefault(
            key,
            {"sply_ty": supply_type, "pos": pos, "typ": "OE", "rt": key[2], "txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0},
        )
        if not valid_gstin(gstin):
            for field in ("txval", "iamt", "camt", "samt", "csamt"):
                source = {"txval": "taxable_value", "iamt": "igst", "camt": "cgst", "samt": "sgst", "csamt": "cess"}[field]
                summary[field] = _r2(summary[field] + _num(row.get(source)))
        hkey = str(row.get("hsn_code") or "")
        hs = hsn.setdefault(
            hkey,
            {"hsn_sc": hkey, "desc": row.get("material") or "Aggregate", "uqc": "MT", "qty": 0.0, "tot_val": 0.0, "txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0},
        )
        # The ERP's MDP Ton is the quantity used by the GST workspace.  Fall
        # back to the canonical quantity when older rows do not carry MDP Ton.
        quantity = _num(row.get("mdp_ton")) or _num(row.get("qty_mt"))
        hs["qty"] = _r3(hs["qty"] + quantity)
        hs["tot_val"] = _r2(hs["tot_val"] + _num(row.get("gross_value")))
        for field in ("txval", "iamt", "camt", "samt", "csamt"):
            source = {"txval": "taxable_value", "iamt": "igst", "camt": "cgst", "samt": "sgst", "csamt": "cess"}[field]
            hs[field] = _r2(hs[field] + _num(row.get(source)))
    hsn_rows = []
    for index, item in enumerate(sorted(hsn.values(), key=lambda value: value["hsn_sc"]), 1):
        hsn_rows.append(
            {
                "num": index,
                "hsn_sc": item["hsn_sc"],
                "desc": item["desc"],
                "uqc": item["uqc"],
                "tt_qty": item["qty"],
                "tot_val": item["tot_val"],
                "txval": item["txval"],
                "iamt": item["iamt"],
                "camt": item["camt"],
                "samt": item["samt"],
                "csamt": item["csamt"],
            }
        )
    payload = {
        "version": "GST3.0.4",
        "gstin": str(company.get("gstin") or ""),
        "fp": f"{int(month):02d}{int(year):04d}",
        "b2b": [b2b[key] for key in sorted(b2b)],
        "b2cl": [],
        "b2cs": [b2cs[key] for key in sorted(b2cs)],
        "b2csa": [],
        "cdnr": [],
        "cdnur": [],
        "exp": [],
        "nil": {"inv": [
            {"sply_tp": "INTRB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
            {"sply_tp": "INTRB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
        ]},
        "hsn": {"data": hsn_rows},
        "doc_issue": {},
    }
    return payload


def _zero_tax_block() -> dict:
    return {"txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}


def build_gstr3b(dataset: dict, year: int, month: int) -> dict:
    rows = _period_rows(dataset, year, month)
    totals = {"txval": _r2(sum(row.get("taxable_value", 0) for row in rows)), "iamt": _r2(sum(row.get("igst", 0) for row in rows)), "camt": _r2(sum(row.get("cgst", 0) for row in rows)), "samt": _r2(sum(row.get("sgst", 0) for row in rows)), "csamt": 0.0}
    zero = _zero_tax_block()
    return {
        "gstin": str((dataset.get("company") or {}).get("gstin") or ""),
        "ret_period": f"{int(month):02d}{int(year):04d}",
        "sup_details": {
            "osup_det": totals,
            "osup_zero": dict(zero),
            "osup_nil_exmp": dict(zero),
            "isup_rev": dict(zero),
            "osup_nongst": dict(zero),
        },
        "inter_sup": {"unreg": {"txval": 0.0, "iamt": 0.0}, "comp": {"txval": 0.0, "iamt": 0.0}, "uin": {"txval": 0.0, "iamt": 0.0}},
        "itc_elg": {"imps": dict(zero), "impg": dict(zero), "isd": dict(zero), "other": dict(zero)},
        "itc_rev": {"reclitc": dict(zero), "oth": dict(zero)},
        "itc_net": dict(zero),
        "inward_sup": {"isup_details": [{"ty": "INTER", "inter": 0.0, "intra": 0.0}]},
        "interest": {"iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0},
        "late_fee": {"camt": 0.0, "samt": 0.0},
    }


def build_gstr2b_reconciliation(dataset: dict, year: int, month: int) -> dict:
    prefix = f"{int(year):04d}-{int(month):02d}-"
    rows = [row for row in dataset.get("expenses", []) if str(row.get("date", "")).startswith(prefix)]
    candidates = [
        {
            "date": row.get("date"),
            "supplier_name": row.get("vendor_name") or "Unmapped supplier",
            "supplier_gstin": row.get("vendor_gstin") or "",
            "reference": row.get("erp_key") or "",
            "erp_amount": _r2(row.get("amount")),
            "portal_2b_taxable": 0.0,
            "portal_2b_igst": 0.0,
            "portal_2b_cgst": 0.0,
            "portal_2b_sgst": 0.0,
            "match_status": "IMPORT GSTR-2B JSON TO RECONCILE",
        }
        for row in rows
        if row.get("vendor_name") or row.get("vendor_gstin")
    ]
    return {
        "gstin": str((dataset.get("company") or {}).get("gstin") or ""),
        "fp": f"{int(month):02d}{int(year):04d}",
        "uploadable": False,
        "source": "Company ERP purchase candidates; portal GSTR-2B must be downloaded after login.",
        "records": candidates,
        "summary": {"erp_purchase_candidates": len(candidates), "erp_candidate_value": _r2(sum(row["erp_amount"] for row in candidates)), "portal_itc_available": False, "matched_itc": 0.0},
        "warnings": ["GSTR-2B is generated by GSTN from supplier filings and is read-only. Import the portal JSON here for matching; do not upload this file to GST Portal."],
    }


def build_audit_ca(dataset: dict) -> dict:
    totals = dataset.get("totals") or {}
    ledgers = [
        {"ledger": "Sales", "credit": _r2(totals.get("taxable_sales")), "debit": 0.0},
        {"ledger": "Output IGST", "credit": _r2(totals.get("igst")), "debit": 0.0},
        {"ledger": "Output CGST", "credit": _r2(totals.get("cgst")), "debit": 0.0},
        {"ledger": "Output SGST", "credit": _r2(totals.get("sgst")), "debit": 0.0},
        {"ledger": "Expenses", "credit": 0.0, "debit": _r2(totals.get("expenses"))},
        {"ledger": "Customer receipts", "credit": _r2(totals.get("receipts")), "debit": 0.0},
        {"ledger": "Vendor payments", "credit": 0.0, "debit": _r2(totals.get("vendor_payments"))},
    ]
    return {
        "engine": dataset.get("engine"),
        "period": dataset.get("period"),
        "company": dataset.get("company"),
        "director_summary": {
            "gross_sales": totals.get("gross_sales", 0.0),
            "taxable_sales": totals.get("taxable_sales", 0.0),
            "output_tax": totals.get("output_tax", 0.0),
            "expenses": totals.get("expenses", 0.0),
            "receipts": totals.get("receipts", 0.0),
            "qty_mt": totals.get("qty_mt", 0.0),
        },
        "ledger_summary": ledgers,
        "daily": dataset.get("daily", []),
        "checks": dataset.get("checks", {}),
        "voucher_counts": {
            "sales": len(dataset.get("sales", [])),
            "expenses": len(dataset.get("expenses", [])),
            "receipts": len(dataset.get("receipts", [])),
            "vendor_payments": len(dataset.get("vendor_payments", [])),
        },
    }


def _tally_entry(name: str, deemed_positive: bool, amount: float) -> str:
    sign = "-" if deemed_positive else ""
    positive = "Yes" if deemed_positive else "No"
    return (
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{xml_escape(str(name or 'Unmapped Ledger'))}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{positive}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{sign}{abs(_num(amount)):.2f}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
    )


def build_tally_xml(dataset: dict) -> str:
    """Build a standard Tally Prime Import Data envelope; caller must review mappings."""
    messages: list[str] = []
    voucher_no = 1
    for row in dataset.get("sales", []):
        rate = _r2(row.get("gst_rate"))
        party = row.get("customer_name") or "Cash"
        sales_ledger = "Sales @ 18%" if rate == 18 else ("MURRAM MT" if "MURRAM" in str(row.get("material", "")).upper() else "AGGREGATE SALES")
        taxes = []
        if _num(row.get("igst")):
            taxes.append((f"Output IGST @ {rate:g}%", row.get("igst")))
        else:
            if _num(row.get("cgst")):
                taxes.append((f"Output CGST @ {rate/2:g}%", row.get("cgst")))
            if _num(row.get("sgst")):
                taxes.append((f"Output SGST @ {rate/2:g}%", row.get("sgst")))
        entries = [_tally_entry(party, True, row.get("gross_value")), _tally_entry(sales_ledger, False, row.get("taxable_value"))]
        entries.extend(_tally_entry(name, False, amount) for name, amount in taxes)
        messages.append(
            "<TALLYMESSAGE xmlns:UDF=\"TallyUDF\"><VOUCHER VCHTYPE=\"Sales\" ACTION=\"Create\" OBJVIEW=\"Invoice Voucher View\">"
            f"<DATE>{str(row.get('date', '')).replace('-', '')}</DATE><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>{xml_escape(row.get('invoice_no') or f'ERP-SALE-{voucher_no}')}</VOUCHERNUMBER><ISINVOICE>Yes</ISINVOICE>"
            f"<PARTYLEDGERNAME>{xml_escape(party)}</PARTYLEDGERNAME><NARRATION>{xml_escape('ERP sale ' + str(row.get('invoice_no') or voucher_no))}</NARRATION>"
            + "".join(entries)
            + "</VOUCHER></TALLYMESSAGE>"
        )
        voucher_no += 1
    for row in dataset.get("expenses", []):
        amount = _r2(row.get("amount"))
        bank_ledger = "Cash" if "CASH" in str(row.get("payment_mode", "Cash")).upper() else "Bank"
        messages.append(
            "<TALLYMESSAGE xmlns:UDF=\"TallyUDF\"><VOUCHER VCHTYPE=\"Payment\" ACTION=\"Create\">"
            f"<DATE>{str(row.get('date', '')).replace('-', '')}</DATE><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>ERP-EXP-{row.get('id', voucher_no)}</VOUCHERNUMBER><NARRATION>{xml_escape(row.get('description') or row.get('category') or 'Expense')}</NARRATION>"
            + _tally_entry(row.get("category") or "General Expenses", True, amount)
            + _tally_entry(bank_ledger, False, amount)
            + "</VOUCHER></TALLYMESSAGE>"
        )
        voucher_no += 1
    for row in dataset.get("receipts", []):
        amount = _r2(row.get("amount"))
        bank_ledger = "Cash" if "CASH" in str(row.get("mode", "Cash")).upper() else "Bank"
        messages.append(
            "<TALLYMESSAGE xmlns:UDF=\"TallyUDF\"><VOUCHER VCHTYPE=\"Receipt\" ACTION=\"Create\">"
            f"<DATE>{str(row.get('date', '')).replace('-', '')}</DATE><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>ERP-REC-{row.get('id', voucher_no)}</VOUCHERNUMBER><NARRATION>{xml_escape(row.get('reference') or 'Customer receipt')}</NARRATION>"
            + _tally_entry(bank_ledger, True, amount)
            + _tally_entry(row.get("customer_name") or "Customer", False, amount)
            + "</VOUCHER></TALLYMESSAGE>"
        )
        voucher_no += 1
    for row in dataset.get("vendor_payments", []):
        amount = _r2(row.get("amount"))
        bank_ledger = "Cash" if "CASH" in str(row.get("mode", "Cash")).upper() else "Bank"
        messages.append(
            "<TALLYMESSAGE xmlns:UDF=\"TallyUDF\"><VOUCHER VCHTYPE=\"Payment\" ACTION=\"Create\">"
            f"<DATE>{str(row.get('date', '')).replace('-', '')}</DATE><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>ERP-VPAY-{row.get('id', voucher_no)}</VOUCHERNUMBER><NARRATION>{xml_escape(row.get('reference') or 'Vendor payment')}</NARRATION>"
            + _tally_entry(row.get("vendor_name") or "Vendor", True, amount)
            + _tally_entry(bank_ledger, False, amount)
            + "</VOUCHER></TALLYMESSAGE>"
        )
        voucher_no += 1
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
        "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC><REQUESTDATA>"
        + "".join(messages)
        + "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


def build_tally_voucher_records(dataset: dict) -> list[dict]:
    """Return canonical, date-addressable Tally voucher fragments.

    The XML is first generated by :func:`build_tally_xml`, so the full export
    and a date-range export can never apply separate accounting rules.
    """
    xml = build_tally_xml(dataset)
    matches = re.findall(
        r"(<TALLYMESSAGE\b.*?<DATE>(\d{8})</DATE>.*?</TALLYMESSAGE>)",
        xml,
        flags=re.DOTALL,
    )
    expected = sum(
        len(dataset.get(section, []))
        for section in ("sales", "expenses", "receipts", "vendor_payments")
    )
    if len(matches) != expected:
        raise RuntimeError(
            "canonical Tally voucher extraction failed: "
            f"expected {expected}, found {len(matches)}"
        )
    return [
        {
            "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}",
            "content": content,
        }
        for content, stamp in matches
    ]
