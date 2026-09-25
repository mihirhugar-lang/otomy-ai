import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import shared_compliance as root_engine


def _load_sync_engine():
    path = ROOT / "scripts" / "shared_compliance.py"
    spec = importlib.util.spec_from_file_location("otomy_sync_shared_compliance", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class AuditReadinessTests(unittest.TestCase):
    def _dataset(self, engine):
        receipts = [
            SimpleNamespace(
                id=1,
                date=date(2026, 9, 1),
                customer_name="Actual Customer",
                payment_received=125.0,
                mode="Bank Transfer",
                reference="UTR-125",
                source="ERP receipt",
                notes="",
            ),
            SimpleNamespace(
                id=2,
                date=date(2026, 9, 1),
                customer_name="Opening Anchor",
                payment_received=9999.0,
                mode="ERP Snapshot",
                reference="PrintCustomerBalance",
                source="ERP receipt",
                notes="Balance anchor only",
            ),
            SimpleNamespace(
                id=3,
                date=date(2026, 9, 1),
                customer_name="Same-day Sale Adjustment",
                payment_received=0.0,
                mode="Cash",
                reference="ERP-CREDIT-3-CASH",
                source="ERP receipt",
                notes="sale_adjusted=125.0; credit_repayment=0.0",
            ),
        ]
        return engine.build_compliance_dataset(
            [],
            [],
            receipts,
            [],
            [],
            [],
            {"company_name": "Fixture", "gstin": "29AAICV4284G1ZV", "state_code": "29"},
            date(2026, 9, 1),
            date(2026, 9, 1),
        )

    def test_snapshot_receipts_are_audit_evidence_not_money_movements(self):
        for engine in (root_engine, _load_sync_engine()):
            with self.subTest(engine=engine.__name__):
                dataset = self._dataset(engine)
                self.assertEqual(len(dataset["receipts"]), 1)
                self.assertEqual(dataset["totals"]["receipts"], 125.0)
                self.assertEqual(dataset["daily"][0]["receipts"], 125.0)
                excluded = dataset["audit_exclusions"]["erp_snapshot_receipts"]
                self.assertEqual(len(excluded), 1)
                self.assertEqual(excluded[0]["amount"], 9999.0)
                zero_value = dataset["audit_exclusions"]["zero_value_receipts"]
                self.assertEqual(len(zero_value), 1)
                self.assertEqual(zero_value[0]["amount"], 0.0)
                if "tally_vouchers" in dataset:
                    self.assertEqual(len(dataset["tally_vouchers"]), 1)
                    self.assertNotIn("9999", dataset["tally_vouchers"][0]["content"])


if __name__ == "__main__":
    unittest.main()
