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
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Base, Customer, CustomerReceipt, Expense, Sale
from routers import dashboard
from routers import erp_sync as local_engine


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
