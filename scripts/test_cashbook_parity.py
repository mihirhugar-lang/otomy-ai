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


if __name__ == "__main__":
    unittest.main(verbosity=2)
