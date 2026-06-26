from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator
from database import get_db, MachineReading

router = APIRouter(prefix="/api/machines", tags=["machines"])

MACHINES = ["Jaw Crusher", "Cone Crusher", "VSI", "Wheel Loader", "Hitachi Excavator"]


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
