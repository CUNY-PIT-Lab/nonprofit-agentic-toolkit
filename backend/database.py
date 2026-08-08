"""Database initialization and scoped session factory."""

from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base, new_telemetry_scope_id

# Register extension tables on the shared metadata before create_all runs. The
# modules do not import this database module, so these imports are cycle-safe.
from . import fieldwork_store as _fieldwork_store  # noqa: F401,E402
from . import evolution_store as _evolution_store  # noqa: F401,E402
from . import pathway_store as _pathway_store  # noqa: F401,E402


NUMBERED_MIGRATION_TABLES = frozenset(
    {
        "fieldwork_projects",
        "fieldwork_cycles",
        "fieldwork_branches",
        "fieldwork_events",
        "fieldwork_scope_versions",
        "pathway_versions",
        "pathway_runs",
        "pathway_facts",
        "pathway_approvals",
        "pathway_transitions",
        "product_telemetry_events",
        "product_telemetry_consents",
        "evolution_proposals",
        "evolution_reviews",
        "evolution_rollout_actions",
        "evolution_evaluations",
    }
)


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


def _sqlite_stage_cycles_current(connection) -> bool:
    inspector = inspect(connection)
    expected_uniques = {
        "conversation_turns": {
            ("record_id", "stage", "cycle_number", "ordinal"),
            ("record_id", "stage", "cycle_number", "idempotency_key"),
        },
        "stage_states": {("record_id", "stage", "cycle_number")},
        "completed_steps": {("record_id", "stage", "cycle_number")},
    }
    for table_name, expected in expected_uniques.items():
        columns = {item["name"] for item in inspector.get_columns(table_name)}
        if "cycle_number" not in columns:
            return False
        actual = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table_name)
        }
        if not expected.issubset(actual):
            return False
    return True


def _rebuild_sqlite_stage_cycle_tables(engine) -> None:
    """Batch-migrate SQLite constraints while retaining every cycle-1 row."""

    with engine.connect() as connection:
        if _sqlite_stage_cycles_current(connection):
            return
        source_columns = {
            table_name: {
                item["name"] for item in inspect(connection).get_columns(table_name)
            }
            for table_name in (
                "conversation_turns",
                "stage_states",
                "completed_steps",
            )
        }
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            with connection.begin():
                connection.exec_driver_sql(
                    """
                    CREATE TABLE _cycle_conversation_turns (
                        id VARCHAR(36) NOT NULL PRIMARY KEY,
                        record_id VARCHAR(36) NOT NULL
                            REFERENCES adoption_records(id) ON DELETE CASCADE,
                        stage VARCHAR(40) NOT NULL,
                        cycle_number INTEGER NOT NULL DEFAULT 1
                            CHECK (cycle_number >= 1),
                        role VARCHAR(16) NOT NULL,
                        content TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        idempotency_key VARCHAR(120),
                        created_by_id VARCHAR(36)
                            REFERENCES users(id) ON DELETE SET NULL,
                        created_at DATETIME NOT NULL,
                        CONSTRAINT uq_turn_ordinal UNIQUE
                            (record_id, stage, cycle_number, ordinal),
                        CONSTRAINT uq_turn_idempotency UNIQUE
                            (record_id, stage, cycle_number, idempotency_key)
                    )
                    """
                )
                turn_cycle = (
                    "cycle_number"
                    if "cycle_number" in source_columns["conversation_turns"]
                    else "1"
                )
                turn_idempotency = (
                    "idempotency_key"
                    if "idempotency_key" in source_columns["conversation_turns"]
                    else "NULL"
                )
                connection.exec_driver_sql(
                    f"""
                    INSERT INTO _cycle_conversation_turns (
                        id, record_id, stage, cycle_number, role, content,
                        ordinal, idempotency_key, created_by_id, created_at
                    )
                    SELECT id, record_id, stage, {turn_cycle}, role, content,
                           ordinal, {turn_idempotency}, created_by_id, created_at
                    FROM conversation_turns
                    """
                )

                connection.exec_driver_sql(
                    """
                    CREATE TABLE _cycle_stage_states (
                        id VARCHAR(36) NOT NULL PRIMARY KEY,
                        record_id VARCHAR(36) NOT NULL
                            REFERENCES adoption_records(id) ON DELETE CASCADE,
                        stage VARCHAR(40) NOT NULL,
                        cycle_number INTEGER NOT NULL DEFAULT 1
                            CHECK (cycle_number >= 1),
                        status VARCHAR(24) NOT NULL,
                        coverage JSON NOT NULL,
                        facts JSON NOT NULL,
                        open_questions JSON NOT NULL,
                        contradictions JSON NOT NULL,
                        blockers JSON NOT NULL,
                        owners JSON NOT NULL,
                        delegations JSON NOT NULL,
                        signals JSON NOT NULL,
                        next_action JSON NOT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_stage_state UNIQUE
                            (record_id, stage, cycle_number)
                    )
                    """
                )
                state_cycle = (
                    "cycle_number"
                    if "cycle_number" in source_columns["stage_states"]
                    else "1"
                )
                connection.exec_driver_sql(
                    f"""
                    INSERT INTO _cycle_stage_states (
                        id, record_id, stage, cycle_number, status, coverage,
                        facts, open_questions, contradictions, blockers, owners,
                        delegations, signals, next_action, created_at, updated_at
                    )
                    SELECT id, record_id, stage, {state_cycle}, status, coverage,
                           facts, open_questions, contradictions, blockers, owners,
                           delegations, signals, next_action, created_at, updated_at
                    FROM stage_states
                    """
                )

                connection.exec_driver_sql(
                    """
                    CREATE TABLE _cycle_completed_steps (
                        id VARCHAR(36) NOT NULL PRIMARY KEY,
                        record_id VARCHAR(36) NOT NULL
                            REFERENCES adoption_records(id) ON DELETE CASCADE,
                        stage VARCHAR(40) NOT NULL,
                        cycle_number INTEGER NOT NULL DEFAULT 1
                            CHECK (cycle_number >= 1),
                        record_text TEXT NOT NULL,
                        completed_by_id VARCHAR(36) NOT NULL
                            REFERENCES users(id) ON DELETE RESTRICT,
                        completed_at DATETIME NOT NULL,
                        CONSTRAINT uq_completed_stage UNIQUE
                            (record_id, stage, cycle_number)
                    )
                    """
                )
                completed_cycle = (
                    "cycle_number"
                    if "cycle_number" in source_columns["completed_steps"]
                    else "1"
                )
                connection.exec_driver_sql(
                    f"""
                    INSERT INTO _cycle_completed_steps (
                        id, record_id, stage, cycle_number, record_text,
                        completed_by_id, completed_at
                    )
                    SELECT id, record_id, stage, {completed_cycle}, record_text,
                           completed_by_id, completed_at
                    FROM completed_steps
                    """
                )

                for table_name in (
                    "conversation_turns",
                    "stage_states",
                    "completed_steps",
                ):
                    connection.exec_driver_sql(f"DROP TABLE {table_name}")
                    connection.exec_driver_sql(
                        f"ALTER TABLE _cycle_{table_name} RENAME TO {table_name}"
                    )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_turns_record_stage ON conversation_turns "
                    "(record_id, stage, cycle_number, ordinal)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_stage_states_record ON stage_states "
                    "(record_id, stage, cycle_number)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_completed_record ON completed_steps "
                    "(record_id, cycle_number, completed_at)"
                )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError("SQLite stage-cycle migration broke foreign keys")


def _ensure_sqlite_stable_telemetry_scopes(engine) -> None:
    """Backfill durable random consent scopes in pre-008 SQLite databases."""

    with engine.begin() as connection:
        columns = {
            item["name"] for item in inspect(connection).get_columns("users")
        }
        if "telemetry_scope_id" not in columns:
            # SQLite cannot add a required column without a constant default.
            # Backfill first, then enforce required values with triggers.
            connection.execute(
                text("ALTER TABLE users ADD COLUMN telemetry_scope_id VARCHAR(120)")
            )

        table_names = set(inspect(connection).get_table_names())
        legacy_scope_by_user: dict[str, str] = {}
        if "product_telemetry_consents" in table_names:
            # A branch build may already have written consent before migration
            # 008. Carry that scope forward so withdrawal still governs its
            # existing events; no auth secret is needed or consulted.
            for actor_id, consent_scope_id in connection.execute(
                text(
                    "SELECT actor_id, consent_scope_id "
                    "FROM product_telemetry_consents "
                    "ORDER BY decided_at, decision_id"
                )
            ):
                if actor_id and consent_scope_id:
                    legacy_scope_by_user[actor_id] = consent_scope_id

        rows = connection.execute(
            text("SELECT id, telemetry_scope_id FROM users ORDER BY id")
        ).all()
        used: set[str] = set()
        for user_id, persisted_scope in rows:
            candidate = (persisted_scope or "").strip()
            if not candidate or candidate == user_id or candidate in used:
                legacy_scope = legacy_scope_by_user.get(user_id, "").strip()
                candidate = (
                    legacy_scope
                    if legacy_scope
                    and legacy_scope != user_id
                    and legacy_scope not in used
                    and len(legacy_scope) <= 120
                    else new_telemetry_scope_id()
                )
                while candidate == user_id or candidate in used:
                    candidate = new_telemetry_scope_id()
                connection.execute(
                    text(
                        "UPDATE users SET telemetry_scope_id = :scope_id "
                        "WHERE id = :user_id"
                    ),
                    {"scope_id": candidate, "user_id": user_id},
                )
            used.add(candidate)

        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_telemetry_scope_id "
                "ON users (telemetry_scope_id)"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS users_telemetry_scope_required_insert "
                "BEFORE INSERT ON users "
                "WHEN NEW.telemetry_scope_id IS NULL OR NEW.telemetry_scope_id = '' "
                "BEGIN SELECT RAISE(ABORT, 'telemetry_scope_id is required'); END"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS users_telemetry_scope_required_update "
                "BEFORE UPDATE OF telemetry_scope_id ON users "
                "WHEN NEW.telemetry_scope_id IS NULL OR NEW.telemetry_scope_id = '' "
                "BEGIN SELECT RAISE(ABORT, 'telemetry_scope_id is required'); END"
            )
        )
        connection.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                "VALUES (:version, CURRENT_TIMESTAMP)"
            ),
            {"version": "008_stable_telemetry_scope"},
        )


def run_safe_migrations(engine, *, create_numbered_tables: bool = True) -> None:
    """Create the initial schema and record its version.

    PostgreSQL pre-deploys exclude extension tables here so their numbered SQL
    files create the tables with the database-specific constraints and triggers.
    SQLite and ordinary local setup continue to create the complete ORM schema.
    """
    tables = None
    if not create_numbered_tables:
        tables = [
            table
            for table in Base.metadata.sorted_tables
            if table.name not in NUMBERED_MIGRATION_TABLES
        ]
    Base.metadata.create_all(engine, tables=tables)
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
                    "ON conversation_turns "
                    + (
                        "(record_id, stage, cycle_number, idempotency_key)"
                        if "cycle_number" in columns
                        else "(record_id, stage, idempotency_key)"
                    )
                )
            )
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ),
                {"version": idempotency_version},
            )
        stage_state_version = "003_stage_state_coverage"
        exists = conn.execute(
            text("SELECT version FROM schema_migrations WHERE version = :version"),
            {"version": stage_state_version},
        ).first()
        if not exists:
            # Base.metadata.create_all above already builds stage_states on a new
            # database. This step only records the version for existing ones.
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ),
                {"version": stage_state_version},
            )
    if engine.dialect.name == "sqlite":
        _rebuild_sqlite_stage_cycle_tables(engine)
        _ensure_sqlite_stable_telemetry_scopes(engine)
        with engine.begin() as conn:
            # The named compatibility index from migration 002 is redundant
            # with the cycle-aware table constraint and may still have the old
            # three-column shape on an already-initialized local database.
            conn.execute(text("DROP INDEX IF EXISTS uq_turn_idempotency_idx"))
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                    "VALUES (:version, CURRENT_TIMESTAMP)"
                ),
                {"version": "007_guided_stage_cycles"},
            )
