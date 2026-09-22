# Sync engine module boundaries

`scripts/gha_sync.py` remains the GitHub Actions entry point and the API imported
by localhost parity checks, repair scripts and `common_engine.py`. Its main sync
orchestration, environment settings, paths and per-import caches remain there.
The entry point is 3,122 lines, down from 6,494 (51.9%). This is a file-size
refactor, not a deletion of half the project's functionality.

| Module | Responsibility |
| --- | --- |
| `sync_loctell.py` | ERP authentication, fetches, parsing, identity and repayment extraction |
| `sync_finance.py` | Dashboard, cash/bank books, ledgers and customer/vendor calculations |
| `sync_archive.py` | Stable row identities, duplicate handling, archive merge/read/write |
| `sync_snapshots.py` | API snapshots and compliance exports |

Functions with runtime dependencies receive explicit keyword-only settings and
helper callbacks. Small wrappers in `gha_sync.py` pass its current values at call
time. This keeps existing signatures, independently loaded engine instances,
temporary data-directory overrides and localhost's fixture hooks working without
circular imports, dynamic code loading or hidden copies of mutable caches.
Read dependencies from the function signature when changing an implementation.

The relocation audit compared all 147 original function bodies and found no AST
changes, including the complete `main()` body. Eighty-three implementations moved.
All original entry-point function signatures remained identical. Baseline commit:
`6174884bad3ad24f83a0292ed316432a7d58f00d`.

The public synthetic fixture was recorded from the original engine **before**
moving code. It freezes the clock and blocks HTTP, then compares every row and
field in Today, MTD, FYTD, current-month, previous-month and historical books,
controls and customer balances. It also checks fetch retries and exhausted-read
failures, source identities, ERP edit/deletion merging, vendor ledgers and the
byte hashes of all 5,209 generated archive/snapshot/compliance JSON files.
Both repeated baseline captures produced identical hashes; the refactor matches.
The saved baselines cover both the local Python 3.9 runtime and GitHub's Python
3.12 runtime. Python 3.12's float-summation change alters eight synthetic output
files even with the original engine. Its separate baseline was therefore
captured from the unchanged original revision, never from the refactor. Every
file remains subject to exact comparison against its runtime's original output.

Run the checks with the repository's existing test dependencies installed:

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/verify_dashboard_parity.py --code-only
```

The updated source guards inspect all four modules plus the entry point, with
implementations before wrappers so function-block guards examine real code.
The output contract check runs in both verification and pre-sync workflows.
Changing financial behavior intentionally requires reviewing the synthetic
fixture expectations; do not regenerate expected hashes merely to silence a
failure.

Validation passed: 156 Python tests, code guards, and localhost's 14-test parity
suite (including six-range cash/bank rows and totals).

Before release, a separate private audit used checksum-verified production
inputs published on September 22, 2026. Both engines used Python 3.12, matching
the production runner. The original and refactored engines
matched exactly for June 1 through September 22: all 114 days, 318 daily,
month-to-date, cumulative and preset ranges, six monthly ledgers, complete
customer/vendor ledgers, and all 4,309 captured output files. The 1,272 range
comparisons covered complete cash/bank, control, customer and vendor responses.
There were zero differences; no financial tolerance or ignored financial fields
were used. Both runs used isolated directories, the same captured inputs and a
fixed clock, with network access blocked during calculation. Private business
records and audit outputs remain outside the repository.

This verifies behavior against the captured inputs; it does not claim that every
possible future input has been tested. Schedules, retention, incremental transfer
and storage-budget settings are unchanged. Release still requires the protected
pull-request checks and a successful production sync with its read-back.
