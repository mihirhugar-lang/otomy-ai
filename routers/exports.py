import os
import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, Sale, Expense, Customer

router = APIRouter(prefix="/api/exports", tags=["exports"])

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "company_config.json"
)

_DEFAULT_CONFIG = {
    "company_name": "CRUSHER & QUARRY",
    "gstin": "",
    "state_code": "29",
}


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return dict(_DEFAULT_CONFIG)


def _save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------

class CompanyConfigUpdate(BaseModel):
    company_name: Optional[str] = None
    gstin: Optional[str] = None
    state_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config():
    return _load_config()


@router.post("/config")
def update_config(payload: CompanyConfigUpdate):
    cfg = _load_config()
    if payload.company_name is not None:
        cfg["company_name"] = payload.company_name
    if payload.gstin is not None:
        cfg["gstin"] = payload.gstin
    if payload.state_code is not None:
        cfg["state_code"] = payload.state_code
    _save_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Tally Sales XML export
# ---------------------------------------------------------------------------

@router.get("/tally/sales")
def export_tally_sales(
    from_date: date,
    to_date: date,
    db: Session = Depends(get_db),
):
    sales = (
        db.query(Sale)
        .filter(Sale.date >= from_date, Sale.date <= to_date)
        .order_by(Sale.date, Sale.id)
        .all()
    )

    # Build customer id -> name map
    cust_map = {}
    customers = db.query(Customer).all()
    for c in customers:
        cust_map[c.id] = c.name

    vouchers_xml = []
    for s in sales:
        gst_rate = s.gst_rate or 5.0
        cgst_rate = gst_rate / 2
        sgst_rate = gst_rate / 2

        amount = s.amount or 0.0
        # Amounts are GST-inclusive
        taxable = round(amount / (1 + gst_rate / 100), 2)
        cgst_amt = round(taxable * cgst_rate / 100, 2)
        sgst_amt = round(taxable * sgst_rate / 100, 2)

        cust_name = (
            cust_map.get(s.customer_id)
            or s.customer_name
            or "Cash"
        )
        date_str = s.date.strftime("%Y%m%d")
        narration = (
            f"Sale of {s.material or 'Aggregate'} — "
            f"Veh: {s.vehicle_no or 'N/A'} — "
            f"Ticket: {s.ticket_no or 'N/A'}"
        )

        vouchers_xml.append(f"""    <VOUCHER VCHTYPE="Sales" ACTION="Create">
      <DATE>{date_str}</DATE>
      <NARRATION>{narration}</NARRATION>
      <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
      <ALLLEDGERENTRIES.LIST>
        <LEDGERNAME>{cust_name}</LEDGERNAME>
        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
        <AMOUNT>{-amount:.2f}</AMOUNT>
      </ALLLEDGERENTRIES.LIST>
      <ALLLEDGERENTRIES.LIST>
        <LEDGERNAME>Sales — {s.material or 'Aggregate'}</LEDGERNAME>
        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
        <AMOUNT>{taxable:.2f}</AMOUNT>
      </ALLLEDGERENTRIES.LIST>
      <ALLLEDGERENTRIES.LIST>
        <LEDGERNAME>Output CGST @{cgst_rate:.1f}%</LEDGERNAME>
        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
        <AMOUNT>{cgst_amt:.2f}</AMOUNT>
      </ALLLEDGERENTRIES.LIST>
      <ALLLEDGERENTRIES.LIST>
        <LEDGERNAME>Output SGST @{sgst_rate:.1f}%</LEDGERNAME>
        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
        <AMOUNT>{sgst_amt:.2f}</AMOUNT>
      </ALLLEDGERENTRIES.LIST>
    </VOUCHER>""")

    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ENVELOPE>\n'
        '  <BODY>\n'
        '    <IMPORTDATA>\n'
        '      <REQUESTDESC>\n'
        '        <REPORTNAME>Vouchers</REPORTNAME>\n'
        '      </REQUESTDESC>\n'
        '      <REQUESTDATA>\n'
        + "\n".join(vouchers_xml)
        + "\n      </REQUESTDATA>\n"
        "    </IMPORTDATA>\n"
        "  </BODY>\n"
        "</ENVELOPE>"
    )

    filename = f"tally_sales_{from_date}_{to_date}.xml"
    return Response(
        content=xml_body,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Tally Expenses XML export
# ---------------------------------------------------------------------------

@router.get("/tally/expenses")
def export_tally_expenses(
    from_date: date,
    to_date: date,
    db: Session = Depends(get_db),
):
    expenses = (
        db.query(Expense)
        .filter(Expense.date >= from_date, Expense.date <= to_date)
        .order_by(Expense.date, Expense.id)
        .all()
    )

    vouchers_xml = []
    for e in expenses:
        date_str = e.date.strftime("%Y%m%d")
        cash_ledger = "Cash" if (e.payment_mode or "Cash") == "Cash" else "Bank"
        narration = (e.description or e.category or "Expense")[:200]

        vouchers_xml.append(f"""    <VOUCHER VCHTYPE="Payment" ACTION="Create">
      <DATE>{date_str}</DATE>
      <NARRATION>{narration}</NARRATION>
      <VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
      <ALLLEDGERENTRIES.LIST>
        <LEDGERNAME>{e.category or 'General Expenses'}</LEDGERNAME>
        <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
        <AMOUNT>{-e.amount:.2f}</AMOUNT>
      </ALLLEDGERENTRIES.LIST>
      <ALLLEDGERENTRIES.LIST>
        <LEDGERNAME>{cash_ledger}</LEDGERNAME>
        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
        <AMOUNT>{e.amount:.2f}</AMOUNT>
      </ALLLEDGERENTRIES.LIST>
    </VOUCHER>""")

    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ENVELOPE>\n'
        '  <BODY>\n'
        '    <IMPORTDATA>\n'
        '      <REQUESTDESC>\n'
        '        <REPORTNAME>Vouchers</REPORTNAME>\n'
        '      </REQUESTDESC>\n'
        '      <REQUESTDATA>\n'
        + "\n".join(vouchers_xml)
        + "\n      </REQUESTDATA>\n"
        "    </IMPORTDATA>\n"
        "  </BODY>\n"
        "</ENVELOPE>"
    )

    filename = f"tally_expenses_{from_date}_{to_date}.xml"
    return Response(
        content=xml_body,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# GSTR-1 JSON export
# ---------------------------------------------------------------------------

@router.get("/gstr1")
def export_gstr1(year: int, month: int, db: Session = Depends(get_db)):
    from calendar import monthrange

    _, last_day = monthrange(year, month)
    from_date = date(year, month, 1)
    to_date = date(year, month, last_day)

    sales = (
        db.query(Sale)
        .filter(Sale.date >= from_date, Sale.date <= to_date)
        .all()
    )

    # Build customer map
    cust_map = {}
    customers = db.query(Customer).all()
    for c in customers:
        cust_map[c.id] = c

    cfg = _load_config()
    gstin = cfg.get("gstin", "")
    fp = f"{month:02d}{year}"

    b2b = {}
    b2cs_taxable = 0.0
    b2cs_igst = 0.0
    b2cs_cgst = 0.0
    b2cs_sgst = 0.0
    total_taxable = 0.0
    total_qty = 0.0

    for s in sales:
        gst_rate = s.gst_rate or 5.0
        amount = s.amount or 0.0
        taxable = round(amount / (1 + gst_rate / 100), 2)
        cgst = round(taxable * (gst_rate / 2) / 100, 2)
        sgst = round(taxable * (gst_rate / 2) / 100, 2)
        qty = s.qty_mt or 0.0

        total_taxable += taxable
        total_qty += qty

        # Check if B2B (customer has valid GSTIN)
        cust = cust_map.get(s.customer_id) if s.customer_id else None
        cust_gstin = (cust.gstin or "").strip() if cust else ""

        if cust_gstin and len(cust_gstin) == 15:
            # B2B invoice
            key = cust_gstin
            if key not in b2b:
                b2b[key] = {
                    "ctin": cust_gstin,
                    "inv": [],
                }
            # Each sale is one invoice
            invoice_no = s.ticket_no or f"INV{s.id}"
            b2b[key]["inv"].append({
                "inum": invoice_no,
                "idt": s.date.strftime("%d-%m-%Y"),
                "val": round(amount, 2),
                "pos": cfg.get("state_code", "29"),
                "rchrg": "N",
                "itms": [{
                    "num": 1,
                    "itm_det": {
                        "txval": taxable,
                        "rt": gst_rate,
                        "igst": 0,
                        "cgst": cgst,
                        "sgst": sgst,
                        "cess": 0,
                    }
                }]
            })
        else:
            b2cs_taxable += taxable
            b2cs_cgst += cgst
            b2cs_sgst += sgst

    # Build output
    gstr1 = {
        "gstin": gstin,
        "fp": fp,
        "gt": round(total_taxable, 2),
        "cur_gt": round(total_taxable, 2),
    }

    if b2b:
        gstr1["b2b"] = list(b2b.values())

    if b2cs_taxable > 0:
        gstr1["b2cs"] = [{
            "sply_tp": "INTRA",
            "pos": cfg.get("state_code", "29"),
            "typ": "OE",
            "rt": 5,
            "txval": round(b2cs_taxable, 2),
            "igst": 0,
            "cgst": round(b2cs_cgst, 2),
            "sgst": round(b2cs_sgst, 2),
            "cess": 0,
        }]

    # HSN summary
    gstr1["hsn"] = {
        "data": [{
            "num": 1,
            "hsn_sc": "2517",
            "desc": "Crushed Stone / Aggregate",
            "uqc": "MT",
            "qty": round(total_qty, 3),
            "val": round(total_taxable, 2),
            "txval": round(total_taxable, 2),
            "igst": 0,
            "cgst": round(b2cs_cgst + sum(
                itm["itm_det"]["cgst"]
                for cust_data in b2b.values()
                for inv in cust_data["inv"]
                for itm in inv["itms"]
            ), 2),
            "sgst": round(b2cs_sgst + sum(
                itm["itm_det"]["sgst"]
                for cust_data in b2b.values()
                for inv in cust_data["inv"]
                for itm in inv["itms"]
            ), 2),
            "cess": 0,
        }]
    }

    filename = f"gstr1_{year}_{month:02d}.json"
    return Response(
        content=json.dumps(gstr1, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
