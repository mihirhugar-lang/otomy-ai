from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, field_validator
from database import get_db, Sale

router = APIRouter(prefix="/api/sales", tags=["sales"])

MATERIALS = ["40mm", "20mm", "12mm", "6mm", "M-Sand", "P-Sand", "Dust", "Mixed"]
PAYMENT_MODES = ["Cash", "Credit", "UPI", "Cheque"]


class SaleIn(BaseModel):
    date: date
    customer_name: str
    material: str
    qty_mt: float
    rate_per_mt: float
    payment_mode: str = "Credit"
    vehicle_no: Optional[str] = ""
    notes: Optional[str] = ""
    customer_id: Optional[int] = None
    ticket_no: Optional[str] = ""
    hsn_code: Optional[str] = "2517"
    gst_rate: Optional[float] = 5.0
    mdp_ton: Optional[float] = None

    class Config:
        from_attributes = True

    @field_validator("vehicle_no", "notes", "ticket_no", "hsn_code", mode="before")
    @classmethod
    def none_to_empty(cls, v):
        return v or ""


class SaleOut(SaleIn):
    id: int
    amount: float
    erp_synced: Optional[bool] = False


@router.post("/", response_model=SaleOut)
def create_sale(sale: SaleIn, db: Session = Depends(get_db)):
    amount = round(sale.qty_mt * sale.rate_per_mt, 2)
    db_sale = Sale(**sale.model_dump(), amount=amount)
    db.add(db_sale)
    db.commit()
    db.refresh(db_sale)
    return db_sale


@router.get("/", response_model=List[SaleOut])
def list_sales(date_filter: Optional[date] = None, from_date: Optional[date] = None, to_date: Optional[date] = None, db: Session = Depends(get_db)):
    q = db.query(Sale)
    if from_date:
        q = q.filter(Sale.date >= from_date)
    if to_date:
        q = q.filter(Sale.date <= to_date)
    if date_filter and not from_date and not to_date:
        q = q.filter(Sale.date == date_filter)
    return q.order_by(Sale.date.desc(), Sale.id.desc()).all()


@router.get("/summary")
def sales_summary(date_filter: date, db: Session = Depends(get_db)):
    rows = db.query(Sale).filter(Sale.date == date_filter).all()
    total_amount = sum(r.amount for r in rows)
    cash_amount = sum(r.amount for r in rows if r.payment_mode == "Cash")
    credit_amount = sum(r.amount for r in rows if r.payment_mode != "Cash")
    by_material = {}
    for r in rows:
        if r.material not in by_material:
            by_material[r.material] = {"qty_mt": 0, "amount": 0}
        by_material[r.material]["qty_mt"] += r.qty_mt
        by_material[r.material]["amount"] += r.amount
    return {
        "total_amount": total_amount,
        "cash_amount": cash_amount,
        "credit_amount": credit_amount,
        "total_qty_mt": sum(r.qty_mt for r in rows),
        "by_material": by_material,
        "transactions": len(rows),
    }


class SalePatch(BaseModel):
    date: Optional[date] = None
    customer_name: Optional[str] = None
    material: Optional[str] = None
    qty_mt: Optional[float] = None
    rate_per_mt: Optional[float] = None
    payment_mode: Optional[str] = None
    vehicle_no: Optional[str] = None
    notes: Optional[str] = None
    customer_id: Optional[int] = None
    ticket_no: Optional[str] = None


@router.patch("/{sale_id}", response_model=SaleOut)
def update_sale(sale_id: int, patch: SalePatch, db: Session = Depends(get_db)):
    s = db.query(Sale).filter(Sale.id == sale_id).first()
    if not s:
        raise HTTPException(404, "Sale not found")
    updates = patch.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(s, field, value)
    if "qty_mt" in updates or "rate_per_mt" in updates:
        s.amount = round((s.qty_mt or 0) * (s.rate_per_mt or 0), 2)
    db.commit()
    db.refresh(s)
    return s


@router.delete("/{sale_id}")
def delete_sale(sale_id: int, db: Session = Depends(get_db)):
    s = db.query(Sale).filter(Sale.id == sale_id).first()
    if not s:
        raise HTTPException(404, "Sale not found")
    db.delete(s)
    db.commit()
    return {"ok": True}
