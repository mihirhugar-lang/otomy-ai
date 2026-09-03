from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
from typing import List, Optional
from pydantic import BaseModel, model_validator
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time
import requests
from database import get_db, MachineReading
from zoneinfo import ZoneInfo

from routers.erp_sync import ERP_BASE, erp_auth, load_config

router = APIRouter(prefix="/api/machines", tags=["machines"])

MACHINES = ["Jaw Crusher", "Cone Crusher", "VSI", "Wheel Loader", "Hitachi Excavator"]

_ODOMETER_TARGETS = [
    ("Jaw", "JAW"),
    ("Cone", "CONE"),
    ("VSI", "VSI"),
    ("Hitachi", "HITACHI"),
    ("VMI Loader", "VMI LOADER"),
    ("Daneswary Soling Vehicles", "DANESWARY SOLING VEHICLES"),
    ("Soling Manju Machines", "SOLING MANJU MACHINES"),
    ("Water Tanker", "WATER TANKER"),
]
_FUEL_HISTORY_START = date(2026, 4, 1)
_FUEL_SPEND_TRACKING_FROM = date(2026, 9, 1)
_MACHINE_SUMMARY_CACHE_TTL_SECONDS = 60
_MACHINE_SUMMARY_MAX_STALE_SECONDS = 2 * 60
_MACHINE_SUMMARY_FAILURE_COOLDOWN_SECONDS = 60
_MACHINE_SUMMARY_CACHE: dict[tuple[str, str], dict] = {}
_MACHINE_SUMMARY_REFRESHING: set[tuple[str, str]] = set()
_MACHINE_SUMMARY_LAST_FAILURE: dict[tuple[str, str], float] = {}
_MACHINE_SUMMARY_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)


def _odometer_key(value: object) -> str:
    return " ".join(str(value or "").upper().split())


def _loctell_ist_date(value: object) -> Optional[date]:
    """Convert Loctell's ISO/UTC timestamp to its operating (IST) date."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        raw = raw.replace("Z[Etc/UTC]", "+00:00").replace("[Etc/UTC]", "+00:00")
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        return datetime.fromisoformat(raw).astimezone(ZoneInfo("Asia/Kolkata")).date()
    except ValueError:
        return None


def _erp_session() -> tuple[str, requests.Session]:
    """Open one authenticated Loctell session for an Operations refresh."""
    cfg = load_config()
    erp_base = cfg.get("erp_base", ERP_BASE)
    org, username, password = cfg.get("erp_org", ""), cfg.get("erp_username", ""), cfg.get("erp_password", "")
    if not username or not password:
        raise HTTPException(503, "Loctell credentials are not configured.")
    return erp_base, erp_auth(erp_base, org, username, password)


def _clone_erp_session(source: requests.Session) -> requests.Session:
    """Reuse the authenticated cookies for parallel read-only Loctell reports."""
    clone = requests.Session()
    clone.headers.update(source.headers)
    clone.cookies.update(source.cookies)
    return clone


def fetch_odometer_readings(
    session: Optional[requests.Session] = None,
    erp_base: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> list[dict]:
    """Read Loctell's official start/end odometers for the selected period."""
    if session is None:
        erp_base, session = _erp_session()

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    start_day = from_date or now.date()
    end_day = to_date or now.date()
    if start_day > end_day:
        raise HTTPException(400, "From date must not be after To date.")
    start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=ZoneInfo("Asia/Kolkata"))
    end_at = now if end_day == now.date() else datetime.combine(end_day, datetime.max.time(), tzinfo=ZoneInfo("Asia/Kolkata"))
    start = int(start_at.timestamp() * 1000)
    end = int(end_at.timestamp() * 1000)
    try:
        response = session.get(
            f"{erp_base}/restserver/rest/machinery/getOdometerReadingForAllVehicles/{start}/{end}",
            timeout=35,
            verify=True,
        )
        response.raise_for_status()
        source_rows = response.json()
    except Exception as exc:
        raise HTTPException(502, f"Loctell machinery readings unavailable: {exc}") from exc

    by_registration = {}
    for row in source_rows if isinstance(source_rows, list) else []:
        vehicle = row.get("vehicle") or {}
        key = _odometer_key(vehicle.get("regNumber"))
        if key:
            by_registration[key] = row

    readings = []
    for vehicle_type, registration in _ODOMETER_TARGETS:
        row = by_registration.get(registration)
        if row is None:
            readings.append({
                "vehicle_type": vehicle_type, "end_reading": None,
                "start_reading": None, "difference": None,
            })
            continue
        end_reading = float(row.get("vehicleEndReadings") or 0.0)
        start_reading = float(row.get("vehicleStartReadings") or 0.0)
        readings.append({
            "vehicle_type": vehicle_type,
            "end_reading": round(end_reading, 2),
            "start_reading": round(start_reading, 2),
            "difference": round(end_reading - start_reading, 2),
            # An all-zero Loctell row means there was no reading in that
            # period; it must not become a selected-range starting point.
            "has_reading": bool(start_reading or end_reading),
        })
    return readings


def fetch_live_odometer_readings(session: Optional[requests.Session] = None, erp_base: Optional[str] = None) -> list[dict]:
    """Backward-compatible current-day Loctell odometer read."""
    return fetch_odometer_readings(session=session, erp_base=erp_base)


def _roll_odometer_openings_from_prior_day(rows: list[dict], prior_rows: list[dict]) -> list[dict]:
    """Display each operating-day opening as the prior day's final reading.

    Loctell represents a not-yet-recorded day as an all-zero placeholder.
    That placeholder must retain a zero difference, but it must not erase the
    genuine opening reading visible to the operator at the start of the day.
    """
    prior_end = {
        str(row.get("vehicle_type") or ""): float(row["end_reading"])
        for row in prior_rows
        if row.get("has_reading") and row.get("end_reading") is not None
    }
    rolled = []
    for source in rows:
        row = dict(source)
        opening = prior_end.get(str(row.get("vehicle_type") or ""))
        if opening is None:
            rolled.append(row)
            continue
        row["start_reading"] = round(opening, 2)
        if row.get("has_reading"):
            row["difference"] = round(float(row.get("end_reading") or 0.0) - opening, 2)
        else:
            row["difference"] = 0.0
        rolled.append(row)
    return rolled


def fetch_machine_fuel_issues(session: Optional[requests.Session] = None, erp_base: Optional[str] = None) -> list[dict]:
    """Return actual fuel issues for every configured live machine.

    Fuel is deliberately taken from Loctell's Fuel Issued report, rather than
    an expense row.  The browser totals these source rows for its selected
    Operations or Dashboard period.
    """
    if session is None:
        erp_base, session = _erp_session()

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    start = int(datetime.combine(_FUEL_HISTORY_START, datetime.min.time(), tzinfo=ZoneInfo("Asia/Kolkata")).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    url = (
        f"{erp_base}/restserver/rest/fuel/getFuelIssuedReportWithPagination/"
        f"{start}/{end}/-1/-1/0/-1/-1/-1/-1/-1"
    )
    try:
        response = session.get(
            url, params={"page": 0, "size": 2000}, timeout=35, verify=True
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise HTTPException(502, f"Loctell fuel issues unavailable: {exc}") from exc

    vehicle_type_by_registration = {
        _odometer_key(registration): vehicle_type
        for vehicle_type, registration in _ODOMETER_TARGETS
    }
    result = []
    for row in (payload.get("data", []) if isinstance(payload, dict) else []):
        vehicle = row.get("vehicle") or {}
        vehicle_type = vehicle_type_by_registration.get(_odometer_key(vehicle.get("regNumber")))
        if vehicle_type is None:
            continue
        issued_on = _loctell_ist_date(row.get("createdDate"))
        if issued_on is None:
            continue
        try:
            liters = abs(float(row.get("qty") or 0.0))
        except (TypeError, ValueError):
            continue
        try:
            fuel_issue_reading = float(row.get("odometerReading")) if row.get("odometerReading") is not None else None
        except (TypeError, ValueError):
            fuel_issue_reading = None
        result.append({
            "date": issued_on.isoformat(),
            "issued_at": str(row.get("createdDate") or ""),
            "vehicle_type": vehicle_type,
            "fuel_issued": round(liters, 2),
            "fuel_issue_reading": round(fuel_issue_reading, 2) if fuel_issue_reading is not None else None,
            "fuel_type": {1: "DIESEL", 2: "PETROL"}.get(row.get("fuelType"), "DIESEL"),
            "remarks": str(row.get("remarks") or "").strip(),
        })
    return sorted(result, key=lambda row: (row["date"], row["issued_at"]))


def fetch_fuel_received(session: Optional[requests.Session] = None, erp_base: Optional[str] = None) -> list[dict]:
    """Fuel received from Loctell's supplier-wise Fuel Received report this FY."""
    if session is None:
        erp_base, session = _erp_session()

    tz = ZoneInfo("Asia/Kolkata")
    now = datetime.now(tz)
    start = int(datetime.combine(_FUEL_HISTORY_START, datetime.min.time(), tzinfo=tz).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    url = (
        f"{erp_base}/restserver/rest/fuel/getFuelReceivedReportsWithPagination/"
        f"{start}/{end}/-1/0/-1/-1/-1/-1"
    )
    try:
        response = session.get(
            url, params={"page": 0, "size": 2000}, timeout=35, verify=True
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise HTTPException(502, f"Loctell fuel received report unavailable: {exc}") from exc

    result = []
    for row in (payload.get("data", []) if isinstance(payload, dict) else []):
        received_on = _loctell_ist_date(row.get("createdDate"))
        if received_on is None:
            continue
        try:
            received_at = str(row.get("createdDate") or "").replace("Z[Etc/UTC]", "+00:00").replace("[Etc/UTC]", "+00:00")
            if received_at.endswith("Z"):
                received_at = f"{received_at[:-1]}+00:00"
            received_label = datetime.fromisoformat(received_at).astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            received_label = received_on.isoformat()
        qty = float(row.get("qty") or 0.0)
        rate = float(row.get("rate") or 0.0)
        result.append({
            "date": received_on.isoformat(),
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


def fetch_fuel_dashboard_balance(session: Optional[requests.Session] = None, erp_base: Optional[str] = None) -> dict:
    """Read Loctell Fuel Dashboard's authoritative current diesel stock."""
    if session is None:
        erp_base, session = _erp_session()
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    start = now - timedelta(days=6)  # Matches Loctell Fuel Dashboard's default view.
    url = (
        f"{erp_base}/restserver/rest/fuel/getFuelDashboardData/"
        f"{int(start.timestamp() * 1000)}/{int(now.timestamp() * 1000)}"
    )
    try:
        response = session.get(url, timeout=35, verify=True)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise HTTPException(502, f"Loctell fuel dashboard balance unavailable: {exc}") from exc
    stock_rows = payload.get("stockData", []) if isinstance(payload, dict) else []
    diesel_litres = round(sum(float((row or {}).get("diesel") or 0.0) for row in stock_rows), 2)
    return {
        "diesel_litres": diesel_litres,
        "as_of": now.isoformat(),
        "source": "Loctell Fuel Dashboard",
        "spend_tracking_from": _FUEL_SPEND_TRACKING_FROM.isoformat(),
    }


def fuel_balance_with_value(balance: dict, fuel_received: list[dict]) -> dict:
    """Value ERP stock at the latest official diesel receipt rate, transparently."""
    result = dict(balance or {})
    latest = next((row for row in fuel_received or [] if row.get("fuel_type") == "DIESEL" and float(row.get("unit_price") or 0) > 0), None)
    if latest is None:
        result.update({"diesel_unit_price": None, "diesel_value": None, "price_as_of": None})
        return result
    rate = round(float(latest["unit_price"]), 2)
    result.update({
        "diesel_unit_price": rate,
        "diesel_value": round(float(result.get("diesel_litres") or 0) * rate, 2),
        "price_as_of": latest.get("date"),
    })
    return result


def _fetch_operations_machine_summary_live(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """One browser request and one Loctell login for all Operations live blocks.

    The upstream reports remain separate Loctell sources, but cloned
    authenticated sessions let them run concurrently without three logins.
    """
    erp_base, session = _erp_session()
    # The 6 AM operating-day opening is the prior day's final measured
    # odometer.  Fetch it alongside the selected period with the same
    # authenticated session, so the browser never invents an opening reading.
    tz = ZoneInfo("Asia/Kolkata")
    selected_start = from_date or datetime.now(tz).date()
    prior_day = selected_start - timedelta(days=1)
    with ThreadPoolExecutor(max_workers=5) as pool:
        odometer = pool.submit(fetch_odometer_readings, _clone_erp_session(session), erp_base, from_date, to_date)
        prior_odometer = pool.submit(fetch_odometer_readings, _clone_erp_session(session), erp_base, prior_day, prior_day)
        fuel_issued = pool.submit(fetch_machine_fuel_issues, _clone_erp_session(session), erp_base)
        fuel_received = pool.submit(fetch_fuel_received, _clone_erp_session(session), erp_base)
        fuel_balance = pool.submit(fetch_fuel_dashboard_balance, _clone_erp_session(session), erp_base)
        current_odometer = odometer.result()
        previous_odometer = prior_odometer.result()
        fuel_received_rows = fuel_received.result()
        return {
            "odometer": _roll_odometer_openings_from_prior_day(current_odometer, previous_odometer),
            "prior_odometer": previous_odometer,
            "fuel_issued": fuel_issued.result(),
            "fuel_received": fuel_received_rows,
            "fuel_balance": fuel_balance_with_value(fuel_balance.result(), fuel_received_rows),
        }


def _machine_summary_key(from_date: Optional[date], to_date: Optional[date]) -> tuple[str, str]:
    return (
        from_date.isoformat() if from_date else "today",
        to_date.isoformat() if to_date else "today",
    )


def _machine_summary_response(data: dict, *, refreshing: bool, cached: bool, age_seconds: float) -> dict:
    """Add transport status without altering the Loctell source rows."""
    return {
        **data,
        "refreshing": refreshing,
        "cached": cached,
        "cache_age_seconds": round(max(age_seconds, 0.0)),
    }


def _configured_odometer_placeholders() -> list[dict]:
    """Keep the complete configured fleet visible during a cold ERP refresh.

    These are display-only unknown readings, never invented meter values.
    Fuel rows deliberately remain empty until Loctell returns genuine issues.
    """
    return [
        {
            "vehicle_type": vehicle_type,
            "end_reading": None,
            "start_reading": None,
            "difference": None,
            "has_reading": False,
        }
        for vehicle_type, _registration in _ODOMETER_TARGETS
    ]


def _refresh_machine_summary(key: tuple[str, str], from_date: Optional[date], to_date: Optional[date]) -> None:
    try:
        data = _fetch_operations_machine_summary_live(from_date=from_date, to_date=to_date)
        with _MACHINE_SUMMARY_LOCK:
            _MACHINE_SUMMARY_CACHE[key] = {"ts": time.monotonic(), "data": data}
            _MACHINE_SUMMARY_LAST_FAILURE.pop(key, None)
    except Exception as exc:  # The browser must never wait on a slow Loctell retry.
        _LOG.warning("Loctell Operations refresh failed for %s to %s: %s", key[0], key[1], exc)
        with _MACHINE_SUMMARY_LOCK:
            _MACHINE_SUMMARY_LAST_FAILURE[key] = time.monotonic()
    finally:
        with _MACHINE_SUMMARY_LOCK:
            _MACHINE_SUMMARY_REFRESHING.discard(key)


def _start_machine_summary_refresh(key: tuple[str, str], from_date: Optional[date], to_date: Optional[date]) -> bool:
    now = time.monotonic()
    with _MACHINE_SUMMARY_LOCK:
        last_failure = _MACHINE_SUMMARY_LAST_FAILURE.get(key)
        if key in _MACHINE_SUMMARY_REFRESHING or (
            last_failure is not None and now - last_failure < _MACHINE_SUMMARY_FAILURE_COOLDOWN_SECONDS
        ):
            return False
        _MACHINE_SUMMARY_REFRESHING.add(key)
    threading.Thread(
        target=_refresh_machine_summary,
        args=(key, from_date, to_date),
        daemon=True,
        name=f"loctell-machines-{key[0]}-{key[1]}",
    ).start()
    return True


def fetch_operations_machine_summary(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict:
    """Refresh Operations data every 60 sec without blocking on Loctell.

    Source rows remain read-only and are replaced only after a complete
    successful refresh. A cold server returns immediately with a loading state;
    a reading older than two minutes is hidden rather than shown as current.
    """
    key = _machine_summary_key(from_date, to_date)
    now = time.monotonic()
    with _MACHINE_SUMMARY_LOCK:
        entry = _MACHINE_SUMMARY_CACHE.get(key)
        age_seconds = now - entry["ts"] if entry is not None else None
        fresh = age_seconds is not None and age_seconds < _MACHINE_SUMMARY_CACHE_TTL_SECONDS
        is_refreshing = key in _MACHINE_SUMMARY_REFRESHING

    if fresh:
        return _machine_summary_response(entry["data"], refreshing=False, cached=True, age_seconds=age_seconds)

    started = _start_machine_summary_refresh(key, from_date, to_date)
    if entry is not None and age_seconds is not None and age_seconds < _MACHINE_SUMMARY_MAX_STALE_SECONDS:
        return _machine_summary_response(
            entry["data"], refreshing=started or is_refreshing, cached=True, age_seconds=age_seconds
        )

    return {
        "odometer": _configured_odometer_placeholders(),
        "prior_odometer": [],
        "fuel_issued": [],
        "fuel_received": [],
        "fuel_balance": {
            "spend_tracking_from": _FUEL_SPEND_TRACKING_FROM.isoformat(),
        },
        "refreshing": started or is_refreshing,
        "cached": False,
        "cache_age_seconds": None,
    }


class MachineReadingIn(BaseModel):
    date: date
    machine_name: str
    start_hours: float
    end_hours: float
    production_mt: float = 0.0
    fuel_liters: float = 0.0
    notes: Optional[str] = ""

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def coerce_none_strings(cls, values):
        if isinstance(values, dict):
            for k, v in values.items():
                if v is None:
                    values[k] = ""
        return values


class MachineReadingOut(MachineReadingIn):
    id: int
    running_hours: float


@router.get("/odometer")
def live_odometer_readings(from_date: Optional[date] = None, to_date: Optional[date] = None):
    return fetch_odometer_readings(from_date=from_date, to_date=to_date)


@router.get("/fuel-issued")
def vmi_loader_fuel_issued():
    return fetch_machine_fuel_issues()


@router.get("/fuel-balance")
def fuel_dashboard_balance():
    erp_base, session = _erp_session()
    receipts = fetch_fuel_received(session=_clone_erp_session(session), erp_base=erp_base)
    return fuel_balance_with_value(fetch_fuel_dashboard_balance(session=session, erp_base=erp_base), receipts)


@router.get("/fuel-received")
def fuel_received():
    return fetch_fuel_received()


@router.get("/summary")
def operations_machine_summary(from_date: Optional[date] = None, to_date: Optional[date] = None):
    return fetch_operations_machine_summary(from_date=from_date, to_date=to_date)


@router.post("/", response_model=MachineReadingOut)
def create_reading(reading: MachineReadingIn, db: Session = Depends(get_db)):
    running = round(reading.end_hours - reading.start_hours, 2)
    if running < 0:
        raise HTTPException(400, "End hours must be greater than start hours")
    db_r = MachineReading(**reading.model_dump(), running_hours=running)
    db.add(db_r)
    db.commit()
    db.refresh(db_r)
    return db_r


@router.get("/", response_model=List[MachineReadingOut])
def list_readings(date_filter: Optional[date] = None, from_date: Optional[date] = None, to_date: Optional[date] = None, db: Session = Depends(get_db)):
    q = db.query(MachineReading)
    if from_date:
        q = q.filter(MachineReading.date >= from_date)
    if to_date:
        q = q.filter(MachineReading.date <= to_date)
    if date_filter and not from_date and not to_date:
        q = q.filter(MachineReading.date == date_filter)
    return q.order_by(MachineReading.date.desc()).all()


@router.get("/summary")
def machines_summary(date_filter: date, db: Session = Depends(get_db)):
    rows = db.query(MachineReading).filter(MachineReading.date == date_filter).all()
    by_machine = {}
    for r in rows:
        by_machine[r.machine_name] = {
            "running_hours": r.running_hours,
            "production_mt": r.production_mt,
            "fuel_liters": r.fuel_liters,
        }
    total_fuel = sum(r.fuel_liters for r in rows)
    total_production = sum(r.production_mt for r in rows)
    return {"by_machine": by_machine, "total_fuel_liters": total_fuel, "total_production_mt": total_production}


@router.delete("/{reading_id}")
def delete_reading(reading_id: int, db: Session = Depends(get_db)):
    r = db.query(MachineReading).filter(MachineReading.id == reading_id).first()
    if not r:
        raise HTTPException(404, "Reading not found")
    db.delete(r)
    db.commit()
    return {"ok": True}
