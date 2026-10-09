import tempfile
import unittest
from pathlib import Path

from flask_migrate import check, downgrade, upgrade
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import app as app_module
from models import IdentityVerificationSession, db


BASE_REVISION = "d9e7b4c2a1f6"
IDENTITY_VERIFICATION_REVISION = "f3a7c9e1b2d4"
PROJECT_HEAD_REVISION = "d36d259b5fdf"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
CREATED_AT = "2026-10-08 00:00:00.000000"
EXPIRES_AT = "2026-10-08 00:10:00.000000"
VERIFIED_AT = "2026-10-08 00:01:00.000000"
CONSUMED_AT = "2026-10-08 00:02:00.000000"
COMPLETION_CLAIMED_AT = "2026-10-08 00:00:30.000000"
COMPLETION_CLAIM_TOKEN = "a" * 32
IDENTITY_SUBJECT_DIGEST = "a" * 64


class IdentityVerificationSchemaTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name)
            / "identity-verification-schema.db"
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
        self._seed_existing_data()

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def _seed_existing_data(self):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "user_id, username, email, password_hash, timezone, "
                    "is_active, created_at, updated_at"
                    ") VALUES ("
                    "1, 'schema-user', 'schema@example.test', 'hash', "
                    "'Asia/Seoul', 1, :created_at, :created_at)"
                ),
                {"created_at": CREATED_AT},
            )

    def _upgrade(self):
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=IDENTITY_VERIFICATION_REVISION,
        )

    def _insert_session(
        self,
        session_number,
        *,
        verification_session_id=None,
        purpose="android_registration",
        provider="toss",
        status="pending",
        provider_transaction_id=None,
        completion_claim_token=None,
        completion_claimed_at=None,
        identity_subject_digest=None,
        age_eligibility=None,
        failure_code=None,
        expires_at=EXPIRES_AT,
        verified_at=None,
        consumed_at=None,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    ):
        if verification_session_id is None:
            verification_session_id = (
                f"00000000-0000-4000-8000-{session_number:012d}"
            )

        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO identity_verification_sessions ("
                    "identity_verification_session_id, "
                    "verification_session_id, purpose, provider, status, "
                    "provider_transaction_id, completion_claim_token, "
                    "completion_claimed_at, identity_subject_digest, "
                    "age_eligibility, failure_code, expires_at, "
                    "verified_at, consumed_at, created_at, updated_at"
                    ") VALUES ("
                    ":identity_verification_session_id, "
                    ":verification_session_id, :purpose, :provider, :status, "
                    ":provider_transaction_id, :completion_claim_token, "
                    ":completion_claimed_at, :identity_subject_digest, "
                    ":age_eligibility, :failure_code, :expires_at, "
                    ":verified_at, :consumed_at, :created_at, :updated_at)"
                ),
                {
                    "identity_verification_session_id": session_number,
                    "verification_session_id": verification_session_id,
                    "purpose": purpose,
                    "provider": provider,
                    "status": status,
                    "provider_transaction_id": provider_transaction_id,
                    "completion_claim_token": completion_claim_token,
                    "completion_claimed_at": completion_claimed_at,
                    "identity_subject_digest": identity_subject_digest,
                    "age_eligibility": age_eligibility,
                    "failure_code": failure_code,
                    "expires_at": expires_at,
                    "verified_at": verified_at,
                    "consumed_at": consumed_at,
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
            )

    def _assert_session_rejected(self, session_number, **values):
        with self.assertRaises(IntegrityError):
            self._insert_session(session_number, **values)

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

    def _read_seed_user(self):
        with self.test_engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT user_id, username, email FROM users "
                    "WHERE user_id = 1"
                )
            ).one()

    def test_model_metadata_matches_independent_workflow_table(self):
        table = IdentityVerificationSession.__table__

        self.assertEqual(table.name, "identity_verification_sessions")
        self.assertEqual(
            tuple(table.c.keys()),
            (
                "identity_verification_session_id",
                "verification_session_id",
                "purpose",
                "provider",
                "status",
                "provider_transaction_id",
                "completion_claim_token",
                "completion_claimed_at",
                "identity_subject_digest",
                "age_eligibility",
                "failure_code",
                "expires_at",
                "verified_at",
                "consumed_at",
                "created_at",
                "updated_at",
            ),
        )
        self.assertEqual(len(table.foreign_keys), 0)
        self.assertEqual(len(IdentityVerificationSession.__mapper__.relationships), 0)
        self.assertIsNotNone(table.c.status.server_default)
        self.assertIsNotNone(table.c.created_at.default)
        self.assertIsNotNone(table.c.updated_at.default)
        self.assertIsNotNone(table.c.updated_at.onupdate)
        self.assertEqual(table.c.completion_claim_token.type.length, 32)
        self.assertEqual(table.c.identity_subject_digest.type.length, 64)

        for column_name in (
            "expires_at",
            "completion_claimed_at",
            "verified_at",
            "consumed_at",
            "created_at",
            "updated_at",
        ):
            self.assertTrue(table.c[column_name].type.timezone)

    def test_upgrade_creates_table_and_required_columns(self):
        self._upgrade()
        inspector = inspect(self.test_engine)

        self.assertIn(
            "identity_verification_sessions",
            inspector.get_table_names(),
        )
        columns = {
            value["name"]: value
            for value in inspector.get_columns(
                "identity_verification_sessions"
            )
        }
        for column_name in (
            "identity_verification_session_id",
            "verification_session_id",
            "purpose",
            "provider",
            "status",
            "expires_at",
            "created_at",
            "updated_at",
        ):
            self.assertFalse(columns[column_name]["nullable"])

        for column_name in (
            "provider_transaction_id",
            "completion_claim_token",
            "completion_claimed_at",
            "identity_subject_digest",
            "age_eligibility",
            "failure_code",
            "verified_at",
            "consumed_at",
        ):
            self.assertTrue(columns[column_name]["nullable"])

    def test_public_session_identifier_is_unique(self):
        self._upgrade()
        public_id = "00000000-0000-4000-8000-000000000001"
        self._insert_session(1, verification_session_id=public_id)

        self._assert_session_rejected(
            2,
            verification_session_id=public_id,
        )

    def test_provider_allowlist_accepts_toss_and_rejects_unknown(self):
        self._upgrade()
        self._insert_session(1, provider="toss")
        self._assert_session_rejected(2, provider="fake")

    def test_purpose_allowlist_accepts_registration_and_rejects_unknown(self):
        self._upgrade()
        self._insert_session(1, purpose="android_registration")
        self._assert_session_rejected(2, purpose="account_linking")

    def test_status_allowlist_accepts_each_lifecycle_status(self):
        self._upgrade()
        self._insert_session(1, status="pending")
        self._insert_session(
            2,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
        )
        self._insert_session(
            3,
            status="failed",
            failure_code="VERIFICATION_FAILED",
        )
        self._insert_session(4, status="expired")
        self._insert_session(
            5,
            status="consumed",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
            consumed_at=CONSUMED_AT,
        )

        self._assert_session_rejected(6, status="unknown")

    def test_database_default_status_is_pending(self):
        self._upgrade()
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO identity_verification_sessions ("
                    "verification_session_id, purpose, provider, expires_at, "
                    "created_at, updated_at"
                    ") VALUES ("
                    "'00000000-0000-4000-8000-000000000001', "
                    "'android_registration', 'toss', :expires_at, "
                    ":created_at, :created_at)"
                ),
                {
                    "expires_at": EXPIRES_AT,
                    "created_at": CREATED_AT,
                },
            )
            status = connection.execute(
                text(
                    "SELECT status FROM identity_verification_sessions"
                )
            ).scalar_one()

        self.assertEqual(status, "pending")

    def test_provider_transaction_is_nullable_and_unique_per_provider(self):
        self._upgrade()
        self._insert_session(1, provider_transaction_id=None)
        self._insert_session(2, provider_transaction_id=None)
        self._insert_session(
            3,
            provider_transaction_id="opaque-provider-transaction",
        )

        self._assert_session_rejected(
            4,
            provider_transaction_id="opaque-provider-transaction",
        )

    def test_completion_claim_columns_are_nullable_and_use_uuid_hex_length(self):
        self._upgrade()
        columns = {
            value["name"]: value
            for value in inspect(self.test_engine).get_columns(
                "identity_verification_sessions"
            )
        }

        self.assertTrue(columns["completion_claim_token"]["nullable"])
        self.assertEqual(columns["completion_claim_token"]["type"].length, 32)
        self.assertTrue(columns["completion_claimed_at"]["nullable"])

    def test_completion_claim_token_and_timestamp_must_exist_together(self):
        self._upgrade()
        self._insert_session(1)
        self._assert_session_rejected(
            2,
            provider_transaction_id="provider-transaction-2",
            completion_claim_token=COMPLETION_CLAIM_TOKEN,
        )
        self._assert_session_rejected(
            3,
            provider_transaction_id="provider-transaction-3",
            completion_claimed_at=COMPLETION_CLAIMED_AT,
        )

    def test_pending_completion_claim_requires_provider_transaction(self):
        self._upgrade()
        self._assert_session_rejected(
            1,
            completion_claim_token=COMPLETION_CLAIM_TOKEN,
            completion_claimed_at=COMPLETION_CLAIMED_AT,
        )
        self._insert_session(
            2,
            provider_transaction_id="provider-transaction-2",
            completion_claim_token=COMPLETION_CLAIM_TOKEN,
            completion_claimed_at=COMPLETION_CLAIMED_AT,
        )

    def test_terminal_statuses_reject_completion_claim(self):
        self._upgrade()
        terminal_states = (
            (
                "verified",
                {
                    "identity_subject_digest": IDENTITY_SUBJECT_DIGEST,
                    "age_eligibility": "AGE_14_OR_OVER",
                    "verified_at": VERIFIED_AT,
                },
            ),
            ("failed", {"failure_code": "VERIFICATION_FAILED"}),
            ("expired", {}),
            (
                "consumed",
                {
                    "identity_subject_digest": IDENTITY_SUBJECT_DIGEST,
                    "age_eligibility": "AGE_14_OR_OVER",
                    "verified_at": VERIFIED_AT,
                    "consumed_at": CONSUMED_AT,
                },
            ),
        )

        for session_number, (status, values) in enumerate(
            terminal_states,
            start=1,
        ):
            with self.subTest(status=status):
                self._assert_session_rejected(
                    session_number,
                    status=status,
                    provider_transaction_id=(
                        f"provider-transaction-{session_number}"
                    ),
                    completion_claim_token=COMPLETION_CLAIM_TOKEN,
                    completion_claimed_at=COMPLETION_CLAIMED_AT,
                    **values,
                )

    def test_completion_claim_rejects_provider_result_fields(self):
        self._upgrade()
        base_claim = {
            "provider_transaction_id": "provider-transaction",
            "completion_claim_token": COMPLETION_CLAIM_TOKEN,
            "completion_claimed_at": COMPLETION_CLAIMED_AT,
        }
        self._insert_session(1, **base_claim)
        self._assert_session_rejected(
            2,
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            **base_claim,
        )
        self._assert_session_rejected(
            3,
            age_eligibility="AGE_14_OR_OVER",
            **base_claim,
        )
        self._assert_session_rejected(
            4,
            verified_at=VERIFIED_AT,
            **base_claim,
        )

    def test_identity_subject_digest_column_uses_64_character_policy(self):
        self._upgrade()
        columns = {
            value["name"]: value
            for value in inspect(self.test_engine).get_columns(
                "identity_verification_sessions"
            )
        }

        digest_column = columns["identity_subject_digest"]
        self.assertTrue(digest_column["nullable"])
        self.assertEqual(digest_column["type"].length, 64)

        self._assert_session_rejected(
            1,
            status="verified",
            identity_subject_digest="a" * 63,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
        )
        self._assert_session_rejected(
            2,
            status="verified",
            identity_subject_digest="a" * 65,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
        )

    def test_pending_requires_null_identity_subject_digest(self):
        self._upgrade()
        self._insert_session(1, status="pending")
        self._assert_session_rejected(
            2,
            status="pending",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
        )

    def test_verified_and_consumed_require_identity_subject_digest(self):
        self._upgrade()
        self._insert_session(
            1,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
        )
        self._assert_session_rejected(
            2,
            status="verified",
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
        )
        self._insert_session(
            3,
            status="consumed",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
            consumed_at=CONSUMED_AT,
        )
        self._assert_session_rejected(
            4,
            status="consumed",
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
            consumed_at=CONSUMED_AT,
        )

    def test_failed_and_expired_require_null_identity_subject_digest(self):
        self._upgrade()
        self._insert_session(1, status="failed")
        self._insert_session(2, status="expired")
        self._assert_session_rejected(
            3,
            status="failed",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
        )
        self._assert_session_rejected(
            4,
            status="expired",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
        )

    def test_identity_subject_digest_is_not_unique_in_session_history(self):
        self._upgrade()
        for session_number in (1, 2):
            self._insert_session(
                session_number,
                status="verified",
                identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
                age_eligibility="AGE_14_OR_OVER",
                verified_at=VERIFIED_AT,
            )

    def test_age_eligibility_allowlist_and_under_14_semantics(self):
        self._upgrade()
        self._insert_session(
            1,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
        )
        self._insert_session(
            2,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="UNDER_14",
            verified_at=VERIFIED_AT,
        )
        self._insert_session(3, status="pending", age_eligibility=None)

        self._assert_session_rejected(
            4,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="ADULT",
            verified_at=VERIFIED_AT,
        )

    def test_verified_state_requires_timestamp_and_age_result(self):
        self._upgrade()
        self._assert_session_rejected(
            1,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
        )
        self._assert_session_rejected(
            2,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            verified_at=VERIFIED_AT,
        )

    def test_consumed_state_requires_and_exclusively_uses_consumed_at(self):
        self._upgrade()
        self._assert_session_rejected(
            1,
            status="consumed",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
        )
        self._assert_session_rejected(
            2,
            status="verified",
            identity_subject_digest=IDENTITY_SUBJECT_DIGEST,
            age_eligibility="AGE_14_OR_OVER",
            verified_at=VERIFIED_AT,
            consumed_at=CONSUMED_AT,
        )

    def test_expiry_must_be_after_creation(self):
        self._upgrade()
        self._assert_session_rejected(
            1,
            expires_at=CREATED_AT,
        )

    def test_expiry_and_core_timestamps_are_required(self):
        self._upgrade()
        self._assert_session_rejected(1, expires_at=None)
        self._assert_session_rejected(2, created_at=None)
        self._assert_session_rejected(3, updated_at=None)

    def test_failure_code_is_nullable_without_a_fixed_allowlist(self):
        self._upgrade()
        self._insert_session(1, status="failed", failure_code=None)
        self._insert_session(
            2,
            status="failed",
            failure_code="FUTURE_DOMAIN_ERROR_CODE",
        )

    def test_sensitive_and_provider_payload_columns_are_absent(self):
        self._upgrade()
        column_names = {
            value["name"]
            for value in inspect(self.test_engine).get_columns(
                "identity_verification_sessions"
            )
        }

        for excluded_column in (
            "user_id",
            "birth_date",
            "birthday",
            "phone",
            "phone_number",
            "name",
            "gender",
            "carrier",
            "nationality",
            "address",
            "resident_registration_number",
            "ci",
            "di",
            "raw_di",
            "identity_subject",
            "state",
            "state_digest",
            "nonce",
            "nonce_digest",
            "provider_response",
            "raw_response",
            "provider_payload",
            "authentication_url",
        ):
            with self.subTest(excluded_column=excluded_column):
                self.assertNotIn(excluded_column, column_names)

    def test_model_repr_does_not_expose_provider_transaction(self):
        transaction_id = "sensitive-provider-transaction"
        identity_subject_digest = IDENTITY_SUBJECT_DIGEST
        session = IdentityVerificationSession(
            verification_session_id=(
                "00000000-0000-4000-8000-000000000001"
            ),
            purpose="android_registration",
            provider="toss",
            provider_transaction_id=transaction_id,
            identity_subject_digest=identity_subject_digest,
            expires_at=EXPIRES_AT,
        )

        self.assertNotIn(transaction_id, repr(session))
        self.assertNotIn(identity_subject_digest, repr(session))

    def test_indexes_and_unique_constraints_match_query_paths(self):
        self._upgrade()
        inspector = inspect(self.test_engine)
        indexes = {
            value["name"]: tuple(value["column_names"])
            for value in inspector.get_indexes(
                "identity_verification_sessions"
            )
        }
        unique_constraints = {
            value["name"]: tuple(value["column_names"])
            for value in inspector.get_unique_constraints(
                "identity_verification_sessions"
            )
        }

        self.assertEqual(
            indexes,
            {
                "ix_identity_verification_session_expires_at": (
                    "expires_at",
                ),
                "ix_identity_verification_session_status_expires_at": (
                    "status",
                    "expires_at",
                ),
            },
        )
        self.assertEqual(
            unique_constraints,
            {
                "uq_identity_verification_session_provider_transaction": (
                    "provider",
                    "provider_transaction_id",
                ),
                "uq_identity_verification_session_public_id": (
                    "verification_session_id",
                ),
            },
        )

    def test_schema_has_expected_checks_and_no_foreign_keys(self):
        self._upgrade()
        inspector = inspect(self.test_engine)
        check_names = {
            value["name"]
            for value in inspector.get_check_constraints(
                "identity_verification_sessions"
            )
        }

        self.assertEqual(
            check_names,
            {
                "ck_identity_verification_session_age_eligibility",
                "ck_identity_verification_session_completion_claim_pair",
                "ck_identity_verification_session_completion_claim_result",
                "ck_identity_verification_session_completion_claim_state",
                "ck_identity_verification_session_completion_claim_transaction",
                "ck_identity_verification_session_consumed_state",
                "ck_identity_verification_session_expiry",
                "ck_identity_verification_session_identity_digest_length",
                "ck_identity_verification_session_identity_digest_state",
                "ck_identity_verification_session_provider",
                "ck_identity_verification_session_purpose",
                "ck_identity_verification_session_status",
                "ck_identity_verification_session_verified_state",
            },
        )
        self.assertEqual(
            inspector.get_foreign_keys("identity_verification_sessions"),
            [],
        )

    def test_migration_upgrade_downgrade_and_reupgrade_are_safe(self):
        user_before = self._read_seed_user()
        self._assert_database_is_healthy()

        self._upgrade()
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=PROJECT_HEAD_REVISION,
        )
        self.assertEqual(self._read_seed_user(), user_before)
        self.assertIn(
            "identity_verification_sessions",
            inspect(self.test_engine).get_table_names(),
        )
        self._assert_database_is_healthy()
        check(directory=MIGRATIONS_DIRECTORY)

        downgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=BASE_REVISION,
        )
        self.assertEqual(self._read_seed_user(), user_before)
        self.assertNotIn(
            "identity_verification_sessions",
            inspect(self.test_engine).get_table_names(),
        )
        self._assert_database_is_healthy()

        self._upgrade()
        self.assertEqual(self._read_seed_user(), user_before)
        self.assertIn(
            "identity_verification_sessions",
            inspect(self.test_engine).get_table_names(),
        )
        self._assert_database_is_healthy()

    def test_migration_revision_chain_is_exact(self):
        self._upgrade()
        with self.test_engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one(),
                IDENTITY_VERIFICATION_REVISION,
            )


if __name__ == "__main__":
    unittest.main()
