from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator
from database import get_db, EMIRecord

router = APIRouter(prefix="/api/emi", tags=["emi"])

EMI_MACHINES = ["Hitachi Excavator", "Wheel Loader"]


class EMIIn(BaseModel):
    machine_name: str
    emi_month: str       # YYYY-MM
    emi_amount: float
    due_date: date
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


class EMIOut(EMIIn):
    id: int
    paid_date: Optional[date] = None
    status: str


@router.get("/", response_model=List[EMIOut])
def list_emi(db: Session = Depends(get_db)):
    return db.query(EMIRecord).order_by(EMIRecord.emi_month.desc(), EMIRecord.machine_name).all()


@router.get("/alerts")
def emi_alerts(db: Session = Depends(get_db)):
    today = date.today()
    all_emi = db.query(EMIRecord).filter(EMIRecord.status != "Paid").all()
    overdue = [e for e in all_emi if e.due_date and e.due_date < today]
    due_soon = [e for e in all_emi if e.due_date and today <= e.due_date <= date(today.year, today.month + 1 if today.month < 12 else 1, today.day)]
    return {
        "overdue": [{"id": e.id, "machine": e.machine_name, "month": e.emi_month, "amount": e.emi_amount, "due": str(e.due_date)} for e in overdue],
        "due_soon": [{"id": e.id, "machine": e.machine_name, "month": e.emi_month, "amount": e.emi_amount, "due": str(e.due_date)} for e in due_soon],
        "total_monthly_emi": sum(e.emi_amount for e in db.query(EMIRecord).all() if e.emi_month == date.today().strftime("%Y-%m")),
    }


@router.post("/", response_model=EMIOut)
def create_emi(emi: EMIIn, db: Session = Depends(get_db)):
    existing = db.query(EMIRecord).filter(
        EMIRecord.machine_name == emi.machine_name,
        EMIRecord.emi_month == emi.emi_month
    ).first()
    if existing:
        raise HTTPException(400, f"EMI for {emi.machine_name} in {emi.emi_month} already exists")
    db_e = EMIRecord(**emi.model_dump(), status="Pending")
    db.add(db_e)
    db.commit()
    db.refresh(db_e)
    return db_e


@router.patch("/{emi_id}/pay")
def mark_emi_paid(emi_id: int, paid_date: Optional[str] = None, db: Session = Depends(get_db)):
    e = db.query(EMIRecord).filter(EMIRecord.id == emi_id).first()
    if not e:
        raise HTTPException(404, "EMI record not found")
    e.status = "Paid"
    e.paid_date = date.fromisoformat(paid_date) if paid_date else date.today()
    db.commit()
    return {"ok": True, "paid_date": str(e.paid_date)}


@router.delete("/{emi_id}")
def delete_emi(emi_id: int, db: Session = Depends(get_db)):
    e = db.query(EMIRecord).filter(EMIRecord.id == emi_id).first()
    if not e:
        raise HTTPException(404, "EMI record not found")
    db.delete(e)
    db.commit()
    return {"ok": True}
