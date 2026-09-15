#!/usr/bin/env python3
"""Synthetic cross-app book/ledger fixtures; only an in-memory database."""
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import (Base, Customer, Sale, Expense, CustomerReceipt,
                      BoulderInput, InternalTransfer)
from routers import dashboard, erp_sync

TODAY = date(2026, 9, 15)
DAYS = ["2026-04-01", "2026-04-29", "2026-05-01", "2026-06-01",
        "2026-08-31", "2026-09-01", "2026-09-15"]
RANGES = [("2026-09-15", "2026-09-15"), ("2026-09-01", "2026-09-15"),
          ("2026-04-01", "2026-09-15"), ("2026-09-01", "2026-09-30"),
          ("2026-08-01", "2026-08-31"), ("2026-04-01", "2026-04-30")]


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 15)


class BookRangeParityTests(unittest.TestCase):
    def setUp(self):
        self.database = create_engine("sqlite://")
        Base.metadata.create_all(self.database)
        self.db = sessionmaker(bind=self.database)()
        path = ROOT.parent / "otomy_ai_repo/scripts/gha_sync.py"
        spec = importlib.util.spec_from_file_location("range_fixture_cloud", path)
        self.cloud = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cloud)
        self.sales, self.expenses, self.receipts, self.transfers, self.boulders = [], [], [], [], []
        self.db.add(Customer(id=1, name="Synthetic Customer"))
        for i, day in enumerate(DAYS):
            stamp = date.fromisoformat(day)
            sale = dict(date=day, customer_id=1, customer_name="Synthetic Customer",
                        amount=195.0, transport_charge=5.0, qty_mt=4.0,
                        cash_amount=100.0, credit_amount=60.0, upi_amount=40.0,
                        payment_mode="UPI", ticket_no=str(100 + i))
            self.sales.append(sale)
            self.db.add(Sale(**{**sale, "date": stamp}, material="40mm", rate_per_mt=48.75))
            for mode, amount in (("Cash", 120.0), ("UPI", 50.0)):
                self.db.add(CustomerReceipt(date=stamp, customer_id=1, amount=amount, mode=mode))
                self.receipts.append(dict(date=day, customer_id=1, customer_name="Synthetic Customer",
                                         payment_received=amount, mode=mode,
                                         cash_received=amount if mode == "Cash" else 0,
                                         bank_received=amount if mode == "UPI" else 0))
            # The snapshot is not real money and must stay out of local books.
            self.db.add(CustomerReceipt(date=stamp, customer_id=1, amount=9999, mode="ERP Snapshot"))
            for mode, amount in (("Cash", 5.0), ("UPI", 7.0)):
                expense = dict(date=day, category="Fixture expense", description="Synthetic vendor",
                               notes="", amount=amount, payment_mode=mode)
                self.expenses.append(expense)
                self.db.add(Expense(**{**expense, "date": stamp}))
            transfer = dict(date=day, amount=3.0, bank_name="Fixture bank",
                            cash_ledger="Office", remarks="Synthetic contra")
            self.transfers.append(transfer)
            self.db.add(InternalTransfer(entry_date=stamp, amount=3, direction="cash_to_bank",
                                        bank_name="Fixture bank", cash_ledger="Office",
                                        remarks="Synthetic contra", source_key=f"fixture-{i}"))
            self.boulders.append(dict(date=day, total_tonnes=6.0, trips=2))
            self.db.add(BoulderInput(date=stamp, total_tonnes=6, trips=2))
        self.db.commit()
        self.opening = dict(as_of="2026-03-31", bank_balance=2000.0, cash_balance_office=1000.0)

    def tearDown(self):
        self.db.close()
        self.database.dispose()

    def balances(self, as_of, *_unused):
        count = sum(day <= str(as_of)[:10] for day in DAYS)
        return 2000.0 + 46 * count, 1000.0 + 112 * count

    @contextmanager
    def sources(self):
        with ExitStack() as stack:
            for obj, name, value in [
                (dashboard, "_operating_balance_opening", {**self.opening, "as_of_date": date(2026, 3, 31)}),
                (dashboard, "_latest_anchor", None), (dashboard, "_statement_bank", (None, None)),
                (dashboard, "_mode_override_channel", None), (dashboard, "_fetch_erp_input_summary", None),
                (self.cloud, "_balance_overlay", {"anchors": [], "corrections": []}),
            ]:
                stack.enter_context(patch.object(obj, name, return_value=value))
            stack.enter_context(patch.object(self.cloud, "_overlay_balance", side_effect=self.balances))
            stack.enter_context(patch.object(dashboard, "date", FrozenDate))
            yield

    def books(self, start, end):
        with self.sources():
            local = erp_sync.build_cashbook(self.db, date.fromisoformat(start), date.fromisoformat(end))
            cloud = self.cloud.build_cashbook_view(start, end, self.sales, self.expenses,
                                                   self.receipts, self.opening, self.transfers)
        return local, cloud

    def ledgers(self, year, month):
        with self.sources():
            local = dashboard.ledger_view(year, month, self.db)
            cloud = self.cloud.build_ledger_view(
                self.sales, self.expenses, [], self.boulders, self.receipts,
                year, month, 2000, 1000, date(2026, 4, 1), TODAY,
                internal_transfers=self.transfers)
        return local, cloud

    def test_every_cashbook_row_and_total_across_six_ranges(self):
        for start, end in RANGES:
            with self.subTest(start=start, end=end):
                local, cloud = self.books(start, end)
                self.assertEqual(local, cloud)
                count = sum(start <= day <= end for day in DAYS)
                for channel, index, incoming, outgoing in (("cash", 1, 120, 8), ("bank", 0, 53, 7)):
                    book = local[channel]
                    self.assertEqual(book["opening"], self.balances(date.fromisoformat(start) - timedelta(days=1))[index])
                    self.assertEqual(book["closing"], self.balances(end)[index])
                    self.assertEqual(book["total_in"], count * incoming)
                    self.assertEqual(book["total_out"], count * outgoing)
                    self.assertEqual(book["settlement_roundoff"], 0)
                    self.assertFalse(any(row.get("adjustment") for row in book["rows"]))

    def test_daily_ledger_rows_and_totals_across_months(self):
        for year, month in ((2026, 4), (2026, 5), (2026, 6), (2026, 8), (2026, 9)):
            with self.subTest(year=year, month=month):
                local, cloud = self.ledgers(year, month)
                self.assertEqual(local, cloud)
                for row in local["rows"]:
                    active = row["date"] in DAYS
                    expected = {"sale_trips": 1, "sale_amount": 200, "spot_sale_amount": 140,
                                "spot_sale_cash": 100, "spot_sale_bank": 40, "credit_sale_amount": 60,
                                "qty_mt": 4, "credit_repayment": 170, "credit_repayment_cash": 120,
                                "credit_repayment_bank": 50, "expenses": 12, "expense_cash": 5,
                                "expense_bank": 7, "internal_transfer": 3, "boulder_input_mt": 6,
                                "boulder_trips": 2, "stock_in_plant_mt": 2}
                    for key, value in expected.items():
                        self.assertEqual(row[key], value if active else 0, (row["date"], key))
                    bank, cash = self.balances(row["date"])
                    self.assertEqual((row["bank_balance"], row["cash_balance_office"]), (bank, cash))

    def test_leap_and_future_month_preserve_response_contracts(self):
        local, cloud = self.ledgers(2024, 2)
        self.assertEqual(len(local["rows"]), 29)
        self.assertEqual(len(cloud["rows"]), 29)
        local, cloud = self.ledgers(2026, 10)
        self.assertEqual(local["rows"], [])
        self.assertEqual(cloud["rows"], [])
        # Existing API difference: local sends zero totals, cloud sends {}.
        self.assertEqual(cloud["totals"], {})
        self.assertEqual(local["totals"]["sale_amount"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
