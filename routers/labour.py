from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator
from database import get_db, Labour

router = APIRouter(prefix="/api/labour", tags=["labour"])

WORKER_TYPES = ["Operator", "Helper", "Driver", "Blasting", "Watchman", "Supervisor", "Other"]


class LabourIn(BaseModel):
    date: date
    worker_name: str
    worker_type: str
    days: float = 1.0
    daily_wage: float
    paid: bool = False
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


class LabourOut(LabourIn):
    id: int
    amount: float


@router.post("/", response_model=LabourOut)
def create_labour(labour: LabourIn, db: Session = Depends(get_db)):
    amount = round(labour.days * labour.daily_wage, 2)
    db_l = Labour(**labour.model_dump(), amount=amount)
    db.add(db_l)
    db.commit()
    db.refresh(db_l)
    return db_l


@router.get("/", response_model=List[LabourOut])
def list_labour(date_filter: Optional[date] = None, from_date: Optional[date] = None, to_date: Optional[date] = None, db: Session = Depends(get_db)):
    q = db.query(Labour)
    if from_date:
        q = q.filter(Labour.date >= from_date)
    if to_date:
        q = q.filter(Labour.date <= to_date)
    if date_filter and not from_date and not to_date:
        q = q.filter(Labour.date == date_filter)
    return q.order_by(Labour.date.desc()).all()


@router.get("/summary")
def labour_summary(date_filter: date, db: Session = Depends(get_db)):
    rows = db.query(Labour).filter(Labour.date == date_filter).all()
    total_amount = sum(r.amount for r in rows)
    paid_amount = sum(r.amount for r in rows if r.paid)
    unpaid_amount = total_amount - paid_amount
    by_type = {}
    for r in rows:
        if r.worker_type not in by_type:
            by_type[r.worker_type] = {"count": 0, "amount": 0}
        by_type[r.worker_type]["count"] += 1
        by_type[r.worker_type]["amount"] += r.amount
    return {
        "total_workers": len(rows),
        "total_amount": total_amount,
        "paid_amount": paid_amount,
        "unpaid_amount": unpaid_amount,
        "by_type": by_type,
    }


@router.patch("/{labour_id}/paid")
def mark_paid(labour_id: int, db: Session = Depends(get_db)):
    l = db.query(Labour).filter(Labour.id == labour_id).first()
    if not l:
        raise HTTPException(404, "Record not found")
    l.paid = True
    db.commit()
    return {"ok": True}


@router.delete("/{labour_id}")
def delete_labour(labour_id: int, db: Session = Depends(get_db)):
    l = db.query(Labour).filter(Labour.id == labour_id).first()
    if not l:
        raise HTTPException(404, "Record not found")
    db.delete(l)
    db.commit()
    return {"ok": True}
