#!/usr/bin/env python3
"""Rules for safely expiring R2 snapshot cache objects.

The monthly archive is the durable financial source.  A snapshot can be
expired only when the public static app has an audited archive reconstruction
for that exact route.  Unknown and canonical routes deliberately fail toward
retention.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit


ARCHIVE_RECONSTRUCTIBLE_RANGE_PATHS = frozenset({
    "/api/sales/",
    "/api/expenses/",
    "/api/boulders/",
    "/api/machines/",
    "/api/vendors/payments/",
    "/api/sync/erp/bank",
    "/api/sync/erp/cash",
    "/api/dashboard/control",
})
ARCHIVE_RECONSTRUCTIBLE_CUSTOMER_PATHS = frozenset({
    "/api/customers/",
    "/api/customers/outstanding",
})


def is_archive_reconstructible_range_snapshot(url: str) -> bool:
    """Return true only for dated routes with a verified browser archive fallback."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if not {"from_date", "to_date"} <= set(query):
        return False
    if parts.path in ARCHIVE_RECONSTRUCTIBLE_RANGE_PATHS:
        return True
    # Customer history needs a dated balance snapshot in addition to the range
    # archive.  An unqualified customer response must remain in R2.
    return parts.path in ARCHIVE_RECONSTRUCTIBLE_CUSTOMER_PATHS and bool(query.get("as_of"))
