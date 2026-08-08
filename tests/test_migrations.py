#!/usr/bin/env python3
"""Checks for the split between the base schema and numbered SQL migrations."""

from __future__ import annotations

import tempfile
import unittest

from sqlalchemy import inspect, select, text

from backend.database import (
    NUMBERED_MIGRATION_TABLES,
    build_database,
    run_safe_migrations,
)
from backend.models import (  # noqa: E402
    AdoptionRecord,
    Base,
    CompletedStep,
    ConversationTurn,
    Organization,
    StageState,
    User,
)


class MigrationBootstrapTests(unittest.TestCase):
    def test_postgres_bootstrap_mode_leaves_extension_tables_to_numbered_sql(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            engine, _sessions = build_database(
                f"sqlite:///{temporary_directory}/bootstrap.db"
            )
            try:
                run_safe_migrations(engine, create_numbered_tables=False)
                table_names = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()

        self.assertIn("adoption_records", table_names)
        self.assertIn("stage_states", table_names)
        self.assertIn("schema_migrations", table_names)
        self.assertTrue(NUMBERED_MIGRATION_TABLES.isdisjoint(table_names))

    def test_default_local_bootstrap_creates_extension_tables(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            engine, _sessions = build_database(
                f"sqlite:///{temporary_directory}/complete.db"
            )
            try:
                run_safe_migrations(engine)
                table_names = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()

        self.assertTrue(NUMBERED_MIGRATION_TABLES.issubset(table_names))

    def test_existing_sqlite_rows_upgrade_to_cycle_one_and_allow_cycle_two(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            engine, sessions = build_database(
                f"sqlite:///{temporary_directory}/legacy.db"
            )
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE users (
                            id VARCHAR(36) PRIMARY KEY,
                            email VARCHAR(254) NOT NULL UNIQUE,
                            password_hash TEXT NOT NULL,
                            display_name VARCHAR(120),
                            email_verified_at DATETIME,
                            is_active BOOLEAN NOT NULL,
                            created_at DATETIME NOT NULL,
                            updated_at DATETIME NOT NULL
                        )
                        """
                    )
                )
            Base.metadata.create_all(
                engine,
                tables=[
                    Organization.__table__,
                    AdoptionRecord.__table__,
                ],
            )
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE conversation_turns (
                            id VARCHAR(36) PRIMARY KEY,
                            record_id VARCHAR(36) NOT NULL REFERENCES adoption_records(id),
                            stage VARCHAR(40) NOT NULL,
                            role VARCHAR(16) NOT NULL,
                            content TEXT NOT NULL,
                            ordinal INTEGER NOT NULL,
                            idempotency_key VARCHAR(120),
                            created_by_id VARCHAR(36) REFERENCES users(id),
                            created_at DATETIME NOT NULL,
                            UNIQUE (record_id, stage, ordinal),
                            UNIQUE (record_id, stage, idempotency_key)
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE stage_states (
                            id VARCHAR(36) PRIMARY KEY,
                            record_id VARCHAR(36) NOT NULL REFERENCES adoption_records(id),
                            stage VARCHAR(40) NOT NULL,
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
                            UNIQUE (record_id, stage)
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE completed_steps (
                            id VARCHAR(36) PRIMARY KEY,
                            record_id VARCHAR(36) NOT NULL REFERENCES adoption_records(id),
                            stage VARCHAR(40) NOT NULL,
                            record_text TEXT NOT NULL,
                            completed_by_id VARCHAR(36) NOT NULL REFERENCES users(id),
                            completed_at DATETIME NOT NULL,
                            UNIQUE (record_id, stage)
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email, password_hash, is_active, created_at, updated_at) "
                        "VALUES "
                        "('user-1', 'owner@example.org', 'unused', 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('user-2', 'reviewer@example.org', 'unused', 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO organizations (id, name, created_by_id, created_at) "
                        "VALUES ('org-1', 'Legacy organization', 'user-1', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO adoption_records "
                        "(id, organization_id, title, current_stage, status, created_by_id, "
                        "created_at, updated_at) VALUES "
                        "('record-1', 'org-1', 'Legacy review', 'entry', 'active', "
                        "'user-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO conversation_turns VALUES "
                        "('turn-1', 'record-1', 'entry', 'user', 'Earlier evidence', 1, "
                        "'same-request', 'user-1', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO stage_states VALUES "
                        "('state-1', 'record-1', 'entry', 'complete', '{}', '[]', '[]', "
                        "'[]', '[]', '[]', '[]', '{}', '{}', CURRENT_TIMESTAMP, "
                        "CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO completed_steps VALUES "
                        "('complete-1', 'record-1', 'entry', 'Earlier record', "
                        "'user-1', CURRENT_TIMESTAMP)"
                    )
                )

            run_safe_migrations(engine)
            with sessions() as session, session.begin():
                legacy_user = session.get(User, "user-1")
                second_legacy_user = session.get(User, "user-2")
                self.assertTrue(legacy_user.telemetry_scope_id.startswith("scope."))
                self.assertNotEqual(legacy_user.telemetry_scope_id, legacy_user.id)
                self.assertNotEqual(
                    second_legacy_user.telemetry_scope_id,
                    legacy_user.telemetry_scope_id,
                )
                first_turn = session.get(ConversationTurn, "turn-1")
                first_state = session.get(StageState, "state-1")
                first_completion = session.get(CompletedStep, "complete-1")
                self.assertEqual(first_turn.cycle_number, 1)
                self.assertEqual(first_state.cycle_number, 1)
                self.assertEqual(first_completion.cycle_number, 1)
                session.add(
                    ConversationTurn(
                        id="turn-2",
                        record_id="record-1",
                        stage="entry",
                        cycle_number=2,
                        role="user",
                        content="Fresh evidence",
                        ordinal=1,
                        idempotency_key="same-request",
                        created_by_id="user-1",
                    )
                )
                session.add(
                    StageState(
                        id="state-2",
                        record_id="record-1",
                        stage="entry",
                        cycle_number=2,
                    )
                )
                session.add(
                    CompletedStep(
                        id="complete-2",
                        record_id="record-1",
                        stage="entry",
                        cycle_number=2,
                        record_text="Fresh record",
                        completed_by_id="user-1",
                    )
                )

            with sessions() as session:
                self.assertEqual(
                    len(
                        session.scalars(
                            select(CompletedStep).where(
                                CompletedStep.record_id == "record-1",
                                CompletedStep.stage == "entry",
                            )
                        ).all()
                    ),
                    2,
                )
            versions = set()
            with engine.connect() as connection:
                versions = set(
                    connection.execute(text("SELECT version FROM schema_migrations"))
                    .scalars()
                    .all()
                )
            engine.dispose()
        self.assertIn("007_guided_stage_cycles", versions)
        self.assertIn("008_stable_telemetry_scope", versions)

    def test_sqlite_backfill_carries_forward_an_existing_consent_scope(self):
        legacy_scope = "user." + "a" * 64
        with tempfile.TemporaryDirectory() as temporary_directory:
            engine, sessions = build_database(
                f"sqlite:///{temporary_directory}/legacy-consent.db"
            )
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE users (
                            id VARCHAR(36) PRIMARY KEY,
                            email VARCHAR(254) NOT NULL UNIQUE,
                            password_hash TEXT NOT NULL,
                            display_name VARCHAR(120),
                            email_verified_at DATETIME,
                            is_active BOOLEAN NOT NULL,
                            created_at DATETIME NOT NULL,
                            updated_at DATETIME NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE product_telemetry_consents (
                            decision_id VARCHAR(120) PRIMARY KEY,
                            consent_scope_id VARCHAR(120) NOT NULL,
                            status VARCHAR(24) NOT NULL,
                            actor_id VARCHAR(120) NOT NULL,
                            actor_role VARCHAR(80) NOT NULL,
                            reason_code VARCHAR(120) NOT NULL,
                            supersedes_id VARCHAR(120),
                            decided_at DATETIME NOT NULL,
                            decision_hash VARCHAR(64) NOT NULL UNIQUE
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email, password_hash, is_active, created_at, updated_at) "
                        "VALUES ('legacy-user', 'legacy@example.org', 'unused', 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO product_telemetry_consents "
                        "(decision_id, consent_scope_id, status, actor_id, actor_role, "
                        "reason_code, decided_at, decision_hash) VALUES "
                        "('decision-1', :scope_id, 'granted', 'legacy-user', "
                        "'authenticated_user', 'user-opt-in', CURRENT_TIMESTAMP, :hash)"
                    ),
                    {"scope_id": legacy_scope, "hash": "b" * 64},
                )

            run_safe_migrations(engine)
            with sessions() as session:
                first_scope = session.get(User, "legacy-user").telemetry_scope_id
            run_safe_migrations(engine)
            with sessions() as session:
                second_scope = session.get(User, "legacy-user").telemetry_scope_id
            engine.dispose()

        self.assertEqual(first_scope, legacy_scope)
        self.assertEqual(second_scope, legacy_scope)


if __name__ == "__main__":
    unittest.main()
