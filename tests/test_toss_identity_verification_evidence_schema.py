import tempfile
import unittest
from pathlib import Path

from flask_migrate import check, downgrade, upgrade
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import app as app_module
from models import (
    IdentityVerificationSession,
    TossIdentityVerificationEvidence,
    db,
)


BASE_REVISION = "d9e7b4c2a1f6"
IDENTITY_REVISION = "f3a7c9e1b2d4"
EVIDENCE_REVISION = "d36d259b5fdf"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
CREATED_AT = "2026-10-09 00:00:00.000000"
EXPIRES_AT = "2026-10-09 00:10:00.000000"
TRANSACTION_ID = "synthetic-toss-transaction-sensitive"
SIGNATURE = "synthetic-base64-der-signature-sensitive"


class TossIdentityVerificationEvidenceSchemaTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "toss-evidence-schema.db"
        )
        self.application_context = app_module.app.app_context()
        self.application_context.push()
        db.session.remove()
        self.original_engine = db.engines[None]
        self.test_engine = create_engine(
            "sqlite:///" + self.database_path.as_posix()
        )
        db.engines[None] = self.test_engine

        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=BASE_REVISION,
        )
        self._seed_existing_user()

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def test_model_and_table_names_are_toss_specific(self):
        self.assertEqual(
            TossIdentityVerificationEvidence.__name__,
            "TossIdentityVerificationEvidence",
        )
        self.assertEqual(
            TossIdentityVerificationEvidence.__tablename__,
            "toss_identity_verification_evidence",
        )

    def test_model_metadata_contains_only_minimal_evidence_columns(self):
        table = TossIdentityVerificationEvidence.__table__
        self.assertEqual(
            tuple(table.c.keys()),
            (
                "toss_identity_verification_evidence_id",
                "identity_verification_session_id",
                "provider_transaction_id",
                "signature",
                "created_at",
            ),
        )

    def test_model_has_one_unidirectional_session_relationship(self):
        relationships = {
            item.key
            for item in TossIdentityVerificationEvidence.__mapper__.relationships
        }
        self.assertEqual(
            relationships,
            {"identity_verification_session"},
        )
        self.assertEqual(
            len(IdentityVerificationSession.__mapper__.relationships),
            0,
        )

    def test_upgrade_creates_evidence_table(self):
        self._upgrade_evidence()
        self.assertIn(
            "toss_identity_verification_evidence",
            inspect(self.test_engine).get_table_names(),
        )

    def test_primary_key_is_exact(self):
        self._upgrade_evidence()
        primary_key = inspect(self.test_engine).get_pk_constraint(
            "toss_identity_verification_evidence"
        )
        self.assertEqual(
            primary_key["constrained_columns"],
            ["toss_identity_verification_evidence_id"],
        )

    def test_session_foreign_key_is_required_and_restricts_delete(self):
        self._upgrade_evidence()
        foreign_keys = inspect(self.test_engine).get_foreign_keys(
            "toss_identity_verification_evidence"
        )
        self.assertEqual(len(foreign_keys), 1)
        self.assertEqual(
            foreign_keys[0]["constrained_columns"],
            ["identity_verification_session_id"],
        )
        self.assertEqual(
            foreign_keys[0]["referred_table"],
            "identity_verification_sessions",
        )
        self.assertEqual(
            foreign_keys[0]["referred_columns"],
            ["identity_verification_session_id"],
        )
        self.assertEqual(
            foreign_keys[0]["options"].get("ondelete"),
            "RESTRICT",
        )

    def test_one_session_accepts_only_one_evidence_row(self):
        self._upgrade_and_insert_session(1)
        self._insert_evidence(1, session_id=1)
        with self.assertRaises(IntegrityError):
            self._insert_evidence(
                2,
                session_id=1,
                transaction_id="other-transaction",
            )

    def test_valid_transaction_id_is_preserved(self):
        self._upgrade_and_insert_session(1)
        self._insert_evidence(1, session_id=1)
        row = self._read_evidence(1)
        self.assertEqual(row.provider_transaction_id, TRANSACTION_ID)

    def test_empty_transaction_id_is_rejected(self):
        self._upgrade_and_insert_session(1)
        with self.assertRaises(IntegrityError):
            self._insert_evidence(1, session_id=1, transaction_id="")

    def test_whitespace_transaction_id_policy_is_not_overconstrained(self):
        self._upgrade_and_insert_session(1)
        self._insert_evidence(1, session_id=1, transaction_id="   ")
        self.assertEqual(self._read_evidence(1).provider_transaction_id, "   ")

    def test_signature_is_required(self):
        self._upgrade_and_insert_session(1)
        with self.assertRaises(IntegrityError):
            self._insert_evidence(1, session_id=1, signature=None)

    def test_empty_signature_is_rejected(self):
        self._upgrade_and_insert_session(1)
        with self.assertRaises(IntegrityError):
            self._insert_evidence(1, session_id=1, signature="")

    def test_signature_uses_unbounded_text_type(self):
        self._upgrade_evidence()
        columns = {
            value["name"]: value
            for value in inspect(self.test_engine).get_columns(
                "toss_identity_verification_evidence"
            )
        }
        self.assertEqual(type(columns["signature"]["type"]).__name__, "TEXT")
        self.assertFalse(columns["signature"]["nullable"])

    def test_transaction_id_is_unique_in_evidence_table(self):
        self._upgrade_and_insert_session(1)
        self._insert_session(2, transaction_id="session-transaction-2")
        self._insert_evidence(1, session_id=1)
        with self.assertRaises(IntegrityError):
            self._insert_evidence(2, session_id=2)

    def test_missing_session_foreign_key_is_rejected(self):
        self._upgrade_evidence()
        with self.assertRaises(IntegrityError):
            self._insert_evidence(1, session_id=999)

    def test_parent_session_delete_is_restricted(self):
        self._upgrade_and_insert_session(1)
        self._insert_evidence(1, session_id=1)
        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM identity_verification_sessions "
                        "WHERE identity_verification_session_id = 1"
                    )
                )

    def test_evidence_can_be_deleted_without_deleting_session(self):
        self._upgrade_and_insert_session(1)
        self._insert_evidence(1, session_id=1)
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM toss_identity_verification_evidence "
                    "WHERE toss_identity_verification_evidence_id = 1"
                )
            )
        self.assertEqual(self._session_count(), 1)

    def test_sensitive_and_raw_provider_columns_are_absent(self):
        self._upgrade_evidence()
        columns = {
            value["name"]
            for value in inspect(self.test_engine).get_columns(
                "toss_identity_verification_evidence"
            )
        }
        for excluded in (
            "birthday",
            "birth_date",
            "di",
            "ci",
            "name",
            "phone",
            "gender",
            "nationality",
            "age_group",
            "raw_response",
            "provider_response",
            "session_key",
            "access_token",
            "client_id",
            "client_secret",
        ):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, columns)

    def test_created_at_is_required_and_has_model_default(self):
        self._upgrade_evidence()
        column = TossIdentityVerificationEvidence.__table__.c.created_at
        self.assertFalse(column.nullable)
        self.assertIsNotNone(column.default)
        self.assertIsNone(column.onupdate)
        self.assertTrue(column.type.timezone)

    def test_model_repr_hides_transaction_and_signature(self):
        evidence = TossIdentityVerificationEvidence(
            identity_verification_session_id=1,
            provider_transaction_id=TRANSACTION_ID,
            signature=SIGNATURE,
        )
        representation = repr(evidence)
        self.assertNotIn(TRANSACTION_ID, representation)
        self.assertNotIn(SIGNATURE, representation)

    def test_unique_constraints_are_exact(self):
        self._upgrade_evidence()
        constraints = {
            value["name"]: tuple(value["column_names"])
            for value in inspect(self.test_engine).get_unique_constraints(
                "toss_identity_verification_evidence"
            )
        }
        self.assertEqual(
            constraints,
            {
                "uq_toss_identity_evidence_session": (
                    "identity_verification_session_id",
                ),
                "uq_toss_identity_evidence_transaction": (
                    "provider_transaction_id",
                ),
            },
        )

    def test_check_constraints_are_exact(self):
        self._upgrade_evidence()
        check_names = {
            value["name"]
            for value in inspect(self.test_engine).get_check_constraints(
                "toss_identity_verification_evidence"
            )
        }
        self.assertEqual(
            check_names,
            {
                "ck_toss_identity_evidence_signature_not_empty",
                "ck_toss_identity_evidence_transaction_not_empty",
            },
        )

    def test_migration_revision_chain_is_exact(self):
        self._upgrade_evidence()
        with self.test_engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one(),
                EVIDENCE_REVISION,
            )

    def test_upgrade_preserves_existing_tables_and_rows(self):
        self._upgrade_identity()
        self._insert_session(1)
        before = self._existing_table_snapshot()

        self._upgrade_evidence()

        self.assertEqual(self._existing_table_snapshot(), before)

    def test_upgrade_downgrade_and_reupgrade_are_safe(self):
        self._upgrade_identity()
        self._insert_session(1)
        before = self._existing_table_snapshot()

        self._upgrade_evidence()
        self._insert_evidence(1, session_id=1)
        self._assert_database_is_healthy()
        check(directory=MIGRATIONS_DIRECTORY)

        downgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=IDENTITY_REVISION,
        )
        self.assertNotIn(
            "toss_identity_verification_evidence",
            inspect(self.test_engine).get_table_names(),
        )
        self.assertEqual(self._existing_table_snapshot(), before)
        self._assert_database_is_healthy()

        self._upgrade_evidence()
        self.assertIn(
            "toss_identity_verification_evidence",
            inspect(self.test_engine).get_table_names(),
        )
        self.assertEqual(self._existing_table_snapshot(), before)
        self._assert_database_is_healthy()

    def test_foreign_key_and_integrity_checks_pass(self):
        self._upgrade_and_insert_session(1)
        self._insert_evidence(1, session_id=1)
        self._assert_database_is_healthy()

    def _seed_existing_user(self):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "user_id, username, email, password_hash, timezone, "
                    "is_active, created_at, updated_at"
                    ") VALUES ("
                    "1, 'evidence-user', 'evidence@example.test', 'hash', "
                    "'Asia/Seoul', 1, :created_at, :created_at)"
                ),
                {"created_at": CREATED_AT},
            )

    def _upgrade_identity(self):
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=IDENTITY_REVISION,
        )

    def _upgrade_evidence(self):
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=EVIDENCE_REVISION,
        )

    def _upgrade_and_insert_session(self, session_id):
        self._upgrade_evidence()
        self._insert_session(session_id)

    def _insert_session(self, session_id, *, transaction_id=TRANSACTION_ID):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO identity_verification_sessions ("
                    "identity_verification_session_id, "
                    "verification_session_id, purpose, provider, status, "
                    "provider_transaction_id, expires_at, created_at, updated_at"
                    ") VALUES ("
                    ":session_id, :public_id, 'android_registration', 'toss', "
                    "'pending', :transaction_id, :expires_at, "
                    ":created_at, :created_at)"
                ),
                {
                    "session_id": session_id,
                    "public_id": (
                        f"00000000-0000-4000-8000-{session_id:012d}"
                    ),
                    "transaction_id": transaction_id,
                    "expires_at": EXPIRES_AT,
                    "created_at": CREATED_AT,
                },
            )

    def _insert_evidence(
        self,
        evidence_id,
        *,
        session_id,
        transaction_id=TRANSACTION_ID,
        signature=SIGNATURE,
    ):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO toss_identity_verification_evidence ("
                    "toss_identity_verification_evidence_id, "
                    "identity_verification_session_id, "
                    "provider_transaction_id, signature, created_at"
                    ") VALUES ("
                    ":evidence_id, :session_id, :transaction_id, "
                    ":signature, :created_at)"
                ),
                {
                    "evidence_id": evidence_id,
                    "session_id": session_id,
                    "transaction_id": transaction_id,
                    "signature": signature,
                    "created_at": CREATED_AT,
                },
            )

    def _read_evidence(self, evidence_id):
        with self.test_engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT provider_transaction_id, signature "
                    "FROM toss_identity_verification_evidence "
                    "WHERE toss_identity_verification_evidence_id = :id"
                ),
                {"id": evidence_id},
            ).one()

    def _session_count(self):
        with self.test_engine.connect() as connection:
            return connection.execute(
                text("SELECT count(*) FROM identity_verification_sessions")
            ).scalar_one()

    def _existing_table_snapshot(self):
        table_names = sorted(
            name
            for name in inspect(self.test_engine).get_table_names()
            if name not in (
                "alembic_version",
                "toss_identity_verification_evidence",
            )
        )
        with self.test_engine.connect() as connection:
            counts = {
                name: connection.execute(
                    text(f'SELECT count(*) FROM "{name}"')
                ).scalar_one()
                for name in table_names
            }
        return table_names, counts

    def _assert_database_is_healthy(self):
        with self.test_engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("PRAGMA foreign_key_check")).all(),
                [],
            )
            self.assertEqual(
                connection.execute(text("PRAGMA integrity_check")).scalar_one(),
                "ok",
            )


if __name__ == "__main__":
    unittest.main()
