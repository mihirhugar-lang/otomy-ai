from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator
from database import get_db, Worker, Labour

router = APIRouter(prefix="/api/workers", tags=["workers"])


class WorkerIn(BaseModel):
    name: str
    worker_type: str
    daily_wage: float
    active: bool = True

    class Config:
        from_attributes = True


class WorkerOut(WorkerIn):
    id: int


class MusterEntry(BaseModel):
    worker_id: int
    days: float = 1.0
    present: bool = True
    notes: Optional[str] = ""

    class Config:
        from_attributes = True


@router.get("/", response_model=List[WorkerOut])
def list_workers(active_only: bool = True, db: Session = Depends(get_db)):
    q = db.query(Worker)
    if active_only:
        q = q.filter(Worker.active == True)
    return q.order_by(Worker.worker_type, Worker.name).all()


@router.post("/", response_model=WorkerOut)
def create_worker(worker: WorkerIn, db: Session = Depends(get_db)):
    existing = db.query(Worker).filter(Worker.name == worker.name).first()
    if existing:
        raise HTTPException(400, f"Worker '{worker.name}' already exists")
    db_w = Worker(**worker.model_dump())
    db.add(db_w)
    db.commit()
    db.refresh(db_w)
    return db_w


@router.patch("/{worker_id}")
def update_worker(worker_id: int, worker: WorkerIn, db: Session = Depends(get_db)):
    w = db.query(Worker).filter(Worker.id == worker_id).first()
    if not w:
        raise HTTPException(404, "Worker not found")
    for k, v in worker.model_dump().items():
        setattr(w, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/{worker_id}")
def deactivate_worker(worker_id: int, db: Session = Depends(get_db)):
    w = db.query(Worker).filter(Worker.id == worker_id).first()
    if not w:
        raise HTTPException(404, "Worker not found")
    w.active = False
    db.commit()
    return {"ok": True}


@router.post("/muster")
def save_muster(muster_date: str, entries: List[MusterEntry], db: Session = Depends(get_db)):
    """Save a full day's muster from the worker registry. Replaces any existing labour for that date."""
    d = date.fromisoformat(muster_date)

    # Remove existing labour entries for this date (to allow re-submission)
    db.query(Labour).filter(Labour.date == d).delete()
    db.commit()

    worker_ids = [e.worker_id for e in entries if e.present]
    workers_map = {w.id: w for w in db.query(Worker).filter(Worker.id.in_(worker_ids)).all()}

    saved = []
    for entry in entries:
        if not entry.present:
            continue
        w = workers_map.get(entry.worker_id)
        if not w:
            continue
        amount = round(entry.days * w.daily_wage, 2)
        labour = Labour(
            date=d,
            worker_name=w.name,
            worker_type=w.worker_type,
            days=entry.days,
            daily_wage=w.daily_wage,
            amount=amount,
            paid=False,
            notes=entry.notes or "",
        )
        db.add(labour)
        saved.append(w.name)

    db.commit()
    return {"saved": len(saved), "workers": saved}
