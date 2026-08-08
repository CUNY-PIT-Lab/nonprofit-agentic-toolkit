"""Run forward-only database setup before the web process starts."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from .config import Settings
from .database import build_database, run_safe_migrations


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def run_postgres_migrations(engine) -> None:
    """Apply each checked-in PostgreSQL migration once, in filename order."""

    if engine.dialect.name != "postgresql":
        return
    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        version = migration.stem
        with engine.begin() as connection:
            # Railway can briefly overlap pre-deploy containers. Serialize the
            # check-and-apply transaction so two releases cannot both execute
            # the same forward-only migration.
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('nonprofit-agentic-toolkit:migrations'))"
                )
            )
            exists = connection.execute(
                text("SELECT version FROM schema_migrations WHERE version = :version"),
                {"version": version},
            ).first()
            if exists:
                continue
            sql = migration.read_text(encoding="utf-8")
            # psycopg accepts a parameter-free multi-statement script on its raw
            # cursor. It remains inside the SQLAlchemy transaction above.
            cursor = connection.connection.driver_connection.cursor()
            try:
                cursor.execute(sql)
            finally:
                cursor.close()
            connection.execute(
                text(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ),
                {"version": version},
            )


def main() -> None:
    settings = Settings.from_env()
    engine, _session_factory = build_database(settings.database_url)
    try:
        run_safe_migrations(
            engine,
            create_numbered_tables=engine.dialect.name != "postgresql",
        )
        run_postgres_migrations(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
