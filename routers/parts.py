from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator
from database import get_db, Part

router = APIRouter(prefix="/api/parts", tags=["parts"])


class PartIn(BaseModel):
    date: date
    machine_name: str
    part_name: str
    quantity: float = 1.0
    unit_price: float
    supplier: Optional[str] = ""
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


class PartOut(PartIn):
    id: int
    total_amount: float


@router.post("/", response_model=PartOut)
def create_part(part: PartIn, db: Session = Depends(get_db)):
    total = round(part.quantity * part.unit_price, 2)
    db_p = Part(**part.model_dump(), total_amount=total)
    db.add(db_p)
    db.commit()
    db.refresh(db_p)
    return db_p


@router.get("/", response_model=List[PartOut])
def list_parts(date_filter: Optional[date] = None, from_date: Optional[date] = None, to_date: Optional[date] = None, db: Session = Depends(get_db)):
    q = db.query(Part)
    if from_date:
        q = q.filter(Part.date >= from_date)
    if to_date:
        q = q.filter(Part.date <= to_date)
    if date_filter and not from_date and not to_date:
        q = q.filter(Part.date == date_filter)
    return q.order_by(Part.date.desc()).all()


@router.get("/summary")
def parts_summary(date_filter: date, db: Session = Depends(get_db)):
    rows = db.query(Part).filter(Part.date == date_filter).all()
    total = sum(r.total_amount for r in rows)
    by_machine = {}
    for r in rows:
        by_machine[r.machine_name] = by_machine.get(r.machine_name, 0) + r.total_amount
    return {"total": total, "by_machine": by_machine, "items": len(rows)}


@router.delete("/{part_id}")
def delete_part(part_id: int, db: Session = Depends(get_db)):
    p = db.query(Part).filter(Part.id == part_id).first()
    if not p:
        raise HTTPException(404, "Record not found")
    db.delete(p)
    db.commit()
    return {"ok": True}
