from sqlalchemy import create_engine, Column, Integer, String, Float, Date, DateTime, Text, Enum, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import date, datetime
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "crusherops.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    customer_name = Column(String(200))
    material = Column(String(50))   # 40mm, 20mm, 12mm, 6mm, M-Sand, P-Sand, Dust
    qty_mt = Column(Float)          # quantity in metric tonnes
    rate_per_mt = Column(Float)     # rate per metric tonne
    amount = Column(Float)
    payment_mode = Column(String(20), default="Credit")  # Cash / Credit
    vehicle_no = Column(String(30))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    # Enhanced fields
    customer_id = Column(Integer, nullable=True, index=True)  # soft FK to customers.id
    ticket_no = Column(String(30), nullable=True, index=True)
    hsn_code = Column(String(10), default="2517")
    gst_rate = Column(Float, default=5.0)
    mdp_ton = Column(Float, nullable=True)        # MDP ton from ERP weighbridge
    erp_synced = Column(Boolean, default=False, index=True)


class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    category = Column(String(50))   # Fuel, EMI, Repair, Wages, Blasting, Other
    description = Column(String(300))
    amount = Column(Float)
    payment_mode = Column(String(20), default="Cash")
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    # Enhanced fields
    vendor_id = Column(Integer, nullable=True, index=True)  # soft FK to vendors.id
    erp_synced = Column(Boolean, default=False, index=True)
    erp_key = Column(String(500), nullable=True, index=True)


class BoulderInput(Base):
    __tablename__ = "boulder_inputs"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    trips = Column(Integer)
    tonnes_per_trip = Column(Float)
    total_tonnes = Column(Float)
    source = Column(String(100), default="Quarry Face")
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class MachineReading(Base):
    __tablename__ = "machine_readings"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    machine_name = Column(String(50))  # Jaw, Cone, VSI, Loader, Hitachi
    start_hours = Column(Float)
    end_hours = Column(Float)
    running_hours = Column(Float)
    production_mt = Column(Float)
    fuel_liters = Column(Float)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class Labour(Base):
    __tablename__ = "labour"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    worker_name = Column(String(100))
    worker_type = Column(String(50))  # Operator, Helper, Driver, Blasting, Watchman
    days = Column(Float, default=1.0)
    daily_wage = Column(Float)
    amount = Column(Float)
    paid = Column(Boolean, default=False)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class Part(Base):
    __tablename__ = "parts"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    machine_name = Column(String(50))
    part_name = Column(String(200))
    quantity = Column(Float, default=1)
    unit_price = Column(Float)
    total_amount = Column(Float)
    supplier = Column(String(200))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class EMIRecord(Base):
    __tablename__ = "emi_records"
    id = Column(Integer, primary_key=True, index=True)
    machine_name = Column(String(100))  # Hitachi, Loader, etc.
    emi_month = Column(String(7))       # YYYY-MM
    emi_amount = Column(Float)
    due_date = Column(Date)
    paid_date = Column(Date)
    status = Column(String(20), default="Pending")  # Pending / Paid / Overdue
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class Worker(Base):
    __tablename__ = "workers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True)
    worker_type = Column(String(50))  # Operator, Helper, Driver, etc.
    daily_wage = Column(Float)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), unique=True, nullable=False)
    gstin = Column(String(15), nullable=True)
    phone = Column(String(20))
    address = Column(Text)
    opening_balance = Column(Float, default=0)
    erp_debit_balance = Column(Float, default=0)
    erp_credit_balance = Column(Float, default=0)
    erp_balance_as_of = Column(Date, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class Vendor(Base):
    __tablename__ = "vendors"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), unique=True, nullable=False)
    gstin = Column(String(15), nullable=True)
    phone = Column(String(20))
    address = Column(Text)
    opening_balance = Column(Float, default=0)
    active = Column(Boolean, default=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)  # e.g. "HDFC Current Account"
    account_no = Column(String(50))
    bank_name = Column(String(100))
    branch = Column(String(100))
    ifsc = Column(String(15))
    initial_balance = Column(Float, default=0)
    initial_balance_date = Column(Date)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    bank_account_id = Column(Integer, ForeignKey("bank_accounts.id"), nullable=False, index=True)
    txn_type = Column(String(10), nullable=False)  # "Credit" / "Debit"
    description = Column(String(300))
    amount = Column(Float, nullable=False)
    reference = Column(String(100), nullable=True, index=True)
    balance_after = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class CustomerReceipt(Base):
    __tablename__ = "customer_receipts"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    mode = Column(String(30), default="Cash", index=True)  # Cash/UPI/Cheque/Bank Transfer
    reference = Column(String(100), nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class VendorPayment(Base):
    __tablename__ = "vendor_payments"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=date.today, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    mode = Column(String(30), default="Cash")
    reference = Column(String(100), nullable=True, index=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class ERPBankEntry(Base):
    """Raw bank transaction rows from ERP ListBankTransaction."""
    __tablename__ = "erp_bank_entries"
    id          = Column(Integer, primary_key=True, index=True)
    entry_date  = Column(Date, index=True)
    description = Column(Text)
    debit       = Column(Float, default=0)
    credit      = Column(Float, default=0)
    bank_name   = Column(String(100))
    raw_cols    = Column(Text)          # JSON of full row for reference
    created_at  = Column(DateTime, default=datetime.now)


class CashLedgerEntry(Base):
    """Cash ledger rows from ERP CashLedger endpoint."""
    __tablename__ = "cash_ledger_entries"
    id          = Column(Integer, primary_key=True, index=True)
    entry_date  = Column(Date, index=True)
    description = Column(Text)
    received    = Column(Float, default=0)
    paid        = Column(Float, default=0)
    balance     = Column(Float, nullable=True)
    ledger_name = Column(String(100))
    raw_cols    = Column(Text)
    created_at  = Column(DateTime, default=datetime.now)


class IOTMovement(Base):
    """Vehicle in/out movements from ERP IOT endpoint."""
    __tablename__ = "iot_movements"
    id           = Column(Integer, primary_key=True, index=True)
    movement_dt  = Column(DateTime, index=True)
    linked_type  = Column(String(50))   # PLANT ENTRY / SALE etc.
    ticket_no    = Column(String(30))
    vehicle_no   = Column(String(30))
    material     = Column(String(50))
    party        = Column(String(200))
    qty          = Column(String(20))
    crusher      = Column(String(100))
    img_url      = Column(Text)
    created_at   = Column(DateTime, default=datetime.now)


def init_db():
    Base.metadata.create_all(bind=engine)
    # Migrate existing tables for new columns
    from sqlalchemy import inspect, text
    with engine.connect() as conn:
        inspector = inspect(engine)
        sale_cols = [c["name"] for c in inspector.get_columns("sales")]
        if "mdp_ton" not in sale_cols:
            conn.execute(text("ALTER TABLE sales ADD COLUMN mdp_ton FLOAT"))
        if "erp_synced" not in sale_cols:
            conn.execute(text("ALTER TABLE sales ADD COLUMN erp_synced BOOLEAN DEFAULT 0"))
        expense_cols = [c["name"] for c in inspector.get_columns("expenses")]
        if "erp_synced" not in expense_cols:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN erp_synced BOOLEAN DEFAULT 1"))
        if "erp_key" not in expense_cols:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN erp_key VARCHAR(500)"))
        customer_cols = [c["name"] for c in inspector.get_columns("customers")]
        if "erp_debit_balance" not in customer_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN erp_debit_balance FLOAT DEFAULT 0"))
        if "erp_credit_balance" not in customer_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN erp_credit_balance FLOAT DEFAULT 0"))
        if "erp_balance_as_of" not in customer_cols:
            conn.execute(text("ALTER TABLE customers ADD COLUMN erp_balance_as_of DATE"))
        conn.commit()
