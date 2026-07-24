"""Database initialization and scoped session factory."""

from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


def build_database(database_url: str):
    sqlite = database_url.startswith("sqlite")
    options = {
        "pool_pre_ping": not sqlite,
        "connect_args": {"check_same_thread": False} if sqlite else {},
    }
    if sqlite and database_url.endswith(":memory:"):
        options["poolclass"] = StaticPool
    else:
        options["pool_size"] = 5
        options["max_overflow"] = 5
    engine = create_engine(database_url, **options)
    if sqlite:

        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    return engine, factory


def run_safe_migrations(engine) -> None:
    """Create the initial schema and record its version.

    This runner is intentionally additive. Future changes belong in numbered,
    idempotent migration functions before the version is advanced.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version VARCHAR(80) PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
            )
        )
        exists = conn.execute(
            text("SELECT version FROM schema_migrations WHERE version = :version"),
            {"version": "001_initial_auth_records_maps"},
        ).first()
        if not exists:
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ),
                {"version": "001_initial_auth_records_maps"},
            )
        idempotency_version = "002_turn_idempotency"
        exists = conn.execute(
            text("SELECT version FROM schema_migrations WHERE version = :version"),
            {"version": idempotency_version},
        ).first()
        if not exists:
            columns = {
                column["name"]
                for column in inspect(conn).get_columns("conversation_turns")
            }
            if "idempotency_key" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE conversation_turns "
                        "ADD COLUMN idempotency_key VARCHAR(120)"
                    )
                )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_turn_idempotency_idx "
                    "ON conversation_turns (record_id, stage, idempotency_key)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ),
                {"version": idempotency_version},
            )
