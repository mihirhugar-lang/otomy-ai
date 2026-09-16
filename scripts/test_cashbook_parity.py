#!/usr/bin/env python3
"""Cross-check the real localhost and Otomy cashbook calculators.

This guard runs entirely against an in-memory database: no Loctell request and
no local financial data are read or changed.  It uses the versioned Otomy
fixture so both engines must return the same rupee-level book.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Base, Customer, CustomerReceipt, Expense, Sale, Vendor, VendorLedgerEntry
from routers import dashboard
from routers import erp_sync as local_engine
from routers import vendors
from routers import customers


OTOMY_ROOT = ROOT.parents[0] / "otomy_ai_repo"
FIXTURE_PATH = OTOMY_ROOT / "scripts" / "fixtures" / "cashbook_parity.json"
CLOUD_ENGINE_PATH = OTOMY_ROOT / "scripts" / "gha_sync.py"


def normalized(book: dict) -> dict:
    def side(value: dict) -> dict:
        return {
            "opening": round(float(value["opening"]), 2),
            "total_in": round(float(value["total_in"]), 2),
            "total_out": round(float(value["total_out"]), 2),
            "closing": round(float(value["closing"]), 2),
            "rows": [
                {
                    "date": str(row["date"])[:10],
                    "particulars": row["particulars"],
                    "party": row.get("party") or "",
                    "kind": row["kind"],
                    "in": round(float(row["in"]), 2),
                    "out": round(float(row["out"]), 2),
                    "balance": round(float(row["balance"]), 2),
                }
                for row in value["rows"]
            ],
        }

    return {
        "from": str(book["from"])[:10],
        "to": str(book["to"])[:10],
        "opening_as_of": str(book["opening_as_of"])[:10],
        "cash": side(book["cash"]),
        "bank": side(book["bank"]),
    }


def load_cloud_engine():
    spec = importlib.util.spec_from_file_location("otomy_cashbook_parity_engine", CLOUD_ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load cloud cashbook engine: {CLOUD_ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CashbookParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE_PATH.exists() or not CLOUD_ENGINE_PATH.exists():
            raise RuntimeError("Otomy parity fixture or cloud engine is missing")
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def test_shared_calculation_copies_match(self):
        # Both deployments are self-contained; do not depend on a sibling
        # checkout at runtime. Block local sync if the packaged rules drift.
        self.assertEqual(
            (ROOT / "shared_calculations.py").read_bytes(),
            (OTOMY_ROOT / "shared_calculations.py").read_bytes(),
            "Shared calculation copies differ; refusing local/cloud drift",
        )

    def test_sale_adapters_match_across_report_ranges(self):
        cloud = load_cloud_engine()
        rows = [
            {"date": day, "amount": 103.0, "transport_charge": 2.0,
             "payment_mode": "Cash", "cash_amount": 100.0,
             "credit_amount": 0.0, "upi_amount": 0.0}
            for day in ("2026-04-01", "2026-08-31", "2026-09-01", "2026-09-15")
        ]
        # Today, MTD, FYTD, current month, previous month and a historic range.
        ranges = [("2026-09-15", "2026-09-15"), ("2026-09-01", "2026-09-15"),
                  ("2026-04-01", "2026-09-15"), ("2026-09-01", "2026-09-30"),
                  ("2026-08-01", "2026-08-31"), ("2026-04-01", "2026-04-30")]
        for start, end in ranges:
            with self.subTest(start=start, end=end):
                selected = [row for row in rows if start <= row["date"] <= end]
                for row in selected:
                    stored = SimpleNamespace(**row)
                    self.assertEqual(local_engine.sale_channels(stored), cloud._sale_channels(row))
                    self.assertEqual(local_engine.sale_settlement_roundoff(stored), cloud._sale_settlement_roundoff(row))
                self.assertEqual(sum(local_engine.sale_channels(SimpleNamespace(**r))[0] for r in selected), 100 * len(selected))

    def test_local_precision_boundary_is_unchanged(self):
        row = SimpleNamespace(amount=10.125, transport_charge=0,
                              payment_mode="Cash", cash_amount=0.004,
                              credit_amount=0, upi_amount=0)
        self.assertEqual(local_engine.sale_channels(row), (0.004, 0, 0))
        self.assertEqual(local_engine.sale_settlement_roundoff(row), (10.12, 0))

    def test_supplier_exclusive_age_bands_match_cloud(self):
        cloud = load_cloud_engine()
        as_of = date(2026, 9, 15)
        entries = [SimpleNamespace(id=i, entry_type="purchase", entry_date=as_of-timedelta(days=days), amount=10.25)
                   for i, days in enumerate((0, 15, 16, 30, 31, 45, 46, 90))]
        rows = [{"date": str(row.entry_date), "type": "purchase", "credit": row.amount} for row in entries]
        for target in (-10, 0, 25.15, 82, 100):
            self.assertEqual(vendors._ledger_payable_age_buckets(target, entries, as_of),
                             cloud.vendor_payable_age_buckets(rows, target, str(as_of)))

    def test_customer_credit_aging_preserves_snapshot_exclusion(self):
        cloud = load_cloud_engine()
        as_of = date(2026, 9, 15)
        sales = [SimpleNamespace(id=i, date=as_of-timedelta(days=days), amount=100,
                                 transport_charge=0, payment_mode="Credit", cash_amount=0,
                                 credit_amount=100, upi_amount=0)
                 for i, days in enumerate((90, 45, 16, 0))]
        receipts = [SimpleNamespace(date=as_of, mode="Cash", notes="", amount=50),
                    SimpleNamespace(date=as_of, mode="ERP Snapshot", notes="", amount=9999)]
        cloud_sales = [{**vars(sale), "date": str(sale.date), "customer_name": "Synthetic"} for sale in sales]
        for days in (15, 16, 30, 31, 45):
            for target in (-10, 0, 75, 350, 450):
                local = customers._credit_due_plus(1, target, as_of, self.session, days,
                                                   _sales=sales, _receipts=receipts)
                remote = cloud._credit_due_15_plus_by_name(
                    [{"name": "Synthetic", "outstanding": target}], cloud_sales,
                    [{"date": str(as_of), "customer_name": "Synthetic", "payment_received": 50}], str(as_of), days)
                self.assertEqual(local, remote["Synthetic"])

    def test_supplier_fifo_matches_cloud_at_historical_cutoffs(self):
        cloud = load_cloud_engine()
        vendor = Vendor(id=1, name="Synthetic Supplier")
        self.session.add(vendor)
        entries = [
            (date(2026, 4, 1), "purchase", 100.0),
            (date(2026, 5, 1), "purchase", 200.0),
            (date(2026, 6, 1), "payment", 50.0),
            (date(2026, 8, 31), "purchase", 100.0),
            (date(2026, 9, 15), "payment", 25.0),
            (date(2026, 12, 1), "purchase", 9000.0),
        ]
        for index, (day, kind, amount) in enumerate(entries):
            self.session.add(VendorLedgerEntry(vendor_id=1, entry_date=day,
                             entry_type=kind, amount=amount, source_key=f"fixture-{index}"))
        self.session.commit()
        cloud_rows = [{"date": str(day), "type": kind,
                       "credit": amount if kind == "purchase" else 0,
                       "debit": amount if kind == "payment" else 0}
                      for day, kind, amount in entries]
        for as_of in (date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 1),
                      date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 15)):
            for payable in (-25, 0, 75.55, 250, 425):
                with self.subTest(as_of=as_of, payable=payable):
                    actual = vendors._payable_due_aging(vendor, payable, self.session, as_of)
                    self.assertEqual(actual, cloud.vendor_payable_due_aging(cloud_rows, payable, str(as_of)))
                    amounts = [actual[f"payable_due_{days}_plus"] for days in (15, 30, 45, 60)]
                    self.assertEqual(amounts, sorted(amounts, reverse=True))
                    self.assertTrue(all(0 <= v <= max(payable, 0) for v in actual.values()))

    def _seed(self, fixture: dict) -> None:
        self.session.add_all(Customer(id=row["id"], name=row["name"]) for row in fixture["customers"])
        self.session.add_all(
            Sale(
                date=date.fromisoformat(row["date"]),
                customer_id=row["customer_id"],
                customer_name=row["customer_name"],
                material="40mm",
                qty_mt=1.0,
                rate_per_mt=row["amount"],
                amount=row["amount"],
                transport_charge=row["transport_charge"],
                payment_mode=row["payment_mode"],
                ticket_no=f"fixture-{index}",
                cash_amount=row["cash_amount"],
                credit_amount=row["credit_amount"],
                upi_amount=row["upi_amount"],
            )
            for index, row in enumerate(fixture["sales"], start=1)
        )
        self.session.add_all(
            CustomerReceipt(
                date=date.fromisoformat(row["date"]),
                customer_id=row["customer_id"],
                amount=row["payment_received"],
                mode=row["mode"],
            )
            for row in fixture["repayments"]
        )
        self.session.add_all(
            Expense(
                date=date.fromisoformat(row["date"]),
                category=row["category"],
                description=row["description"],
                notes=row["notes"],
                amount=row["amount"],
                payment_mode=row["payment_mode"],
            )
            for row in fixture["expenses"]
        )
        self.session.commit()

    def _books(self, fixture: dict) -> tuple[dict, dict]:
        self._seed(fixture)
        opening = fixture["opening"]
        start = date.fromisoformat(fixture["range"]["from"])
        end = date.fromisoformat(fixture["range"]["to"])
        anchors = fixture.get("anchors", [])
        statement = fixture.get("bank_statement")
        opening_base = {
            "as_of_date": date.fromisoformat(opening["as_of"]),
            "cash_balance_office": opening["cash_balance_office"],
            "bank_balance": opening["bank_balance"],
        }

        def local_anchor(as_of):
            applicable = [row for row in anchors if row["date"] <= str(as_of)[:10]]
            return applicable[-1] if applicable else None

        def local_statement(as_of):
            if statement and statement["date"] <= str(as_of)[:10]:
                return statement["balance"], statement["date"]
            return None, None

        with patch.object(dashboard, "_operating_balance_opening", return_value=opening_base), patch.object(
            dashboard, "_latest_anchor", side_effect=local_anchor
        ), patch.object(dashboard, "_statement_bank", side_effect=local_statement), patch.object(
            dashboard, "_mode_override_channel", return_value=None
        ):
            localhost_book = local_engine.build_cashbook(self.session, start, end)

        cloud = load_cloud_engine()
        balances = {
            opening["as_of"]: (opening["bank_balance"], opening["cash_balance_office"]),
            fixture["range"]["to"]: (
                fixture["expected"]["bank"]["closing"],
                fixture["expected"]["cash"]["closing"],
            ),
        }
        with patch.object(
            cloud, "_balance_overlay", return_value={"anchors": anchors, "corrections": []}
        ), patch.object(cloud, "_overlay_balance", side_effect=lambda as_of, *_unused: balances[str(as_of)[:10]]):
            cloud_book = cloud.build_cashbook_view(start, end, fixture["sales"], fixture["expenses"], fixture["repayments"], opening)

        return normalized(localhost_book), normalized(cloud_book)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_localhost_and_cloud_match_split_repayment_fixture(self):
        localhost_book, cloud_book = self._books(self.fixture)
        self.assertEqual(localhost_book, self.fixture["expected"])
        self.assertEqual(cloud_book, self.fixture["expected"])
        self.assertEqual(localhost_book, cloud_book)

    def test_localhost_and_cloud_keep_named_verified_reanchors(self):
        fixture = self.fixture["verified_reanchor"]
        localhost_book, cloud_book = self._books(fixture)
        self.assertEqual(localhost_book, fixture["expected"])
        self.assertEqual(cloud_book, fixture["expected"])
        self.assertEqual(localhost_book, cloud_book)

    def test_ticket_roundoff_is_visible_but_not_a_cash_movement(self):
        self.session.add(Customer(id=1, name="Fixture Customer"))
        self.session.add(Sale(
            date=date(2026, 6, 29), customer_id=1, customer_name="Fixture Customer",
            material="40mm", qty_mt=1, rate_per_mt=3173, amount=3173,
            payment_mode="Cash", ticket_no="10067", cash_amount=3170,
            credit_amount=0, upi_amount=0,
        ))
        self.session.commit()
        cash_rows, bank_rows = local_engine._cashbank_movement_rows(
            self.session, date(2026, 6, 29), date(2026, 6, 29), {1: "Fixture Customer"}
        )
        self.assertEqual(bank_rows, [])
        self.assertEqual(len(cash_rows), 1)
        self.assertEqual(cash_rows[0]["ticket_no"], "10067")
        self.assertEqual(cash_rows[0]["in"], 3170)
        self.assertEqual(cash_rows[0]["settlement_roundoff"], -3)

    def test_deferred_physical_count_keeps_positive_running_balance(self):
        fixture = deepcopy(self.fixture)
        fixture["range"] = {"from": "2026-04-29", "to": "2026-04-29"}
        fixture["opening"] = {"as_of": "2026-04-28", "cash_balance_office": 10, "bank_balance": 0}
        fixture["customers"] = [{"id": 1, "name": "Synthetic Customer"}]
        fixture["sales"] = [{"date": "2026-04-29", "customer_id": 1, "customer_name": "Synthetic Customer",
                             "amount": 100, "transport_charge": 0, "payment_mode": "Cash",
                             "cash_amount": 100, "credit_amount": 0, "upi_amount": 0}]
        fixture["repayments"], fixture["expenses"] = [], []
        fixture["anchors"] = [{"date": "2026-04-29", "cash": 20}]
        fixture["bank_statement"] = None
        fixture["expected"] = {"cash": {"closing": 20}, "bank": {"closing": 0}}
        local, cloud = self._books(fixture)
        self.assertEqual(local, cloud)
        self.assertEqual([r["balance"] for r in local["cash"]["rows"]], [110, 20])
        self.assertEqual(local["cash"]["rows"][-1]["particulars"], "Verified balance adjustment (physical cash count)")


def load_tests(loader, tests, pattern):
    # Keep the pre-sync guard as the one entry point; include complete range
    # and Daily Ledger comparisons without requiring another scheduled job.
    import test_book_range_parity
    tests.addTests(loader.loadTestsFromModule(test_book_range_parity))
    import test_control_range_parity
    tests.addTests(loader.loadTestsFromModule(test_control_range_parity))
    return tests


if __name__ == "__main__":
    unittest.main(verbosity=2)
