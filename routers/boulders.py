from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator
from database import get_db, BoulderInput

router = APIRouter(prefix="/api/boulders", tags=["boulders"])


class BoulderIn(BaseModel):
    date: date
    trips: int
    tonnes_per_trip: float
    source: Optional[str] = "Quarry Face"
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


class BoulderOut(BoulderIn):
    id: int
    total_tonnes: float


@router.post("/", response_model=BoulderOut)
def create_boulder_input(boulder: BoulderIn, db: Session = Depends(get_db)):
    total = round(boulder.trips * boulder.tonnes_per_trip, 2)
    db_b = BoulderInput(**boulder.model_dump(), total_tonnes=total)
    db.add(db_b)
    db.commit()
    db.refresh(db_b)
    return db_b


@router.get("/", response_model=List[BoulderOut])
def list_boulder_inputs(date_filter: Optional[date] = None, from_date: Optional[date] = None, to_date: Optional[date] = None, db: Session = Depends(get_db)):
    q = db.query(BoulderInput)
    if from_date:
        q = q.filter(BoulderInput.date >= from_date)
    if to_date:
        q = q.filter(BoulderInput.date <= to_date)
    if date_filter and not from_date and not to_date:
        q = q.filter(BoulderInput.date == date_filter)
    return q.order_by(BoulderInput.date.desc()).all()


@router.get("/summary")
def boulder_summary(date_filter: date, db: Session = Depends(get_db)):
    rows = db.query(BoulderInput).filter(BoulderInput.date == date_filter).all()
    return {
        "total_trips": sum(r.trips for r in rows),
        "total_tonnes": sum(r.total_tonnes for r in rows),
    }


@router.delete("/{boulder_id}")
def delete_boulder(boulder_id: int, db: Session = Depends(get_db)):
    b = db.query(BoulderInput).filter(BoulderInput.id == boulder_id).first()
    if not b:
        raise HTTPException(404, "Record not found")
    db.delete(b)
    db.commit()
    return {"ok": True}
