from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator
from database import get_db, Expense

router = APIRouter(prefix="/api/expenses", tags=["expenses"])

CATEGORIES = ["Fuel", "EMI", "Crusher Repair", "Vehicle Repair", "Blasting", "Wages", "Transport", "Electricity", "Other"]


class ExpenseIn(BaseModel):
    date: date
    category: str
    description: str
    amount: float
    payment_mode: str = "Cash"
    notes: Optional[str] = ""
    vendor_id: Optional[int] = None

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


class ExpenseOut(ExpenseIn):
    id: int
    erp_synced: Optional[bool] = False


@router.post("/", response_model=ExpenseOut)
def create_expense(expense: ExpenseIn, db: Session = Depends(get_db)):
    db_exp = Expense(**expense.model_dump())
    db.add(db_exp)
    db.commit()
    db.refresh(db_exp)
    return db_exp


@router.get("/", response_model=List[ExpenseOut])
def list_expenses(date_filter: Optional[date] = None, from_date: Optional[date] = None, to_date: Optional[date] = None, db: Session = Depends(get_db)):
    q = db.query(Expense)
    if from_date:
        q = q.filter(Expense.date >= from_date)
    if to_date:
        q = q.filter(Expense.date <= to_date)
    if date_filter and not from_date and not to_date:
        q = q.filter(Expense.date == date_filter)
    return q.order_by(Expense.date.desc(), Expense.id.desc()).all()


@router.get("/summary")
def expenses_summary(date_filter: date, db: Session = Depends(get_db)):
    rows = db.query(Expense).filter(Expense.date == date_filter).all()
    total = sum(r.amount for r in rows)
    by_category = {}
    for r in rows:
        by_category[r.category] = by_category.get(r.category, 0) + r.amount
    return {"total": total, "by_category": by_category, "transactions": len(rows)}


@router.delete("/{expense_id}")
def delete_expense(expense_id: int, db: Session = Depends(get_db)):
    e = db.query(Expense).filter(Expense.id == expense_id).first()
    if not e:
        raise HTTPException(404, "Expense not found")
    db.delete(e)
    db.commit()
    return {"ok": True}
