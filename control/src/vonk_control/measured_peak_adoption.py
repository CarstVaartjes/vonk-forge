"""Fill ``measured_role_peaks`` once from the peaks older versions kept on rows.

Before the table, admission scanned every ``run_nodes`` row and every
``installation_nodes`` row that carried ``observed_memory_bytes`` and matched
its recipe's content digest. Startup folds the highest of those into the table
(per content digest and role) so a deploy loses no measurement; from then on
the table is the only reader's source. A live ``installation_nodes`` column
left by an older schema is read here and nowhere else. The statement only
raises rows, so a second run, or a peak a newer Controller already recorded,
is never lowered.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

_FROM_RUNS = """
SELECT d.content_digest AS digest, n.role AS role, n.observed_memory_bytes AS peak
FROM run_nodes n
JOIN recipe_runs r ON r.id = n.run_id
JOIN recipe_installations i ON i.id = r.installation_id
JOIN catalog_document_revisions d ON d.id = i.recipe_revision_id
WHERE n.observed_memory_bytes IS NOT NULL
"""
_FROM_INSTALLATIONS = """
SELECT d.content_digest, n.role, n.observed_memory_bytes
FROM installation_nodes n
JOIN recipe_installations i ON i.id = n.installation_id
JOIN catalog_document_revisions d ON d.id = i.recipe_revision_id
WHERE n.observed_memory_bytes IS NOT NULL
"""
_RAISE = """
INSERT INTO measured_role_peaks (content_sha256, role, peak_bytes, measured_at)
SELECT digest, role, max(peak), :now FROM ({sources}) AS observed
GROUP BY digest, role
ON CONFLICT (content_sha256, role) DO UPDATE
SET peak_bytes = EXCLUDED.peak_bytes, measured_at = EXCLUDED.measured_at
WHERE measured_role_peaks.peak_bytes < EXCLUDED.peak_bytes
"""


def adopt_measured_role_peaks(connection: Connection) -> int:
    """Raise the table to the highest peak any stored row holds; return rows touched."""

    columns = {
        column["name"]
        for column in inspect(connection).get_columns("installation_nodes")
    }
    sources = [_FROM_RUNS]
    if "observed_memory_bytes" in columns:
        sources.append(_FROM_INSTALLATIONS)
    result = connection.execute(
        text(_RAISE.format(sources=" UNION ALL ".join(sources))),
        {"now": datetime.now(UTC)},
    )
    return max(result.rowcount, 0)
