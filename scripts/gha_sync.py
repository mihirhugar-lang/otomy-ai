#!/usr/bin/env python3
"""
GitHub Actions sync script — fetches live data from loctell.com ERP
and generates JSON files for otomy.ai. Runs on GitHub servers.
No Mac or local database required.
"""
import base64, json, re, html as htmllib, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import requests
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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("COMMON_ENGINE_DATA_DIR", ROOT / "data"))
SNAPSHOT_API_DIR = DATA_DIR / "snapshot" / "api"
ARCHIVE_DIR = DATA_DIR / "archive"
LOCAL_SEED_PATH = DATA_DIR / "local_seed.json"
CUSTOMER_MASTER_OVERRIDES_PATH = DATA_DIR / "customer_master_overrides.json"
VENDOR_MASTER_PATH = Path(__file__).resolve().parent.parent / "seed" / "vendor_master.json"
BOOK_BALANCE_ACCOUNTS_PATH = ROOT / "seed" / "book_balance_accounts.json"
BANK_STATEMENT_PATH = DATA_DIR / "bank_statement_icici_2026-04-01_2026-06-28.json"
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
        return round(cash, 2), round(credit, 2), round(upi, 2)
    total = _sale_total(s)
    mode = (s.get("payment_mode") or "Credit")
    if mode.lower() == "credit":
        return 0.0, total, 0.0
    if "CASH" in mode.upper():
        return total, 0.0, 0.0
    return 0.0, 0.0, total


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
    difference = round(gross - cash - credit - upi, 2)
    if abs(difference) < 0.005:
        return 0.0, 0.0
    if cash > 0 and upi <= 0:
        return difference, 0.0
    if upi > 0 and cash <= 0:
        return 0.0, difference
    return 0.0, 0.0


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

def load_archive_window(from_d, to_d):
    out = {
        "sales": [],
        "expenses": [],
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

def merge_rows_by_archive_key(archive_rows, fresh_rows, section, *, drop_current_window=True):
    return _merge_archive_rows(
        archive_rows or [], fresh_rows or [], section,
        drop_current_window=drop_current_window,
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


def assert_fytd_source_coverage(fy_start, as_of, sales, expenses):
    """Fail closed rather than publish an anchor-only FYTD snapshot.

    A recent sync fetches a small Loctell delta, but its FYTD snapshots are
    served directly by the UI. Closing-balance parity alone cannot detect a
    truncated source because a later balance anchor can make it tie.
    """
    fy_start = fy_start if isinstance(fy_start, date) else date.fromisoformat(str(fy_start)[:10])
    as_of = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
    expected_months = set()
    cursor = fy_start.replace(day=1)
    while cursor <= as_of:
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
    print(f"  FYTD source coverage: {', '.join(sorted(expected_months))}")

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
    cred = base64.b64encode(f"{ERP_ORG};{ERP_USER}:{ERP_PASS}".encode()).decode()
    last_error = None
    # A Loctell login has two network requests.  Retrying only later data
    # fetches is ineffective when its authentication endpoint is temporarily
    # slow, and a new session avoids carrying a half-created login state.
    for attempt in range(1, ERP_FETCH_RETRIES + 1):
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0"})
        try:
            response = sess.get(
                f"{ERP_BASE}/restserver/rest/users/login?web=true",
                headers={"Authorization": f"Basic {cred}", "content-type": "application/json"},
                timeout=25, verify=True,
            )
            response.raise_for_status()
            response = sess.post(
                f"{ERP_BASE}/home/MainLogin",
                data={"loginUsername": ERP_USER, "loginPassword": ERP_PASS,
                      "loginOrgName": ERP_ORG, "pType": "attendance"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=25, verify=True,
            )
            response.raise_for_status()
            return sess
        except Exception as e:
            last_error = e
            if attempt < ERP_FETCH_RETRIES:
                print(f"  Loctell login retry {attempt}/{ERP_FETCH_RETRIES} after {type(e).__name__}: {e}")
                time.sleep(ERP_RETRY_DELAY_SECONDS * attempt)
    raise ErpFetchError(f"Loctell login failed after {ERP_FETCH_RETRIES} attempt(s): {last_error}") from last_error

def _clone_sess(sess):
    """Return a new session with the same cookies — safe to use in a thread."""
    s = requests.Session()
    s.headers.update(dict(sess.headers))
    for cookie in sess.cookies:
        s.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    return s

# ─── fetchers ────────────────────────────────────────────────────────────────

def _fetch_sales_window(sess, from_d, to_d):
    """Fetch one bounded CustomerWiseReport window from Loctell."""
    tickets = []
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    try:
        raw = _request_text_with_retry(
            sess,
            f"{ERP_BASE}/crusher/ListCustomerWiseReport"
            f"?start={fs}&end={ts}&customerId=-1&type=3",
            timeout=60,
            label=f"sales {fs} to {ts}",
        )
        try:    cw_html = htmllib.unescape(json.loads(raw))
        except: cw_html = raw
        for block in cw_html.split("Party Name :"):
            block = block.strip()
            if not block: continue
            party = re.sub(r"<[^>]+>.*", "", block, flags=re.DOTALL).strip().split("\n")[0].strip()[:200]
            for tr in _TR.finditer(block):
                cols = [_clean(c) for c in _TD.findall(tr.group(1))]
                if len(cols) < 10: continue
                if not re.match(r"\d{2}-\d{2}-\d{4}", cols[2]): continue
                if not re.match(r"\d+:\d+\s*[AP]M", cols[3]):   continue
                if cols[9].upper().strip() not in _PAY:           continue
                qty = _num(cols[7])
                if qty == 0: continue
                material_amount = _num(cols[8])
                # Gross Sales must follow Loctell's Gross Total column. Net
                # Amount is a separate round-off field and may be below
                # Material Amount; clamping it caused ticket-level drift.
                gross_total = _num(cols[10] if len(cols) > 10 else (cols[13] if len(cols) > 13 else cols[8]))
                transport_charge = round(gross_total - material_amount, 2)
                dd, mm, yyyy = cols[2].split("-")
                payment_mode = _norm_pay(cols[9])
                cash_amount, credit_amount, upi_amount = _channels_for_payment_mode(
                    gross_total, payment_mode
                )
                tickets.append({
                    "id": 0, "date": str(date(int(yyyy), int(mm), int(dd))),
                    "sale_time": cols[3].strip(),
                    "customer_name": party, "ticket_no": cols[1].strip(),
                    "vehicle_no": cols[4].strip(),
                    "material": _norm_material(cols[5]),
                    "rate_per_mt": _num(cols[6]),
                    "qty_mt": qty, "mdp_ton": 0.0,  # real MDP Ton is applied from ListSale splits; never default to qty
                    "amount": material_amount,
                    "transport_charge": transport_charge,
                    "payment_mode": payment_mode,
                    "cash_amount": cash_amount,
                    "credit_amount": credit_amount,
                    "upi_amount": upi_amount,
                    "hsn_code": "2517", "gst_rate": 5.0, "notes": "", "erp_synced": True,
                })
    except Exception as e:
        print(f"  sales fetch error: {e}")
        raise ErpFetchError(f"sales fetch failed; skipped Otomy write: {e}") from e
    return tickets


def _sales_fetch_windows(from_d, to_d, days=1):
    """Yield daily windows so Loctell cannot truncate a FY sales report."""
    cursor = from_d
    while cursor <= to_d:
        end = min(cursor + timedelta(days=days - 1), to_d)
        yield cursor, end
        cursor = end + timedelta(days=1)


def _ledger_archive_start(sync_mode, sync_start, month_start):
    """Choose which monthly ledger archives a run is allowed to regenerate.

    A recent sync refreshes source rows but must not recalculate a closed month
    against a shorter operational window.  Historical month ledgers and their
    canonical cashbooks are rebuilt together only by a full run.
    """
    return sync_start.replace(day=1) if sync_mode == "full" else month_start


def fetch_sales(sess, from_d, to_d):
    """Fetch complete sales safely, including full-FY rebuilds.

    The ERP can return an incomplete CustomerWiseReport for a very large date
    range without an HTTP error. Daily ERP windows are independently complete;
    any failed window raises and prevents publication.
    """
    tickets = []
    for window_start, window_end in _sales_fetch_windows(from_d, to_d):
        window_tickets = _fetch_sales_window(_clone_sess(sess), window_start, window_end)
        tickets.extend(window_tickets)
        print(f"  sales {window_start}..{window_end}: {len(window_tickets)} tickets")
    return tickets


def fetch_sale_splits(sess, from_d, to_d):
    """{(date, ticket_no): {cash, credit, upi, total, pay_type}} from ERP ListSale.

    Captures real SPLIT payments (part cash + part UPI) that ListCustomerWiseReport
    collapses into one payment mode.  Ticket numbers are not globally unique
    (for example, 10086 occurs on 26-May and 30-Jun), so date is part of the
    identity. Best-effort: a per-day failure is logged and skipped, never aborts
    the sync.
    """
    splits = {}
    cur = from_d
    while cur <= to_d:
        ds = cur.strftime("%d-%m-%Y")
        try:
            url = (
                f"{ERP_BASE}/crusher/ListSale?startDt={ds}&end={ds}"
                "&materialId=-1&customerId=-1&operatorId=-1&startTicket=&endTicket=&crusherId=-1"
                "&paymentType=-1&vehicleId=-1&marketingPersonId=-1&transporterId=-1&dateTicketOrder=4"
                "&startTime=12:00:00 AM&endTime=11:59:59 PM&destination=&ledgerGroupId=-1"
                "&invoiceGenerated=-1&dcGenerated=-1&royaltyIssued=-1&isStock=-1"
                "&shippingAddressId=-1&vehicleType=-1&type=3"
            )
            raw = _clone_sess(sess).post(
                url, data={"draw": 1, "start": 0, "length": 2000},
                headers={"X-Requested-With": "XMLHttpRequest"}, timeout=35, verify=True,
            ).text
            html = json.loads(raw) if raw.lstrip().startswith('"') else raw
            table = next((t for t in re.findall(r"<table.*?</table>", html, re.DOTALL)
                          if "Final" in t or "Payment" in t), None)
            if table:
                cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", table, re.DOTALL)]
                REC = 22  # Sl,Date,Time,Ticket,Veh,Mat,Cust,EWt,LWt,NWt,Rate,Amt,Tpt,Roy,Total,PayType,Cash,Credit,UPI,Rmk,EDate,MDP
                for i in range(0, len(cells) - REC + 1, REC):
                    row = cells[i:i + REC]
                    if not re.fullmatch(r"\d+", row[3]):
                        break  # footer / total row -> end of data
                    splits[_sale_split_key(cur, row[3])] = {
                        "total": _num(row[14]), "pay_type": row[15],
                        "cash": round(_num(row[16]), 2), "credit": round(_num(row[17]), 2), "upi": round(_num(row[18]), 2),
                        "mdp": round(_num(row[21]), 3),  # real "MDP Ton" column from ListSale
                    }
        except Exception as e:
            print(f"  sale splits {ds}: {e}")
        cur += timedelta(days=1)
        time.sleep(0.1)
    return splits


def _cash_row_is_bank_expense(row, bank_expenses):
    """A cash-ledger row that is really a bank/UPI expense (e.g. 'PAID FROM VMI ACCOUNT'),
    so it must be dropped from the Cash section. Mirrors localhost _cash_row_matches_bank_expense."""
    paid = round(_num(row.get("paid")), 2)
    if paid <= 0:
        return False
    text = " ".join(str(row.get(k) or "") for k in ("ledger", "ledger_name", "description")).upper()
    if "EXPENSE" not in text:
        return False
    row_date = str(row.get("date") or "")[:10]
    for e in bank_expenses:
        if _payment_channel(e.get("payment_mode") or "Cash") == "cash":
            continue
        if str(e.get("date") or "")[:10] != row_date or abs(paid - round(_num(e.get("amount")), 2)) > 0.01:
            continue
        if any(str(e.get(k) or "") and str(e.get(k)).upper() in text for k in ("category", "description", "notes")):
            return True
    return False


def fetch_expenses(sess, from_d, to_d):
    days = [from_d + timedelta(days=i) for i in range((to_d - from_d).days + 1)]

    def _fetch_day(d):
        ds = d.strftime("%d-%m-%Y")
        rows = []
        try:
            url = (f"{ERP_BASE}/crusher/ListCrusherExpense"
                   f"?startDt={ds}&endDt={ds}&categoryId=-1&vehicleId=-1"
                   f"&cashLedgerId=-1&bankId=-1&tag=-1&campId=-1&type=1&draw=1&start=0&length=1000")
            data = json.loads(_request_text_with_retry(
                _clone_sess(sess),
                url,
                timeout=25,
                label=f"expenses {ds}",
            ))
            expense_sequence = 0
            for row in data.get("data", []):
                cells = [_clean(c) for c in row]
                if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
                amt = _num(cells[1]) if len(cells) > 1 else 0
                if amt <= 0: continue
                category = cells[3].strip() if len(cells) > 3 else "Other"
                desc     = cells[2].strip() if len(cells) > 2 else category
                remarks  = cells[7].strip() if len(cells) > 7 else ""
                if re.search(r"Ticket\s*(?:No\s*)?[:#]?\s*\d+", remarks, re.IGNORECASE): continue
                pay_mode = "Bank Transfer" if "vmi acc" in remarks.lower() else "Cash"
                expense_sequence += 1
                record = {
                    "id": 0, "date": str(d), "category": category[:50],
                    "description": desc[:300], "amount": amt,
                    "payment_mode": pay_mode, "notes": remarks[:200],
                    "vendor_id": None, "erp_synced": True,
                }
                record["erp_key"] = _expense_key(record, expense_sequence)
                rows.append(record)
        except Exception as e:
            print(f"  expenses fetch error {ds}: {e}")
            raise ErpFetchError(f"expenses fetch failed for {ds}; skipped Otomy write: {e}") from e
        return rows

    entries = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for day_rows in pool.map(_fetch_day, days):
            entries.extend(day_rows)
    entries.sort(key=lambda e: e["date"])
    for i, e in enumerate(entries, 1):
        e["id"] = i
    return entries


def fetch_cash_ledger(sess, from_d, to_d):
    entries = []
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(_request_text_with_retry(
            sess,
            f"{ERP_BASE}/crusher/CashLedger?start={fs}&end={ts}&type=1&cashLedgerId=-1",
            timeout=35,
            label=f"cash ledger {fs} to {ts}",
        ))
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
            entry_date = _parse_date(cells[0], to_d)
            received = _num(cells[1]) if len(cells) > 1 else 0
            paid     = _num(cells[2]) if len(cells) > 2 else 0
            balance  = _num(cells[3]) if len(cells) > 3 else None
            desc     = cells[4]       if len(cells) > 4 else ""
            ledger   = cells[5]       if len(cells) > 5 else ""
            if received == 0 and paid == 0 and not desc: continue
            entries.append({
                "date": str(entry_date), "ledger": ledger[:100] or "Entry",
                "description": desc[:300], "received": received,
                "paid": paid, "balance": balance,
            })
    except Exception as e:
        print(f"  cash_ledger: {e}")
        raise ErpFetchError(f"cash ledger fetch failed; skipped Otomy write: {e}") from e
    return entries


def fetch_bank_entries(sess, from_d, to_d):
    entries = []
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(_request_text_with_retry(
            sess,
            f"{ERP_BASE}/crusher/ListBankTransaction?start={fs}&end={ts}&bankId=-1&type=1",
            timeout=35,
            label=f"bank entries {fs} to {ts}",
        ))
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
            entry_date = _parse_date(cells[0], to_d)
            credit = _num(cells[1]) if len(cells) > 1 else 0
            debit  = _num(cells[2]) if len(cells) > 2 else 0
            desc   = cells[3]       if len(cells) > 3 else ""
            bank   = cells[4]       if len(cells) > 4 else "Bank"
            if credit == 0 and debit == 0: continue
            entries.append({
                "date": str(entry_date), "bank_name": bank[:100],
                "description": desc[:300], "credit": credit, "debit": debit,
            })
    except Exception as e:
        print(f"  bank_entries: {e}")
        raise ErpFetchError(f"bank entries fetch failed; skipped Otomy write: {e}") from e
    return entries


def fetch_boulders(sess, from_d, to_d):
    result = {"total_tonnes": 0.0, "total_trips": 0.0, "materials": [], "suppliers": []}
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        html = _request_text_with_retry(
            sess,
            f"{ERP_BASE}/crusher/listInput",
            params={"startDt": fs, "end": ts},
            timeout=35,
            label=f"boulders {fs} to {ts}",
        )

        def parse_table(src, table_id, label_key):
            pat = r"<table[^>]*id=['\"]" + re.escape(table_id) + r"['\"][^>]*>(.*?)</table>"
            m = re.search(pat, src, re.DOTALL | re.IGNORECASE)
            rows, trips, tonnes = [], 0.0, 0.0
            if not m:
                return {"rows": rows, "total_trips": trips, "total_tonnes": tonnes}
            for tr in _TR.finditer(m.group(1)):
                cols = [_clean(c) for c in _TD.findall(tr.group(1))]
                if len(cols) < 3: continue
                label = cols[0].strip()
                if not label: continue
                if label.lower() == "total":
                    trips, tonnes = _num(cols[1]), _num(cols[2])
                    continue
                rows.append({label_key: label, "trips": _num(cols[1]), "tonnes": _num(cols[2])})
            if not trips:   trips   = sum(r["trips"]  for r in rows)
            if not tonnes:  tonnes  = sum(r["tonnes"] for r in rows)
            rows.sort(key=lambda r: r["tonnes"], reverse=True)
            return {"rows": rows, "total_trips": trips, "total_tonnes": tonnes}

        mats = parse_table(html, "itemTable",  "material")
        sups = parse_table(html, "itemTable1", "supplier")
        result = {
            "total_tonnes": mats["total_tonnes"] or sups["total_tonnes"],
            "total_trips":  mats["total_trips"]  or sups["total_trips"],
            "materials":    mats["rows"],
            "suppliers":    sups["rows"],
        }
    except Exception as e:
        print(f"  boulders fetch error: {e}")
        raise ErpFetchError(f"boulders fetch failed; skipped Otomy write: {e}") from e
    return result


def fetch_boulder_rows(sess, from_d, to_d):
    days = [from_d + timedelta(days=i) for i in range((to_d - from_d).days + 1)]

    def _fetch_day(d):
        summary = fetch_boulders(_clone_sess(sess), d, d)
        trips = _num(summary.get("total_trips"))
        tonnes = _num(summary.get("total_tonnes"))
        if trips or tonnes:
            return {
                "id": 0, "date": str(d),
                "trips": int(round(trips)),
                "tonnes_per_trip": round(tonnes / trips, 2) if trips else 0.0,
                "total_tonnes": round(tonnes, 2),
                "source": "ERP Input - BOULDERS",
                "notes": "Loctell input summary",
            }
        return None

    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for result in pool.map(_fetch_day, days):
            if result:
                rows.append(result)
    rows.sort(key=lambda r: r["date"])
    for i, r in enumerate(rows, 1):
        r["id"] = i
    return rows


def fetch_iot(sess, from_d, to_d):
    movements = []
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{ERP_BASE}/iot/ListIOTSaleLinkReport"
            f"?startDt={fs}&endDt={ts}&startTime=12:00:00 AM&endTime=11:59:59 PM"
            f"&crusherId=-1&type=1",
            timeout=8, verify=True).text)
        for idx, row in enumerate(data.get("data", []), start=1):
            raw0 = htmllib.unescape(str(row[0])) if len(row) > 0 else ""
            dt_raw = re.split(r"<", raw0)[0].strip()
            lbl_m = re.search(r">\s*([^<]+?)\s*</a>", raw0)
            linked = lbl_m.group(1).strip() if lbl_m else "PLANT ENTRY"
            mv_dt = None
            for fmt in ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
                try:
                    mv_dt = datetime.strptime(re.sub(r"\s+", " ", dt_raw).strip(), fmt)
                    break
                except Exception:
                    pass
            if not mv_dt:
                continue
            img_html = htmllib.unescape(str(row[8])) if len(row) > 8 else ""
            img_urls = re.findall(r"https?://[^\s\"'<>]+\.(?:png|jpg|jpeg)", img_html)
            movements.append({
                "id": idx,
                "date": mv_dt.date().isoformat(),
                "dt": mv_dt.strftime("%d-%m-%Y %I:%M %p"),
                "linked": linked[:50],
                "ticket": (_clean(row[1]) if len(row) > 1 else "")[:30],
                "vehicle": (_clean(row[2]) if len(row) > 2 else "")[:30],
                "material": (_clean(row[3]) if len(row) > 3 else "")[:50],
                "party": (_clean(row[4]) if len(row) > 4 else "")[:200],
                "qty": (_clean(row[5]) if len(row) > 5 else "")[:20],
                "crusher": (_clean(row[6]) if len(row) > 6 else "")[:100],
                "img_url": (img_urls[0] if img_urls else "")[:500],
            })
    except Exception as e:
        print(f"  iot fetch error: {e}")
    return movements


def fetch_debtors(sess, as_of=None):
    """Fetch customer outstanding balances from ERP for a given date."""
    debtors = []
    try:
        ds = (as_of or date.today()).strftime("%d-%m-%Y")
        start_at, length = 0, 500
        total = None
        while total is None or start_at < total:
            payload = _request_json_with_retry(
                sess,
                f"{ERP_BASE}/crusher/ListCustomerBalance",
                params={"date": ds, "type": 1, "sortByName": -1, "sortByPayment": -1,
                        "customerId": -1, "draw": 1, "start": start_at, "length": length},
                timeout=35,
                label=f"debtors {ds} page {start_at}",
            )
            rows = payload.get("data", []) or []
            total = int(payload.get("recordsTotal", len(rows)))
            if not rows: break
            for row in rows:
                if len(row) < 4: continue
                raw_name = re.sub(r"<span[^>]*>.*?</span>", " ", str(row[0]),
                                  flags=re.IGNORECASE | re.DOTALL)
                name = re.sub(r"\s+", " ", _clean(raw_name)).strip()
                if not name or name.upper() in ("CUSTOMER", "TOTAL", "NAME", "SR NO", ""):
                    continue
                billed   = _num(row[2]) if len(row) > 2 else 0
                received = _num(row[3]) if len(row) > 3 else 0
                action   = str(row[4] or "") if len(row) > 4 else ""
                m = re.search(r"viewLedgerTransactions\?customerId=(\d+)", action, re.IGNORECASE)
                debtors.append({
                    "name": name[:200],
                    "outstanding": round(billed - received, 2),
                    "billed":      round(billed, 2),
                    "received":    round(received, 2),
                    "erp_customer_id": int(m.group(1)) if m else None,
                })
            start_at += len(rows)
            if len(rows) < length: break
    except Exception as e:
        print(f"  debtors fetch error ({as_of}): {e}")
        raise ErpFetchError(f"debtors fetch failed for {as_of}; skipped Otomy write: {e}") from e
    return debtors


def fetch_creditors(sess, as_of=None):
    """Fetch vendor outstanding payables from ERP for a given date."""
    creditors = []
    try:
        ds = (as_of or date.today()).strftime("%d-%m-%Y")
        data = _request_json_with_retry(
            sess,
            f"{ERP_BASE}/crusher/ListSupplierBalance?date={ds}&type=1",
            timeout=35,
            label=f"creditors {ds}",
        )
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or not cells[0]: continue
            name = cells[0].strip()
            if name.upper() in ("SUPPLIER", "TOTAL", "NAME", ""): continue
            credit = _num(cells[1]) if len(cells) > 1 else 0
            debit  = _num(cells[2]) if len(cells) > 2 else 0
            action = str(row[3] or "") if len(row) > 3 else ""
            match = re.search(r"viewSupplierLedgerTransactions\?supplierId=([^'\"&\s]+)", action, re.IGNORECASE)
            creditors.append({
                "name": name[:200],
                "payable": round(debit - credit, 2),
                "erp_supplier_id": match.group(1) if match else None,
            })
    except Exception as e:
        print(f"  creditors fetch error: {e}")
        raise ErpFetchError(f"creditors fetch failed; skipped Otomy write: {e}") from e
    return creditors


def _supplier_id_from_master_markup(markup, known_supplier_ids):
    """Resolve a Supplier List row's ID to the balance-report supplier ID."""
    text = str(markup or "")
    exact = [supplier_id for supplier_id in known_supplier_ids if supplier_id and supplier_id in text]
    if len(exact) == 1:
        return exact[0]
    ids = re.findall(r"(?:[?&]|\b)(?:supplierId|supplier_id|id)\s*[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    for raw_id in ids:
        matches = [supplier_id for supplier_id in known_supplier_ids
                   if supplier_id == raw_id or supplier_id.split("_", 1)[0] == raw_id]
        if len(matches) == 1:
            return matches[0]
    # Supplier List action buttons are not consistent across Loctell releases:
    # some use updateSupplier(8237), rather than a query-string supplierId.
    # A numeric token is safe only when it maps to exactly one live balance ID;
    # this deliberately rejects row serial numbers and ambiguous IDs.
    for raw_id in re.findall(r"(?<![A-Za-z0-9_])(\d{3,})(?![A-Za-z0-9_])", text):
        matches = [supplier_id for supplier_id in known_supplier_ids
                   if supplier_id == raw_id or supplier_id.split("_", 1)[0] == raw_id]
        if len(matches) == 1:
            return matches[0]
    return None


def _supplier_name_from_cells(cells, supplier_id):
    """Supplier List tables put the display name in the first textual cell."""
    for cell in cells:
        value = re.sub(r"\s+", " ", str(cell or "")).strip()
        compact = re.sub(r"[^A-Za-z0-9]", "", value)
        if (not value or value.lower() in {"supplier", "name", "action", "edit", "delete"}
                or value == supplier_id or value == supplier_id.split("_", 1)[0]
                or re.fullmatch(r"\d+", value) or re.fullmatch(r"[+()\-\d ]{7,}", value)
                or re.fullmatch(r"[A-Za-z0-9]{15}", compact)):
            continue
        return value[:200]
    return ""


def _supplier_master_rows_from_payload(payload, known_supplier_ids):
    """Read either a rendered Supplier List table or its DataTables JSON."""
    known = {str(value).strip() for value in known_supplier_ids or [] if str(value).strip()}
    rows = []
    seen = set()

    def add(markup, cells):
        supplier_id = _supplier_id_from_master_markup(markup, known)
        if not supplier_id or supplier_id in seen:
            return
        name = _supplier_name_from_cells(cells, supplier_id)
        if not name:
            return
        rows.append({"name": name, "erp_supplier_id": supplier_id, "active": True})
        seen.add(supplier_id)

    text = str(payload or "")
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        decoded = None
    if isinstance(decoded, dict):
        decoded = decoded.get("data", decoded.get("aaData", []))
    if isinstance(decoded, list):
        for row in decoded:
            if isinstance(row, dict):
                add(json.dumps(row, ensure_ascii=False), [str(value or "") for value in row.values()])
            elif isinstance(row, (list, tuple)):
                add(json.dumps(row, ensure_ascii=False), [_clean(value) for value in row])
    else:
        for match in _TR.finditer(text):
            markup = match.group(1)
            add(markup, [_clean(cell) for cell in _TD.findall(markup)])
    return rows


def _supplier_master_ajax_url(payload):
    """Find the data URL configured by Loctell's Supplier List DataTable."""
    text = str(payload or "")
    patterns = (
        r"(?:ajax|sAjaxSource)\s*[:=]\s*[\"']([^\"']*[Ss]upplier[^\"']*)[\"']",
        r"url\s*:\s*[\"']([^\"']*[Ss]upplier[^\"']*)[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return htmllib.unescape(match.group(1).strip())
    return ""


def _supplier_master_page_links(payload):
    """Return only pagination links that stay on Home > Suppliers."""
    out = []
    for href in re.findall(r"href\s*=\s*[\"']([^\"']+)[\"']", str(payload or ""), re.IGNORECASE):
        candidate = htmllib.unescape(href.strip())
        if "listsuppliers" not in candidate.lower() or candidate.lower().startswith("javascript:"):
            continue
        out.append(candidate)
    return out


def _require_complete_supplier_master(rows, known_supplier_ids):
    known = {str(value).strip() for value in known_supplier_ids or [] if str(value).strip()}
    by_id = {str(row.get("erp_supplier_id") or "").strip(): row for row in rows if row.get("erp_supplier_id")}
    missing = sorted(known - set(by_id))
    if missing:
        raise ErpFetchError(
            "Loctell Supplier List did not map every current supplier ID "
            f"({len(missing)} missing); refusing to use balance-report names as a master"
        )
    return [by_id[supplier_id] for supplier_id in sorted(by_id)]


def parse_supplier_master_page(payload, known_supplier_ids):
    """Parse Loctell Home > Suppliers into ID-backed master rows.

    The master page is authoritative for the supplier display name.  The
    balance report remains authoritative only for payable amounts and its
    ledger-link supplier IDs.  Refuse a partial mapping rather than silently
    falling back to balance-report spelling.
    """
    return _require_complete_supplier_master(
        _supplier_master_rows_from_payload(payload, known_supplier_ids), known_supplier_ids
    )


def fetch_supplier_master(sess, creditors):
    """Fetch the authoritative Home > Suppliers master, linked to balance IDs."""
    known_supplier_ids = {
        str(row.get("erp_supplier_id") or "").strip()
        for row in creditors or []
        if str(row.get("erp_supplier_id") or "").strip()
    }
    if not known_supplier_ids:
        raise ErpFetchError("Supplier Balance returned no supplier IDs; cannot build supplier master")
    try:
        base_url = f"{ERP_BASE}/home/listSuppliers"
        payload = _request_text_with_retry(sess, base_url, timeout=45, label="supplier master")
        rows_by_id = {row["erp_supplier_id"]: row for row in _supplier_master_rows_from_payload(payload, known_supplier_ids)}
        pending = [urljoin(base_url, value) for value in _supplier_master_page_links(payload)]
        seen_urls = {base_url}
        ajax_url = _supplier_master_ajax_url(payload)
        if ajax_url:
            pending.append(urljoin(base_url, ajax_url))
        while pending and len(rows_by_id) < len(known_supplier_ids):
            next_url = pending.pop(0)
            if next_url in seen_urls:
                continue
            seen_urls.add(next_url)
            page = _request_text_with_retry(sess, next_url, timeout=45, label="supplier master page")
            rows_by_id.update({row["erp_supplier_id"]: row for row in _supplier_master_rows_from_payload(page, known_supplier_ids)})
            for value in _supplier_master_page_links(page):
                page_url = urljoin(base_url, value)
                if page_url not in seen_urls:
                    pending.append(page_url)
            if len(seen_urls) > 60:
                raise ErpFetchError("Supplier List pagination exceeded 60 pages; refusing ambiguous master refresh")
        rows = _require_complete_supplier_master(list(rows_by_id.values()), known_supplier_ids)
    except Exception as e:
        if isinstance(e, ErpFetchError):
            raise
        raise ErpFetchError(f"supplier master fetch failed; skipped Otomy write: {e}") from e
    print(f"  {len(rows)} Supplier List master rows")
    return rows


def fetch_vendor_payments(sess, creditors, from_d, to_d):
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    if not creditors:
        return []

    def _fetch_one(creditor):
        supplier_id = creditor.get("erp_supplier_id")
        if not supplier_id:
            return []
        rows = []
        try:
            data = json.loads(_clone_sess(sess).get(
                f"{ERP_BASE}/crusher/ViewSupplierLedgerTransactions"
                f"?start={fs}&end={ts}&supplierId={supplier_id}&materialId=-1&crusherId=-1&orderType=2&type=1",
                timeout=45, verify=True).text)
            sequence = 0
            for row in data.get("data", []):
                cells = [_clean(c) for c in row]
                if not cells or "TOTAL" in cells[0].upper():
                    continue
                amount = _num(cells[6]) if len(cells) > 6 else 0
                payment_type = cells[8].strip() if len(cells) > 8 else ""
                details = cells[9].strip() if len(cells) > 9 else ""
                remarks = cells[12].strip() if len(cells) > 12 else ""
                if amount <= 0 or not payment_type:
                    continue
                paid_on = _parse_date(cells[0], to_d)
                sequence += 1
                rows.append({
                    "date": str(paid_on),
                    "vendor_name": creditor["name"],
                    "erp_supplier_id": str(supplier_id),
                    "amount": amount,
                    "mode": _mode_bucket(payment_type),
                    "reference": f"ERP-SUP-{supplier_id}-{paid_on.isoformat()}-{sequence}-{int(round(amount))}"[:100],
                    "notes": f"ERP supplier_id={supplier_id}; {payment_type}; {details}; {remarks}"[:1000],
                })
        except Exception as e:
            print(f"  vendor payment fetch error ({creditor.get('name')}): {e}")
            raise ErpFetchError(f"vendor payment fetch failed for {creditor.get('name')}; skipped Otomy write: {e}") from e
        return rows

    payments = []
    with ThreadPoolExecutor(max_workers=min(len(creditors), 10)) as pool:
        for result in pool.map(_fetch_one, creditors):
            payments.extend(result)
    return payments


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _vendor_identity(row):
    """Stable supplier identity; display names are not unique in Loctell."""
    row = row or {}
    supplier_id = str(row.get("erp_supplier_id") or row.get("supplier_id") or "").strip()
    if supplier_id:
        return f"erp:{supplier_id}"
    return f"name:{_norm_name(row.get('name'))}"


def _customer_master_key(name):
    """Display-master identity: keep meaningful internal spacing intact."""
    return str(name or "").strip().casefold()


def canonical_customer_master_rows(rows):
    """Keep one customer-master row per display identity.

    Balances are normalized separately against the Loctell debtor snapshot.
    Do not erase a real master simply because it has different internal
    spacing: the Customers page must represent the source master faithfully.
    """
    canonical = {}
    for source in rows or []:
        row = dict(source)
        name = str(row.get("name") or "").strip()
        key = _customer_master_key(name)
        if not key:
            continue
        canonical.setdefault(key, row)
    return canonical


def canonical_debtors_by_name(rows):
    """Index one exact debtor balance per normalized customer name.

    Equal spelling variants are safe to collapse.  Differing positive balances
    are an ERP ambiguity, not something the engine may silently add or choose.
    Refuse to publish until that source inconsistency is resolved.
    """
    canonical = {}
    for source in rows or []:
        row = dict(source)
        key = _norm_name(row.get("name"))
        if not key:
            continue
        existing = canonical.get(key)
        if existing is not None:
            old = _num(existing.get("outstanding", existing.get("balance", 0.0)))
            new = _num(row.get("outstanding", row.get("balance", 0.0)))
            if abs(old - new) > 0.01:
                raise ErpFetchError(
                    "conflicting Loctell debtor balances for normalized customer "
                    f"{key}: {old:.2f} versus {new:.2f}"
                )
            continue
        canonical[key] = row
    return canonical


VENDOR_LEDGER_START = date(2025, 2, 15)  # full itemized vendor history begins here


def fetch_supplier_ledgers_full(sess, creditors, from_d, to_d, *, strict=False):
    """Full itemized supplier ledgers keyed by Loctell supplier ID.

    Tally supplier convention: Purchase=Credit (raises payable),
    Payment=Debit (lowers payable).

    The ordinary common-engine path is deliberately resilient: a failed supplier
    request returns no entries and the prior local snapshot can remain in use.
    A vendor-only repair can instead request ``strict=True``. In that mode every
    requested Loctell supplier request must succeed (including an empty ledger),
    so a partial ledger bundle can never be published.
    """
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    if not creditors:
        return {}

    def _one(creditor):
        sid = creditor.get("erp_supplier_id")
        name = creditor.get("name", "")
        if not sid:
            return (creditor, [], None)
        entries = []
        try:
            data = json.loads(_clone_sess(sess).get(
                f"{ERP_BASE}/crusher/ViewSupplierLedgerTransactions"
                f"?start={fs}&end={ts}&supplierId={sid}&materialId=-1&crusherId=-1&orderType=2&type=1",
                timeout=45, verify=True).text)
            for row in data.get("data", []):
                cells = [_clean(c) for c in row]
                if not cells or not cells[0]:
                    continue
                if not re.match(r"\d{1,2}-\d{1,2}-\d{4}", cells[0]):
                    continue  # skip the trailing TOTAL row
                d = _parse_date(cells[0], to_d)
                payment = _num(cells[6]) if len(cells) > 6 else 0.0
                purchase = _num(cells[7]) if len(cells) > 7 else 0.0
                mode = cells[8] if len(cells) > 8 else ""
                narration = cells[9] if len(cells) > 9 else ""
                if purchase > 0:
                    entries.append({"type": "purchase", "date": str(d), "vch_type": "Purchase",
                                    "description": narration or "Material Purchase",
                                    "debit": 0.0, "credit": round(purchase, 2)})
                if payment > 0:
                    entries.append({"type": "payment", "date": str(d), "vch_type": "Payment",
                                    "description": ((narration or "Payment") + (f" — {mode}" if mode else "")).strip(" —"),
                                    "debit": round(payment, 2), "credit": 0.0})
        except Exception as e:
            print(f"  supplier ledger fetch error ({name}); using lightweight fallback: {e}")
            return (creditor, [], str(e))
        return (creditor, entries, None)

    result = {}
    failures = []
    with ThreadPoolExecutor(max_workers=min(len(creditors), 10)) as pool:
        for creditor, entries, error in pool.map(_one, creditors):
            name = creditor.get("name", "")
            if error:
                failures.append(f"{name}: {error}")
                continue
            if strict:
                # An empty but successfully-read ledger is still canonical ERP
                # data.  Preserve the key so callers do not mistake it for a
                # failed fetch and fall back to an old local calculation.
                result[_vendor_identity(creditor)] = entries
            elif entries:
                result[_vendor_identity(creditor)] = entries
    if failures and strict:
        raise ErpFetchError(
            "supplier ledger fetch failed; refusing a partial vendor bundle: "
            + "; ".join(failures)
        )
    return result


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
    """Full itemized customer ledgers (every sale + receipt, incl. same-day spot receipts) for a
    reconciling Tally view, keyed by normalised name. ViewLedgerTransactions cols: [0]=date,
    [1]=material, [2]=vehicle, [11]=Debit (sale), [12]=Credit (receipt), [13]=mode. Sale=Debit
    (raises receivable), Receipt=Credit (lowers it). Limited to debtors with an outstanding balance
    to bound sync load. Resilient: a customer that errors just yields no entries (its ledger falls
    back to the archive-based build) — never aborts the sync."""
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    targets = [d for d in debtors
               if d.get("erp_customer_id") and (not only_outstanding or _num(d.get("outstanding")) > 0)]
    if not targets:
        return {}

    def _one(d):
        cid, name = d.get("erp_customer_id"), d.get("name", "")
        entries = []
        try:
            data = json.loads(_clone_sess(sess).get(
                f"{ERP_BASE}/crusher/ViewLedgerTransactions",
                params={"start": fs, "end": ts, "customerId": cid, "materialId": -1,
                        "transactionType": -1, "marketingPersonId": -1, "orderType": 2, "type": 1},
                timeout=45, verify=True).text)
            for row in data.get("data", []):
                cells = [_clean(c) for c in row]
                if not cells or not cells[0]:
                    continue
                if not re.match(r"\d{1,2}-\d{1,2}-\d{4}", cells[0]):
                    continue  # skip the trailing TOTAL row
                dt = _parse_date(cells[0], to_d)
                debit = _num(cells[11]) if len(cells) > 11 else 0.0
                credit = _num(cells[12]) if len(cells) > 12 else 0.0
                material = cells[1] if len(cells) > 1 else ""
                vehicle = cells[2] if len(cells) > 2 else ""
                mode = cells[13] if len(cells) > 13 else ""
                if debit > 0:
                    entries.append({"type": "sale", "date": str(dt), "vch_type": "Sale",
                                    "description": (f"{material} — {vehicle}".strip(" —")) or "Sale",
                                    "debit": round(debit, 2), "credit": 0.0,
                                    "material": material, "vehicle_no": vehicle,
                                    "qty_mt": _num(cells[5]) if len(cells) > 5 else 0.0,
                                    "rate_per_mt": _num(cells[6]) if len(cells) > 6 else 0.0})
                if credit > 0:
                    entries.append({"type": "receipt", "date": str(dt), "vch_type": "Receipt",
                                    "description": f"Receipt ({mode})" if mode else "Receipt",
                                    "debit": 0.0, "credit": round(credit, 2)})
        except Exception as e:
            print(f"  customer ledger fetch error ({name}); using fallback: {e}")
            return (name, [])
        return (name, entries)

    result = {}
    with ThreadPoolExecutor(max_workers=min(len(targets), CUST_LEDGER_WORKERS)) as pool:
        for name, entries in pool.map(_one, targets):
            if entries:
                result[_norm_name(name)] = entries
    return result


def compute_repayments(debtors_prev, debtors_curr, as_of_date):
    """
    Credit repayments = customers whose outstanding balance DECREASED between
    the previous snapshot and the current snapshot.
    Returns a list matching the customer_repayments format used by the dashboard.
    """
    prev_map = {d["name"]: d for d in debtors_prev}
    repayments = []
    for curr in debtors_curr:
        prev = prev_map.get(curr["name"])
        if not prev:
            continue
        delta = round(prev["outstanding"] - curr["outstanding"], 2)
        if delta <= 0:
            continue
        received_delta = round(curr["received"] - prev["received"], 2)
        repayments.append({
            "date": str(as_of_date),
            "customer_name": curr["name"],
            "mode": "Cash/Bank",
            "reference": "ERP balance delta",
            "payment_received": round(received_delta if received_delta > 0 else delta, 2),
            "bank_received": 0.0,
            "cash_received": 0.0,
            "sale_adjusted": 0.0,
            "amount": delta,
            "balance": curr["outstanding"],
            "previous_balance": prev["outstanding"],
            "source": "ERP Outstanding Delta",
        })
    repayments.sort(key=lambda r: r["amount"], reverse=True)
    return repayments

def fetch_customer_ledger_rows(sess, from_d, to_d, erp_customer_id):
    try:
        payload = sess.get(
            f"{ERP_BASE}/crusher/ViewLedgerTransactions",
            params={
                "start": from_d.strftime("%d-%m-%Y"),
                "end": to_d.strftime("%d-%m-%Y"),
                "customerId": erp_customer_id,
                "materialId": -1,
                "transactionType": -1,
                "marketingPersonId": -1,
                "orderType": 2,
                "type": 1,
            },
            timeout=35,
            verify=True,
        ).json()
        return payload.get("data", []) or []
    except Exception as e:
        print(f"  customer ledger {erp_customer_id}: {e}")
        raise ErpFetchError(f"customer ledger fetch failed for {erp_customer_id}; skipped Otomy write: {e}") from e

def compute_repayments_from_erp(sess, start, end, previous_debtors, current_debtors, debtors_cache=None):
    # --- Phase 1: pre-fetch all intermediate days' debtors in parallel ---
    inter_days = [start + timedelta(days=i) for i in range((end - start).days)]
    need_fetch = [d for d in inter_days if not (debtors_cache and d in debtors_cache)]
    pre = {}
    if need_fetch:
        with ThreadPoolExecutor(max_workers=min(len(need_fetch), ERP_DEBTOR_WORKERS)) as pool:
            futs = {d: pool.submit(fetch_debtors, _clone_sess(sess), d) for d in need_fetch}
            for d, f in futs.items():
                pre[d] = f.result()

    def _day_debtors(d):
        if d == end:
            return current_debtors
        if debtors_cache and d in debtors_cache:
            return debtors_cache[d]
        return pre.get(d, [])

    # --- Phase 2: traverse snapshot chain, collect (day, cid, current_row) tasks ---
    previous_snapshot = {
        row.get("erp_customer_id"): row
        for row in previous_debtors
        if row.get("erp_customer_id") is not None
    }
    tasks = []
    current_day = start
    while current_day <= end:
        current_snapshot = {
            row.get("erp_customer_id"): row
            for row in _day_debtors(current_day)
            if row.get("erp_customer_id") is not None
        }
        for cid, curr in current_snapshot.items():
            prev = previous_snapshot.get(cid, {})
            credit_delta = round(_num(curr.get("received")) - _num(prev.get("received")), 2)
            balance_change = round(abs(_num(curr.get("outstanding")) - _num(prev.get("outstanding"))), 2)
            if credit_delta > 0 or balance_change > 0:
                tasks.append((current_day, cid, curr))
        previous_snapshot = current_snapshot
        current_day += timedelta(days=1)

    # --- Phase 3: parallel fetch all customer ledger rows ---
    def _process_one(task):
        day, cid, curr = task
        # Match localhost's import_customer_credit_receipts: a repayment with no identifiable
        # customer name is skipped (_get_or_create_customer("") -> None -> continue). Otherwise a
        # blank-name row can't net against its same-day spot sale and inflates the cash/bank book.
        if not str(curr.get("name", "")).strip():
            return []
        rows = fetch_customer_ledger_rows(_clone_sess(sess), day, day, cid)
        total_debit = 0.0
        total_credit = 0.0
        credit_by_channel = {"bank": 0.0, "cash": 0.0}
        raw_modes = []
        for row in rows:
            cols = [_clean(col) for col in row]
            if not cols or (cols[0] or "").upper() == "TOTAL":
                continue
            debit  = _num(cols[11]) if len(cols) > 11 else 0.0
            credit = _num(cols[12]) if len(cols) > 12 else 0.0
            mode   = cols[13]       if len(cols) > 13 else ""
            if debit  > 0: total_debit += debit
            if credit > 0:
                total_credit += credit
                credit_by_channel[_ledger_payment_channel(cols)] += credit
                raw_modes.append(mode or "Payment")
        if total_credit <= 0:
            return []
        safe_tc = total_credit or 1
        cash_sa = min(round(credit_by_channel["cash"], 2),
                      round(total_debit * (credit_by_channel["cash"] / safe_tc), 2))
        bank_sa = min(round(credit_by_channel["bank"], 2),
                      round(total_debit * (credit_by_channel["bank"] / safe_tc), 2))
        cash_amt = round(max(credit_by_channel["cash"] - cash_sa, 0.0), 2)
        bank_amt = round(max(credit_by_channel["bank"] - bank_sa, 0.0), 2)
        mode_notes = ", ".join(dict.fromkeys([m for m in raw_modes if m]))[:120]
        result = []
        for mode, amount, payment_received, sale_adjusted in (
            ("Cash", cash_amt, round(credit_by_channel["cash"], 2), cash_sa),
            ("Bank", bank_amt, round(credit_by_channel["bank"], 2), bank_sa),
        ):
            if payment_received <= 0:
                continue
            reference = f"ERP-CREDIT-{cid}-{day.isoformat()}-{mode.upper()}"
            if reference in EXCLUDED_CUSTOMER_RECEIPT_REFS:
                continue
            result.append({
                "date": str(day),
                "customer_name": curr["name"],
                "mode": mode,
                "reference": reference,
                "payment_received": payment_received,
                "bank_received": payment_received if mode == "Bank" else 0.0,
                "cash_received": payment_received if mode == "Cash" else 0.0,
                "sale_adjusted": round(sale_adjusted, 2),
                "amount": amount,
                "balance": round(_num(curr.get("outstanding")), 2),
                "source": "Customer Ledger",
                "erp_customer_id": cid,
                "notes": f"ledger modes={mode_notes}",
            })
        return result

    repayments = []
    if tasks:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for result in pool.map(_process_one, tasks):
                repayments.extend(result)

    repayments.sort(key=lambda row: (row["date"], row["amount"], row.get("customer_name", ""), row.get("mode", "")), reverse=True)
    return repayments

# ─── control room builder ─────────────────────────────────────────────────────

def build_control(sales, expenses, from_d, to_d,
                  boulders=None, debtors=None, creditors=None,
                  cash_balance=0.0, bank_net=0.0, repayments=None,
                  labour=None, parts=None, machines=None,
                  vendor_payments=None,
                  bank_balance_book=0.0, cash_balance_office_book=0.0):
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
        g["ticket_count"] += 1
        g["qty_mt"]        += _num(s["qty_mt"])
        g["mdp_ton"]       += _num(s.get("mdp_ton"))
        g["amount"]        += amt
        g["credit_sale_amount"] += s_credit
        g["cash_received"]      += s_cash
        g["bank_received"]      += s_upi
        g["paid_against_sale"]  += s_cash + s_upi
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
    credit_liquidity_available = from_d >= date(2026, 6, 1) and total_qty > 0
    credit_sale_total = round(sum(row["credit_sale_amount"] for row in csr), 2)
    credit_recovery_total = rp_pay_total
    net_credit_change = round(credit_sale_total - credit_recovery_total, 2)
    if credit_liquidity_available:
        credit_sale_per_tonne = round(credit_sale_total / total_qty, 2)
        credit_recovery_per_tonne = round(credit_recovery_total / total_qty, 2)
        credit_locked_per_tonne = round(net_credit_change / total_qty, 2)
        cash_converted_profit_per_tonne = round(
            (profit - net_credit_change) / total_qty, 2
        )
    else:
        credit_sale_per_tonne = None
        credit_recovery_per_tonne = None
        credit_locked_per_tonne = None
        cash_converted_profit_per_tonne = None

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
            "credit_liquidity_available": credit_liquidity_available,
            "credit_sale_for_liquidity": credit_sale_total,
            "credit_recovery_for_liquidity": credit_recovery_total,
            "net_credit_change_for_liquidity": net_credit_change,
            "credit_sale_per_tonne": credit_sale_per_tonne,
            "credit_recovery_per_tonne": credit_recovery_per_tonne,
            "credit_locked_per_tonne": credit_locked_per_tonne,
            "cash_converted_profit_per_tonne": cash_converted_profit_per_tonne,
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
        "customer_sales_totals": {
            "ticket_count":       sum(r["ticket_count"]      for r in csr),
            "qty_mt":             round(sum(r["qty_mt"]       for r in csr), 2),
            "amount":             round(sum(r["amount"]       for r in csr), 2),
            "mdp_ton":            round(sum(r["mdp_ton"]      for r in csr), 3),
            "bank_received":      round(sum(r["bank_received"]  for r in csr), 2),
            "cash_received":      round(sum(r["cash_received"]  for r in csr), 2),
            "paid_against_sale":  round(sum(r["paid_against_sale"] for r in csr), 2),
            "credit_sale_amount": round(sum(r["credit_sale_amount"] for r in csr), 2),
        },
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
    source = ROOT / "seed" / "balance_anchors.json"
    target = DATA_DIR / "balance_anchors.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = source.read_bytes()
    if not target.exists() or target.read_bytes() != content:
        target.write_bytes(content)
        print("  balance_anchors.json (mirrored reviewed seed)")
    _BALANCE_OVERLAY = None

def _bank_amount_key(row):
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

def _bank_dedupe_key(row):
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

def dedupe_bank_rows(rows):
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

def _archive_key(section, row):
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

def _prefer_archive_row(section, existing, incoming):
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

def _historical_existing_dates(rows):
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

def _merge_archive_rows(existing, incoming, section, *, drop_current_window=True):
    if section == "expenses":
        existing = [row for row in existing if not _is_vendor_payment_expense(row)]
        incoming = [row for row in incoming if not _is_vendor_payment_expense(row)]
    if drop_current_window and section in {"sales", "expenses", "cash", "bank", "receipts", "vendor_payments"}:
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
    protected_dates = _historical_existing_dates(existing) if section in {"sales", "expenses", "receipts", "bank", "cash", "vendor_payments"} else set()
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

def derive_bank_transactions(sales, expenses, repayments, existing=None):
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
    cash_rows,
    bank_rows,
    boulder_rows,
    repayments,
    vendor_payments,
    local_seed,
    balance_snapshots=None,
    ledger_by_month=None,
):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for section, rows in (
        ("sales", all_sales),
        ("expenses", all_expenses),
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

def snapshot_key(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "_"]
    normalized = urlunsplit(("", "", parts.path, urlencode(query), ""))
    return base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")

def write_snapshot(url, data):
    SNAPSHOT_API_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{snapshot_key(url)}.json"
    with open(SNAPSHOT_API_DIR / filename, "w") as f:
        json.dump(data, f, default=str, separators=(",", ":"))
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
    for path in SNAPSHOT_API_DIR.glob("*.json"):
        if path.name in _WRITTEN_SNAPSHOT_FILES:
            continue
        url = _snapshot_url_from_path(path)
        if not url:
            continue
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if "from_date" not in query or "to_date" not in query:
            continue
        if parts.path == "/api/sync/erp/cashbook":
            continue
        try:
            removed_bytes += path.stat().st_size
            path.unlink()
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

def read_snapshot_payload(url):
    path = SNAPSHOT_API_DIR / f"{snapshot_key(url)}.json"
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
            with open(ROOT / "seed" / "balance_anchors.json") as f:
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


def _overlay_balance(to_iso, sales, expenses, repayments):
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
            # Vendor payments are already booked as expenses; never subtract the vendor
            # stream again (that double-counts a vendor who is also an expense).

        if current >= month_start:
            day_sales = sales_by_date.get(key, [])
            day_expenses = expenses_by_date.get(key, [])
            day_boulders = boulders_by_date.get(key, [])
            day_repayments = repayments_by_date.get(key, [])
            sale_amount = sum(_sale_total(row) for row in day_sales)
            # Match localhost ledger: split each sale by its real cash/credit/UPI channels
            # (spot = cash + UPI, credit = credit channel) instead of a crude payment_mode test,
            # so split-payment tickets land the right amount in each column.
            sale_splits = [_sale_channels(row) for row in day_sales]
            spot_sale_amount = sum(s_cash + s_upi for s_cash, _s_credit, s_upi in sale_splits)
            spot_sale_cash = sum(s_cash for s_cash, _s_credit, _s_upi in sale_splits)
            spot_sale_bank = sum(s_upi for _s_cash, _s_credit, s_upi in sale_splits)
            credit_sale_amount = sum(s_credit for _s_cash, s_credit, _s_upi in sale_splits)
            qty_mt = sum(_num(row.get("qty_mt")) for row in day_sales)
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
            expense_total = expense_cash + expense_bank
            boulder_input_mt = sum(_num(row.get("total_tonnes")) for row in day_boulders)
            # Balance overlay must see the FULL repayment history from the anchor (mirrors the
            # tile), not just month-to-date — else pre-month receipts (e.g. 29-30 Jun) are missed
            # and the ledger cash/bank read low. `repayments` here is only mtd; use all-history.
            _ov = _overlay_balance(key, sales, expenses, overlay_repayments if overlay_repayments is not None else repayments)
            row_bank = _ov[0] if _ov else round(bank_balance, 2)
            row_cash = _ov[1] if _ov else round(cash_balance, 2)
            rows.append({
                "date": key,
                "sale_trips": len(day_sales),
                "sale_amount": round(sale_amount, 2),
                "spot_sale_amount": round(spot_sale_amount, 2),
                "spot_sale_cash": round(spot_sale_cash, 2),
                "spot_sale_bank": round(spot_sale_bank, 2),
                "credit_sale_amount": round(credit_sale_amount, 2),
                "qty_mt": round(qty_mt, 2),
                "credit_repayment": round(credit_repayment_cash + credit_repayment_bank, 2),
                "credit_repayment_cash": round(credit_repayment_cash, 2),
                "credit_repayment_bank": round(credit_repayment_bank, 2),
                "expenses": round(expense_total, 2),
                "expense_cash": round(expense_cash, 2),
                "expense_bank": round(expense_bank, 2),
                "cash_balance_office": row_cash,
                "bank_balance": row_bank,
                "boulder_input_mt": round(boulder_input_mt, 2),
                "boulder_trips": round(sum(_num(row.get("trips")) for row in day_boulders), 2),
                "stock_in_plant_mt": round(boulder_input_mt - qty_mt, 2),
            })
        current += timedelta(days=1)

    totals = {
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
    return {"year": year, "month": month, "rows": rows, "totals": totals}


def build_cashbook_view(from_d, to_d, sales, expenses, repayments, opening):
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

    def _row(day, particulars, party, kind, incoming, outgoing, ticket_no=None, settlement_roundoff=0.0):
        return {
            "date": str(day)[:10],
            "particulars": particulars,
            "party": party or "",
            "kind": kind,
            "in": round(_num(incoming), 2),
            "out": round(_num(outgoing), 2),
            "ticket_no": str(ticket_no or ""),
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
        target.append(_row(expense.get("date"), f"Expense: {label}", party, "expense", 0, amount))

    def _balance(as_of):
        verified = _overlay_balance(str(as_of), sales, expenses, repayments)
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
                    running = round(running + _num(row.get("in")) - _num(row.get("out")), 2)
                    row["balance"] = running
                    reconciled.append(row)
                elif abs(gap) > 0.5:
                    deferred_gap = gap
            for row in day_rows:
                running = round(running + _num(row.get("in")) - _num(row.get("out")), 2)
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
            running = round(_num(opening_balance), 2)
            for row in shown:
                running = round(running + _num(row.get("in")) - _num(row.get("out")), 2)
                row["balance"] = running
            if abs(round(_num(closing_balance) - running, 2)) > 0.5:
                raise ErpFetchError(
                    f"common engine {channel} book does not tie for {from_d}..{to_d}: "
                    f"opening={_num(opening_balance):.2f}, expected_closing={_num(closing_balance):.2f}, "
                    f"gap_after_anchor={_num(closing_balance) - running:.2f}"
                )
        for row in shown:
            row.pop("_cashbook_order", None)
        return {
            "opening": round(_num(opening_balance), 2),
            "rows": shown,
            "total_in": round(sum(_num(row.get("in")) for row in shown), 2),
            "total_out": round(sum(_num(row.get("out")) for row in shown), 2),
            "settlement_roundoff": round(sum(_num(row.get("settlement_roundoff")) for row in shown), 2),
            "closing": round(running, 2),
        }

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

def build_vendor_ledgers(vendors_full, vendor_payments, full_ledgers=None):
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


def vendor_payable_due_aging(entries, payable, as_of):
    """Customer-style FIFO aging for supplier bills, anchored to Loctell payable."""
    target = round(max(_num(payable), 0.0), 2)
    result = {f"payable_due_{days}_plus": 0.0 for days in (15, 30, 45, 60)}
    result["payable_prior_ledger"] = 0.0
    if target <= 0:
        return result
    invoices, payments = [], []
    for entry in entries or []:
        entry_date = str(entry.get("date") or "")[:10]
        if not entry_date or entry_date > str(as_of):
            continue
        amount = round(_num(entry.get("credit") or entry.get("debit")), 2)
        if amount <= 0:
            continue
        if entry.get("type") == "purchase" or entry.get("vch_type") == "Purchase":
            invoices.append({"date": entry_date, "unpaid": amount})
        else:
            payments.append(amount)
    invoices.sort(key=lambda row: row["date"])
    for amount in payments:
        remaining = amount
        for invoice in invoices:
            if remaining <= 0:
                break
            applied = min(remaining, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            remaining = round(remaining - applied, 2)
    ledger_unpaid = round(sum(row["unpaid"] for row in invoices), 2)
    if ledger_unpaid > target:
        reduction = round(ledger_unpaid - target, 2)
        for invoice in invoices:
            if reduction <= 0:
                break
            applied = min(reduction, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            reduction = round(reduction - applied, 2)
    prior = round(max(target - sum(row["unpaid"] for row in invoices), 0.0), 2)
    result["payable_prior_ledger"] = prior
    as_of_date = date.fromisoformat(str(as_of))
    for days in (15, 30, 45, 60):
        cutoff = str(as_of_date - timedelta(days=days))
        due = prior + sum(row["unpaid"] for row in invoices if row["date"] <= cutoff)
        result[f"payable_due_{days}_plus"] = round(min(max(due, 0.0), target), 2)
    return result


def vendor_payable_age_buckets(entries, payable, as_of):
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
    remaining = target
    for entry_date, _index, amount in sorted(bills, reverse=True):
        if remaining <= 0:
            break
        unpaid = min(remaining, amount)
        age = max((as_of_date - date.fromisoformat(entry_date)).days, 0)
        bucket = "age_0_15" if age <= 15 else "age_16_30" if age <= 30 else "age_31_45" if age <= 45 else "age_45_plus"
        result[bucket] = round(result[bucket] + unpaid, 2)
        remaining = round(remaining - unpaid, 2)
    # A balance older than the imported ledger is explicitly old debt.
    if remaining > 0:
        result["age_45_plus"] = round(result["age_45_plus"] + remaining, 2)
    return {key: round(value, 2) for key, value in result.items()}


def vendor_rows_as_of(master_rows, balance_rows, vendor_ledgers, as_of):
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


def canonical_vendor_master(seed_rows, creditors, *, source_master=None):
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

def _format_material_sold(materials):
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

def _credit_due_15_plus_by_name(customers, all_sales, all_repayments, as_of, days=15):
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
        for receipt in receipts:
            remaining = receipt["amount"]
            for invoice in invoices:
                if remaining <= 0:
                    break
                applied = min(remaining, invoice["unpaid"])
                invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
                remaining = round(remaining - applied, 2)
        invoice_unpaid = round(sum(row["unpaid"] for row in invoices), 2)
        target = round(max(_num(customer.get("outstanding", customer.get("balance", 0.0))), 0.0), 2)
        if target < invoice_unpaid:
            extra_unpaid = round(invoice_unpaid - target, 2)
            for invoice in invoices:
                if extra_unpaid <= 0:
                    break
                reduction = min(extra_unpaid, invoice["unpaid"])
                invoice["unpaid"] = round(invoice["unpaid"] - reduction, 2)
                extra_unpaid = round(extra_unpaid - reduction, 2)
            older_unmatched = 0.0
        else:
            older_unmatched = round(target - invoice_unpaid, 2)
        overdue = round(sum(row["unpaid"] for row in invoices if row["date"] <= cutoff) + older_unmatched, 2)
        result[name] = round(min(max(overdue, 0.0), target), 2)
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
    ) if as_of else {}
    due_30_plus = _credit_due_15_plus_by_name(
        customers_full,
        aging_sales if aging_sales is not None else all_sales,
        aging_repayments if aging_repayments is not None
        else (all_repayments if all_repayments is not None else range_repayments),
        as_of,
        days=30,
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

def build_gstr1(sales_rows, name_to_gstin, exports_config, year, month):
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
    labour_rows,
    parts_rows,
    machines_rows,
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
        return [{"name": row.get("name"), "payable": row.get("payable", row.get("balance", 0.0))} for row in rows]

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
        end_creditors = creditors_as_of(end) or archive_balance_rows(
            archive_balance, "payables_rows", "payable"
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
        vendor_rows = vendor_rows_as_of(vendors_full, end_creditors, vendor_ledgers, str(end))
        receivable_rows = positive_balance_rows(customer_rows, "total_outstanding")
        payable_rows = positive_balance_rows(vendor_rows, "payable")

        # The Dashboard must not have independent balance math. Its tiles and
        # top-five lists come directly from the selected-date Customer/Vendor
        # rows written below.
        control = apply_seed_control_overrides(control, local_seed, start, end)
        summary = control.setdefault("summary", {})
        summary["receivables"] = round(sum(row["balance"] for row in receivable_rows), 2)
        summary["payables"] = round(sum(row["balance"] for row in payable_rows), 2)
        control["top_receivables"] = receivable_rows[:5]
        control["top_payables"] = payable_rows[:5]
        overlay_balance = _overlay_balance(str(end), all_sales, all_expenses, repayments)
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
        try:
            return fetch_vendor_payments(sess, creditors, sync_start, today), True
        except ErpFetchError as e:
            saved_rows = saved_vendor_payment_rows(sync_start, today)
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
        f_debtors  = pool.submit(fetch_debtors,      _clone_sess(sess), today)
        f_debtors_yest = pool.submit(fetch_debtors,  _clone_sess(sess), yesterday)
        f_creditors = pool.submit(fetch_creditors,   _clone_sess(sess), today)

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
        debtors_today, debtors_today_fresh = fetch_result_or_saved(
            f_debtors, "today debtors", today, saved_debtors_snapshot
        )
        _debtors_yest_pre, debtors_yesterday_fresh = fetch_result_or_saved(
            f_debtors_yest, "yesterday debtors", yesterday, saved_debtors_snapshot
        )
        creditors, creditors_today_fresh = fetch_result_or_saved(
            f_creditors, "today creditors", today, saved_creditors_snapshot
        )

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
        print(f"  sale splits unavailable: {_e}")

    all_sales = merge_rows_by_archive_key(archive_rows.get("sales"), fresh_sales, "sales")
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
    print(f"  {len(vendor_payments)} vendor payments")
    try:
        # Fetch the complete checked-in supplier master, not only today's
        # payable suppliers.  A settled supplier can still have a bill/payment
        # history that must remain visible in its Tally-style ledger.
        vendor_ledger_sources = canonical_vendor_master([], creditors)
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
        overlay = _overlay_balance(str(as_of), all_sales, all_expenses, all_repayments)
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
        customers_full, aging_sales, aging_repayments, today
    )
    due_30_plus = _credit_due_15_plus_by_name(
        customers_full, aging_sales, aging_repayments, today, days=30
    )
    for row in customers_full:
        row["credit_due_15_plus"] = due_15_plus.get(row.get("name", ""), 0.0)
        row["credit_due_30_plus"] = due_30_plus.get(row.get("name", ""), 0.0)
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
    vendors_full = canonical_vendor_master(seed_endpoints.get("vendors_all", []), creditors)
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
        labour_rows,
        parts_rows,
        machines_rows,
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
