#!/usr/bin/env python3
"""Fixture guard for the canonical cloud cashbook calculator.

The localhost guard consumes this exact same fixture and compares its real
database-backed calculator with this cloud implementation.  Keeping the
expected book here means a cloud change is blocked in GitHub before it can
publish R2 data.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "scripts" / "fixtures" / "cashbook_parity.json"


def load_engine():
    spec = importlib.util.spec_from_file_location("otomy_cashbook_fixture_engine", ROOT / "scripts" / "gha_sync.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load canonical cashbook engine")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class CashbookParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _cloud_book(self, fixture: dict) -> dict:
        engine = load_engine()
        balances = {
            fixture["opening"]["as_of"]: (
                fixture["opening"]["bank_balance"],
                fixture["opening"]["cash_balance_office"],
            ),
            fixture["range"]["to"]: (
                fixture["expected"]["bank"]["closing"],
                fixture["expected"]["cash"]["closing"],
            ),
        }

        def fixture_balance(as_of, *_unused):
            return balances[str(as_of)[:10]]

        with patch.object(
            engine, "_balance_overlay", return_value={"anchors": fixture.get("anchors", []), "corrections": []}
        ), patch.object(
            engine, "_overlay_balance", side_effect=fixture_balance
        ):
            book = engine.build_cashbook_view(
                date.fromisoformat(fixture["range"]["from"]),
                date.fromisoformat(fixture["range"]["to"]),
                fixture["sales"],
                fixture["expenses"],
                fixture["repayments"],
                fixture["opening"],
            )
        return normalized(book)

    def test_canonical_cashbook_matches_split_repayment_fixture(self):
        self.assertEqual(self._cloud_book(self.fixture), self.fixture["expected"])

    def test_canonical_cashbook_keeps_named_verified_reanchors(self):
        fixture = self.fixture["verified_reanchor"]
        self.assertEqual(self._cloud_book(fixture), fixture["expected"])


class SourceWindowCoverageTests(unittest.TestCase):
    def test_accepts_every_fresh_source_row_in_the_merged_result(self):
        engine = load_engine()
        rows = [{"id": "a"}, {"id": "b"}]
        engine.assert_fresh_source_rows_preserved("fixture", rows, rows, lambda row: row["id"])

    def test_rejects_a_fresh_source_row_that_was_dropped(self):
        engine = load_engine()
        with self.assertRaisesRegex(RuntimeError, "source-window coverage failed"):
            engine.assert_fresh_source_rows_preserved(
                "fixture", [{"id": "a"}, {"id": "b"}], [{"id": "a"}], lambda row: row["id"]
            )

    def test_rejects_an_anchor_only_fytd_source(self):
        engine = load_engine()
        fy_start, as_of = date(2026, 4, 1), date(2026, 8, 9)
        complete = [{"date": f"2026-{month:02d}-01"} for month in range(4, 9)]
        engine.assert_fytd_source_coverage(fy_start, as_of, complete, complete)
        truncated = [{"date": f"2026-{month:02d}-01"} for month in range(6, 9)]
        with self.assertRaisesRegex(RuntimeError, "FYTD source coverage failed.*2026-04.*2026-05"):
            engine.assert_fytd_source_coverage(fy_start, as_of, truncated, truncated)

    def test_keeps_distinct_same_amount_expense_bank_payments(self):
        engine = load_engine()
        base = {
            "date": "2026-04-21", "description": "Expense paid by bank/UPI - COMPONTATION / FARMER - Default Ledger",
            "credit": 0.0, "debit": 15000.0, "bank_name": "UPI/Bank Expense", "source": "Expense",
        }
        rows = [dict(base, id="expense-518"), dict(base, id="expense-519"), dict(base, id="expense-518")]
        deduped = engine.dedupe_bank_rows(rows)
        self.assertEqual(sorted(row["id"] for row in deduped), ["expense-518", "expense-519"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
