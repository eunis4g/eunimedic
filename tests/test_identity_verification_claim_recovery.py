import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask_migrate import upgrade
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import Session

import app as app_module
from models import IdentityVerificationSession, db
from services.age_eligibility_service import AgeEligibility
from services.fake_identity_verification_provider import (
    FakeIdentityVerificationProvider,
)
from services.identity_verification_provider import (
    IdentityVerificationProviderName,
)
from services.identity_verification_service import (
    IdentityVerificationClaimRecoveryResult,
    _claim_completion,
    _finalize_verified_completion,
    complete_identity_verification,
    recover_stale_identity_verification_claims,
    start_identity_verification,
)


MIGRATION_HEAD = "f3a7c9e1b2d4"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
ACTION_TIME = datetime(2026, 10, 8, 12, tzinfo=UTC)
CLAIM_TIMEOUT = timedelta(minutes=30)
CUTOFF = ACTION_TIME - CLAIM_TIMEOUT
STALE_CLAIMED_AT = CUTOFF - timedelta(microseconds=1)
CLAIM_TOKEN_A = "a" * 32
CLAIM_TOKEN_B = "b" * 32
IDENTITY_DIGEST = "c" * 64
HMAC_KEY = b"synthetic-recovery-test-key"


def stored_as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


class IdentityVerificationClaimRecoveryTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "identity-recovery.db"
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
            revision=MIGRATION_HEAD,
        )

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def add_session(
        self,
        session_number,
        *,
        status="pending",
        claim_token=CLAIM_TOKEN_A,
        claimed_at=STALE_CLAIMED_AT,
        provider_transaction_id=None,
        failure_code=None,
        created_at=ACTION_TIME - timedelta(hours=2),
        expires_at=ACTION_TIME + timedelta(hours=1),
    ):
        if provider_transaction_id is None:
            provider_transaction_id = f"provider-transaction-{session_number}"

        values = {
            "verification_session_id": (
                "00000000-0000-4000-8000-"
                f"{session_number:012d}"
            ),
            "purpose": "android_registration",
            "provider": "toss",
            "status": status,
            "provider_transaction_id": provider_transaction_id,
            "completion_claim_token": claim_token,
            "completion_claimed_at": claimed_at,
            "failure_code": failure_code,
            "created_at": created_at,
            "updated_at": created_at,
            "expires_at": expires_at,
        }
        if status in ("verified", "consumed"):
            values.update(
                identity_subject_digest=IDENTITY_DIGEST,
                age_eligibility="AGE_14_OR_OVER",
                verified_at=created_at + timedelta(minutes=1),
            )
        if status == "consumed":
            values["consumed_at"] = created_at + timedelta(minutes=2)

        verification_session = IdentityVerificationSession(**values)
        db.session.add(verification_session)
        db.session.commit()
        return verification_session

    def refresh(self, verification_session):
        db.session.expire_all()
        return db.session.get(
            IdentityVerificationSession,
            verification_session.identity_verification_session_id,
        )

    def recover(self, session=db.session, **overrides):
        arguments = {
            "action_time": ACTION_TIME,
            "claim_timeout": CLAIM_TIMEOUT,
        }
        arguments.update(overrides)
        return recover_stale_identity_verification_claims(
            session,
            **arguments,
        )

    def test_stale_pending_claim_is_recovered(self):
        verification_session = self.add_session(1)

        result = self.recover()
        db.session.commit()
        stored = self.refresh(verification_session)

        self.assertIsInstance(
            result,
            IdentityVerificationClaimRecoveryResult,
        )
        self.assertEqual(result.recovered_count, 1)
        self.assertIsNone(stored.completion_claim_token)
        self.assertIsNone(stored.completion_claimed_at)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored_as_utc(stored.updated_at), ACTION_TIME)
        with self.assertRaises(FrozenInstanceError):
            result.recovered_count = 2

    def test_fresh_pending_claim_is_not_recovered(self):
        verification_session = self.add_session(
            1,
            claimed_at=CUTOFF + timedelta(microseconds=1),
        )

        result = self.recover()
        db.session.commit()
        stored = self.refresh(verification_session)

        self.assertEqual(result.recovered_count, 0)
        self.assertEqual(stored.completion_claim_token, CLAIM_TOKEN_A)

    def test_claim_exactly_at_cutoff_is_not_recovered(self):
        verification_session = self.add_session(1, claimed_at=CUTOFF)

        result = self.recover()
        db.session.commit()
        stored = self.refresh(verification_session)

        self.assertEqual(result.recovered_count, 0)
        self.assertEqual(stored.completion_claim_token, CLAIM_TOKEN_A)

    def test_claim_one_microsecond_before_cutoff_is_recovered(self):
        verification_session = self.add_session(
            1,
            claimed_at=CUTOFF - timedelta(microseconds=1),
        )

        result = self.recover()
        db.session.commit()

        self.assertEqual(result.recovered_count, 1)
        self.assertIsNone(
            self.refresh(verification_session).completion_claim_token
        )

    def test_pending_session_without_claim_is_ignored(self):
        verification_session = self.add_session(
            1,
            claim_token=None,
            claimed_at=None,
        )

        result = self.recover()
        db.session.commit()
        stored = self.refresh(verification_session)

        self.assertEqual(result.recovered_count, 0)
        self.assertIsNone(stored.completion_claim_token)
        self.assertEqual(
            stored_as_utc(stored.updated_at),
            ACTION_TIME - timedelta(hours=2),
        )

    def test_terminal_rows_are_never_repaired_even_if_claim_data_exists(self):
        sessions = [
            self.add_session(
                session_number,
                status=status,
                claim_token=None,
                claimed_at=None,
                failure_code=(
                    "verification_failed" if status == "failed" else None
                ),
            )
            for session_number, status in enumerate(
                ("verified", "failed", "expired", "consumed"),
                start=1,
            )
        ]
        db.session.execute(text("PRAGMA ignore_check_constraints=ON"))
        try:
            db.session.execute(
                update(IdentityVerificationSession)
                .values(
                    completion_claim_token=CLAIM_TOKEN_A,
                    completion_claimed_at=STALE_CLAIMED_AT,
                )
                .execution_options(synchronize_session=False)
            )
            db.session.commit()
        finally:
            db.session.execute(text("PRAGMA ignore_check_constraints=OFF"))
            db.session.commit()

        result = self.recover()
        db.session.commit()

        self.assertEqual(result.recovered_count, 0)
        for verification_session in sessions:
            stored = self.refresh(verification_session)
            self.assertEqual(stored.completion_claim_token, CLAIM_TOKEN_A)
            self.assertEqual(
                stored_as_utc(stored.completion_claimed_at),
                STALE_CLAIMED_AT,
            )

    def test_recovery_preserves_domain_and_lifecycle_fields(self):
        created_at = ACTION_TIME - timedelta(hours=3)
        expires_at = ACTION_TIME + timedelta(hours=2)
        verification_session = self.add_session(
            1,
            failure_code="provider_unavailable",
            created_at=created_at,
            expires_at=expires_at,
        )
        transaction_id = verification_session.provider_transaction_id

        result = self.recover()
        db.session.commit()
        stored = self.refresh(verification_session)

        self.assertEqual(result.recovered_count, 1)
        self.assertEqual(stored.provider_transaction_id, transaction_id)
        self.assertEqual(stored.failure_code, "provider_unavailable")
        self.assertEqual(stored_as_utc(stored.created_at), created_at)
        self.assertEqual(stored_as_utc(stored.expires_at), expires_at)
        self.assertIsNone(stored.verified_at)
        self.assertIsNone(stored.consumed_at)
        self.assertIsNone(stored.age_eligibility)
        self.assertIsNone(stored.identity_subject_digest)

    def test_expired_by_time_pending_session_only_loses_stale_claim(self):
        verification_session = self.add_session(
            1,
            created_at=ACTION_TIME - timedelta(hours=2),
            expires_at=ACTION_TIME - timedelta(hours=1),
        )

        result = self.recover()
        db.session.commit()
        stored = self.refresh(verification_session)

        self.assertEqual(result.recovered_count, 1)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(
            stored_as_utc(stored.expires_at),
            ACTION_TIME - timedelta(hours=1),
        )

    def test_recovered_count_is_exact_and_zero_when_nothing_is_stale(self):
        self.add_session(1)
        self.add_session(2)
        self.add_session(3, claimed_at=CUTOFF)
        self.add_session(4, claim_token=None, claimed_at=None)

        first = self.recover()
        db.session.commit()
        second = self.recover()
        db.session.commit()

        self.assertEqual(first.recovered_count, 2)
        self.assertEqual(second.recovered_count, 0)

    def test_nonpositive_timeout_is_rejected_without_changes(self):
        verification_session = self.add_session(1)

        for timeout in (timedelta(0), timedelta(microseconds=-1)):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.recover(claim_timeout=timeout)

        db.session.rollback()
        stored = self.refresh(verification_session)
        self.assertEqual(stored.completion_claim_token, CLAIM_TOKEN_A)

    def test_naive_action_time_is_rejected_without_changes(self):
        verification_session = self.add_session(1)

        with self.assertRaises(ValueError):
            self.recover(action_time=datetime(2026, 10, 8, 12))

        db.session.rollback()
        self.assertEqual(
            self.refresh(verification_session).completion_claim_token,
            CLAIM_TOKEN_A,
        )

    def test_non_utc_action_time_is_normalized_to_utc(self):
        verification_session = self.add_session(1)
        seoul_time = ACTION_TIME.astimezone(
            timezone(timedelta(hours=9))
        )

        result = self.recover(action_time=seoul_time)
        db.session.commit()
        stored = self.refresh(verification_session)

        self.assertEqual(result.recovered_count, 1)
        self.assertEqual(stored_as_utc(stored.updated_at), ACTION_TIME)

    def test_recovery_does_not_commit_and_caller_can_rollback(self):
        verification_session = self.add_session(1)
        session_id = verification_session.identity_verification_session_id

        with Session(self.test_engine) as recovery_session:
            result = self.recover(recovery_session)
            recovered_view = recovery_session.get(
                IdentityVerificationSession,
                session_id,
            )
            with Session(self.test_engine) as other_session:
                committed_view = other_session.get(
                    IdentityVerificationSession,
                    session_id,
                )

            self.assertEqual(result.recovered_count, 1)
            self.assertIsNone(recovered_view.completion_claim_token)
            self.assertEqual(
                committed_view.completion_claim_token,
                CLAIM_TOKEN_A,
            )
            recovery_session.rollback()

        db.session.expire_all()
        self.assertEqual(
            db.session.get(
                IdentityVerificationSession,
                session_id,
            ).completion_claim_token,
            CLAIM_TOKEN_A,
        )

    def test_two_database_sessions_recover_each_row_only_once(self):
        verification_session = self.add_session(1)

        with Session(self.test_engine) as first_session:
            first = self.recover(first_session)
            first_session.commit()

        with Session(self.test_engine) as second_session:
            second = self.recover(second_session)
            second_session.commit()

        stored = self.refresh(verification_session)
        self.assertEqual(first.recovered_count, 1)
        self.assertEqual(second.recovered_count, 0)
        self.assertIsNone(stored.completion_claim_token)

    def test_old_owner_cannot_finalize_after_new_owner_claims(self):
        verification_session = self.add_session(1)
        public_id = verification_session.verification_session_id

        with Session(self.test_engine) as recovery_session:
            self.assertEqual(
                self.recover(recovery_session).recovered_count,
                1,
            )
            recovery_session.commit()

        with Session(self.test_engine) as new_owner_session:
            self.assertTrue(
                _claim_completion(
                    new_owner_session,
                    verification_session_id=public_id,
                    claim_token=CLAIM_TOKEN_B,
                    action_time=ACTION_TIME,
                )
            )

        with Session(self.test_engine) as old_owner_session:
            self.assertFalse(
                _finalize_verified_completion(
                    old_owner_session,
                    verification_session_id=public_id,
                    claim_token=CLAIM_TOKEN_A,
                    action_time=ACTION_TIME,
                    age_eligibility=AgeEligibility.AGE_14_OR_OVER,
                    identity_subject_digest=IDENTITY_DIGEST,
                )
            )

        stored = self.refresh(verification_session)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.completion_claim_token, CLAIM_TOKEN_B)
        self.assertIsNone(stored.identity_subject_digest)

    def test_recovered_session_can_be_completed_with_a_new_claim(self):
        provider = FakeIdentityVerificationProvider(
            provider=IdentityVerificationProviderName.TOSS,
            verified_birth_date=datetime(2000, 1, 1).date(),
            identity_subject="recovery-retry-subject",
            transaction_id_factory=lambda: "recovery-provider-transaction",
        )
        public_id = "00000000-0000-4000-8000-000000000001"
        started = start_identity_verification(
            db.session,
            provider=provider,
            action_time=ACTION_TIME - timedelta(hours=2),
            session_ttl=timedelta(hours=4),
            verification_session_id_factory=lambda: public_id,
        )
        with Session(self.test_engine) as owner_session:
            self.assertTrue(
                _claim_completion(
                    owner_session,
                    verification_session_id=public_id,
                    claim_token=CLAIM_TOKEN_A,
                    action_time=ACTION_TIME - timedelta(hours=1),
                )
            )

        recovery = self.recover()
        db.session.commit()
        completed = complete_identity_verification(
            db.session,
            verification_session_id=started.verification_session_id,
            provider=provider,
            action_time=ACTION_TIME,
            identity_subject_hmac_key=HMAC_KEY,
            claim_token_factory=lambda: CLAIM_TOKEN_B,
        )

        stored = self.refresh(
            db.session.scalar(
                select(IdentityVerificationSession).where(
                    IdentityVerificationSession.verification_session_id
                    == public_id
                )
            )
        )
        self.assertEqual(recovery.recovered_count, 1)
        self.assertEqual(completed.status, "verified")
        self.assertEqual(stored.status, "verified")
        self.assertIsNone(stored.completion_claim_token)

    def test_recovery_never_calls_provider_or_hmac(self):
        self.add_session(1)

        with (
            patch.object(
                FakeIdentityVerificationProvider,
                "start_verification",
            ) as provider_start,
            patch.object(
                FakeIdentityVerificationProvider,
                "get_verified_identity",
            ) as provider_get,
            patch(
                "services.identity_verification_service.hmac.new"
            ) as hmac_new,
        ):
            result = self.recover()
            db.session.commit()

        self.assertEqual(result.recovered_count, 1)
        provider_start.assert_not_called()
        provider_get.assert_not_called()
        hmac_new.assert_not_called()


if __name__ == "__main__":
    unittest.main()
