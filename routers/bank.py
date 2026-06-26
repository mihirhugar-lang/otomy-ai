from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from datetime import date
from pydantic import BaseModel

from database import get_db, BankAccount, BankTransaction

router = APIRouter(prefix="/api/bank", tags=["bank"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class BankAccountIn(BaseModel):
    name: str
    account_no: Optional[str] = ""
    bank_name: Optional[str] = ""
    branch: Optional[str] = ""
    ifsc: Optional[str] = ""
    initial_balance: float = 0.0
    initial_balance_date: date
    active: bool = True


class BankAccountOut(BankAccountIn):
    id: int
    current_balance: float

    class Config:
        from_attributes = True


class BankAccountUpdate(BaseModel):
    name: Optional[str] = None
    account_no: Optional[str] = None
    bank_name: Optional[str] = None
    branch: Optional[str] = None
    ifsc: Optional[str] = None
    initial_balance: Optional[float] = None
    initial_balance_date: Optional[date] = None
    active: Optional[bool] = None


class BankTxnIn(BaseModel):
    date: date
    bank_account_id: int
    txn_type: str  # "Credit" or "Debit"
    description: str
    amount: float
    reference: Optional[str] = ""


class BankTxnOut(BankTxnIn):
    id: int
    balance_after: float

    class Config:
        from_attributes = True


class StatementEntry(BaseModel):
    date: date
    description: str
    credit: float
    debit: float
    balance: float
    reference: str


class AccountSummaryItem(BaseModel):
    name: str
    balance: float


class SummaryOut(BaseModel):
    total_balance: float
    by_account: List[AccountSummaryItem]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_balance(account: BankAccount, db: Session) -> float:
    credits = db.query(
        func.coalesce(func.sum(BankTransaction.amount), 0.0)
    ).filter(
        BankTransaction.bank_account_id == account.id,
        BankTransaction.txn_type == "Credit",
    ).scalar()
    debits = db.query(
        func.coalesce(func.sum(BankTransaction.amount), 0.0)
    ).filter(
        BankTransaction.bank_account_id == account.id,
        BankTransaction.txn_type == "Debit",
    ).scalar()
    return round(account.initial_balance + float(credits) - float(debits), 2)


def _compute_balance_after(account: BankAccount, txn: BankTransaction, db: Session) -> float:
    """Running balance up to and including txn, ordered by (date, id)."""
    txns = (
        db.query(BankTransaction)
        .filter(
            BankTransaction.bank_account_id == account.id,
            (
                (BankTransaction.date < txn.date)
                | ((BankTransaction.date == txn.date) & (BankTransaction.id <= txn.id))
            ),
        )
        .order_by(BankTransaction.date, BankTransaction.id)
        .all()
    )
    balance = account.initial_balance
    for t in txns:
        if t.txn_type == "Credit":
            balance += t.amount
        else:
            balance -= t.amount
    return round(balance, 2)


def _account_out(account: BankAccount, db: Session) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "account_no": account.account_no or "",
        "bank_name": account.bank_name or "",
        "branch": account.branch or "",
        "ifsc": account.ifsc or "",
        "initial_balance": account.initial_balance,
        "initial_balance_date": account.initial_balance_date,
        "active": account.active,
        "current_balance": _compute_balance(account, db),
    }


def _txn_out(txn: BankTransaction) -> dict:
    return {
        "id": txn.id,
        "date": txn.date,
        "bank_account_id": txn.bank_account_id,
        "txn_type": txn.txn_type,
        "description": txn.description,
        "amount": txn.amount,
        "reference": txn.reference or "",
        "balance_after": txn.balance_after if txn.balance_after is not None else 0.0,
    }


# ---------------------------------------------------------------------------
# Account endpoints
# ---------------------------------------------------------------------------

@router.get("/accounts", response_model=List[BankAccountOut])
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.query(BankAccount).order_by(BankAccount.name).all()
    return [_account_out(a, db) for a in accounts]


@router.post("/accounts", response_model=BankAccountOut, status_code=201)
def create_account(payload: BankAccountIn, db: Session = Depends(get_db)):
    account = BankAccount(**payload.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return _account_out(account, db)


@router.patch("/accounts/{account_id}", response_model=BankAccountOut)
def update_account(account_id: int, payload: BankAccountUpdate, db: Session = Depends(get_db)):
    account = db.query(BankAccount).filter(BankAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Bank account not found")
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(account, field, value)
    db.commit()
    db.refresh(account)
    return _account_out(account, db)


@router.delete("/accounts/{account_id}", status_code=204)
def deactivate_account(account_id: int, db: Session = Depends(get_db)):
    account = db.query(BankAccount).filter(BankAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Bank account not found")
    account.active = False
    db.commit()


# ---------------------------------------------------------------------------
# Statement endpoint — defined before generic /accounts/{id} routes
# ---------------------------------------------------------------------------

@router.get("/accounts/{account_id}/statement", response_model=List[StatementEntry])
def get_statement(
    account_id: int,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    account = db.query(BankAccount).filter(BankAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Bank account not found")

    q = db.query(BankTransaction).filter(BankTransaction.bank_account_id == account_id)
    if from_date:
        q = q.filter(BankTransaction.date >= from_date)
    if to_date:
        q = q.filter(BankTransaction.date <= to_date)
    txns = q.order_by(BankTransaction.date, BankTransaction.id).all()

    entries = []
    balance = account.initial_balance
    for t in txns:
        credit = t.amount if t.txn_type == "Credit" else 0.0
        debit = t.amount if t.txn_type == "Debit" else 0.0
        balance += credit - debit
        entries.append(
            StatementEntry(
                date=t.date,
                description=t.description,
                credit=credit,
                debit=debit,
                balance=round(balance, 2),
                reference=t.reference or "",
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Transaction endpoints
# ---------------------------------------------------------------------------

@router.post("/transactions", response_model=BankTxnOut, status_code=201)
def create_transaction(payload: BankTxnIn, db: Session = Depends(get_db)):
    if payload.txn_type not in ("Credit", "Debit"):
        raise HTTPException(status_code=400, detail="txn_type must be 'Credit' or 'Debit'")

    account = db.query(BankAccount).filter(BankAccount.id == payload.bank_account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Bank account not found")

    txn = BankTransaction(**payload.model_dump(), balance_after=0.0)
    db.add(txn)
    db.commit()
    db.refresh(txn)

    # Compute running balance after insert so we have the correct id for ordering
    txn.balance_after = _compute_balance_after(account, txn, db)
    db.commit()
    db.refresh(txn)
    return _txn_out(txn)


@router.get("/transactions", response_model=List[BankTxnOut])
def list_transactions(
    account_id: Optional[int] = None,
    date_filter: Optional[date] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    query = db.query(BankTransaction)

    if account_id is not None:
        query = query.filter(BankTransaction.bank_account_id == account_id)

    if date_filter is not None:
        query = query.filter(BankTransaction.date == date_filter)
    else:
        if from_date is not None:
            query = query.filter(BankTransaction.date >= from_date)
        if to_date is not None:
            query = query.filter(BankTransaction.date <= to_date)

    txns = query.order_by(BankTransaction.date, BankTransaction.id).all()
    return [_txn_out(t) for t in txns]


@router.delete("/transactions/{txn_id}", status_code=204)
def delete_transaction(txn_id: int, db: Session = Depends(get_db)):
    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    db.delete(txn)
    db.commit()


# ---------------------------------------------------------------------------
# Summary endpoint
# ---------------------------------------------------------------------------

@router.get("/summary", response_model=SummaryOut)
def get_summary(db: Session = Depends(get_db)):
    accounts = (
        db.query(BankAccount)
        .filter(BankAccount.active == True)
        .order_by(BankAccount.name)
        .all()
    )
    by_account = []
    total = 0.0
    for account in accounts:
        bal = _compute_balance(account, db)
        total += bal
        by_account.append(AccountSummaryItem(name=account.name, balance=bal))
    return SummaryOut(total_balance=round(total, 2), by_account=by_account)
