"""Direct, resettable PostgreSQL bootstrap for the greenfield control schema."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import Engine

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "0001_initial.sql"


def schema_sql() -> str:
    """Return the checked-in initial schema SQL."""
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def reset_schema(engine: Engine) -> None:
    """Drop all authority tables and recreate the direct initial schema."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            DROP TABLE IF EXISTS control_authority_proposals,
                control_authority_heads, control_authority_revisions,
                source_bundle_archives, job_log_entries,
                operations, runs, installations, placements,
                recipe_import_reports, recipe_revisions, recipes,
                inventory_snapshots, telemetry_snapshots, presence_snapshots,
                certificate_records, enrollment_evidence, enrollment_intents,
                agent_profiles, agent_nodes CASCADE
            """
        )
        connection.exec_driver_sql(schema_sql())


def bootstrap_schema(engine: Engine, *, reset: bool = False) -> None:
    """Create the schema in a fresh database, optionally resetting it first."""
    with engine.begin() as connection:
        if reset:
            connection.exec_driver_sql(
                """
                    DROP TABLE IF EXISTS control_authority_proposals,
                        control_authority_heads, control_authority_revisions,
                        source_bundle_archives, job_log_entries,
                        operations, runs, installations, placements,
                    recipe_import_reports, recipe_revisions, recipes,
                    inventory_snapshots, telemetry_snapshots, presence_snapshots,
                    certificate_records, enrollment_evidence, enrollment_intents,
                    agent_profiles, agent_nodes CASCADE
                """
            )
        connection.exec_driver_sql(schema_sql())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_url")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the authority schema")
    args = parser.parse_args()

    from .db import build_engine

    bootstrap_schema(build_engine(args.database_url), reset=args.reset)


if __name__ == "__main__":
    main()
