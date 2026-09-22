"""Loctell read requests, parsing, source identity and repayment extraction.

Runtime settings and helper callbacks are explicit keyword-only dependencies.
The gha_sync entry point supplies them, preserving its existing public interface
and isolated importlib loaders used by localhost and repair tools.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import base64, json, re, html as htmllib, os, sys, time
import requests


def erp_auth(
    *,
    ERP_BASE,
    ERP_FETCH_RETRIES,
    ERP_ORG,
    ERP_PASS,
    ERP_RETRY_DELAY_SECONDS,
    ERP_USER,
    ErpFetchError,
):
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


def _fetch_sales_window(
    sess,
    from_d,
    to_d,
    *,
    ERP_BASE,
    ErpFetchError,
    _PAY,
    _TD,
    _TR,
    _channels_for_payment_mode,
    _clean,
    _norm_material,
    _norm_pay,
    _num,
    _request_text_with_retry,
):
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


def fetch_sales(sess, from_d, to_d, *, _clone_sess, _fetch_sales_window, _sales_fetch_windows):
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


def fetch_sale_splits(sess, from_d, to_d, *, ERP_BASE, _clone_sess, _parse_listsale_splits):
    """{(date, ticket_no): {cash, credit, upi, total, pay_type}} from ERP ListSale.

    Captures real SPLIT payments (part cash + part UPI) that ListCustomerWiseReport
    collapses into one payment mode.  Ticket numbers are not globally unique
    (for example, 10086 occurs on 26-May and 30-Jun), so date is part of the
    identity. The ListSale layout is a financial source, so a failed fetch or
    header validation aborts the build rather than silently publishing a bad
    MDP/payment split.
    """
    splits = {}
    errors = []
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
            splits.update(_parse_listsale_splits(html, cur))
        except Exception as e:
            errors.append(f"{ds}: {e}")
        cur += timedelta(days=1)
        time.sleep(0.1)
    if errors:
        raise RuntimeError("ListSale split fetch/validation failed; build stopped: " + "; ".join(errors[:3]))
    return splits


def _cash_row_is_bank_expense(row, bank_expenses, *, _num, _payment_channel):
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


def fetch_expenses(
    sess,
    from_d,
    to_d,
    *,
    ERP_BASE,
    ErpFetchError,
    _clean,
    _clone_sess,
    _expense_key,
    _num,
    _request_text_with_retry,
):
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


def fetch_cash_ledger(
    sess,
    from_d,
    to_d,
    *,
    ERP_BASE,
    ErpFetchError,
    _clean,
    _num,
    _parse_date,
    _request_text_with_retry,
):
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


def fetch_bank_entries(
    sess,
    from_d,
    to_d,
    *,
    ERP_BASE,
    ErpFetchError,
    _clean,
    _num,
    _parse_date,
    _request_text_with_retry,
):
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


def fetch_internal_transfers(
    sess,
    from_d,
    to_d,
    *,
    ERP_BASE,
    ErpFetchError,
    _clean,
    _num,
    _parse_date,
    _request_text_with_retry,
):
    """Return only complete Loctell cash-to-bank contra pairs."""
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(_request_text_with_retry(
            sess, f"{ERP_BASE}/crusher/ListInternalTransfer?start={fs}&end={ts}&type=1",
            timeout=35, label=f"internal transfers {fs} to {ts}",
        ))
        cash_legs, bank_legs = [], []
        for raw_row in data.get("data", []):
            cells = [_clean(cell) for cell in raw_row]
            if not cells or "TOTAL" in cells[0].upper():
                continue
            bank_name = cells[3] if len(cells) > 3 else ""
            cash_ledger = cells[4] if len(cells) > 4 else ""
            amount = max(_num(cells[5]) if len(cells) > 5 else 0, _num(cells[6]) if len(cells) > 6 else 0)
            if amount <= 0 or (not bank_name and not cash_ledger):
                continue
            ids = [value for cell in raw_row for value in re.findall(r"\b\d{5,}\b", str(cell))]
            leg = {"date": str(_parse_date(cells[0], to_d)), "bank_name": bank_name,
                   "cash_ledger": cash_ledger, "amount": round(amount, 2),
                   "remarks": cells[7] if len(cells) > 7 else "", "ids": ids}
            (cash_legs if cash_ledger else bank_legs).append(leg)
        result, used_cash = [], set()
        for bank_leg in bank_legs:
            index = next((idx for idx, cash_leg in enumerate(cash_legs)
                if idx not in used_cash and cash_leg["date"] == bank_leg["date"]
                and abs(cash_leg["amount"] - bank_leg["amount"]) < 0.01
                # Loctell can vary punctuation/spacing between the paired legs
                # (for example "::PLANT" vs ":: PLANT").  Match the same
                # meaningful remark text, never formatting alone.
                and re.sub(r"[^A-Z0-9]+", "", cash_leg["remarks"].upper())
                    == re.sub(r"[^A-Z0-9]+", "", bank_leg["remarks"].upper())), None)
            if index is None:
                continue
            used_cash.add(index)
            cash_leg = cash_legs[index]
            ids = sorted(set(cash_leg["ids"] + bank_leg["ids"]))
            record_id = "-".join(ids) or f"{bank_leg['date']}|{cash_leg['cash_ledger']}|{bank_leg['bank_name']}|{bank_leg['amount']:.2f}|{bank_leg['remarks']}"
            result.append({"id": f"internal-transfer:{record_id}", "date": bank_leg["date"],
                           "cash_ledger": cash_leg["cash_ledger"][:100], "bank_name": bank_leg["bank_name"][:100],
                           "amount": bank_leg["amount"], "remarks": bank_leg["remarks"][:500]})
        return result
    except Exception as e:
        print(f"  internal_transfers: {e}")
        raise ErpFetchError(f"internal transfer fetch failed; skipped Otomy write: {e}") from e


def fetch_boulders(
    sess,
    from_d,
    to_d,
    *,
    ERP_BASE,
    ErpFetchError,
    _TD,
    _TR,
    _clean,
    _num,
    _request_text_with_retry,
):
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


def _odometer_key(value):
    return " ".join(str(value or "").upper().split())


def fetch_odometer_readings(
    sess,
    from_day,
    to_day,
    *,
    ERP_BASE,
    ErpFetchError,
    IST,
    _ODOMETER_TARGETS,
    _num,
    _odometer_key,
    _request_json_with_retry,
):
    """Loctell's official start/end odometers for one inclusive date range."""
    if from_day > to_day:
        raise ErpFetchError("machinery odometer range start is after its end")
    now = datetime.now(IST)
    start_at = datetime.combine(from_day, datetime.min.time(), tzinfo=IST)
    end_at = now if to_day == now.date() else datetime.combine(to_day, datetime.max.time(), tzinfo=IST)
    start = int(start_at.timestamp() * 1000)
    end = int(end_at.timestamp() * 1000)
    try:
        rows = _request_json_with_retry(
            sess,
            f"{ERP_BASE}/restserver/rest/machinery/getOdometerReadingForAllVehicles/{start}/{end}",
            timeout=35,
            label=f"machinery odometers {from_day} to {to_day}",
        )
    except Exception as exc:
        raise ErpFetchError(f"machinery odometer fetch failed: {exc}") from exc

    by_registration = {}
    for row in rows if isinstance(rows, list) else []:
        vehicle = row.get("vehicle") or {}
        key = _odometer_key(vehicle.get("regNumber"))
        if key:
            by_registration[key] = row

    result = []
    for vehicle_type, registration in _ODOMETER_TARGETS:
        row = by_registration.get(registration)
        if row is None:
            result.append({"vehicle_type": vehicle_type, "end_reading": None, "start_reading": None, "difference": None})
            continue
        end_reading = _num(row.get("vehicleEndReadings"))
        start_reading = _num(row.get("vehicleStartReadings"))
        result.append({
            "vehicle_type": vehicle_type,
            "end_reading": round(end_reading, 2),
            "start_reading": round(start_reading, 2),
            "difference": round(end_reading - start_reading, 2),
            # Loctell uses an all-zero row as a no-reading placeholder.  Keep
            # that distinction so a selected range starts at the first actual
            # reading, exactly as the Loctell report does.
            "has_reading": bool(start_reading or end_reading),
        })
    return result


def fetch_live_odometer_readings(sess, today, *, fetch_odometer_readings):
    """Backward-compatible current-day Loctell odometer read."""
    return fetch_odometer_readings(sess, today, today)


def normalize_odometer_readings(readings, *, _ODOMETER_TARGETS):
    """Keep cached history aligned when configured machinery is added later.

    A newly configured vehicle has no historical reading until Loctell returns
    one.  Represent that fact with an explicit blank row rather than rejecting
    the otherwise-valid historic five-machine records or inventing readings.
    """
    by_type = {
        str((row or {}).get("vehicle_type") or ""): dict(row)
        for row in readings or []
        if str((row or {}).get("vehicle_type") or "")
    }
    result = []
    for vehicle_type, _registration in _ODOMETER_TARGETS:
        result.append(by_type.get(vehicle_type, {
            "vehicle_type": vehicle_type,
            "end_reading": None,
            "start_reading": None,
            "difference": None,
        }))
    return result


def merge_odometer_history(existing, fresh, *, normalize_odometer_readings):
    """Replace refreshed days and normalize retained history to target machines."""
    by_day = {}
    for row in existing or []:
        day = str((row or {}).get("date") or "")[:10]
        if day:
            by_day[day] = {**dict(row), "readings": normalize_odometer_readings((row or {}).get("readings"))}
    for row in fresh or []:
        day = str((row or {}).get("date") or "")[:10]
        if day:
            by_day[day] = {**dict(row), "readings": normalize_odometer_readings((row or {}).get("readings"))}
    return [by_day[day] for day in sorted(by_day)]


def validate_odometer_history(rows, *, _ODOMETER_TARGETS, _num):
    """Reject malformed cached readings before they can reach an Otomy range view."""
    expected = {vehicle_type for vehicle_type, _registration in _ODOMETER_TARGETS}
    seen_days = set()
    for day_row in rows or []:
        day = str((day_row or {}).get("date") or "")[:10]
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError(f"invalid odometer history date: {day!r}") from exc
        if day in seen_days:
            raise ValueError(f"duplicate odometer history date: {day}")
        seen_days.add(day)
        readings = (day_row or {}).get("readings") or []
        names = [str((row or {}).get("vehicle_type") or "") for row in readings]
        if set(names) != expected or len(names) != len(expected):
            raise ValueError(f"odometer history {day} does not contain exactly the configured target machines")
        for row in readings:
            start, end, difference = row.get("start_reading"), row.get("end_reading"), row.get("difference")
            if start is None or end is None or difference is None:
                continue
            if abs((_num(end) - _num(start)) - _num(difference)) > 0.01:
                raise ValueError(f"odometer history {day} arithmetic mismatch for {row.get('vehicle_type')}")
            expected_has_reading = bool(_num(start) or _num(end))
            has_reading = row.get("has_reading")
            if has_reading is not None and has_reading is not expected_has_reading:
                raise ValueError(f"odometer history {day} reading-marker mismatch for {row.get('vehicle_type')}")


def fetch_odometer_history(
    sess,
    from_day,
    to_day,
    workers=8,
    *,
    ErpFetchError,
    _clone_sess,
    fetch_odometer_readings,
):
    """Daily Loctell odometer summaries, used client-side for any selected range.

    Loctell returns exact range start/end values but Otomy is static between
    syncs.  A compact daily series lets the browser make the same calculation
    as Loctell without one snapshot object per user-selected range.
    """
    days = []
    cursor = from_day
    while cursor <= to_day:
        days.append(cursor)
        cursor += timedelta(days=1)
    if not days:
        return []

    def fetch_day(day):
        return {"date": str(day), "readings": fetch_odometer_readings(_clone_sess(sess), day, day)}

    result, errors = [], []
    with ThreadPoolExecutor(max_workers=min(max(1, workers), len(days))) as pool:
        futures = {pool.submit(fetch_day, day): day for day in days}
        for future in as_completed(futures):
            day = futures[future]
            try:
                result.append(future.result())
            except Exception as exc:
                errors.append(f"{day}: {exc}")
    if not result and errors:
        raise ErpFetchError("machinery odometer history fetch failed: " + "; ".join(errors[:3]))
    if errors:
        print(f"  machinery odometer history partial: {len(errors)} day(s) unavailable")
    return sorted(result, key=lambda row: row["date"])


def _loctell_ist_date(value, *, IST):
    """Return the India operating date for a Loctell ISO/UTC timestamp."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        raw = raw.replace("Z[Etc/UTC]", "+00:00").replace("[Etc/UTC]", "+00:00")
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST).date()
    except ValueError:
        return None


def fetch_machine_fuel_issues(
    sess,
    financial_year_start,
    today,
    *,
    ERP_BASE,
    ErpFetchError,
    IST,
    _FUEL_ISSUE_REGISTRATION_ALIASES,
    _ODOMETER_TARGETS,
    _loctell_ist_date,
    _num,
    _odometer_key,
    _request_json_with_retry,
):
    """Actual configured-machine fuel issues from Loctell's Fuel Issued report.

    The frontend totals these source rows for the currently selected period.
    It never derives fuel from an expense row or a manual adjustment.
    """
    start = int(datetime.combine(financial_year_start, datetime.min.time(), tzinfo=IST).timestamp() * 1000)
    now = datetime.now(IST)
    end = int(now.timestamp() * 1000)
    url = (
        f"{ERP_BASE}/restserver/rest/fuel/getFuelIssuedReportWithPagination/"
        f"{start}/{end}/-1/-1/0/-1/-1/-1/-1/-1"
    )
    try:
        payload = _request_json_with_retry(
            sess, url, params={"page": 0, "size": 2000}, timeout=35,
            label=f"machine fuel issues {financial_year_start} to {today}",
        )
    except Exception as exc:
        raise ErpFetchError(f"machine fuel-issued fetch failed: {exc}") from exc

    vehicle_type_by_registration = {
        _odometer_key(registration): vehicle_type
        for vehicle_type, registration in _ODOMETER_TARGETS
    }
    vehicle_type_by_registration.update({
        _odometer_key(registration): vehicle_type
        for registration, vehicle_type in _FUEL_ISSUE_REGISTRATION_ALIASES.items()
    })
    result = []
    for row in (payload.get("data", []) if isinstance(payload, dict) else []):
        vehicle = row.get("vehicle") or {}
        vehicle_type = vehicle_type_by_registration.get(_odometer_key(vehicle.get("regNumber")))
        if vehicle_type is None:
            continue
        issued_on = _loctell_ist_date(row.get("createdDate"))
        if issued_on is None:
            continue
        result.append({
            "date": str(issued_on),
            "issued_at": str(row.get("createdDate") or ""),
            "vehicle_type": vehicle_type,
            "fuel_issued": round(abs(_num(row.get("qty"))), 2),
            "fuel_issue_reading": round(_num(row.get("odometerReading")), 2) if row.get("odometerReading") is not None else None,
            "fuel_type": {1: "DIESEL", 2: "PETROL"}.get(row.get("fuelType"), "DIESEL"),
            "remarks": str(row.get("remarks") or "").strip(),
        })
    return sorted(result, key=lambda row: (row["date"], row["issued_at"]))


def fetch_fuel_received(
    sess,
    financial_year_start,
    today,
    *,
    ERP_BASE,
    ErpFetchError,
    IST,
    _loctell_ist_date,
    _num,
    _request_json_with_retry,
):
    """Supplier-wise fuel receipts from Loctell, limited to report columns through Received By."""
    start = int(datetime.combine(financial_year_start, datetime.min.time(), tzinfo=IST).timestamp() * 1000)
    end = int(datetime.now(IST).timestamp() * 1000)
    url = (
        f"{ERP_BASE}/restserver/rest/fuel/getFuelReceivedReportsWithPagination/"
        f"{start}/{end}/-1/0/-1/-1/-1/-1"
    )
    try:
        payload = _request_json_with_retry(
            sess, url, params={"page": 0, "size": 2000}, timeout=35,
            label=f"fuel received {financial_year_start} to {today}",
        )
    except Exception as exc:
        raise ErpFetchError(f"fuel received fetch failed: {exc}") from exc

    result = []
    for row in (payload.get("data", []) if isinstance(payload, dict) else []):
        received_on = _loctell_ist_date(row.get("createdDate"))
        if received_on is None:
            continue
        try:
            received_at = str(row.get("createdDate") or "").replace("Z[Etc/UTC]", "+00:00").replace("[Etc/UTC]", "+00:00")
            if received_at.endswith("Z"):
                received_at = f"{received_at[:-1]}+00:00"
            received_label = datetime.fromisoformat(received_at).astimezone(IST).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            received_label = str(received_on)
        qty = _num(row.get("qty"))
        rate = _num(row.get("rate"))
        result.append({
            "date": str(received_on),
            "received_at": str(row.get("createdDate") or ""),
            "received_date": received_label,
            "supplier_name": row.get("supplierName") or "—",
            "camp": row.get("campName") or "—",
            "fuel_type": {1: "DIESEL", 2: "PETROL"}.get(row.get("fuelType"), "—"),
            "quantity": round(qty, 2),
            "unit_price": round(rate, 2),
            "amount": round(qty * rate, 2),
            "received_by": (row.get("createdBy") or {}).get("userFullName") or "—",
            "remarks": str(row.get("remarks") or "").strip(),
        })
    return sorted(result, key=lambda row: row["received_date"], reverse=True)


def fetch_fuel_dashboard_balance(
    sess,
    *,
    ERP_BASE,
    ErpFetchError,
    IST,
    _FUEL_SPEND_TRACKING_FROM,
    _num,
    _request_json_with_retry,
):
    """Loctell Fuel Dashboard's authoritative current diesel stock."""
    now = datetime.now(IST)
    start = now - timedelta(days=6)  # Same default range as /fuels/dashboard.
    url = (
        f"{ERP_BASE}/restserver/rest/fuel/getFuelDashboardData/"
        f"{int(start.timestamp() * 1000)}/{int(now.timestamp() * 1000)}"
    )
    try:
        payload = _request_json_with_retry(
            sess, url, timeout=35, label="fuel dashboard balance"
        )
    except Exception as exc:
        raise ErpFetchError(f"fuel dashboard balance fetch failed: {exc}") from exc
    stock_rows = payload.get("stockData", []) if isinstance(payload, dict) else []
    return {
        "diesel_litres": round(sum(_num((row or {}).get("diesel")) for row in stock_rows), 2),
        "as_of": now.isoformat(),
        "source": "Loctell Fuel Dashboard",
        "spend_tracking_from": str(_FUEL_SPEND_TRACKING_FROM),
    }


def fuel_balance_with_value(balance, fuel_received, *, _num):
    """Value ERP stock at the latest official diesel receipt rate."""
    result = dict(balance or {})
    latest = next((row for row in fuel_received or [] if row.get("fuel_type") == "DIESEL" and _num(row.get("unit_price")) > 0), None)
    if latest is None:
        result.update({"diesel_unit_price": None, "diesel_value": None, "price_as_of": None})
        return result
    rate = round(_num(latest.get("unit_price")), 2)
    result.update({
        "diesel_unit_price": rate,
        "diesel_value": round(_num(result.get("diesel_litres")) * rate, 2),
        "price_as_of": latest.get("date"),
    })
    return result


def fetch_boulder_rows(sess, from_d, to_d, *, _clone_sess, _num, fetch_boulders):
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


def fetch_iot(sess, from_d, to_d, *, ERP_BASE, _clean):
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


def fetch_debtors(sess, as_of=None, *, ERP_BASE, ErpFetchError, _clean, _num, _request_json_with_retry):
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


def fetch_creditors(sess, as_of=None, *, ERP_BASE, ErpFetchError, _clean, _num, _request_json_with_retry):
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


def fetch_vendor_payments(
    sess,
    creditors,
    from_d,
    to_d,
    *,
    ERP_BASE,
    ErpFetchError,
    _clean,
    _clone_sess,
    _mode_bucket,
    _num,
    _parse_date,
):
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


def _vendor_identity(row, *, _norm_name):
    """Stable supplier identity; display names are not unique in Loctell."""
    row = row or {}
    supplier_id = str(row.get("erp_supplier_id") or row.get("supplier_id") or "").strip()
    if supplier_id:
        return f"erp:{supplier_id}"
    return f"name:{_norm_name(row.get('name'))}"


def _customer_master_key(name):
    """Display-master identity: keep meaningful internal spacing intact."""
    return str(name or "").strip().casefold()


def canonical_customer_master_rows(rows, *, _customer_master_key):
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


def canonical_debtors_by_name(rows, *, ErpFetchError, _norm_name, _num):
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


def fetch_supplier_ledgers_full(
    sess,
    creditors,
    from_d,
    to_d,
    *,
    strict=False,
    ERP_BASE,
    ErpFetchError,
    _clean,
    _clone_sess,
    _num,
    _parse_date,
    _vendor_identity,
):
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


def fetch_customer_ledgers_full(
    sess,
    debtors,
    from_d,
    to_d,
    only_outstanding=True,
    *,
    CUST_LEDGER_WORKERS,
    ERP_BASE,
    _clean,
    _clone_sess,
    _norm_name,
    _num,
    _parse_date,
):
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
                                    "customer_name": name, "erp_customer_id": cid,
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


def _customer_identity_sale_key(row, amount_key, *, _norm_name, _num):
    """Loctell's stable sale fingerprint when ListCustomerWiseReport omits customer ID."""
    return (
        str(row.get("date") or "")[:10],
        _norm_name(row.get("material")),
        _norm_name(row.get("vehicle_no")),
        round(_num(row.get(amount_key)), 2),
    )


def reconcile_fresh_credit_sale_identities(
    sales,
    debtors,
    ledger_sales,
    *,
    _customer_identity_sale_key,
    _norm_name,
    _num,
    _sale_channels,
):
    """Resolve a renamed fresh credit-sale name only from one exact ERP ledger match.

    ListCustomerWiseReport provides a display name while the detailed customer
    ledger provides the immutable ERP customer ID.  A missing or ambiguous
    match must abort publication: allowing it would split receivables between
    an old and a renamed customer identity.
    """
    debtor_names = {_norm_name(row.get("name")) for row in debtors or []}
    pending = [
        sale for sale in sales or []
        if _num(_sale_channels(sale)[1]) > 0.005
        and _norm_name(sale.get("customer_name")) not in debtor_names
    ]
    if not pending:
        return 0
    candidates = {}
    for entry in ledger_sales or []:
        if _num(entry.get("debit")) <= 0.005:
            continue
        candidates.setdefault(_customer_identity_sale_key(entry, "debit"), []).append(entry)
    resolved, problems = 0, []
    for sale in pending:
        key = _customer_identity_sale_key(sale, "credit_amount")
        matches = candidates.get(key, [])
        source_ids = {str(row.get("erp_customer_id") or "") for row in matches if row.get("erp_customer_id")}
        if len(matches) == 1 and len(source_ids) == 1:
            sale["customer_name"] = matches[0]["customer_name"]
            sale["erp_customer_id"] = matches[0]["erp_customer_id"]
            resolved += 1
            continue
        problems.append(
            f"ticket {sale.get('ticket_no') or '?'} on {key[0]} "
            f"({sale.get('customer_name') or 'blank'}): {len(matches)} ledger matches"
        )
    if problems:
        raise RuntimeError("customer identity guard blocked R2 publish; " + "; ".join(problems[:5]))
    return resolved


def resolve_fresh_credit_sale_identities(
    sess,
    sales,
    debtors,
    from_d,
    to_d,
    *,
    _norm_name,
    _num,
    _sale_channels,
    fetch_customer_ledgers_full,
    reconcile_fresh_credit_sale_identities,
):
    """Fetch detailed ledger evidence only when a fresh credit-sale name changed."""
    debtor_names = {_norm_name(row.get("name")) for row in debtors or []}
    needs_identity = any(
        _num(_sale_channels(sale)[1]) > 0.005
        and _norm_name(sale.get("customer_name")) not in debtor_names
        for sale in sales or []
    )
    if not needs_identity:
        return 0
    ledgers = fetch_customer_ledgers_full(sess, debtors, from_d, to_d)
    ledger_sales = [
        entry for entries in ledgers.values() for entry in entries
        if entry.get("type") == "sale"
    ]
    return reconcile_fresh_credit_sale_identities(sales, debtors, ledger_sales)


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


def fetch_customer_ledger_rows(sess, from_d, to_d, erp_customer_id, *, ERP_BASE, ErpFetchError):
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


def compute_repayments_from_erp(
    sess,
    start,
    end,
    previous_debtors,
    current_debtors,
    debtors_cache=None,
    *,
    ERP_DEBTOR_WORKERS,
    EXCLUDED_CUSTOMER_RECEIPT_REFS,
    _clean,
    _clone_sess,
    _ledger_payment_channel,
    _num,
    fetch_customer_ledger_rows,
    fetch_debtors,
):
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


