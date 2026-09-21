#!/usr/bin/env python3
"""Compare refactored output with hashes captured from the original engine.

The fixture contains no business records or credentials. Capture output is
isolated in a temporary directory and HTTP requests are explicitly forbidden.
"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import gha_sync as engine
from sync_refactor_fixture import RANGES, capture


class SyncRefactorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(__file__).parent/'fixtures/sync_refactor_expected.json'
        cls.expected=json.loads(path.read_text())['outputs']
        with tempfile.TemporaryDirectory(prefix='otomy-refactor-test-') as root, redirect_stdout(io.StringIO()):
            cls.actual=capture(engine,root)

    def test_transaction_rows_openings_movements_closings_and_party_balances(self):
        for label in RANGES:
            with self.subTest(range=label):
                self.assertEqual(self.actual[label],self.expected[label])

    def test_every_generated_archive_snapshot_and_compliance_file(self):
        self.assertEqual(self.actual['generated_files'],self.expected['generated_files'])
        self.assertEqual(self.actual['snapshot_ranges'],self.expected['snapshot_ranges'])

    def test_archive_edit_deletion_and_vendor_ledger_contracts(self):
        for name in ('archive_window','merge_edits_deletions','vendor_ledgers'):
            with self.subTest(contract=name):
                self.assertEqual(self.actual[name],self.expected[name])

    def test_fetch_parsing_retry_and_exhausted_source_failure_contract(self):
        self.assertEqual(self.actual['fetches'],self.expected['fetches'])


if __name__=='__main__':
    unittest.main(verbosity=2)
