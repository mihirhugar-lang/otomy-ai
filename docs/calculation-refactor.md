# Calculation restructuring: first verified slice

`shared_calculations.py` contains pure sales tender allocation, informational
settlement round-off, and cumulative supplier FIFO ageing. It has no ERP,
database, configuration, filesystem or current-clock dependency. `gha_sync.py`
and the localhost routers keep their existing public function signatures and
adapt their own inputs to these calculations.

The localhost package carries a byte-identical `shared_calculations.py` so each
application remains self-contained. Localhost's existing pre-sync fixture guard
now checks both copies and the adapter outputs. Keep both copies in the same
reviewed change; do not hand-edit separate calculation rules. GitHub verifies
the cloud fixtures without requiring the Mac or private financial inputs.

## Preserved boundaries

- Cloud retains its signed/formatted numeric parser and captured-split rounding.
- Localhost retains its database numeric conversion and gross rounding.
- A positive captured split is selected before rounding tiny values.
- Settlement round-off stays informational; it never changes money movements.
- Supplier identity, authoritative payable, source-row selection, date cutoffs,
  unknown-entry classification and chronological ordering stay in the adapters.
- The arithmetic copies invoice state; no caller-owned rows are modified.
- No balance anchors, schema, MDP rules, UI, sync frequency, storage policy or
  financial source records change in this slice.

## Checks

Cloud, from the repository root:

```sh
python3 scripts/test_shared_calculations.py
python3 scripts/test_cashbook_parity.py
python3 scripts/test_sale_split_guard.py
python3 scripts/test_vendor_aging.py
```

Localhost, from its application root (no PYTHONPATH override):

```sh
.venv/bin/python scripts/test_cashbook_parity.py
```

All permanent fixtures are synthetic or existing reviewed test fixtures, not
exports of live financial data. The first migration additionally compared the
old committed functions with the new functions over 22,000 deterministic
synthetic evaluations. No differences were found. This is not a claim of
complete live ERP reconciliation or all-input equivalence.

## Remaining stages

Cashbook movement construction, physical/bank anchor resolution, daily-ledger
aggregation, customer balances, exclusive supplier age bands and operational
MDP calculations are not fully extracted yet. For each stage, freeze the old
results, test date/identity/rounding boundaries, then extract without changing
formulas. Optimize repeated scans only in a separate measured change. Do not
introduce cross-sync caching without data-version invalidation.
