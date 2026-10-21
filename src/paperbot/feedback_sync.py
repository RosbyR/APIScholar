"""Pull thumbs clicked on the website into the local store.

The website's Pages Function appends rows to a Cloudflare D1 table `feedback`
(web/schema.sql). This reads rows newer than the last one imported through the D1 REST
API and records each with `Store.add_feedback`, which also makes it a calibration label.
The last imported D1 row id is kept as the `feedback:d1` watermark in `source_state`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from .store import Store

log = logging.getLogger(__name__)

D1_QUERY_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query"
WATERMARK = "feedback:d1"


class D1Error(RuntimeError):
    pass


@dataclass
class SyncStats:
    imported: int = 0
    unknown: list[str] = field(default_factory=list)  # paper ids not in the local store


def pull_feedback(store: Store, client: httpx.Client, account_id: str, database_id: str,
                  token: str) -> SyncStats:
    after = int(store.get_watermark(WATERMARK) or 0)
    r = client.post(
        D1_QUERY_URL.format(account=account_id, database=database_id),
        headers={"Authorization": f"Bearer {token}"},
        json={"sql": "SELECT id, paper_id, rating, note FROM feedback WHERE id > ? ORDER BY id",
              "params": [after]},
    )
    if r.status_code in (401, 403):
        raise D1Error(f"D1 rejected the API token ({r.status_code}); it needs D1 read access")
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise D1Error(f"D1 query failed: {body.get('errors')}")
    rows = body["result"][0]["results"]
    stats = SyncStats()
    for row in rows:
        try:
            store.add_feedback(row["paper_id"], int(row["rating"]), row.get("note"))
            stats.imported += 1
        except KeyError:
            log.warning("feedback for unknown paper %s (D1 row %s) skipped", row["paper_id"], row["id"])
            stats.unknown.append(row["paper_id"])
    if rows:
        store.set_watermark(WATERMARK, str(rows[-1]["id"]))
    store.log_run("feedback:d1", fetched=len(rows), new=stats.imported, errors=len(stats.unknown))
    return stats
