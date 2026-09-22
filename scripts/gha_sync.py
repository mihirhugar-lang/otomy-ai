#!/usr/bin/env python3
"""
GitHub Actions sync script — fetches live data from loctell.com ERP
and generates JSON files for otomy.ai. Runs on GitHub servers.
No Mac or local database required.
"""
import base64, json, re, html as htmllib, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import requests
# Also support importlib loaders used by localhost's pre-sync parity guard.
# Importing this module must not require a caller-provided PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from snapshot_retention import is_archive_reconstructible_range_snapshot
try:
    from shared_compliance import (
        build_audit_ca as build_compliance_audit_ca,
        build_compliance_dataset,
        build_gstr1 as build_compliance_gstr1,
        build_gstr2b_reconciliation as build_compliance_gstr2b,
        build_gstr3b as build_compliance_gstr3b,
        build_tally_xml as build_compliance_tally_xml,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from shared_compliance import (
        build_audit_ca as build_compliance_audit_ca,
        build_compliance_dataset,
        build_gstr1 as build_compliance_gstr1,
        build_gstr2b_reconciliation as build_compliance_gstr2b,
        build_gstr3b as build_compliance_gstr3b,
        build_tally_xml as build_compliance_tally_xml,
    )

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_calculations import (
    sale_channels as calculate_sale_channels,
    settlement_roundoff as calculate_settlement_roundoff,
    payable_due_aging as calculate_payable_due_aging,
    advance_book_balance,
    rebalance_book_rows,
    cashbook_totals as calculate_cashbook_totals,
    daily_ledger_row as calculate_daily_ledger_row,
    daily_ledger_totals as calculate_daily_ledger_totals,
    credit_due as calculate_credit_due,
    exclusive_age_buckets,
    accumulate_sale_group,
    customer_sales_totals,
    credit_liquidity_metrics,
)

import sync_loctell
import sync_finance
import sync_archive
import sync_snapshots

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("COMMON_ENGINE_DATA_DIR", ROOT / "data"))
# Reviewed financial control inputs are kept outside the public repository.
# GitHub Actions hydrates this directory from the private R2 ``control/``
# prefix before starting the engine.  The seed directory remains the local
# development fallback so localhost checks continue to work offline.
PRIVATE_SEED_DIR = Path(os.environ.get("OTOMY_PRIVATE_SEED_DIR", ROOT / "seed"))
SNAPSHOT_API_DIR = DATA_DIR / "snapshot" / "api"
ARCHIVE_DIR = DATA_DIR / "archive"
LOCAL_SEED_PATH = DATA_DIR / "local_seed.json"
CUSTOMER_MASTER_OVERRIDES_PATH = DATA_DIR / "customer_master_overrides.json"
VENDOR_MASTER_PATH = PRIVATE_SEED_DIR / "vendor_master.json"
BOOK_BALANCE_ACCOUNTS_PATH = PRIVATE_SEED_DIR / "book_balance_accounts.json"
BANK_STATEMENT_PATH = PRIVATE_SEED_DIR / "bank_statement_icici_2026-04-01_2026-06-28.json"
IST = ZoneInfo("Asia/Kolkata")
# The workbook is retained only as audit evidence.  It is never a financial
# ledger source: cash books use Loctell movements and named physical anchors.
MERGE_PROTECT_BEFORE_DATE = None
COMMON_ENGINE_NAME = "loctell-common-engine"
COMMON_ENGINE_VERSION = "2026-08-02.2-compliance-range-v1"

ERP_BASE = os.environ.get("ERP_BASE", "https://erp.loctell.com")
ERP_ORG  = os.environ.get("ERP_ORG",  "VMIPL")
ERP_USER = os.environ.get("ERP_USER", "admin")
ERP_PASS = os.environ.get("ERP_PASS", "")

_TR  = re.compile(r"<tr[^>]*>(.*?)</tr>",  re.DOTALL | re.IGNORECASE)
_TD  = re.compile(r"<td[^>]*>(.*?)</td>",  re.DOTALL | re.IGNORECASE)
_PAY = {"CASH", "CREDIT", "CARD/UPI", "SPLIT", "UPI"}
EXCLUDED_CUSTOMER_RECEIPT_REFS = {
    "ERP-CREDIT-170238-2026-07-02-CASH",
    "ERP-CREDIT-170238-2026-07-02-BANK",
}
EXCLUDED_CUSTOMER_RECEIPT_BANK_IDS = {
    "receipt-170238-2026-07-02",
}

# A GitHub runner starts from the previous R2 bundle.  Record the files this
# run deliberately regenerates so obsolete *derived range* snapshots can be
# removed after all reconciliation readers have finished.  The archive,
# anchors, customer/vendor ledgers, and canonical Cash/Bank books are never
# covered by this retention pass.
_WRITTEN_SNAPSHOT_FILES: set[str] = set()

# ─── helpers ────────────────────────────────────────────────────────────────

class ErpFetchError(RuntimeError):
    pass

def _env_int(name, default, min_value=1, max_value=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value

def _env_float(name, default, min_value=0.0):
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(min_value, value)

ERP_FETCH_RETRIES = _env_int("OTOMY_ERP_RETRIES", 3, min_value=1, max_value=6)
ERP_RETRY_DELAY_SECONDS = _env_float("OTOMY_ERP_RETRY_DELAY", 1.5, min_value=0.0)
ERP_DEBTOR_WORKERS = _env_int("OTOMY_DEBTOR_WORKERS", 4, min_value=1, max_value=8)
ERP_BALANCE_WORKERS = _env_int("OTOMY_BALANCE_WORKERS", 4, min_value=1, max_value=8)

def _request_json_with_retry(sess, url, *, params=None, timeout=35, label="ERP request"):
    last_error = None
    for attempt in range(1, ERP_FETCH_RETRIES + 1):
        try:
            response = sess.get(url, params=params, timeout=timeout, verify=True)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            last_error = e
            if attempt < ERP_FETCH_RETRIES:
                print(f"  {label} retry {attempt}/{ERP_FETCH_RETRIES} after {type(e).__name__}: {e}")
                time.sleep(ERP_RETRY_DELAY_SECONDS * attempt)
    raise last_error

def _request_text_with_retry(sess, url, *, params=None, timeout=35, label="ERP request"):
    last_error = None
    for attempt in range(1, ERP_FETCH_RETRIES + 1):
        try:
            response = sess.get(url, params=params, timeout=timeout, verify=True)
            response.raise_for_status()
            return response.text
        except Exception as e:
            last_error = e
            if attempt < ERP_FETCH_RETRIES:
                print(f"  {label} retry {attempt}/{ERP_FETCH_RETRIES} after {type(e).__name__}: {e}")
                time.sleep(ERP_RETRY_DELAY_SECONDS * attempt)
    raise last_error

def _clean(x):
    return re.sub(r"<[^>]+>", "", htmllib.unescape(str(x))).strip()

def _num(s):
    text = str(s).replace(",", "").strip()
    sign_text = text.replace("₹", "")
    negative = bool(
        re.search(r"(^|[^\d])[-\u2212]\s*(?:rs\.?\s*)?\d", sign_text, re.IGNORECASE)
        or re.match(r"^\s*\(.*\)\s*$", text)
    )
    cleaned = re.sub(r"[^\d.]", "", text)
    try:
        value = float(cleaned)
    except:
        return 0.0
    return -value if negative and value else value

def _pay_channel(p):
    p = (p or "").upper().strip()
    if p in ("CASH",):        return "cash"
    if p == "CREDIT":         return "credit"
    return "bank"

def _payment_channel(raw):
    return "cash" if "CASH" in (raw or "").upper() else "bank"


def _ledger_payment_channel(cells):
    """Classify a customer-ledger payment from its complete ERP row.

    The mode column is usually sufficient, but Loctell can label it ``CASH``
    while its transaction narrative explicitly says ``CARD/UPI - VMIPL
    (ICICI)``.  The money is then real bank money, not cash in office.  A
    specific electronic-payment reference takes precedence over the generic
    mode; otherwise retain the established mode-column behaviour.
    """
    text = " ".join(str(value or "") for value in (cells or [])).upper()
    if re.search(r"\b(?:CARD\s*/\s*UPI|UPI|NEFT|RTGS|IMPS|ICICI)\b", text):
        return "bank"
    mode = cells[13] if len(cells or []) > 13 else ""
    return _payment_channel(mode)


def _is_explicit_mixed_tender_split(split):
    """Whether ListSale explicitly identifies a real cash + non-cash tender.

    These physical channel amounts remain authoritative even when Loctell's
    invoice total has a larger-than-usual settlement round-off.  Restrict this
    exception to a named SPLIT tender so stale partial splits cannot replace a
    normal cash, UPI, or credit ticket.
    """
    if not isinstance(split, dict) or "SPLIT" not in str(split.get("pay_type") or "").upper():
        return False
    return _num(split.get("cash")) > 0 and (
        _num(split.get("credit")) > 0 or _num(split.get("upi")) > 0
    )


def _sale_split_key(sale_date, ticket_no):
    """Stable ListSale key; Loctell ticket numbers repeat on later dates."""
    return str(sale_date or "")[:10], str(ticket_no or "").strip()


_LISTSALE_REQUIRED_COLUMNS = {
    "ticket_no": "Bill Number",
    "total": "Total Amount",
    "pay_type": "Payment Type",
    "cash": "Final Cash",
    "credit": "Final Credit",
    "upi": "Final UPI",
    "mdp": "MDP Ton",
}


def _listsale_header_key(value):
    """Compare ListSale headers independent of spaces, punctuation and case."""
    return re.sub(r"[^a-z0-9]+", "", _clean(value).lower())


def _parse_listsale_splits(html, sale_date):
    """Parse ListSale by its labelled columns, never by a fixed record width.

    Loctell can add display-only fields such as ``Operator``.  A fixed flat-cell
    width shifts every later ticket, which can turn MDP into Empty Date and put
    payment channels on the wrong sale.  Missing required headers are fatal:
    publishing a guessed financial split is never acceptable.
    """
    table = next((t for t in re.findall(r"<table.*?</table>", html, re.DOTALL)
                  if "Final" in t or "Payment" in t), None)
    if not table:
        raise RuntimeError("ListSale payment table is missing")

    headers = [_clean(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", table, re.DOTALL)]
    header_index = {_listsale_header_key(name): pos for pos, name in enumerate(headers)}
    columns = {}
    missing = []
    for field, label in _LISTSALE_REQUIRED_COLUMNS.items():
        pos = header_index.get(_listsale_header_key(label))
        if pos is None:
            missing.append(label)
        else:
            columns[field] = pos
    if missing:
        raise RuntimeError(
            "ListSale required header(s) missing: " + ", ".join(missing)
            + "; received: " + ", ".join(headers)
        )

    required_last = max(columns.values())
    record_width = len(headers)
    if record_width <= required_last:
        raise RuntimeError("ListSale header layout is shorter than its required columns")

    # Loctell currently renders all daily tickets inside one HTML <tr>, followed
    # by a separate total row.  The labelled header count, not <tr>, is the
    # reliable record width.
    cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", table, re.DOTALL)]
    splits = {}
    for start in range(0, len(cells), record_width):
        row = cells[start:start + record_width]
        if len(row) <= required_last:
            continue
        ticket_no = row[columns["ticket_no"]].strip()
        if not re.fullmatch(r"\d+", ticket_no):
            continue
        splits[_sale_split_key(sale_date, ticket_no)] = {
            "total": _num(row[columns["total"]]),
            "pay_type": row[columns["pay_type"]],
            "cash": round(_num(row[columns["cash"]]), 2),
            "credit": round(_num(row[columns["credit"]]), 2),
            "upi": round(_num(row[columns["upi"]]), 2),
            # MDP is a physical tonnes reading.  Loctell can render a
            # correction as "-5.0", but a physical MDP quantity is always
            # non-negative; retain its magnitude for the operational total.
            "mdp": round(abs(_num(row[columns["mdp"]])), 3),
        }
    return splits


def _is_excluded_customer_receipt(row):
    return str((row or {}).get("reference") or "") in EXCLUDED_CUSTOMER_RECEIPT_REFS


def _is_excluded_customer_receipt_bank_row(row):
    return str((row or {}).get("id") or "") in EXCLUDED_CUSTOMER_RECEIPT_BANK_IDS


def _refresh_repayment_totals(control, removed_rows):
    repayments = control.get("customer_repayments")
    if not isinstance(repayments, list) or not removed_rows:
        return
    kept = [row for row in repayments if not _is_excluded_customer_receipt(row)]
    control["customer_repayments"] = kept
    payment_total = round(sum(_num(row.get("payment_received", row.get("amount"))) for row in kept), 2)
    amount_total = round(sum(_num(row.get("amount")) for row in kept), 2)
    bank_total = round(sum(_num(row.get("bank_received")) for row in kept), 2)
    cash_total = round(sum(_num(row.get("cash_received")) for row in kept), 2)
    removed_bank = round(sum(_num(row.get("bank_received")) for row in removed_rows), 2)
    removed_cash = round(sum(_num(row.get("cash_received")) for row in removed_rows), 2)
    control["customer_repayments_total"] = amount_total
    control["customer_repayments_payment_total"] = payment_total
    control["customer_repayments_bank_total"] = bank_total
    control["customer_repayments_cash_total"] = cash_total
    summary = control.get("summary")
    if isinstance(summary, dict):
        summary["credit_payment_received"] = payment_total
        if removed_bank and summary.get("bank_balance") is not None:
            summary["bank_balance"] = round(_num(summary.get("bank_balance")) - removed_bank, 2)
        if removed_cash and summary.get("cash_balance_office") is not None:
            summary["cash_balance_office"] = round(_num(summary.get("cash_balance_office")) - removed_cash, 2)


def _clean_excluded_customer_receipt_rows(value):
    if isinstance(value, list):
        changed = False
        cleaned_rows = []
        for row in value:
            if isinstance(row, dict) and (
                _is_excluded_customer_receipt(row)
                or _is_excluded_customer_receipt_bank_row(row)
            ):
                changed = True
                continue
            cleaned, child_changed = _clean_excluded_customer_receipt_rows(row)
            changed = changed or child_changed
            cleaned_rows.append(cleaned)
        return cleaned_rows, changed
    if isinstance(value, dict):
        changed = False
        result = dict(value)
        repayments = result.get("customer_repayments")
        if isinstance(repayments, list):
            removed = [
                row for row in repayments
                if isinstance(row, dict) and _is_excluded_customer_receipt(row)
            ]
            if removed:
                _refresh_repayment_totals(result, removed)
                changed = True
        for key, child in list(result.items()):
            if key == "customer_repayments":
                continue
            cleaned, child_changed = _clean_excluded_customer_receipt_rows(child)
            if child_changed:
                result[key] = cleaned
                changed = True
        return result, changed
    return value, False


def cleanup_excluded_customer_receipt_artifacts():
    changed_files = 0
    for path in DATA_DIR.rglob("*.json"):
        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except Exception:
            continue
        cleaned, changed = _clean_excluded_customer_receipt_rows(payload)
        if not changed:
            continue
        with open(path, "w") as f:
            json.dump(cleaned, f, default=str, separators=(",", ":"))
        changed_files += 1
    if changed_files:
        print(f"  cleaned excluded customer receipt rows from {changed_files} JSON files")


def cleanup_residual_balance_artifacts():
    """Remove unexplained balance rows while preserving localhost's named anchors."""
    changed_files = 0

    def clean(value):
        if isinstance(value, list):
            cleaned = []
            changed = False
            for item in value:
                particulars = str(item.get("particulars") or "") if isinstance(item, dict) else ""
                named_anchor = particulars in {
                    "Verified balance adjustment (physical cash count)",
                    "Verified balance adjustment (bank statement)",
                }
                if isinstance(item, dict) and (
                    particulars == "Verified balance adjustment (residual)"
                    or (item.get("kind") == "adjustment" and not named_anchor)
                ):
                    changed = True
                    continue
                new_item, child_changed = clean(item)
                changed = changed or child_changed
                cleaned.append(new_item)
            return cleaned, changed
        if isinstance(value, dict):
            result = {}
            changed = False
            for key, item in value.items():
                new_item, child_changed = clean(item)
                changed = changed or child_changed
                result[key] = new_item
            return result, changed
        return value, False

    for path in DATA_DIR.rglob("*.json"):
        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except Exception:
            continue
        cleaned, changed = clean(payload)
        if not changed:
            continue
        with open(path, "w") as f:
            json.dump(cleaned, f, default=str, separators=(",", ":"))
        changed_files += 1
    if changed_files:
        print(f"  removed retired residual balance rows from {changed_files} JSON files")


def _sale_total(row):
    return _num(row.get("amount")) + _num(row.get("transport_charge"))


def _sale_channels(s):
    """(cash, credit, upi) for a sale dict. Uses the captured ListSale split
    (Final Cash/Credit/UPI) when present, else derives from payment_mode so
    archive rows keep working."""
    cash = _num(s.get("cash_amount")); credit = _num(s.get("credit_amount")); upi = _num(s.get("upi_amount"))
    if cash + credit + upi > 0:
        # Preserve the old positive-split decision BEFORE rounding tiny values.
        return tuple(round(value, 2) for value in calculate_sale_channels(
            0.0, None, cash, credit, upi))
    return calculate_sale_channels(_sale_total(s), s.get("payment_mode"), cash, credit, upi)


def _sale_settlement_roundoff(s):
    """Return the non-cash settlement difference allocated to cash/bank.

    Loctell can finalise a cash or bank spot ticket a few rupees below/above
    its invoice total.  The actual Final Cash/UPI is the physical movement and
    must remain the only amount that changes the book balance.  This helper
    exposes the invoice-vs-settlement difference on that ticket as an
    informational reconciliation value; it never becomes a cash/bank entry.

    A mixed tender has no safe channel allocation unless Loctell tells us one,
    so it is deliberately left at zero rather than guessed.
    """
    gross = _sale_total(s)
    cash, credit, upi = _sale_channels(s)
    return calculate_settlement_roundoff(gross, cash, credit, upi)


def _channels_for_payment_mode(total, payment_mode):
    """Canonical unsplit sale channels from CustomerWiseReport.

    ListCustomerWiseReport is the authoritative source for a ticket's gross
    value and payment mode.  Store this baseline on every fresh row so a stale
    archived ListSale split can never survive a rebuild; a ListSale split may
    replace it only after it reconciles to the same gross value.
    """
    total = round(_num(total), 2)
    mode = (payment_mode or "Credit").upper()
    if mode == "CREDIT":
        return 0.0, total, 0.0
    if "CASH" in mode:
        return total, 0.0, 0.0
    return 0.0, 0.0, total


def _split_reconciles_sale(sale, split, tolerance=5.0):
    """Return true only when a ListSale channel split credibly ties to its ticket.

    ListSale can return stale or incomplete Final Cash/Credit/UPI values for an
    older ticket. CustomerWiseReport still has the correct payment mode and
    gross total in that case, so never let a partial split alter the archive.
    Loctell also records small ticket round-offs in Final Cash/Credit/UPI (for
    example gross ₹3,173 and final cash ₹3,170); accept only that bounded
    ₹5 settlement difference.  Large partial/stale splits remain rejected.
    """
    if not isinstance(split, dict):
        return False
    gross_total = _sale_total(sale)
    split_total = _num(split.get("cash")) + _num(split.get("credit")) + _num(split.get("upi"))
    return gross_total > 0 and abs(split_total - gross_total) <= tolerance


# Before this date, exclude ONLY genuine director drawings (category "... SIR SHARE"),
# NOT company expenses a director merely fronted (notes like "KUMAR SIR PAID ..."). From
# June 2026 onward the prior name-anywhere rule is kept unchanged (already reconciled).
_DIRECTOR_SHARE_ONLY_BEFORE = "2026-06-01"
# Payments to these personal accounts are Prashant's director drawings.  Keep
# this deliberately exact: a generic "Sidd"/"N J" rule could misclassify a
# normal supplier or employee payment.
_PRASHANT_DIRECTOR_SHARE_PAYEES = (
    "N J SHUSHRUTHA",
    "NJ SHUSHRUTHA",
    "SRI SIDDA",
)


def _is_director_payment(*values, when=None):
    text = " ".join(str(value or "") for value in values).upper()
    # The owner has confirmed these are Prashant director-share payments,
    # irrespective of payment channel or the historical pre-June wording.
    if any(payee in text for payee in _PRASHANT_DIRECTOR_SHARE_PAYEES):
        return True
    # Director 1/2 are directors, not shareholders — their spend is a normal expense,
    # never a shareholder drawing (even if a note names Kumar/Prashant). Mirrors
    # dashboard.py:_is_director_payment and index.html:_isDirectorPayment.
    if values and "DIRECTOR" in str(values[0] or "").upper():
        return False
    if not ("PRASHANT" in text or "KUMAR" in text):
        return False
    # Apr-May 2026 (and earlier): only actual drawings ("... SIR SHARE") count as a
    # director payment; "KUMAR SIR PAID" company expenses remain operating expenses.
    if when is not None and str(when) < _DIRECTOR_SHARE_ONLY_BEFORE:
        return "SHARE" in text
    return True
def _mode_bucket(raw):
    value = (raw or "").strip()
    upper = value.upper()
    if "CASH" in upper:
        return "Cash"
    if any(token in upper for token in ("BANK", "CARD", "UPI", "NEFT", "RTGS", "IMPS", "ICICI", "HDFC", "AXIS", "SBI")):
        return "Bank"
    return value or "Payment"

def _norm_pay(p):
    p = (p or "").upper().strip()
    if p in ("CARD/UPI", "UPI", "SPLIT"): return "UPI"
    if p == "CREDIT":                      return "Credit"
    return "Cash"

def _norm_material(m):
    m = m.strip().upper()
    if "40" in m:                                               return "40mm"
    if "20" in m:                                               return "20mm"
    if "12" in m or "10" in m:                                  return "12mm"
    if "6" in m and "MM" in m:                                  return "6mm"
    if "M-SAND" in m or "MSAND" in m or "MANUFACTURED" in m:   return "M-Sand"
    if "P-SAND" in m or "PSAND" in m or "PLASTER" in m:        return "P-Sand"
    if "DUST" in m:                                             return "Dust"
    return m[:50] or "Mixed"

def _parse_date(raw, fallback):
    raw = re.sub(r"\s+", " ", str(raw)).strip()
    for fmt in ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try: return datetime.strptime(raw, fmt).date()
        except: pass
    try:   return datetime.strptime(raw[:10], "%d-%m-%Y").date()
    except: return fallback

def _expense_legacy_key(row):
    return (
        row["date"].isoformat() if hasattr(row.get("date"), "isoformat") else str(row.get("date", "")),
        (row.get("category") or "").strip(),
        (row.get("description") or "").strip(),
        round(float(row.get("amount") or 0), 2),
        (row.get("payment_mode") or "").strip(),
        (row.get("notes") or "").strip(),
    )

def _expense_key(row, sequence):
    base = "|".join(str(value) for value in _expense_legacy_key(row))
    return f"{base}|seq={sequence}"

def load_local_seed():
    try:
        with open(LOCAL_SEED_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_book_balance_accounts():
    """Manual BankAccount balances that exist locally but not in Loctell."""
    try:
        with open(BOOK_BALANCE_ACCOUNTS_PATH) as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []

def load_customer_master_overrides():
    try:
        with open(CUSTOMER_MASTER_OVERRIDES_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def load_vendor_master():
    try:
        with open(VENDOR_MASTER_PATH) as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []

def load_bank_statement_rows():
    try:
        with open(BANK_STATEMENT_PATH) as f:
            data = json.load(f)
    except Exception:
        return []
    bank_name = data.get("bank_name") or "ICICI Bank"
    rows = []
    for row in data.get("rows") or []:
        rows.append({
            "id": f"icici-{row.get('tran_id') or row.get('sr_no')}",
            "date": row.get("date"),
            "description": row.get("description") or row.get("tran_id") or "",
            "credit": _num(row.get("credit")),
            "debit": _num(row.get("debit")),
            "balance": _num(row.get("balance")),
            "bank_name": bank_name,
            "source": "ICICI Statement",
        })
    return rows

def latest_bank_statement_balance(rows, as_of):
    candidates = [
        row for row in rows or []
        if row.get("date") and str(row.get("date")) <= str(as_of) and row.get("balance") is not None
    ]
    if not candidates:
        return None
    latest = sorted(candidates, key=lambda row: (row.get("date", ""), str(row.get("id", ""))))[-1]
    return round(_num(latest.get("balance")), 2)

def load_archive_manifest():
    return sync_archive.load_archive_manifest(ARCHIVE_DIR=ARCHIVE_DIR)

_date_months = sync_archive._date_months

def load_archive_window(from_d, to_d):
    return sync_archive.load_archive_window(from_d, to_d, ARCHIVE_DIR=ARCHIVE_DIR, _date_months=_date_months)

def merge_rows_by_archive_key(archive_rows, fresh_rows, section, *, drop_current_window=True):
    return sync_archive.merge_rows_by_archive_key(
        archive_rows, fresh_rows, section, drop_current_window=drop_current_window,
        _merge_archive_rows=_merge_archive_rows,
    )


def assert_fresh_source_rows_preserved(label, fresh_rows, merged_rows, key_fn):
    """Fail closed if a freshly fetched Loctell row disappears before publication."""
    expected = {key_fn(row) for row in fresh_rows or [] if isinstance(row, dict)}
    actual = {key_fn(row) for row in merged_rows or [] if isinstance(row, dict)}
    missing = expected - actual
    if missing:
        sample = "; ".join(str(item) for item in sorted(missing, key=str)[:3])
        raise RuntimeError(
            f"{label} source-window coverage failed: {len(missing)} freshly fetched "
            f"Loctell rows disappeared before publish (examples: {sample})"
        )
    print(f"  {label} source-window coverage: {len(expected)} fresh rows preserved")


def assert_fresh_sale_mdp_preserved(fresh_rows, merged_rows):
    """Fail closed if an archive merge alters a freshly read ListSale MDP value.

    MDP Ton is sourced from Loctell's labelled ListSale column, not inferred
    from ticket quantity.  Row-count coverage alone cannot catch a stale
    negative value surviving beside a fresh positive one, so compare every
    refreshed ticket by the same stable date + ticket identity used in the
    archive.  A later ERP edit is naturally collected by the next recent
    window; this guard ensures the engine never loses it while merging.
    """
    merged_by_key = {
        _archive_key("sales", row): row
        for row in merged_rows or []
        if isinstance(row, dict)
    }
    mismatches = []
    for row in fresh_rows or []:
        if not isinstance(row, dict):
            continue
        key = _archive_key("sales", row)
        merged = merged_by_key.get(key)
        expected = round(_num(row.get("mdp_ton")), 3)
        actual = round(_num((merged or {}).get("mdp_ton")), 3)
        if expected < 0 or actual < 0 or merged is None or abs(actual - expected) > 0.0005:
            mismatches.append(
                f"{row.get('date')} ticket {row.get('ticket_no')}: "
                f"Loctell={expected:.3f}, merged={actual:.3f}"
            )
    if mismatches:
        raise RuntimeError(
            "Sales MDP source-window parity failed; refusing to publish: "
            + "; ".join(mismatches[:5])
        )
    print(f"  Sales MDP source-window parity: {len(fresh_rows or [])} fresh tickets match ListSale")


def assert_fytd_source_coverage(fy_start, as_of, sales, expenses):
    """Fail closed rather than publish an anchor-only FYTD snapshot.

    A recent sync fetches a small Loctell delta, but its FYTD snapshots are
    served directly by the UI. Closing-balance parity alone cannot detect a
    truncated source because a later balance anchor can make it tie. Every
    completed FY month must therefore contain source rows. The open month is
    deliberately excluded: a valid new month can have sales but no expenses
    (or the reverse), and fresh-window preservation guards its live rows.
    """
    fy_start = fy_start if isinstance(fy_start, date) else date.fromisoformat(str(fy_start)[:10])
    as_of = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
    expected_months = set()
    cursor = fy_start.replace(day=1)
    current_month = as_of.replace(day=1)
    while cursor < current_month:
        expected_months.add(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    for label, rows in (("Sales", sales), ("Expenses", expenses)):
        covered_months = {
            str(row.get("date") or "")[:7]
            for row in rows or []
            if str(fy_start) <= str(row.get("date") or "")[:10] <= str(as_of)
        }
        missing = sorted(expected_months - covered_months)
        if missing:
            raise RuntimeError(
                f"FYTD source coverage failed for {label}: missing month(s) {', '.join(missing)}; "
                "refusing to publish a truncated FYTD snapshot"
            )
    print(f"  FYTD completed-month source coverage: {', '.join(sorted(expected_months)) or 'none'}")

def archive_receipts_to_repayments(receipts):
    rows = []
    for receipt in receipts or []:
        if _is_excluded_customer_receipt(receipt):
            continue
        amount = _num(receipt.get("payment_received", receipt.get("amount")))
        if amount <= 0:
            amount = _num(receipt.get("amount"))
        mode = receipt.get("mode") or "Cash"
        rows.append({
            "date": str(receipt.get("date", ""))[:10],
            "customer_id": receipt.get("customer_id", receipt.get("erp_customer_id")),
            "erp_customer_id": receipt.get("erp_customer_id", receipt.get("customer_id")),
            "customer_name": receipt.get("customer_name") or "Customer",
            "mode": mode,
            "reference": receipt.get("reference", ""),
            "payment_received": round(amount, 2),
            "bank_received": 0.0 if _payment_channel(mode) == "cash" else round(amount, 2),
            "cash_received": round(amount, 2) if _payment_channel(mode) == "cash" else 0.0,
            "sale_adjusted": _num(receipt.get("sale_adjusted")),
            "amount": _num(receipt.get("amount", amount)),
            "balance": _num(receipt.get("balance")),
            "source": "Archive Customer Receipt",
        })
    return rows

# ─── auth ────────────────────────────────────────────────────────────────────

def erp_auth():
    return sync_loctell.erp_auth(
        ERP_BASE=ERP_BASE, ERP_FETCH_RETRIES=ERP_FETCH_RETRIES, ERP_ORG=ERP_ORG, ERP_PASS=ERP_PASS,
        ERP_RETRY_DELAY_SECONDS=ERP_RETRY_DELAY_SECONDS, ERP_USER=ERP_USER, ErpFetchError=ErpFetchError,
    )

_clone_sess = sync_loctell._clone_sess

# ─── fetchers ────────────────────────────────────────────────────────────────

def _fetch_sales_window(sess, from_d, to_d):
    return sync_loctell._fetch_sales_window(
        sess, from_d, to_d, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _PAY=_PAY, _TD=_TD, _TR=_TR,
        _channels_for_payment_mode=_channels_for_payment_mode, _clean=_clean, _norm_material=_norm_material,
        _norm_pay=_norm_pay, _num=_num, _request_text_with_retry=_request_text_with_retry,
    )


_sales_fetch_windows = sync_loctell._sales_fetch_windows


_ledger_archive_start = sync_loctell._ledger_archive_start


def fetch_sales(sess, from_d, to_d):
    return sync_loctell.fetch_sales(
        sess, from_d, to_d, _clone_sess=_clone_sess, _fetch_sales_window=_fetch_sales_window,
        _sales_fetch_windows=_sales_fetch_windows,
    )


def fetch_sale_splits(sess, from_d, to_d):
    return sync_loctell.fetch_sale_splits(
        sess, from_d, to_d, ERP_BASE=ERP_BASE, _clone_sess=_clone_sess,
        _parse_listsale_splits=_parse_listsale_splits,
    )


def _cash_row_is_bank_expense(row, bank_expenses):
    return sync_loctell._cash_row_is_bank_expense(
        row, bank_expenses, _num=_num, _payment_channel=_payment_channel,
    )


def fetch_expenses(sess, from_d, to_d):
    return sync_loctell.fetch_expenses(
        sess, from_d, to_d, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _clean=_clean,
        _clone_sess=_clone_sess, _expense_key=_expense_key, _num=_num,
        _request_text_with_retry=_request_text_with_retry,
    )


def fetch_cash_ledger(sess, from_d, to_d):
    return sync_loctell.fetch_cash_ledger(
        sess, from_d, to_d, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _clean=_clean, _num=_num,
        _parse_date=_parse_date, _request_text_with_retry=_request_text_with_retry,
    )


def fetch_bank_entries(sess, from_d, to_d):
    return sync_loctell.fetch_bank_entries(
        sess, from_d, to_d, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _clean=_clean, _num=_num,
        _parse_date=_parse_date, _request_text_with_retry=_request_text_with_retry,
    )


def fetch_internal_transfers(sess, from_d, to_d):
    return sync_loctell.fetch_internal_transfers(
        sess, from_d, to_d, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _clean=_clean, _num=_num,
        _parse_date=_parse_date, _request_text_with_retry=_request_text_with_retry,
    )


def fetch_boulders(sess, from_d, to_d):
    return sync_loctell.fetch_boulders(
        sess, from_d, to_d, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _TD=_TD, _TR=_TR, _clean=_clean,
        _num=_num, _request_text_with_retry=_request_text_with_retry,
    )


_ODOMETER_TARGETS = [
    ("Jaw", "JAW"),
    ("Cone", "CONE"),
    ("VSI", "VSI"),
    ("Hitachi", "HITACHI"),
    ("VMI Loader", "VMI LOADER"),
    ("VMI Secondary Blasting (OB Work)", "VMI SECONDARY BLASTING(OB WORK)"),
    ("Daneswary Soling Vehicles", "DANESWARY SOLING VEHICLES"),
    ("Soling Manju Machines", "SOLING MANJU MACHINES"),
    ("Water Tanker", "WATER TANKER"),
]
_FUEL_SPEND_TRACKING_FROM = date(2026, 9, 1)
# Preserve the issue line recorded before Loctell's vehicle rename.  New rows
# use the current registration above; both labels belong to the same machine.
_FUEL_ISSUE_REGISTRATION_ALIASES = {
    "VMI SECONDARY BLASTING": "VMI Secondary Blasting (OB Work)",
}


_odometer_key = sync_loctell._odometer_key


def fetch_odometer_readings(sess, from_day, to_day):
    return sync_loctell.fetch_odometer_readings(
        sess, from_day, to_day, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, IST=IST,
        _ODOMETER_TARGETS=_ODOMETER_TARGETS, _num=_num, _odometer_key=_odometer_key,
        _request_json_with_retry=_request_json_with_retry,
    )


def fetch_live_odometer_readings(sess, today):
    return sync_loctell.fetch_live_odometer_readings(
        sess, today, fetch_odometer_readings=fetch_odometer_readings,
    )


def normalize_odometer_readings(readings):
    return sync_loctell.normalize_odometer_readings(readings, _ODOMETER_TARGETS=_ODOMETER_TARGETS)


def merge_odometer_history(existing, fresh):
    return sync_loctell.merge_odometer_history(
        existing, fresh, normalize_odometer_readings=normalize_odometer_readings,
    )


def validate_odometer_history(rows):
    return sync_loctell.validate_odometer_history(rows, _ODOMETER_TARGETS=_ODOMETER_TARGETS, _num=_num)


def fetch_odometer_history(sess, from_day, to_day, workers=8):
    return sync_loctell.fetch_odometer_history(
        sess, from_day, to_day, workers, ErpFetchError=ErpFetchError, _clone_sess=_clone_sess,
        fetch_odometer_readings=fetch_odometer_readings,
    )


def _loctell_ist_date(value):
    return sync_loctell._loctell_ist_date(value, IST=IST)


def fetch_machine_fuel_issues(sess, financial_year_start, today):
    return sync_loctell.fetch_machine_fuel_issues(
        sess, financial_year_start, today, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, IST=IST,
        _FUEL_ISSUE_REGISTRATION_ALIASES=_FUEL_ISSUE_REGISTRATION_ALIASES,
        _ODOMETER_TARGETS=_ODOMETER_TARGETS, _loctell_ist_date=_loctell_ist_date, _num=_num,
        _odometer_key=_odometer_key, _request_json_with_retry=_request_json_with_retry,
    )


def fetch_fuel_received(sess, financial_year_start, today):
    return sync_loctell.fetch_fuel_received(
        sess, financial_year_start, today, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, IST=IST,
        _loctell_ist_date=_loctell_ist_date, _num=_num, _request_json_with_retry=_request_json_with_retry,
    )


def fetch_fuel_dashboard_balance(sess):
    return sync_loctell.fetch_fuel_dashboard_balance(
        sess, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, IST=IST,
        _FUEL_SPEND_TRACKING_FROM=_FUEL_SPEND_TRACKING_FROM, _num=_num,
        _request_json_with_retry=_request_json_with_retry,
    )


def fuel_balance_with_value(balance, fuel_received):
    return sync_loctell.fuel_balance_with_value(balance, fuel_received, _num=_num)


def fetch_boulder_rows(sess, from_d, to_d):
    return sync_loctell.fetch_boulder_rows(
        sess, from_d, to_d, _clone_sess=_clone_sess, _num=_num, fetch_boulders=fetch_boulders,
    )


def fetch_iot(sess, from_d, to_d):
    return sync_loctell.fetch_iot(sess, from_d, to_d, ERP_BASE=ERP_BASE, _clean=_clean)


def fetch_debtors(sess, as_of=None):
    return sync_loctell.fetch_debtors(
        sess, as_of, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _clean=_clean, _num=_num,
        _request_json_with_retry=_request_json_with_retry,
    )


def fetch_creditors(sess, as_of=None):
    return sync_loctell.fetch_creditors(
        sess, as_of, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _clean=_clean, _num=_num,
        _request_json_with_retry=_request_json_with_retry,
    )


def fetch_vendor_payments(sess, creditors, from_d, to_d):
    return sync_loctell.fetch_vendor_payments(
        sess, creditors, from_d, to_d, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError, _clean=_clean,
        _clone_sess=_clone_sess, _mode_bucket=_mode_bucket, _num=_num, _parse_date=_parse_date,
    )


_norm_name = sync_loctell._norm_name


def _vendor_identity(row):
    return sync_loctell._vendor_identity(row, _norm_name=_norm_name)


_customer_master_key = sync_loctell._customer_master_key


def canonical_customer_master_rows(rows):
    return sync_loctell.canonical_customer_master_rows(rows, _customer_master_key=_customer_master_key)


def canonical_debtors_by_name(rows):
    return sync_loctell.canonical_debtors_by_name(
        rows, ErpFetchError=ErpFetchError, _norm_name=_norm_name, _num=_num,
    )


VENDOR_LEDGER_START = date(2025, 2, 15)  # full itemized vendor history begins here


def fetch_supplier_ledgers_full(sess, creditors, from_d, to_d, *, strict=False):
    return sync_loctell.fetch_supplier_ledgers_full(
        sess, creditors, from_d, to_d, strict=strict, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError,
        _clean=_clean, _clone_sess=_clone_sess, _num=_num, _parse_date=_parse_date,
        _vendor_identity=_vendor_identity,
    )


CUST_LEDGER_START = date(2025, 2, 15)  # full itemized customer history begins here
CUST_LEDGER_WORKERS = _env_int("OTOMY_CUST_LEDGER_WORKERS", 6, min_value=1, max_value=12)
CUST_LEDGER_MARKER = "/internal/cust-ledger-fetch"  # tracks the last daily full-ledger refresh


def _should_fetch_cust_ledgers(today):
    """The ~99-customer full-ledger fetch is heavy (~9 min), and this sync is dispatched every few
    minutes — so refresh at most ONCE per day (after 05:30 IST), tracked by a marker snapshot in R2;
    other runs reuse the previous reconciling snapshots. OTOMY_FETCH_CUST_LEDGERS=1 forces a refresh."""
    if os.environ.get("OTOMY_FETCH_CUST_LEDGERS", "").strip() not in ("", "0", "false"):
        return True
    try:
        now_ist = datetime.now(IST)
    except Exception:
        return False
    minutes = now_ist.hour * 60 + now_ist.minute
    if not (1290 <= minutes <= 1350):  # confine the heavy run to 21:30–22:30 IST (loctell off-peak)
        return False
    marker = read_snapshot(CUST_LEDGER_MARKER) or {}
    return marker.get("date") != str(today) or marker.get("slot") != "night"


def fetch_customer_ledgers_full(sess, debtors, from_d, to_d, only_outstanding=True):
    return sync_loctell.fetch_customer_ledgers_full(
        sess, debtors, from_d, to_d, only_outstanding, CUST_LEDGER_WORKERS=CUST_LEDGER_WORKERS,
        ERP_BASE=ERP_BASE, _clean=_clean, _clone_sess=_clone_sess, _norm_name=_norm_name, _num=_num,
        _parse_date=_parse_date,
    )


def _customer_identity_sale_key(row, amount_key):
    return sync_loctell._customer_identity_sale_key(row, amount_key, _norm_name=_norm_name, _num=_num)


def reconcile_fresh_credit_sale_identities(sales, debtors, ledger_sales):
    return sync_loctell.reconcile_fresh_credit_sale_identities(
        sales, debtors, ledger_sales, _customer_identity_sale_key=_customer_identity_sale_key,
        _norm_name=_norm_name, _num=_num, _sale_channels=_sale_channels,
    )


def resolve_fresh_credit_sale_identities(sess, sales, debtors, from_d, to_d):
    return sync_loctell.resolve_fresh_credit_sale_identities(
        sess, sales, debtors, from_d, to_d, _norm_name=_norm_name, _num=_num, _sale_channels=_sale_channels,
        fetch_customer_ledgers_full=fetch_customer_ledgers_full,
        reconcile_fresh_credit_sale_identities=reconcile_fresh_credit_sale_identities,
    )


compute_repayments = sync_loctell.compute_repayments

def fetch_customer_ledger_rows(sess, from_d, to_d, erp_customer_id):
    return sync_loctell.fetch_customer_ledger_rows(
        sess, from_d, to_d, erp_customer_id, ERP_BASE=ERP_BASE, ErpFetchError=ErpFetchError,
    )

def compute_repayments_from_erp(sess, start, end, previous_debtors, current_debtors, debtors_cache=None):
    return sync_loctell.compute_repayments_from_erp(
        sess, start, end, previous_debtors, current_debtors, debtors_cache,
        ERP_DEBTOR_WORKERS=ERP_DEBTOR_WORKERS,
        EXCLUDED_CUSTOMER_RECEIPT_REFS=EXCLUDED_CUSTOMER_RECEIPT_REFS, _clean=_clean,
        _clone_sess=_clone_sess, _ledger_payment_channel=_ledger_payment_channel, _num=_num,
        fetch_customer_ledger_rows=fetch_customer_ledger_rows, fetch_debtors=fetch_debtors,
    )

# ─── control room builder ─────────────────────────────────────────────────────

def build_control(sales, expenses, from_d, to_d,
                  boulders=None, debtors=None, creditors=None,
                  cash_balance=0.0, bank_net=0.0, repayments=None,
                  labour=None, parts=None, machines=None,
                  vendor_payments=None,
                  bank_balance_book=0.0, cash_balance_office_book=0.0):
    return sync_finance.build_control(
        sales, expenses, from_d, to_d, boulders, debtors, creditors, cash_balance, bank_net, repayments,
        labour, parts, machines, vendor_payments, bank_balance_book, cash_balance_office_book,
        _balance_overlay=_balance_overlay, _is_director_payment=_is_director_payment, _num=_num,
        _overlay_mode=_overlay_mode, _payment_channel=_payment_channel, _sale_channels=_sale_channels,
        _sale_total=_sale_total,
    )

# ─── write helper ─────────────────────────────────────────────────────────────

def write(filename, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / filename, "w") as f:
        json.dump(data, f, default=str, indent=2)
    print(f"  {filename}")


def stage_balance_overlay_config():
    """Mirror the reviewed anchor policy into the generated R2 bundle.

    The source-controlled seed is the only financial-rule authority.  R2 holds
    a byte-for-byte working copy so the live bundle cannot look editable while
    being ignored by the common engine.
    """
    global _BALANCE_OVERLAY
    source = PRIVATE_SEED_DIR / "balance_anchors.json"
    target = DATA_DIR / "balance_anchors.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = source.read_bytes()
    if not target.exists() or target.read_bytes() != content:
        target.write_bytes(content)
        print("  balance_anchors.json (mirrored reviewed seed)")
    _BALANCE_OVERLAY = None

def _bank_amount_key(row):
    return sync_archive._bank_amount_key(row, _num=_num)

_erp_credit_ref = sync_archive._erp_credit_ref

def _bank_dedupe_key(row):
    return sync_archive._bank_dedupe_key(row, _bank_amount_key=_bank_amount_key)

_bank_row_quality = sync_archive._bank_row_quality

def dedupe_bank_rows(rows):
    return sync_archive.dedupe_bank_rows(
        rows, _bank_dedupe_key=_bank_dedupe_key, _bank_row_quality=_bank_row_quality,
        _is_excluded_customer_receipt_bank_row=_is_excluded_customer_receipt_bank_row,
        _is_vendor_payment_bank_row=_is_vendor_payment_bank_row,
    )

def _archive_key(section, row):
    return sync_archive._archive_key(section, row, _bank_dedupe_key=_bank_dedupe_key)

_is_vendor_payment_expense = sync_archive._is_vendor_payment_expense

_is_vendor_payment_bank_row = sync_archive._is_vendor_payment_bank_row

_row_quality = sync_archive._row_quality

def _prefer_archive_row(section, existing, incoming):
    return sync_archive._prefer_archive_row(section, existing, incoming, _row_quality=_row_quality)

def _historical_existing_dates(rows):
    return sync_archive._historical_existing_dates(
        rows, IST=IST, MERGE_PROTECT_BEFORE_DATE=MERGE_PROTECT_BEFORE_DATE,
    )

_expense_content_key = sync_archive._expense_content_key

def _merge_archive_rows(existing, incoming, section, *, drop_current_window=True):
    return sync_archive._merge_archive_rows(
        existing, incoming, section, drop_current_window=drop_current_window, IST=IST,
        MERGE_PROTECT_BEFORE_DATE=MERGE_PROTECT_BEFORE_DATE, _archive_key=_archive_key,
        _expense_content_key=_expense_content_key, _historical_existing_dates=_historical_existing_dates,
        _is_vendor_payment_bank_row=_is_vendor_payment_bank_row,
        _is_vendor_payment_expense=_is_vendor_payment_expense, _prefer_archive_row=_prefer_archive_row,
    )

_bank_key = sync_archive._bank_key

def derive_bank_transactions(sales, expenses, repayments, existing=None):
    return sync_archive.derive_bank_transactions(
        sales, expenses, repayments, existing, _bank_key=_bank_key,
        _is_excluded_customer_receipt=_is_excluded_customer_receipt,
        _is_excluded_customer_receipt_bank_row=_is_excluded_customer_receipt_bank_row,
        _is_vendor_payment_bank_row=_is_vendor_payment_bank_row, _num=_num,
        _payment_channel=_payment_channel, _sale_channels=_sale_channels,
    )

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
):
    return sync_archive.write_archive_updates(
        today, all_sales, all_expenses, internal_transfers, cash_rows, bank_rows, boulder_rows, repayments,
        vendor_payments, local_seed, balance_snapshots, ledger_by_month, ARCHIVE_DIR=ARCHIVE_DIR, IST=IST,
        _is_excluded_customer_receipt=_is_excluded_customer_receipt,
        _is_excluded_customer_receipt_bank_row=_is_excluded_customer_receipt_bank_row,
        _merge_archive_rows=_merge_archive_rows, _num=_num,
    )

def snapshot_key(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "_"]
    normalized = urlunsplit(("", "", parts.path, urlencode(query), ""))
    return base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")

def write_snapshot(url, data):
    SNAPSHOT_API_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{snapshot_key(url)}.json"
    from r2_working_set import skip_cold_unchanged, reserve_snapshot_write
    payload = json.dumps(data, default=str, separators=(",", ":")).encode("utf-8")
    if not skip_cold_unchanged(DATA_DIR, "snapshot/api/" + filename, payload):
        reserve_snapshot_write(DATA_DIR, "snapshot/api/" + filename, len(payload))
        (SNAPSHOT_API_DIR / filename).write_bytes(payload)
    _WRITTEN_SNAPSHOT_FILES.add(filename)


def _snapshot_url_from_path(path: Path) -> Optional[str]:
    """Decode a static API filename back to its canonical request URL."""
    try:
        encoded = path.stem
        encoded += "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def prune_obsolete_derived_range_snapshots() -> tuple[int, int]:
    """Drop stale non-book range-cache files imported from an older R2 run.

    The browser can fall back to the monthly archive for these routes.  Cash
    book objects are explicitly retained: their displayed balances are only
    supplied by the canonical server-generated book, never browser arithmetic.
    This runs at the end of a normal engine build, after previous snapshots may
    have been used to reconcile customer ledgers.
    """
    removed_count = removed_bytes = 0
    removed_keys = []
    from r2_working_set import previous_files
    previous = previous_files()
    candidates = set(SNAPSHOT_API_DIR.glob("*.json")) | {
        DATA_DIR / key for key in previous if key.startswith("snapshot/api/")
    }
    for path in sorted(candidates):
        if path.name in _WRITTEN_SNAPSHOT_FILES:
            continue
        url = _snapshot_url_from_path(path)
        if not url:
            continue
        if not is_archive_reconstructible_range_snapshot(url):
            continue
        try:
            key = path.relative_to(SNAPSHOT_API_DIR.parent.parent).as_posix()
            removed_bytes += path.stat().st_size if path.exists() else previous[key]["size"]
            path.unlink(missing_ok=True)
            removed_count += 1
            # This key is an archive-reconstructible browser cache, not
            # financial source data nor a canonical Cash/Bank book.  Recovery
            # therefore need not copy thousands of such stale cache files.
            removed_keys.append(path.relative_to(SNAPSHOT_API_DIR.parent.parent).as_posix())
        except FileNotFoundError:
            pass
    retention_list = SNAPSHOT_API_DIR.parent.parent / "control" / "retention_expired_snapshot_keys.txt"
    retention_list.parent.mkdir(parents=True, exist_ok=True)
    retention_list.write_text("".join(f"{key}\n" for key in sorted(removed_keys)), encoding="utf-8")
    return removed_count, removed_bytes


def write_compliance_snapshots(dataset, from_date, to_date):
    return sync_snapshots.write_compliance_snapshots(dataset, from_date, to_date, write_snapshot=write_snapshot)

def read_snapshot_payload(url):
    path = SNAPSHOT_API_DIR / f"{snapshot_key(url)}.json"
    from r2_working_set import hydrate
    # Hydration failures must escape the optional-payload fallback below.
    hydrate(DATA_DIR, path.relative_to(DATA_DIR).as_posix())
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def read_snapshot(url):
    data = read_snapshot_payload(url)
    return data if isinstance(data, dict) else None

def read_snapshot_list(url):
    data = read_snapshot_payload(url)
    return data if isinstance(data, list) else []

def read_data_payload(filename):
    try:
        with open(DATA_DIR / filename, "r") as f:
            return json.load(f)
    except Exception:
        return None

def read_data_list(filename):
    data = read_data_payload(filename)
    return data if isinstance(data, list) else []

def _repayment_copy(rows):
    if not isinstance(rows, list):
        return None
    return [dict(row) for row in rows if isinstance(row, dict)]

def _saved_debtors_from_rows(rows):
    debtors = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("customer_name") or "").strip()
        if not name:
            continue
        outstanding = _num(row.get("outstanding", row.get("balance", 0.0)))
        billed = _num(row.get("erp_debit_balance", row.get("total_sales", row.get("billed", outstanding))))
        received = _num(row.get("erp_credit_balance", row.get("total_receipts", row.get("received", 0.0))))
        debtors.append({
            "name": name[:200],
            "outstanding": round(outstanding, 2),
            "billed": round(billed, 2),
            "received": round(received, 2),
            "erp_customer_id": row.get("erp_customer_id") or row.get("id"),
        })
    return debtors

def _saved_creditors_from_rows(rows):
    creditors = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("vendor_name") or "").strip()
        if not name:
            continue
        payable = _num(row.get("payable", row.get("balance", 0.0)))
        creditors.append({
            "name": name[:200],
            "payable": round(payable, 2),
            "erp_supplier_id": row.get("erp_supplier_id") or row.get("id"),
        })
    return creditors

def _repayment_key(row):
    return (
        str(row.get("date", ""))[:10],
        row.get("customer_name", ""),
        row.get("reference", ""),
        round(_num(row.get("amount")), 2),
        round(_num(row.get("payment_received", row.get("amount"))), 2),
    )

def merge_repayment_rows(*row_sets):
    merged = {}
    for rows in row_sets:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if _is_excluded_customer_receipt(row):
                continue
            merged[_repayment_key(row)] = dict(row)
    return sorted(
        merged.values(),
        key=lambda row: (str(row.get("date", ""))[:10], row.get("customer_name", "")),
        reverse=True,
    )

def replace_repayment_day(base_rows, day, fresh_rows):
    day_s = str(day)
    kept = [dict(row) for row in base_rows or [] if str(row.get("date", ""))[:10] != day_s]
    return merge_repayment_rows(kept, fresh_rows or [])

def latest_seed_control(local_seed):
    controls = (local_seed.get("controls") or {}) if isinstance(local_seed, dict) else {}
    latest_key = ""
    latest_control = None
    for key, value in controls.items():
        if "|" not in key:
            continue
        _, end = key.split("|", 1)
        if end >= latest_key:
            latest_key = end
            latest_control = value
    return latest_control

def apply_seed_control_overrides(control, local_seed, start, end):
    controls = (local_seed.get("controls") or {}) if isinstance(local_seed, dict) else {}
    seed_control = controls.get(f"{start}|{end}")
    fallback_control = latest_seed_control(local_seed)
    source_control = seed_control or fallback_control
    if not source_control:
        return control
    seed_summary = source_control.get("summary") or {}
    summary = control.setdefault("summary", {})
    for key in (
        "bank_balance_book",
        "cash_balance_office_book",
    ):
        if key in seed_summary:
            summary[key] = seed_summary[key]
    if (
        seed_control
        and "credit_payment_received" in seed_summary
        and "customer_repayments_payment_total" not in seed_control
        and "customer_repayments_total" not in seed_control
    ):
        summary["credit_payment_received"] = seed_summary["credit_payment_received"]
    if seed_control:
        for key in ("receivables", "payables"):
            if key in seed_summary:
                summary[key] = seed_summary[key]
    for key in (
        "customer_repayments",
        "customer_repayments_total",
        "customer_repayments_payment_total",
        "customer_repayments_bank_total",
        "customer_repayments_cash_total",
    ):
        if seed_control and key in seed_control:
            control[key] = seed_control[key]
    if seed_control and "top_receivables" in seed_control:
        control["top_receivables"] = seed_control["top_receivables"]
    if seed_control and "top_payables" in seed_control:
        control["top_payables"] = seed_control["top_payables"]
    if "customer_repayments_payment_total" in control:
        summary["credit_payment_received"] = control["customer_repayments_payment_total"]
    elif "customer_repayments_total" in control:
        summary["credit_payment_received"] = control["customer_repayments_total"]
    return control

_BALANCE_OVERLAY = None


def _balance_overlay():
    """Verified balance overlay (anchors + ICICI statement + mode corrections), mirroring the
    client _archiveOperatingBalances. Lets snapshots carry correct balances for TODAY too
    (the archive lags a day). No extra files/commits — only corrects snapshot content."""
    global _BALANCE_OVERLAY
    if _BALANCE_OVERLAY is None:
        cfg = {}
        try:
            # The reviewed anchor/reconciliation policy is source-controlled;
            # R2 data is intentionally a generated working copy and must not
            # overwrite this financial rule during startup.
            with open(PRIVATE_SEED_DIR / "balance_anchors.json") as f:
                cfg = json.load(f)
        except Exception:
            try:
                with open(DATA_DIR / "balance_anchors.json") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        stmt_rows, stmt_to = [], None
        fn = cfg.get("bank_statement_file")
        if fn:
            try:
                with open(DATA_DIR / fn) as f:
                    sd = json.load(f)
                    stmt_rows = sd.get("rows") or []
                    stmt_to = str(sd.get("to") or "")
            except Exception:
                pass
        _BALANCE_OVERLAY = {
            "anchors": sorted(cfg.get("anchors", []), key=lambda a: str(a.get("date"))),
            "corrections": cfg.get("mode_corrections", []),
            # Retain the source file as historical evidence, but never turn
            # its reconstructed daily balances into ledger movements.  The
            # cash book must come from Loctell movements plus named physical
            # balance anchors only.
            "cash_daily_closings": {},
            "stmt_rows": stmt_rows,
            "stmt_to": stmt_to,
        }
    return _BALANCE_OVERLAY


def _overlay_mode(corrs, e):
    return sync_finance._overlay_mode(corrs, e, _num=_num)


def _overlay_balance(to_iso, sales, expenses, repayments, internal_transfers=None):
    return sync_finance._overlay_balance(
        to_iso, sales, expenses, repayments, internal_transfers, _balance_overlay=_balance_overlay,
        _num=_num, _overlay_mode=_overlay_mode, _payment_channel=_payment_channel,
        _sale_channels=_sale_channels,
    )


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
):
    return sync_finance.build_ledger_view(
        sales, expenses, vendor_payments, boulder_rows, repayments, year, month, opening_bank, opening_cash,
        movement_start, today, overlay_repayments, internal_transfers, _balance_overlay=_balance_overlay,
        _num=_num, _overlay_balance=_overlay_balance, _overlay_mode=_overlay_mode,
        _payment_channel=_payment_channel, _sale_channels=_sale_channels, _sale_total=_sale_total,
    )


def build_cashbook_view(from_d, to_d, sales, expenses, repayments, opening, internal_transfers=None):
    return sync_finance.build_cashbook_view(
        from_d, to_d, sales, expenses, repayments, opening, internal_transfers, ErpFetchError=ErpFetchError,
        _balance_overlay=_balance_overlay, _num=_num, _overlay_balance=_overlay_balance,
        _overlay_mode=_overlay_mode, _payment_channel=_payment_channel, _sale_channels=_sale_channels,
        _sale_settlement_roundoff=_sale_settlement_roundoff,
    )

empty_ledger = sync_finance.empty_ledger

def build_vendor_ledgers(vendors_full, vendor_payments, full_ledgers=None):
    return sync_finance.build_vendor_ledgers(
        vendors_full, vendor_payments, full_ledgers, _num=_num, _vendor_identity=_vendor_identity,
        empty_ledger=empty_ledger,
    )


def vendor_payable_due_aging(entries, payable, as_of):
    return sync_finance.vendor_payable_due_aging(entries, payable, as_of, _num=_num)


def vendor_payable_age_buckets(entries, payable, as_of):
    return sync_finance.vendor_payable_age_buckets(entries, payable, as_of, _num=_num)


def vendor_rows_as_of(master_rows, balance_rows, vendor_ledgers, as_of):
    return sync_finance.vendor_rows_as_of(
        master_rows, balance_rows, vendor_ledgers, as_of, _num=_num, _vendor_identity=_vendor_identity,
        vendor_payable_age_buckets=vendor_payable_age_buckets,
        vendor_payable_due_aging=vendor_payable_due_aging,
    )


def archived_vendor_balances_as_of(archive_rows, master_rows):
    return sync_finance.archived_vendor_balances_as_of(
        archive_rows, master_rows, ErpFetchError=ErpFetchError, _norm_name=_norm_name, _num=_num,
    )


def historical_vendor_master_rows(current_rows, balance_rows):
    return sync_finance.historical_vendor_master_rows(
        current_rows, balance_rows, ErpFetchError=ErpFetchError, _norm_name=_norm_name,
        _vendor_identity=_vendor_identity, load_vendor_master=load_vendor_master,
    )


def canonical_vendor_master(seed_rows, creditors, *, source_master=None):
    return sync_finance.canonical_vendor_master(
        seed_rows, creditors, source_master=source_master, _norm_name=_norm_name,
        _vendor_identity=_vendor_identity, load_vendor_master=load_vendor_master,
    )


LEDGER_HISTORY_START = date(2026, 3, 1)  # receipts data begins here; before this is folded into opening balance


_PREV_LEDGER_CACHE = None
def _prev_customer_ledgers_by_name():
    """Index the previous run's reconciling (source=='erp') customer-ledger snapshots by normalised
    customer name, so a customer's own full ledger is reused across positional-id shifts. Runs before
    this sync overwrites any ledger snapshot, so the files on disk are the previous run's (from R2).
    Memoised per run (snapshots aren't overwritten until the end of the run)."""
    global _PREV_LEDGER_CACHE
    if _PREV_LEDGER_CACHE is not None:
        return _PREV_LEDGER_CACHE
    out = {}
    try:
        for path in SNAPSHOT_API_DIR.glob("*.json"):
            try:
                with open(path) as fh:
                    j = json.load(fh)
            except Exception:
                continue
            if isinstance(j, dict) and j.get("customer_name") and j.get("source") == "erp" and j.get("entries"):
                out[_norm_name(j["customer_name"])] = j
    except Exception:
        pass
    _PREV_LEDGER_CACHE = out
    return out


def build_customer_ledgers(customers_full, all_sales, repayments, today, full_ledgers=None):
    return sync_finance.build_customer_ledgers(
        customers_full, all_sales, repayments, today, full_ledgers,
        LEDGER_HISTORY_START=LEDGER_HISTORY_START, _norm_name=_norm_name, _num=_num,
        _prev_customer_ledgers_by_name=_prev_customer_ledgers_by_name, _sale_total=_sale_total,
        empty_ledger=empty_ledger, load_archive_window=load_archive_window,
    )

def _format_material_sold(materials):
    return sync_finance._format_material_sold(materials, _num=_num)

def _credit_due_15_plus_by_name(customers, all_sales, all_repayments, as_of, days=15):
    return sync_finance._credit_due_15_plus_by_name(
        customers, all_sales, all_repayments, as_of, days, IST=IST, _norm_name=_norm_name, _num=_num,
        _sale_channels=_sale_channels,
    )


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
):
    return sync_finance.build_customer_range_rows(
        customers_full, all_sales, range_sales, range_repayments, archive_balance, ending_debtors, as_of,
        all_repayments, aging_sales, aging_repayments,
        _credit_due_15_plus_by_name=_credit_due_15_plus_by_name,
        _format_material_sold=_format_material_sold, _norm_name=_norm_name, _num=_num,
        _sale_channels=_sale_channels, _sale_total=_sale_total,
    )

def build_gstr1(sales_rows, name_to_gstin, exports_config, year, month):
    return sync_snapshots.build_gstr1(sales_rows, name_to_gstin, exports_config, year, month, _num=_num)

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
):
    return sync_snapshots.write_snapshot_bundle(
        today, yesterday, month_start, financial_year_start, all_sales, all_expenses, internal_transfers,
        labour_rows, parts_rows, machines_rows, odometer_readings, odometer_history, vmi_loader_fuel_issues,
        fuel_received_rows, fuel_balance, boulder_rows, iot_rows, cash_rows, bank_rows, cash_balance,
        bank_net, bank_balance_book, cash_balance_office_book, customers_full, customers_outstanding,
        vendors_full, vendors_payables, vendor_ledgers, vendor_payments, repayments, local_seed, controls,
        balance_snapshots, archive_balances, customer_ledgers_full, historical_start, aging_sales,
        aging_repayments, DATA_DIR=DATA_DIR, ERP_BASE=ERP_BASE, ERP_ORG=ERP_ORG, ERP_USER=ERP_USER,
        ErpFetchError=ErpFetchError, IST=IST, _balance_overlay=_balance_overlay, _num=_num,
        _overlay_balance=_overlay_balance, _vendor_identity=_vendor_identity,
        apply_seed_control_overrides=apply_seed_control_overrides,
        archived_vendor_balances_as_of=archived_vendor_balances_as_of, build_control=build_control,
        build_customer_ledgers=build_customer_ledgers, build_customer_range_rows=build_customer_range_rows,
        build_gstr1=build_gstr1, build_ledger_view=build_ledger_view, empty_ledger=empty_ledger,
        historical_vendor_master_rows=historical_vendor_master_rows,
        latest_seed_control=latest_seed_control, load_archive_manifest=load_archive_manifest,
        load_book_balance_accounts=load_book_balance_accounts, vendor_rows_as_of=vendor_rows_as_of,
        write_snapshot=write_snapshot,
    )

# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().isoformat(timespec='seconds')}] GHA ERP sync starting...")
    # Establish the reviewed anchor policy before any range/window calculation
    # asks the overlay for its latest verified balance anchor.
    stage_balance_overlay_config()
    sess = erp_auth()
    print("  Authenticated with loctell.com")

    global MERGE_PROTECT_BEFORE_DATE
    today       = datetime.now(IST).date()
    yesterday   = today - timedelta(days=1)
    month_start = today.replace(day=1)
    financial_year_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    week_start  = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start - timedelta(days=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    sync_mode = os.environ.get("OTOMY_SYNC_MODE", "recent").strip().lower()
    try:
        recent_days = max(1, int(os.environ.get("OTOMY_RECENT_DAYS", "7")))
    except ValueError:
        recent_days = 7
    if sync_mode in {"full", "fy", "rebuild"}:
        full_from = os.environ.get("OTOMY_FULL_FROM", "2026-04-01").strip() or "2026-04-01"
        try:
            sync_start = date.fromisoformat(full_from)
        except ValueError:
            sync_start = date(2026, 4, 1)
            full_from = sync_start.isoformat()
        sync_mode = "full"
        sync_label = f"full historical window from {full_from}"
    elif sync_mode in {"monthly", "month", "current_last_month"}:
        sync_start = last_month_start
        sync_label = "current month + last month"
    else:
        sync_mode = "recent"
        sync_start = today - timedelta(days=recent_days - 1)
        sync_label = f"last {recent_days} days"
    archive_start = min(sync_start, last_month_start)
    # A recent sync fetches only a small Loctell delta, but it also publishes
    # canonical FYTD snapshots.  Those snapshots must be derived from the
    # complete FY archive, never from the latest balance anchor onward.
    # Keeping sync_start unchanged preserves the cheap delta fetch; only the
    # local archive read is widened.
    archive_start = min(archive_start, financial_year_start)
    # The balance overlay runs from the latest verified anchor and needs EVERY movement after it.
    # When the anchor predates the sync window (e.g. a 28-Jun anchor while August's "last month" is
    # July), the days between the anchor and the window start (29-30 Jun) fall in a gap and the
    # cash/bank balance comes out short by exactly those movements. Floor the archive window to the
    # anchor date so its month's archive is loaded and those movements are always counted. In a
    # monthly (on-demand) sync also floor the FETCH/refresh window itself to the anchor, so that
    # post-anchor sliver is re-fetched and its receipts re-derived fresh (named/deduped) rather than
    # left as stale stripped archive rows — this is what lets otomy self-compute straight from the
    # anchor with no external pin. Only extends coverage by the few days between anchor and window.
    anchor_date = None
    try:
        _anchors = _balance_overlay().get("anchors", [])
        if _anchors:
            anchor_date = date.fromisoformat(str(_anchors[-1]["date"]))
            archive_start = min(archive_start, anchor_date)
            if sync_mode in {"monthly", "month", "current_last_month"}:
                sync_start = min(sync_start, anchor_date)
    except Exception as _e:
        print(f"  anchor-floor skipped: {_e}")
    MERGE_PROTECT_BEFORE_DATE = sync_start.isoformat()
    print(f"  Sync mode: {sync_mode} ({sync_label}); fetching {sync_start} to {today}")
    local_seed = load_local_seed()
    seed_endpoints = local_seed.get("endpoints", {}) if isinstance(local_seed, dict) else {}
    archive_manifest = load_archive_manifest()
    archive_rows = load_archive_window(archive_start, today)
    archive_balances = {
        str(row.get("date", ""))[:10]: row
        for row in archive_rows.get("balances", [])
        if row.get("date")
    }
    labour_rows = merge_rows_by_archive_key(archive_rows.get("labour"), seed_endpoints.get("labour_30d", []), "labour")
    parts_rows = merge_rows_by_archive_key(archive_rows.get("parts"), seed_endpoints.get("parts_30d", []), "parts")
    machines_rows = merge_rows_by_archive_key(archive_rows.get("machines"), seed_endpoints.get("machines_30d", []), "machines")
    seed_config = dict(seed_endpoints.get("exports_config", {}))
    if not seed_config.get("operating_balance_opening") and archive_manifest.get("operating_balance_opening"):
        seed_config["operating_balance_opening"] = archive_manifest["operating_balance_opening"]
    # Reuse the same reviewed/manual fallback used by the published account
    # endpoint.  Previously these Dashboard fields summed only an empty local
    # seed list even though the fallback account itself was available.
    seed_bank_accounts = seed_endpoints.get("bank_accounts") or load_book_balance_accounts()

    bank_balance_book = round(
        sum(_num(row.get("current_balance")) for row in seed_bank_accounts if row.get("active", True)),
        2,
    )
    cash_balance_office_book = round(
        sum(
            _num(row.get("current_balance"))
            for row in seed_bank_accounts
            if row.get("active", True)
            and "HDFC" in f"{row.get('name', '')} {row.get('bank_name', '')}".upper()
        ),
        2,
    )

    def saved_debtors_snapshot(as_of):
        for rows in (
            read_snapshot_list(f"/api/customers/outstanding?as_of={as_of}"),
            read_snapshot_list("/api/customers/outstanding"),
            seed_endpoints.get("customers_outstanding", []),
            read_snapshot_list(f"/api/customers/?active_only=false&as_of={as_of}"),
            read_snapshot_list("/api/customers/?active_only=false"),
            seed_endpoints.get("customers_all", []),
        ):
            debtors = _saved_debtors_from_rows(rows)
            if debtors:
                return debtors
        return []

    def saved_creditors_snapshot(as_of):
        for rows in (
            read_snapshot_list(f"/api/vendors/payables?as_of={as_of}"),
            read_snapshot_list("/api/vendors/payables"),
            seed_endpoints.get("vendors_payables", []),
            read_snapshot_list(f"/api/vendors/?active_only=false&as_of={as_of}"),
            read_snapshot_list("/api/vendors/?active_only=false"),
            seed_endpoints.get("vendors_all", []),
        ):
            creditors_snapshot = _saved_creditors_from_rows(rows)
            if creditors_snapshot:
                return creditors_snapshot
        return []

    def fetch_result_or_saved(future, label, as_of, saved_loader):
        try:
            return future.result(), True
        except ErpFetchError as e:
            saved_rows = saved_loader(as_of)
            if saved_rows:
                print(f"  {label} fetch failed; using saved non-empty snapshot ({len(saved_rows)} rows): {e}")
                return saved_rows, False
            raise

    def rows_between_dates(rows, start, end):
        fs, ts = str(start), str(end)
        return [dict(row) for row in rows or [] if fs <= str(row.get("date", "")) <= ts]

    def saved_stream_rows(section, start, end, filename=None):
        rows = rows_between_dates(archive_rows.get(section, []), start, end)
        if rows:
            return rows
        if filename:
            rows = rows_between_dates(read_data_list(filename), start, end)
            if rows:
                return rows
        if section in ("cash", "bank"):
            ledger = read_data_payload("erp_ledger.json") or {}
            rows = rows_between_dates(ledger.get(section, []), start, end)
            if rows:
                return rows
        return []

    def saved_vendor_payment_rows(start, end):
        archived_rows = rows_between_dates(archive_rows.get("vendor_payments", []), start, end)
        if archived_rows:
            return [dict(row) for row in archived_rows]
        rows = []
        for row in saved_stream_rows("bank", start, end):
            if row.get("source") != "Vendor Payment" and row.get("bank_name") != "UPI/Bank Vendor Payment":
                continue
            amount = _num(row.get("debit"))
            if amount <= 0:
                continue
            description = str(row.get("description") or "")
            vendor_name = description.split(" - ", 1)[1].strip() if " - " in description else "Vendor"
            row_id = str(row.get("id") or "")
            reference = row_id.replace("vendor-payment-", "", 1) if row_id.startswith("vendor-payment-") else row_id
            rows.append({
                "date": str(row.get("date", ""))[:10],
                "vendor_name": vendor_name,
                "amount": amount,
                "mode": "Bank",
                "reference": reference or f"ARCHIVE-VENDOR-{row.get('date')}-{int(round(amount))}",
                "notes": "Archived vendor payment fallback",
            })
        return rows

    def fetch_rows_or_saved(future, label, saved_rows):
        try:
            return future.result(), True
        except ErpFetchError as e:
            if saved_rows:
                print(f"  {label} fetch failed; using archived non-empty rows ({len(saved_rows)} rows): {e}")
                return saved_rows, False
            raise

    def fetch_vendor_payments_or_saved():
        # Vendor-page MTD totals must include the whole live month.  A short
        # recent delta alone can omit an early-month supplier payment that was
        # added or corrected after its original archive window.
        vendor_payment_start = min(sync_start, month_start)
        try:
            return fetch_vendor_payments(sess, creditors, vendor_payment_start, today), True
        except ErpFetchError as e:
            saved_rows = saved_vendor_payment_rows(vendor_payment_start, today)
            if saved_rows:
                print(f"  vendor payments fetch failed; using archived non-empty rows ({len(saved_rows)} rows): {e}")
                return saved_rows, False
            print(f"  vendor payments fetch failed; no saved rows; continuing without vendor-payment rows: {e}")
            return [], False

    def saved_boulder_summary(label, start, end):
        summaries = read_data_payload("boulders.json") or {}
        summary = summaries.get(label) if isinstance(summaries, dict) else None
        if isinstance(summary, dict) and (_num(summary.get("total_tonnes")) or _num(summary.get("total_trips"))):
            return dict(summary)
        rows = saved_stream_rows("boulders", start, end)
        tonnes = round(sum(_num(row.get("total_tonnes")) for row in rows), 2)
        trips = round(sum(_num(row.get("trips")) for row in rows), 2)
        if rows or tonnes or trips:
            return {"total_tonnes": tonnes, "total_trips": trips, "materials": [], "suppliers": []}
        return {}

    def fetch_summary_or_saved(future, label, saved_summary):
        try:
            return future.result(), True
        except ErpFetchError as e:
            if saved_summary:
                print(f"  {label} fetch failed; using saved boulder summary: {e}")
                return saved_summary, False
            raise

    # A compact per-day odometer history lets Otomy calculate any selected
    # range exactly like Loctell without publishing one R2 object per range.
    previous_odometer_history = read_snapshot_list("/api/machines/odometer-history")
    odometer_history_start = financial_year_start if sync_mode == "full" or not previous_odometer_history else max(sync_start, financial_year_start)

    # ── parallel fetch all independent ERP streams ────────────────────────────
    print("  Fetching all ERP streams in parallel...")
    with ThreadPoolExecutor(max_workers=10) as pool:
        f_sales    = pool.submit(fetch_sales,        _clone_sess(sess), sync_start, today)
        f_expenses = pool.submit(fetch_expenses,     _clone_sess(sess), sync_start, today)
        f_b_today  = pool.submit(fetch_boulders,     _clone_sess(sess), today,      today)
        f_b_yest   = pool.submit(fetch_boulders,     _clone_sess(sess), yesterday,  yesterday)
        f_b_week   = pool.submit(fetch_boulders,     _clone_sess(sess), week_start, today)
        f_b_mtd    = pool.submit(fetch_boulders,     _clone_sess(sess), month_start, today)
        f_b_rows   = pool.submit(fetch_boulder_rows, _clone_sess(sess), sync_start, today)
        # IOT removed — endpoint not used
        f_cash     = pool.submit(fetch_cash_ledger,  _clone_sess(sess), sync_start, today)
        f_bank     = pool.submit(fetch_bank_entries, _clone_sess(sess), sync_start, today)
        f_transfers = pool.submit(fetch_internal_transfers, _clone_sess(sess), sync_start, today)
        f_debtors  = pool.submit(fetch_debtors,      _clone_sess(sess), today)
        f_debtors_yest = pool.submit(fetch_debtors,  _clone_sess(sess), yesterday)
        f_creditors = pool.submit(fetch_creditors,   _clone_sess(sess), today)
        f_odometer = pool.submit(fetch_live_odometer_readings, _clone_sess(sess), today)
        f_odometer_history = pool.submit(fetch_odometer_history, _clone_sess(sess), odometer_history_start, today, 5)
        f_vmi_fuel = pool.submit(fetch_machine_fuel_issues, _clone_sess(sess), financial_year_start, today)
        f_fuel_received = pool.submit(fetch_fuel_received, _clone_sess(sess), financial_year_start, today)
        f_fuel_balance = pool.submit(fetch_fuel_dashboard_balance, _clone_sess(sess))

        fresh_sales, sales_fresh = fetch_rows_or_saved(
            f_sales, "sales", saved_stream_rows("sales", sync_start, today, "sales_all.json")
        )
        fresh_expenses, expenses_fresh = fetch_rows_or_saved(
            f_expenses, "expenses", saved_stream_rows("expenses", sync_start, today, "expenses_all.json")
        )
        boulders_today, boulders_today_fresh = fetch_summary_or_saved(
            f_b_today, "today boulders", saved_boulder_summary("today", today, today)
        )
        boulders_yesterday, boulders_yesterday_fresh = fetch_summary_or_saved(
            f_b_yest, "yesterday boulders", saved_boulder_summary("yesterday", yesterday, yesterday)
        )
        boulders_week, boulders_week_fresh = fetch_summary_or_saved(
            f_b_week, "this week boulders", saved_boulder_summary("week", week_start, today)
        )
        boulders_mtd, boulders_mtd_fresh = fetch_summary_or_saved(
            f_b_mtd, "MTD boulders", saved_boulder_summary("mtd", month_start, today)
        )
        fresh_b_rows, boulder_rows_fresh = fetch_rows_or_saved(
            f_b_rows, "boulder rows", saved_stream_rows("boulders", sync_start, today)
        )
        iot_rows          = []
        fresh_cash, cash_fresh = fetch_rows_or_saved(
            f_cash, "cash ledger", saved_stream_rows("cash", sync_start, today)
        )
        fresh_bank, bank_fresh = fetch_rows_or_saved(
            f_bank, "bank entries", saved_stream_rows("bank", sync_start, today)
        )
        fresh_internal_transfers, transfers_fresh = fetch_rows_or_saved(
            f_transfers, "internal transfers", saved_stream_rows("internal_transfers", sync_start, today)
        )
        debtors_today, debtors_today_fresh = fetch_result_or_saved(
            f_debtors, "today debtors", today, saved_debtors_snapshot
        )
        _debtors_yest_pre, debtors_yesterday_fresh = fetch_result_or_saved(
            f_debtors_yest, "yesterday debtors", yesterday, saved_debtors_snapshot
        )
        creditors, creditors_today_fresh = fetch_result_or_saved(
            f_creditors, "today creditors", today, saved_creditors_snapshot
        )
        try:
            odometer_readings = f_odometer.result()
        except ErpFetchError as exc:
            # This is a live-only operating panel.  Do not show stale readings
            # or block financial publication if Loctell's machinery report is
            # temporarily unavailable.
            print(f"  machinery odometers unavailable: {exc}")
            odometer_readings = []
        try:
            candidate_odometer_history = merge_odometer_history(previous_odometer_history, f_odometer_history.result())
            validate_odometer_history(candidate_odometer_history)
            odometer_history = candidate_odometer_history
        except ErpFetchError as exc:
            print(f"  machinery odometer history unavailable; retaining prior history: {exc}")
            odometer_history = previous_odometer_history
        except ValueError as exc:
            print(f"  machinery odometer history guard failed; retaining prior history: {exc}")
            odometer_history = previous_odometer_history
        # Today's aggregate is the same official source as the live panel and
        # must win if the parallel history refresh used an earlier read.
        if odometer_readings:
            odometer_history = merge_odometer_history(odometer_history, [{"date": str(today), "readings": odometer_readings}])
        if odometer_history:
            validate_odometer_history(odometer_history)
        try:
            vmi_loader_fuel_issues = f_vmi_fuel.result()
        except ErpFetchError as exc:
            # Do not carry a stale fuel total into a new selected range.
            print(f"  machine fuel issues unavailable: {exc}")
            vmi_loader_fuel_issues = []
        try:
            fuel_received_rows = f_fuel_received.result()
        except ErpFetchError as exc:
            # This report is displayed as an operational source table only.
            print(f"  fuel received report unavailable: {exc}")
            fuel_received_rows = []
        try:
            fuel_balance = fuel_balance_with_value(f_fuel_balance.result(), fuel_received_rows)
        except ErpFetchError as exc:
            print(f"  fuel dashboard balance unavailable: {exc}")
            fuel_balance = {}

    # Capture per-ticket payment split (ERP ListSale Final Cash/Credit/UPI) onto fresh sales.
    try:
        _splits = fetch_sale_splits(sess, sync_start, today)
        _n = 0
        _rejected = 0
        for _s in fresh_sales:
            _sp = _splits.get(_sale_split_key(_s.get("date"), _s.get("ticket_no")))
            if _sp is not None and "mdp" in _sp:
                _s["mdp_ton"] = _sp["mdp"]  # real MDP Ton (differs from sale/net tonnage)
            if _sp and (_split_reconciles_sale(_s, _sp) or _is_explicit_mixed_tender_split(_sp)):
                _s["cash_amount"] = _sp["cash"]; _s["credit_amount"] = _sp["credit"]; _s["upi_amount"] = _sp["upi"]
                _n += 1
            elif _sp and (_num(_sp.get("cash")) + _num(_sp.get("credit")) + _num(_sp.get("upi"))) > 0:
                _rejected += 1
        print(f"  sale splits captured for {_n}/{len(fresh_sales)} fresh tickets; rejected {_rejected} non-reconciling splits")
    except Exception as _e:
        raise RuntimeError(f"ListSale split validation failed; refusing to publish sales: {_e}") from _e

    try:
        resolved_identities = resolve_fresh_credit_sale_identities(
            sess, fresh_sales, debtors_today, sync_start, today
        )
        if resolved_identities:
            print(f"  customer identities resolved from ERP ledgers: {resolved_identities}")
    except Exception as _e:
        raise RuntimeError(f"Customer identity validation failed; refusing to publish sales: {_e}") from _e

    all_sales = merge_rows_by_archive_key(archive_rows.get("sales"), fresh_sales, "sales")
    assert_fresh_sale_mdp_preserved(fresh_sales, all_sales)
    print(f"  {len(all_sales)} sales tickets")
    # The fresh fetch is the authoritative current state for its window — drop archived
    # expense versions inside that window so an ERP edit (note added, amount corrected)
    # replaces the old row instead of duplicating it (e.g. DMG OFFICER 8900 appearing twice).
    _fresh_window = {(sync_start + timedelta(days=i)).isoformat() for i in range((today - sync_start).days + 1)}
    _archive_exp = [e for e in (archive_rows.get("expenses") or []) if str(e.get("date"))[:10] not in _fresh_window]
    all_expenses = merge_rows_by_archive_key(_archive_exp, fresh_expenses, "expenses")
    if expenses_fresh:
        assert_fresh_source_rows_preserved("Expenses", fresh_expenses, all_expenses, _expense_content_key)
    print(f"  {len(all_expenses)} expenses")
    _archive_transfers = [row for row in (archive_rows.get("internal_transfers") or []) if str(row.get("date"))[:10] not in _fresh_window]
    all_internal_transfers = merge_rows_by_archive_key(_archive_transfers, fresh_internal_transfers, "internal_transfers")
    print(f"  {len(all_internal_transfers)} internal cash-to-bank transfers")
    boulder_rows = merge_rows_by_archive_key(
        archive_rows.get("boulders"),
        fresh_b_rows or seed_endpoints.get("boulders_30d", []),
        "boulders",
    )
    print(f"  Boulders today: {boulders_today['total_trips']} trips, {boulders_today['total_tonnes']} t")


    def sales_for(f, t):
        fs, ts = str(f), str(t)
        return [s for s in all_sales if fs <= s["date"] <= ts]

    def exp_for(f, t):
        fs, ts = str(f), str(t)
        return [e for e in all_expenses if fs <= e["date"] <= ts]

    def seed_for(rows, f, t):
        fs, ts = str(f), str(t)
        return [row for row in rows if fs <= row.get("date", "") <= ts]

    write("boulders.json", {
        "today":     boulders_today,
        "yesterday": boulders_yesterday,
        "week":      boulders_week,
        "mtd":       boulders_mtd,
    })

    statement_bank_rows = load_bank_statement_rows()
    cash_rows = merge_rows_by_archive_key(archive_rows.get("cash"), fresh_cash, "cash")
    # Drop cash-ledger rows that are really bank/UPI expense payments (e.g. VMI LOADER
    # "PAID FROM VMI ACCOUNT") so they appear only under Bank/UPI, not Cash.
    _bank_expenses = [e for e in all_expenses if _payment_channel(e.get("payment_mode") or "Cash") != "cash"]
    cash_rows = [r for r in cash_rows if not _cash_row_is_bank_expense(r, _bank_expenses)]
    bank_rows = [
        row for row in merge_rows_by_archive_key(archive_rows.get("bank"), fresh_bank, "bank")
        if str(row.get("source") or "") != "ICICI Statement"
    ]

    # absolute cash balance = last row's running balance from ERP cash ledger
    cash_balance = 0.0
    for row in cash_rows:
        if row.get("balance") is not None:
            cash_balance = row["balance"]

    statement_bank_balance = latest_bank_statement_balance(statement_bank_rows, today)
    # bank_net is the current bank balance shown on the dashboard.
    bank_net = statement_bank_balance if statement_bank_balance is not None else round(
        sum(r["credit"] for r in bank_rows) - sum(r["debit"] for r in bank_rows), 2
    )

    write("erp_ledger.json", {
        "opening":       {"date": str(sync_start), "cash": 0.0, "bank": 0.0},
        "cash":          cash_rows,
        "bank":          bank_rows,
        "cash_balance":  round(cash_balance, 2),
        "bank_net":      bank_net,
    })

    # ── debtors and ERP credit repayments ─────────────────────────────────────
    print(f"  {len(debtors_today)} customers")
    seed_controls = local_seed.get("controls") or {}

    def saved_control(start, end):
        control = seed_controls.get(f"{start}|{end}") or {}
        if control:
            return control
        return read_snapshot(f"/api/dashboard/control?from_date={start}&to_date={end}") or {}

    def saved_repayments(start, end):
        control = saved_control(start, end)
        return _repayment_copy(control.get("customer_repayments"))

    def require_repayments(label, rows):
        if rows is None:
            raise ErpFetchError(f"{label} repayments unavailable; skipped Otomy write")
        return [row for row in rows if not _is_excluded_customer_receipt(row)]

    debtors_yesterday = _debtors_yest_pre  # already fetched in parallel above
    debtors_cache = {today: debtors_today}
    if debtors_yesterday:
        debtors_cache[yesterday] = debtors_yesterday

    def fetch_debtor_cache(dates):
        missing = sorted({d for d in dates if d not in debtors_cache})
        if not missing:
            return
        with ThreadPoolExecutor(max_workers=min(len(missing), ERP_DEBTOR_WORKERS)) as pool:
            futs = {d: pool.submit(fetch_debtors, _clone_sess(sess), d) for d in missing}
            for d, f in futs.items():
                debtors_cache[d] = f.result()

    def compute_range_repayments(label, start, end, previous_date, current_date):
        inter_days = [start + timedelta(days=i) for i in range((end - start).days)]
        fetch_debtor_cache({previous_date, current_date, *inter_days})
        previous_rows = debtors_cache.get(previous_date) or []
        current_rows = debtors_cache.get(current_date) or []
        rows = compute_repayments_from_erp(
            _clone_sess(sess),
            start,
            end,
            previous_rows,
            current_rows,
            debtors_cache,
        )
        print(f"  {label} repayments computed from ERP: {len(rows)} rows")
        return rows

    saved_today = saved_repayments(today, today)
    saved_yesterday = saved_repayments(yesterday, yesterday)
    saved_mtd = saved_repayments(month_start, today)
    saved_last_month = saved_repayments(last_month_start, last_month_end)
    # A monthly (on-demand) sync must REFRESH last-month + MTD repayments from ERP rather than reuse
    # a stale saved control snapshot. The overlay balance nets these repayments against spot sales;
    # if they're stale, recent receipt edits are missed and cash/bank stays off. Force a recompute.
    if sync_mode in {"monthly", "month", "current_last_month"}:
        saved_mtd = None
        saved_last_month = None

    # ── Fresh ERP fetch is authoritative for the whole live window ──────────────
    # An ERP data-entry correction (an entry edited, or a duplicate removed) can land
    # on ANY recent day, not just today. The local DB re-reads the ledger every sync,
    # so it always reconciles; otomy must do the same or a prior day's stale value
    # sticks (e.g. a receipt duplicated then removed still shows the doubled figure).
    # So we recompute repayments for every day in the live window [repay_window_start,
    # today] straight from ERP and keep the saved snapshot only for OLDER days. This is
    # the same rule already applied to sales/expenses/cash/bank in _merge_archive_rows.
    # A full rebuild is specifically the repair path for back-dated Loctell
    # edits.  Recompute repayments from its requested start, not merely from
    # the current month; otherwise an edited April-June receipt is silently
    # inherited from the archive.
    repay_window_start = sync_start if sync_mode == "full" else max(month_start, sync_start)

    window_repayments = None
    if debtors_today_fresh and debtors_yesterday_fresh:
        try:
            window_repayments = compute_range_repayments(
                "window",
                repay_window_start,
                today,
                repay_window_start - timedelta(days=1),
                today,
            )
            print(f"  window repayments recomputed from ERP ({repay_window_start}..{today}): {len(window_repayments)} rows")
        except ErpFetchError as e:
            print(f"  window repayments ERP compute failed; falling back to saved snapshot: {e}")
            window_repayments = None
    else:
        print("  window repayments ERP compute skipped; using saved snapshot because debtor balances are fallback")

    if sync_mode == "full" and window_repayments is None:
        raise ErpFetchError(
            "full-history repayment refresh unavailable; refusing to retain stale archived receipts"
        )

    def _repayments_on(rows, day):
        day_s = str(day)
        return [dict(row) for row in rows or [] if str(row.get("date", ""))[:10] == day_s]

    # today
    if window_repayments is not None:
        repayments_today = _repayments_on(window_repayments, today)
    else:
        repayments_today = saved_today
    repayments_today = require_repayments("today", repayments_today)

    # yesterday (inside the window whenever recent_days >= 2)
    if window_repayments is not None and repay_window_start <= yesterday:
        repayments_yesterday = _repayments_on(window_repayments, yesterday)
    else:
        repayments_yesterday = saved_yesterday
        if repayments_yesterday is None:
            try:
                repayments_yesterday = compute_range_repayments(
                    "yesterday",
                    yesterday,
                    yesterday,
                    yesterday - timedelta(days=1),
                    yesterday,
                )
            except ErpFetchError as e:
                print(f"  yesterday repayments ERP compute failed; using saved snapshot if available: {e}")
                repayments_yesterday = saved_yesterday
    repayments_yesterday = require_repayments("yesterday", repayments_yesterday)

    # month-to-date: the freshly recomputed window is authoritative for its days; the older MTD
    # days (month_start .. window_start) come from the saved snapshot when it exists. But the MTD
    # snapshot is keyed by (month_start, today), so on the FIRST run of a new day it hasn't been
    # written yet (saved_mtd is None) — in that case recompute the pre-window days straight from ERP
    # instead of dropping them, else the balance silently loses every repayment before the 7-day
    # window (e.g. 01–06 of the month) until the monthly-nightly full recompute runs.
    if window_repayments is not None:
        if saved_mtd is not None:
            older_saved = [
                dict(row) for row in saved_mtd
                if str(row.get("date", ""))[:10] < str(repay_window_start)
            ]
        elif repay_window_start > month_start:
            older_saved = compute_range_repayments(
                "mtd pre-window",
                month_start,
                repay_window_start - timedelta(days=1),
                month_start - timedelta(days=1),
                repay_window_start - timedelta(days=1),
            )
        else:
            older_saved = []
        repayments_mtd = merge_repayment_rows(older_saved, window_repayments)
    elif saved_mtd is not None:
        repayments_mtd = replace_repayment_day(saved_mtd, today, repayments_today)
    else:
        repayments_mtd = compute_range_repayments(
            "mtd",
            month_start,
            today,
            month_start - timedelta(days=1),
            today,
        )
    # A full-history refresh supplies `window_repayments` from April onward.
    # The MTD control must still expose only this calendar month's rows; using
    # the full window here made the Bank-page guard compare FY repayments to
    # an August-only bank snapshot.
    if sync_mode == "full" and window_repayments is not None:
        repayments_mtd = [
            dict(row) for row in window_repayments
            if str(month_start) <= str(row.get("date", ""))[:10] <= str(today)
        ]
    repayments_mtd = require_repayments("mtd", repayments_mtd)

    repayments_last_month = saved_last_month
    if repayments_last_month is None:
        repayments_last_month = compute_range_repayments(
            "last month",
            last_month_start,
            last_month_end,
            last_month_start - timedelta(days=1),
            last_month_end,
        )
    repayments_last_month = require_repayments("last month", repayments_last_month)
    # Re-derive the sliver between the balance anchor and the last-month window (e.g. 28-30 Jun when
    # the anchor is 28-Jun and last month is July) fresh from ERP, so those post-anchor days carry
    # named/deduped receipts and net exactly like localhost — instead of the stale stripped rows the
    # archive keeps there. Lets the overlay self-compute straight from the anchor (no pin needed).
    anchor_gap_repayments = []
    gap_start = gap_end = None
    if anchor_date is not None and anchor_date < last_month_start and debtors_today_fresh:
        gap_start = anchor_date
        gap_end = last_month_start - timedelta(days=1)
        try:
            anchor_gap_repayments = compute_range_repayments(
                "anchor-gap", gap_start, gap_end, gap_start - timedelta(days=1), gap_end)
            print(f"  anchor-gap repayments recomputed ({gap_start}..{gap_end}): {len(anchor_gap_repayments)} rows")
        except ErpFetchError as e:
            print(f"  anchor-gap repayments skipped: {e}")
            gap_start = gap_end = None
    # Archive receipts cover months before the anchor; drop any date the anchor-gap re-derivation
    # now owns so a stale stripped row can't sit beside its fresh named version.
    archive_repayments = [
        row for row in archive_receipts_to_repayments(archive_rows.get("receipts"))
        if str(row.get("date", ""))[:10] < str(last_month_start)
        and not (gap_start is not None and str(gap_start) <= str(row.get("date", ""))[:10] <= str(gap_end))
    ]
    if sync_mode == "full":
        # The full window is authoritative.  Do not let a cached receipt
        # restore an older amount after Loctell has supplied a corrected one.
        all_repayments = merge_repayment_rows(window_repayments)
    else:
        repayment_map = {}
        for row in archive_repayments + anchor_gap_repayments + repayments_last_month + repayments_mtd:
            key = (
                row.get("date", ""),
                row.get("customer_name", ""),
                row.get("reference", ""),
                round(_num(row.get("payment_received", row.get("amount"))), 2),
            )
            repayment_map[key] = row
        all_repayments = sorted(
            repayment_map.values(),
            key=lambda row: (row.get("date", ""), row.get("customer_name", "")),
            reverse=True,
        )
    if window_repayments is not None:
        assert_fresh_source_rows_preserved(
            "Customer repayments", window_repayments, all_repayments, _repayment_key
        )

    # Credit aging needs the sale history that existed BEFORE this FY as well.
    # FIFO receipts consume those older invoices first; without them, a FY-only
    # source can make known FY receipts appear to overpay every invoice and
    # incorrectly label the entire ERP balance as 15+ days due.  Keep this
    # history internal to aging: visible sales/period reports remain FY/range
    # based exactly as before.
    aging_history_start = CUST_LEDGER_START
    aging_fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    aging_archive_rows = load_archive_window(aging_history_start, today)
    missing_aging_months = []
    _aging_month = aging_history_start.replace(day=1)
    while _aging_month < month_start:
        if not (ARCHIVE_DIR / f"{_aging_month:%Y-%m}.json").exists():
            missing_aging_months.append(f"{_aging_month:%Y-%m}")
        if _aging_month.month == 12:
            _aging_month = _aging_month.replace(year=_aging_month.year + 1, month=1)
        else:
            _aging_month = _aging_month.replace(month=_aging_month.month + 1)
    historical_aging_sales = []
    if missing_aging_months:
        print(
            "  Bootstrapping customer-aging sale history before FY: "
            f"missing archive months {', '.join(missing_aging_months)}"
        )
        historical_aging_sales = fetch_sales(
            _clone_sess(sess), aging_history_start, aging_fy_start - timedelta(days=1)
        )
        if not historical_aging_sales:
            raise RuntimeError(
                "customer-aging history bootstrap returned no pre-FY sales; "
                "refusing to publish potentially false 15+ values"
            )
    aging_sales = merge_rows_by_archive_key(
        aging_archive_rows.get("sales"), historical_aging_sales, "sales"
    )
    aging_sales = merge_rows_by_archive_key(
        aging_sales, fresh_sales, "sales"
    )
    aging_repayments = merge_repayment_rows(
        archive_receipts_to_repayments(aging_archive_rows.get("receipts")),
        all_repayments,
    )
    # `all_sales` already holds the complete archive + freshly fetched source
    # window.  Do not apply the recent-window replacement rule a second time
    # here: doing so removed the freshly fetched 30/31-Jul rows before the
    # monthly archive was written, even though the engine had fetched them.
    # That made completed ranges (which correctly read the archive) lose sales.
    # `all_sales` is already the archive with the freshly fetched sync window
    # replaced.  Historical aging data is supplemental only; merge it first
    # so it can never overwrite a corrected ticket from the authoritative
    # source window during a full rebuild.
    archive_sales_for_write = merge_rows_by_archive_key(
        historical_aging_sales, all_sales, "sales"
    )
    print(
        f"  Customer aging source: history {aging_history_start}..{today}; "
        f"{len(aging_sales)} sales, {len(aging_repayments)} repayments"
    )
    # Match localhost Bank & Cash page: ERP rows plus derived bank/UPI sales,
    # expenses, and customer credit repayments. Bank statement rows are used
    # for balance anchoring only, not for the transaction table.
    bank_rows = dedupe_bank_rows(derive_bank_transactions(all_sales, all_expenses, all_repayments, bank_rows))
    statement_bank_balance = latest_bank_statement_balance(statement_bank_rows, today)
    bank_net = statement_bank_balance if statement_bank_balance is not None else round(
        sum(_num(r.get("credit")) for r in bank_rows) - sum(_num(r.get("debit")) for r in bank_rows),
        2,
    )
    write("erp_ledger.json", {
        "opening":       {"date": str(sync_start), "cash": 0.0, "bank": 0.0},
        "cash":          cash_rows,
        "bank":          bank_rows,
        "cash_balance":  round(cash_balance, 2),
        "bank_net":      bank_net,
    })

    opening = seed_config.get("operating_balance_opening") or {}
    try:
        opening_as_of = datetime.fromisoformat(str(opening.get("as_of"))).date()
    except Exception:
        opening_as_of = today - timedelta(days=1)
    movement_start = opening_as_of + timedelta(days=1)
    today_seed_summary = (seed_controls.get(f"{today}|{today}") or {}).get("summary") or {}
    if "bank_balance" in today_seed_summary and "cash_balance_office" in today_seed_summary:
        operating_bank_balance = _num(today_seed_summary.get("bank_balance"))
        operating_cash_balance = _num(today_seed_summary.get("cash_balance_office"))
    else:
        repayments_movement = seed_for(all_repayments, movement_start, today)
        operating_bank_balance = _num(opening.get("bank_balance"))
        operating_cash_balance = _num(opening.get("cash_balance_office"))
        for sale in sales_for(movement_start, today):
            # cash portion -> Cash-in-office tile, UPI/bank portion -> Bank tile (SPLIT-aware)
            s_cash, _s_credit, s_upi = _sale_channels(sale)
            operating_cash_balance += s_cash
            operating_bank_balance += s_upi
        for receipt in repayments_movement:
            operating_cash_balance += _num(receipt.get("cash_received"))
            operating_bank_balance += _num(receipt.get("bank_received"))
        for expense in exp_for(movement_start, today):
            if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                operating_cash_balance -= _num(expense.get("amount"))
            else:
                operating_bank_balance -= _num(expense.get("amount"))
    operating_bank_balance = round(operating_bank_balance, 2)
    operating_cash_balance = round(operating_cash_balance, 2)

    # ── creditors (already fetched in parallel above) ─────────────────────────
    print(f"  {len(creditors)} vendors")
    vendor_payments, vendor_payments_fresh = fetch_vendor_payments_or_saved()
    # Recent syncs fetch only a short delta, whereas an MTD snapshot needs all
    # of the month's payments.  Merge the fresh window into the existing
    # archive before every downstream consumer (vendor page, control snapshots,
    # ledger, compliance) reads it.  Without this, the exact MTD payment
    # snapshot silently omitted valid early-month payments.
    vendor_payments = merge_rows_by_archive_key(
        archive_rows.get("vendor_payments"), vendor_payments, "vendor_payments"
    )
    print(f"  {len(vendor_payments)} vendor payments (merged archive + fresh window)")
    try:
        # Fetch the complete checked-in supplier master, not only today's
        # payable suppliers.  A settled supplier can still have a bill/payment
        # history that must remain visible in its Tally-style ledger.
        vendor_ledger_sources = canonical_vendor_master([], creditors, source_master=creditors)
        vendor_ledger_sources = vendor_rows_as_of(vendor_ledger_sources, creditors, {}, today)
        vendor_ledgers_full = fetch_supplier_ledgers_full(sess, vendor_ledger_sources, VENDOR_LEDGER_START, today)
        print(f"  {len(vendor_ledgers_full)} full vendor ledgers")
    except Exception as e:
        print(f"  full vendor ledgers unavailable; using lightweight fallback: {e}")
        vendor_ledgers_full = {}
    # Heavy: ~99 customer ledger fetches (~9 min). Auto-refresh at most once per day (after 05:30 IST,
    # tracked by a marker snapshot); other runs reuse the previous reconciling snapshots from R2.
    if _should_fetch_cust_ledgers(today):
        try:
            customer_ledgers_full = fetch_customer_ledgers_full(sess, debtors_today, CUST_LEDGER_START, today)
            print(f"  {len(customer_ledgers_full)} full customer ledgers")
        except Exception as e:
            print(f"  full customer ledgers unavailable; using reuse/archive fallback: {e}")
            customer_ledgers_full = {}
        # Only mark the daily rebuild as done if it actually returned ledgers — so a loctell timeout
        # inside the window lets the next sync retry instead of skipping the rebuild for the whole day.
        if customer_ledgers_full:
            write_snapshot(CUST_LEDGER_MARKER, {"date": str(today), "slot": "night", "count": len(customer_ledgers_full)})
    else:
        customer_ledgers_full = {}
        print("  customer ledger full-fetch skipped (reusing previous snapshots)")
    # Incremental refresh EVERY sync: re-fetch the full ledger for any outstanding customer whose
    # balance changed vs its last snapshot (a fresh sale/repayment, e.g. AYAM paying today), so otomy
    # reflects it within a sync cycle instead of waiting for the nightly rebuild. Bounded per sync so
    # it stays light; the nightly full-fetch still catches everything else. This is the permanent fix
    # for "localhost updated but otomy didn't" — localhost fetches live, this keeps otomy nearly live.
    try:
        _prev_led = _prev_customer_ledgers_by_name()
        def _prev_close(nm):
            p = _prev_led.get(_norm_name(nm))
            return _num(p.get("closing_balance")) if isinstance(p, dict) else None
        changed = [d for d in debtors_today
                   if d.get("erp_customer_id") and _num(d.get("outstanding")) > 0
                   and _norm_name(d.get("name")) not in customer_ledgers_full
                   and (_prev_close(d.get("name")) is None
                        or abs(_num(d.get("outstanding")) - _prev_close(d.get("name"))) > 0.5)]
        changed = changed[:30]  # cap per sync; nightly full-fetch covers any overflow
        if changed:
            inc = fetch_customer_ledgers_full(sess, changed, CUST_LEDGER_START, today, only_outstanding=False)
            customer_ledgers_full = {**customer_ledgers_full, **inc}
            print(f"  {len(inc)} changed-customer ledgers refreshed incrementally")
    except Exception as e:
        print(f"  incremental customer-ledger refresh skipped: {e}")
    bank_rows = dedupe_bank_rows(bank_rows)
    bank_net = round(
        sum(_num(r.get("credit")) for r in bank_rows) - sum(_num(r.get("debit")) for r in bank_rows),
        2,
    )
    write("erp_ledger.json", {
        "opening":       {"date": str(sync_start), "cash": 0.0, "bank": 0.0},
        "cash":          cash_rows,
        "bank":          bank_rows,
        "cash_balance":  round(cash_balance, 2),
        "bank_net":      bank_net,
    })
    # Vendor payments are already booked as expenses; never subtract the vendor stream
    # again here (that double-counts a vendor who is also an expense, e.g. ASHWATH SOLING).
    operating_bank_balance = round(operating_bank_balance, 2)
    operating_cash_balance = round(operating_cash_balance, 2)

    def operating_balance_for(as_of):
        overlay = _overlay_balance(str(as_of), all_sales, all_expenses, all_repayments, all_internal_transfers)
        if overlay:
            return overlay
        return operating_bank_balance, operating_cash_balance

    seed_debtors = [
        {"name": row.get("name"), "outstanding": row.get("balance", row.get("outstanding", 0.0))}
        for row in seed_endpoints.get("customers_outstanding", [])
    ]
    seed_creditors = [
        {"name": row.get("name"), "payable": row.get("payable", row.get("balance", 0.0))}
        for row in seed_endpoints.get("vendors_payables", [])
    ]
    debtor_cache = {today: debtors_today}
    if debtors_yesterday and debtors_yesterday is not debtors_today:
        debtor_cache[yesterday] = debtors_yesterday
    if "debtors_last_month_end" in locals():
        debtor_cache[last_month_end] = debtors_last_month_end
    creditor_cache = {today: creditors}

    def debtors_for(as_of):
        if as_of not in debtor_cache:
            try:
                debtor_cache[as_of] = fetch_debtors(sess, as_of)
            except ErpFetchError as e:
                if as_of == today:
                    raise
                print(f"  optional debtor balance lookup skipped ({as_of}): {e}")
                return seed_debtors or debtors_today
        return debtor_cache.get(as_of) or seed_debtors or debtors_today

    def creditors_for(as_of):
        if as_of not in creditor_cache:
            try:
                creditor_cache[as_of] = fetch_creditors(sess, as_of)
            except ErpFetchError as e:
                if as_of == today:
                    raise
                print(f"  optional creditor balance lookup skipped ({as_of}): {e}")
                return seed_creditors or creditors
        return creditor_cache.get(as_of) or seed_creditors or creditors

    # ── control room JSON ─────────────────────────────────────────────────────
    today_bank_balance, today_cash_balance = operating_balance_for(today)
    yesterday_bank_balance, yesterday_cash_balance = operating_balance_for(yesterday)
    # Publish the daily balance chain explicitly. This is the authoritative hand-off between
    # yesterday's closing book and today's opening book; the frontend uses it for the two live
    # dates while historical ranges continue to derive from the anchor + movement engine.
    write("balance_daily.json", {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": "github-actions / loctell.com ERP balance overlay",
        "as_of": str(today),
        "previous_close": {
            "as_of": str(yesterday),
            "bank_balance": round(yesterday_bank_balance, 2),
            "cash_balance_office": round(yesterday_cash_balance, 2),
        },
        "today_opening": {
            "as_of": str(yesterday),
            "bank_balance": round(yesterday_bank_balance, 2),
            "cash_balance_office": round(yesterday_cash_balance, 2),
        },
        "today_close": {
            "as_of": str(today),
            "bank_balance": round(today_bank_balance, 2),
            "cash_balance_office": round(today_cash_balance, 2),
        },
    })
    print(
        f"  Verified opening ({yesterday} close): bank ₹{yesterday_bank_balance:,.2f} "
        f"| cash ₹{yesterday_cash_balance:,.2f}"
    )
    print(
        f"  Verified closing ({today}): bank ₹{today_bank_balance:,.2f} "
        f"| cash ₹{today_cash_balance:,.2f}"
    )
    ctrl_today = build_control(
        sales_for(today, today), exp_for(today, today), today, today,
        boulders=boulders_today, debtors=debtors_for(today), creditors=creditors_for(today),
        cash_balance=today_cash_balance, bank_net=today_bank_balance,
        labour=seed_for(labour_rows, today, today),
        parts=seed_for(parts_rows, today, today),
        machines=seed_for(machines_rows, today, today),
        vendor_payments=seed_for(vendor_payments, today, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_today,
    )
    ctrl_yesterday = build_control(
        sales_for(yesterday, yesterday), exp_for(yesterday, yesterday), yesterday, yesterday,
        boulders=boulders_yesterday, debtors=debtors_for(yesterday), creditors=creditors_for(yesterday),
        cash_balance=yesterday_cash_balance, bank_net=yesterday_bank_balance,
        labour=seed_for(labour_rows, yesterday, yesterday),
        parts=seed_for(parts_rows, yesterday, yesterday),
        machines=seed_for(machines_rows, yesterday, yesterday),
        vendor_payments=seed_for(vendor_payments, yesterday, yesterday),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_yesterday,
    )
    ctrl_week = build_control(
        sales_for(week_start, today), exp_for(week_start, today), week_start, today,
        boulders=boulders_week, debtors=debtors_for(today), creditors=creditors_for(today),
        cash_balance=today_cash_balance, bank_net=today_bank_balance,
        labour=seed_for(labour_rows, week_start, today),
        parts=seed_for(parts_rows, week_start, today),
        machines=seed_for(machines_rows, week_start, today),
        vendor_payments=seed_for(vendor_payments, week_start, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=seed_for(all_repayments, week_start, today),
    )
    ctrl_mtd = build_control(
        sales_for(month_start, today), exp_for(month_start, today), month_start, today,
        boulders=boulders_mtd, debtors=debtors_for(today), creditors=creditors_for(today),
        cash_balance=today_cash_balance, bank_net=today_bank_balance,
        labour=seed_for(labour_rows, month_start, today),
        parts=seed_for(parts_rows, month_start, today),
        machines=seed_for(machines_rows, month_start, today),
        vendor_payments=seed_for(vendor_payments, month_start, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_mtd,
    )
    ctrl_today = apply_seed_control_overrides(ctrl_today, local_seed, today, today)
    ctrl_yesterday = apply_seed_control_overrides(ctrl_yesterday, local_seed, yesterday, yesterday)
    ctrl_week = apply_seed_control_overrides(ctrl_week, local_seed, week_start, today)
    ctrl_mtd = apply_seed_control_overrides(ctrl_mtd, local_seed, month_start, today)
    # Keep historical balance archive updates best-effort. The 5-minute cloud
    # sync should not freeze just because one older Loctell balance date times out.
    all_snap_dates = sorted({
        sync_start + timedelta(days=i)
        for i in range((today - sync_start).days + 1)
    } | {today, yesterday})
    needed_d = [d for d in all_snap_dates if d not in debtor_cache]
    needed_c = [d for d in all_snap_dates if d not in creditor_cache]
    if needed_d or needed_c:
        with ThreadPoolExecutor(max_workers=ERP_BALANCE_WORKERS) as pool:
            d_futures = {d: pool.submit(fetch_debtors,   _clone_sess(sess), d) for d in needed_d}
            c_futures = {d: pool.submit(fetch_creditors,  _clone_sess(sess), d) for d in needed_c}
            for d, f in d_futures.items():
                try:
                    rows = f.result()
                except ErpFetchError as e:
                    print(f"  optional debtor balance snapshot skipped ({d}): {e}")
                    continue
                if rows:
                    debtor_cache[d] = rows
            for d, f in c_futures.items():
                try:
                    rows = f.result()
                except ErpFetchError as e:
                    print(f"  optional creditor balance snapshot skipped ({d}): {e}")
                    continue
                if rows:
                    creditor_cache[d] = rows
    balance_snapshots = {
        str(as_of): {
            "debtors": debtor_cache.get(as_of) or [],
            "creditors": creditor_cache.get(as_of) or [],
        }
        for as_of in sorted(set(debtor_cache.keys()) | set(creditor_cache.keys()))
    }
    write("ctrl_today.json", ctrl_today)
    write("ctrl_yesterday.json", ctrl_yesterday)
    write("ctrl_week.json", ctrl_week)
    write("ctrl_mtd.json", ctrl_mtd)

    # ── sales & expenses lists ─────────────────────────────────────────────────
    write("sales_all.json",    sorted(all_sales,    key=lambda r: r["date"], reverse=True))
    write("expenses_all.json", sorted(all_expenses, key=lambda r: r["date"], reverse=True))

    # ── customers ─────────────────────────────────────────────────────────────
    # Preserve all display-master rows while consuming each normalized Loctell
    # debtor balance once.  This retains spacing-sensitive customer names
    # without duplicating the ERP receivable.
    seed_customers = canonical_customer_master_rows(seed_endpoints.get("customers_all", []))
    debtors_by_name = canonical_debtors_by_name(debtors_today)
    customers_by_name = {}
    max_customer_id = 0
    for customer_key, seed_row in seed_customers.items():
        row = dict(seed_row)
        max_customer_id = max(max_customer_id, int(row.get("id") or 0))
        d = debtors_by_name.pop(_norm_name(row.get("name")), None)
        if d:
            row.update({
                "balance": d["outstanding"],
                # These columns are lifetime ERP debtor balances, not the
                # selected FY window.  Range sales stay in the dedicated
                # range_* fields returned by build_customer_view.
                "total_sales": round(d["billed"], 2),
                "total_receipts": round(d["received"], 2),
                "manual_receipts": row.get("manual_receipts", 0.0),
                "erp_received": round(d["received"], 2),
                "received": round(d["received"], 2),
                "erp_debit_balance": round(d["billed"], 2),
                "erp_credit_balance": round(d["received"], 2),
                "erp_balance_as_of": str(today),
                "outstanding": d["outstanding"],
                "age_45_plus": round(max(d["outstanding"], 0.0), 2),
            })
        customers_by_name[customer_key] = row

    for _debtor_key, d in debtors_by_name.items():
        max_customer_id += 1
        customer_key = _customer_master_key(d["name"])
        customers_by_name[customer_key] = {
            "id": max_customer_id, "name": d["name"], "gstin": "", "phone": "", "address": "",
            "opening_balance": 0.0, "active": True,
            "balance":           d["outstanding"],
            "total_sales":       round(d["billed"],   2),
            "total_receipts":    round(d["received"], 2),
            "manual_receipts":   0.0,
            "erp_received":      round(d["received"], 2),
            "received":          round(d["received"], 2),
            "erp_debit_balance": round(d["billed"],   2),
            "erp_credit_balance":round(d["received"], 2),
            "erp_balance_as_of": str(today),
            "outstanding":       d["outstanding"],
            "age_0_15": 0.0, "age_16_30": 0.0, "age_31_45": 0.0,
            "age_45_plus": round(max(d["outstanding"], 0.0), 2),
        }

    for override in load_customer_master_overrides():
        name = str(override.get("name") or "").strip()
        customer_key = _customer_master_key(name)
        if not customer_key or customer_key in customers_by_name:
            continue
        max_customer_id += 1
        customers_by_name[customer_key] = {
            "id": max_customer_id,
            "name": name,
            "gstin": override.get("gstin") or "",
            "phone": override.get("phone") or "",
            "address": override.get("address") or "",
            "opening_balance": round(_num(override.get("opening_balance")), 2),
            "active": bool(override.get("active", True)),
            "balance": round(_num(override.get("balance")), 2),
            "total_sales": round(_num(override.get("total_sales")), 2),
            "total_receipts": round(_num(override.get("total_receipts")), 2),
            "manual_receipts": round(_num(override.get("manual_receipts")), 2),
            "erp_received": round(_num(override.get("erp_received")), 2),
            "received": round(_num(override.get("received")), 2),
            "erp_debit_balance": round(_num(override.get("erp_debit_balance")), 2),
            "erp_credit_balance": round(_num(override.get("erp_credit_balance")), 2),
            "erp_balance_as_of": str(today),
            "outstanding": round(_num(override.get("outstanding")), 2),
            "age_0_15": round(_num(override.get("age_0_15")), 2),
            "age_16_30": round(_num(override.get("age_16_30")), 2),
            "age_31_45": round(_num(override.get("age_31_45")), 2),
            "age_45_plus": round(_num(override.get("age_45_plus")), 2),
        }

    customers_full = sorted(customers_by_name.values(), key=lambda row: row.get("name", ""))
    due_15_plus = _credit_due_15_plus_by_name(
        customers_full, aging_sales, aging_repayments, today, days=16
    )
    due_30_plus = _credit_due_15_plus_by_name(
        customers_full, aging_sales, aging_repayments, today, days=31
    )
    due_45_plus = _credit_due_15_plus_by_name(
        customers_full, aging_sales, aging_repayments, today, days=45
    )
    for row in customers_full:
        row["credit_due_15_plus"] = due_15_plus.get(row.get("name", ""), 0.0)
        row["credit_due_30_plus"] = due_30_plus.get(row.get("name", ""), 0.0)
        row["credit_due_45_plus"] = due_45_plus.get(row.get("name", ""), 0.0)
    customers_outstanding = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "gstin": row.get("gstin"),
            "phone": row.get("phone"),
            "balance": row.get("outstanding", row.get("balance", 0.0)),
            "outstanding": row.get("outstanding", row.get("balance", 0.0)),
            "total_sales": row.get("total_sales", 0.0),
            "total_receipts": row.get("total_receipts", row.get("received", 0.0)),
        }
        for row in customers_full
        if row.get("active", True) and _num(row.get("outstanding", row.get("balance", 0.0))) > 0
    ]
    customers_outstanding.sort(key=lambda row: row.get("balance", 0.0), reverse=True)
    if not customers_outstanding and seed_endpoints.get("customers_outstanding"):
        customers_outstanding = seed_endpoints["customers_outstanding"]

    write("customers_outstanding.json", customers_outstanding)
    write("customers.json",             customers_full)

    # ── vendors ───────────────────────────────────────────────────────────────
    # The balance report is the sole live Vendor-page source.  Do not retain
    # stale seed masters after Loctell removes or renames a supplier.
    vendors_full = canonical_vendor_master([], creditors, source_master=creditors)
    # Set the current Loctell payable before calculating ERP-ledger openings.
    # The master is a name/ID list, not itself a balance snapshot.
    vendors_full = vendor_rows_as_of(vendors_full, creditors, {}, today)
    vendor_ledgers = build_vendor_ledgers(vendors_full, vendor_payments, vendor_ledgers_full)
    vendors_full = vendor_rows_as_of(vendors_full, creditors, vendor_ledgers, today)
    vendors_payables = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "gstin": row.get("gstin"),
            "phone": row.get("phone"),
            "payable": row.get("payable", 0.0),
            "total_purchases": row.get("total_purchases", 0.0),
            "total_payments": row.get("total_payments", 0.0),
            "payable_due_15_plus": row.get("payable_due_15_plus", 0.0),
            "payable_due_30_plus": row.get("payable_due_30_plus", 0.0),
            "payable_due_45_plus": row.get("payable_due_45_plus", 0.0),
            "payable_due_60_plus": row.get("payable_due_60_plus", 0.0),
            "payable_prior_ledger": row.get("payable_prior_ledger", 0.0),
            "age_0_15": row.get("age_0_15", 0.0),
            "age_16_30": row.get("age_16_30", 0.0),
            "age_31_45": row.get("age_31_45", 0.0),
            "age_45_plus": row.get("age_45_plus", 0.0),
        }
        for row in vendors_full
        if row.get("active", True) and _num(row.get("payable")) > 0
    ]
    vendors_payables.sort(key=lambda row: row.get("payable", 0.0), reverse=True)
    write("vendors_payables.json", vendors_payables)
    write("vendors.json",          vendors_full)

    # ── meta ──────────────────────────────────────────────────────────────────
    write("meta.json", {
        "company":     "Crusher & Quarry Operations",
        "last_sync":   datetime.now(IST).isoformat(timespec="seconds"),
        "source":      "github-actions / loctell.com ERP",
        "version":     "3.0",
        "cash_balance": round(cash_balance, 2),
        "bank_net":     bank_net,
    })
    write("common_engine.json", {
        "name": COMMON_ENGINE_NAME,
        "version": COMMON_ENGINE_VERSION,
        "source": "Loctell ERP",
        "sync_mode": sync_mode,
        "from": str(sync_start),
        "to": str(today),
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "status": "calculated",
    })

    print("  Building canonical daily ledger archive...")
    ledger_by_month = {}
    # Preserve closed-month ledger/cashbook parity during normal 7-day syncs.
    # A full rebuild explicitly regenerates every month from April onward.
    ledger_month = _ledger_archive_start(sync_mode, sync_start, month_start)
    while ledger_month <= today:
        ledger_payload = build_ledger_view(
            all_sales,
            all_expenses,
            vendor_payments,
            boulder_rows,
            all_repayments,
            ledger_month.year,
            ledger_month.month,
            opening.get("bank_balance", 0.0),
            opening.get("cash_balance_office", 0.0),
            movement_start,
            today,
            overlay_repayments=all_repayments,
            internal_transfers=all_internal_transfers,
        )
        ledger_by_month[ledger_month.strftime("%Y-%m")] = ledger_payload
        if ledger_month.month == 12:
            ledger_month = ledger_month.replace(year=ledger_month.year + 1, month=1)
        else:
            ledger_month = ledger_month.replace(month=ledger_month.month + 1)

    print("  Updating monthly archive files...")
    write_archive_updates(
        today,
        archive_sales_for_write,
        all_expenses,
        all_internal_transfers,
        cash_rows,
        bank_rows,
        boulder_rows,
        all_repayments,
        vendor_payments,
        local_seed,
        balance_snapshots,
        ledger_by_month,
    )
    archive_rows = load_archive_window(archive_start, today)
    archive_balances = {
        str(row.get("date", ""))[:10]: row
        for row in archive_rows.get("balances", [])
        if row.get("date")
    }

    # Compliance is always FY-to-date, even when the ERP refresh itself is a
    # recent-window run.  Re-read the merged archive after it has been updated so
    # April 1 through today is present in the canonical GST/AUDIT dataset.
    compliance_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    compliance_rows = load_archive_window(compliance_start, today)
    if expenses_fresh:
        assert_fresh_source_rows_preserved(
            "Archived expenses", fresh_expenses, compliance_rows.get("expenses"), _expense_content_key
        )
    if window_repayments is not None:
        archived_repayments = archive_receipts_to_repayments(compliance_rows.get("receipts"))
        assert_fresh_source_rows_preserved(
            "Archived customer repayments", window_repayments, archived_repayments, _repayment_key
        )
    compliance_config = dict((local_seed.get("endpoints") or {}).get("exports_config") or {})
    compliance_dataset = build_compliance_dataset(
        compliance_rows.get("sales", []),
        compliance_rows.get("expenses", []),
        compliance_rows.get("receipts", []),
        customers_full,
        vendors_full,
        compliance_rows.get("vendor_payments", []),
        compliance_config,
        compliance_start,
        today,
    )
    write_compliance_snapshots(compliance_dataset, compliance_start, today)
    print(
        "  Compliance FY snapshot: "
        f"{len(compliance_dataset['sales'])} sales, "
        f"{len(compliance_dataset['expenses'])} expenses, "
        f"{len(compliance_dataset['receipts'])} receipts, "
        f"{len(compliance_dataset['vendor_payments'])} vendor payments"
    )

    print("  Writing static API snapshot files...")
    assert_fytd_source_coverage(financial_year_start, today, all_sales, all_expenses)
    write_snapshot_bundle(
        today,
        yesterday,
        month_start,
        financial_year_start,
        all_sales,
        all_expenses,
        all_internal_transfers,
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
        operating_cash_balance,
        operating_bank_balance,
        bank_balance_book,
        cash_balance_office_book,
        customers_full,
        customers_outstanding,
        vendors_full,
        vendors_payables,
        vendor_ledgers,
        vendor_payments,
        all_repayments,
        local_seed,
        {"today": ctrl_today, "yesterday": ctrl_yesterday, "week": ctrl_week, "mtd": ctrl_mtd},
        balance_snapshots,
        archive_balances,
        customer_ledgers_full,
        sync_start if sync_mode == "full" else None,
        aging_sales,
        aging_repayments,
    )
    for month, ledger_payload in ledger_by_month.items():
        year, month_number = (int(part) for part in month.split("-"))
        write_snapshot(
            f"/api/dashboard/ledger-view?year={year}&month={month_number}",
            ledger_payload,
        )

    # These are the ranges exposed by the Cash/Bank page presets. Publish each one from the
    # canonical builder so the browser never falls back to its independent balance calculator.
    cashbook_ranges = [
        (today, today),
        (yesterday, yesterday),
        (week_start, today),
        (last_week_start, last_week_end),
        (month_start, today),
        (last_month_start, last_month_end),
        # Keep the FYTD Cash Book current on the normal seven-day sync.  The
        # rows come from the merged archive, so this is a cheap re-derivation
        # after the recent Loctell delta rather than a second historical fetch.
        (financial_year_start, today),
    ]
    # Cash/Bank books deliberately use canonical server-built snapshots rather
    # than browser arithmetic.  Keep their rolling presets in lockstep with
    # the dashboard/page range list above, otherwise these buttons work on
    # every page except Cash & Bank.
    def cashbook_calendar_month_range_start(months: int) -> date:
        month_index = today.year * 12 + (today.month - 1) - (months - 1)
        target_year, target_month_index = divmod(month_index, 12)
        target_month = target_month_index + 1
        return date(target_year, target_month, 1)

    rolling_cashbook_ranges = [
        (cashbook_calendar_month_range_start(2), today),
        (cashbook_calendar_month_range_start(3), today),
        *((today - timedelta(days=days - 1), today) for days in (7, 15, 30, 45, 60, 90)),
    ]
    for rolling_range in rolling_cashbook_ranges:
        if rolling_range not in cashbook_ranges:
            cashbook_ranges.append(rolling_range)

    # Retain only the current moving rolling-book snapshot keys.  Cashbook
    # objects are otherwise protected from generic pruning because their
    # balances must never fall back to client-side calculation.  This small
    # dedicated index safely removes yesterday's moving presets while keeping
    # all established canonical and historical book snapshots intact.
    rolling_cashbook_index = DATA_DIR / "control" / "rolling_cashbook_snapshot_keys.json"
    try:
        previous_rolling_files = set(json.loads(rolling_cashbook_index.read_text()).get("files", []))
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        previous_rolling_files = set()
    current_rolling_files = {
        f"{snapshot_key(f'/api/sync/erp/cashbook?from_date={book_from}&to_date={book_to}')}.json"
        for book_from, book_to in rolling_cashbook_ranges
    }
    for filename in previous_rolling_files - current_rolling_files:
        path = SNAPSHOT_API_DIR / str(filename)
        if path.name == filename and path.suffix == ".json":
            path.unlink(missing_ok=True)
    rolling_cashbook_index.parent.mkdir(parents=True, exist_ok=True)
    rolling_cashbook_index.write_text(
        json.dumps({"files": sorted(current_rolling_files)}, separators=(",", ":")),
        encoding="utf-8",
    )
    if sync_mode == "full":
        historical_end = min(yesterday, last_month_end)
        cashbook_ranges.extend([
            (sync_start, historical_end),
            (sync_start, yesterday),
            (sync_start, today),
        ])
        # The browser also requests a completed-month range from the Cash/Bank pages.
        # Publish those ranges from the same canonical builder so a month view cannot
        # fall back to a separate client calculation either.
        month_cursor = sync_start.replace(day=1)
        while month_cursor <= historical_end:
            next_month = (month_cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_from = max(sync_start, month_cursor)
            month_to = min(next_month - timedelta(days=1), historical_end)
            if month_from <= month_to:
                cashbook_ranges.append((month_from, month_to))
            month_cursor = next_month
        # Historical dashboard ranges can ask for one exact day (for example 31-Jul).
        # Publish the same canonical cashbook object for every day in the completed window so
        # the browser never falls back to a second balance calculation for a historical day.
        cashbook_ranges.extend(
            (sync_start + timedelta(days=offset), sync_start + timedelta(days=offset))
            for offset in range((historical_end - sync_start).days + 1)
        )
    for book_from, book_to in cashbook_ranges:
        if book_to < book_from:
            continue
        write_snapshot(
            f"/api/sync/erp/cashbook?from_date={book_from}&to_date={book_to}",
            build_cashbook_view(
                book_from,
                book_to,
                all_sales,
                all_expenses,
                all_repayments,
                opening,
                all_internal_transfers,
            ),
        )
    pruned_count, pruned_bytes = prune_obsolete_derived_range_snapshots()
    if pruned_count:
        print(
            "  R2 snapshot retention: removed "
            f"{pruned_count} obsolete derived range files "
            f"({pruned_bytes / (1024 * 1024):.1f} MiB); canonical Cash/Bank books retained"
        )
    cleanup_excluded_customer_receipt_artifacts()
    cleanup_residual_balance_artifacts()

    today_sales = sales_for(today, today)
    print(f"  Done. Today: ₹{sum(_sale_total(s) for s in today_sales):,.0f} "
          f"| {len(today_sales)} tickets | Cash: ₹{operating_cash_balance:,.0f}")

if __name__ == "__main__":
    main()
