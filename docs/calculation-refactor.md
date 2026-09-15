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

## Second slice: Cash/Bank arithmetic and Daily Ledger output

The shared module now also owns per-movement running-balance rounding,
post-adjustment rebalancing, cashbook summary totals, Daily Ledger row
aggregation and monthly totals. Both applications call the same functions.
Rows retain their existing ticket, remarks, adjustment and round-off fields.

Verified source selection remains outside this extraction: no changes to
bank statements, physical anchors, customer/payment overlap, expense mode
corrections, transfer direction, source queries or financial-period selection.
The existing cloud closing-balance check is retained. This refactor does not
certify or change the pre-existing reconciliation policies.

Additional permanent checks:

```sh
python3 scripts/test_book_calculations.py
```

The localhost `scripts/test_cashbook_parity.py` entry point now also runs
`scripts/test_book_range_parity.py` against an in-memory synthetic database.
It compares complete Cash/Bank responses for Today, MTD, FYTD, current month,
previous month and a historical range; it compares every Daily Ledger column
across five months. Leap-year and future-month response contracts are tested.
The cloud's existing empty future-month totals and localhost's zero totals
remain unchanged. Deferred physical counts remain after the day's movements.

Migration checks additionally compared 26 synthetic whole-app responses and
2,000 randomized cloud responses with the committed implementations, including
sub-cent rounding and physical-anchor cases. All matched exactly. A separate
read-only in-memory copy of the current localhost database produced exact
old/new matches for six Cash/Bank ranges and six monthly Daily Ledgers. Only
match status was reported; no financial payload was exported to tests or logs.
These checks establish refactor parity, not independent ERP reconciliation.

## Third slice: customers, vendors, Sales/MDP and bounded performance work

Customer credit FIFO and customer/vendor exclusive ageing bands now use the
shared arithmetic. Local customer balance fallback is separated from database
selection; authoritative signed balances, including advances, remain unchanged.
Sales-group accumulation also uses one shared function: every ticket adds its
MDP tonnes, net tonnes and captured cash/credit/bank amounts once. Grouping keys,
ticket ordering, source selection and existing rounding stay in the adapters.

FIFO settlement advances through invoices instead of repeatedly scanning paid
invoices. It preserves the old sub-cent receipt rounding that occurs when a
settled invoice precedes the next unpaid invoice. A deterministic 1,000-case
legacy comparison covers this; a separate operation-count test bounds invoice
visits linearly. A 2,000-invoice synthetic benchmark measured approximately
1.113 seconds before and 0.002 seconds after. This is an isolated FIFO benchmark,
not an end-to-end website or cloud-sync speed claim. No cross-sync caching or
change to the sync schedule is introduced.

`scripts/test_party_calculations.py` adds customer/vendor/advance/MDP fixtures to
cloud CI. Local pre-sync checks include customer snapshot exclusion, overdue
cutoffs and exclusive supplier bands. Migration tests compared 1,500 randomized
cloud customer/vendor/control responses and 18 actual-data localhost responses
(Customers, Vendors and Control across six ranges) with the committed code.
All matched; no financial payloads were exported. Same-name supplier identity
and authoritative historical ERP balance selection remain untouched.

## Future work outside these verified slices

Cashbook movement construction, physical/bank anchor resolution, daily-ledger
source selection and balance resolution, and ERP fetch/publish orchestration
remain in their existing adapters. For any further extraction, freeze the old
results, test date/identity/rounding boundaries, then extract without changing
formulas. Optimize repeated scans only in a separate measured change. Do not
introduce cross-sync caching without data-version invalidation.
