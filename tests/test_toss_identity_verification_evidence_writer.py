import inspect
import unittest
from datetime import UTC, date, datetime

from models import IdentityVerificationSession
from services.identity_verification_evidence_writer import (
    IdentityVerificationEvidenceWriterError,
    IdentityVerificationEvidenceWriterErrorCode,
    VerifiedIdentityEvidenceWriter,
)
from services.identity_verification_provider import (
    IdentityVerificationProviderName,
    VerifiedIdentityResult,
)
from services.toss_identity_verification_evidence_writer import (
    TossIdentityVerificationEvidenceWriter,
)
from services.toss_identity_verification_provider import (
    TossVerifiedIdentityResult,
)


ACTION_TIME = datetime(2026, 10, 9, 1, 2, 3, tzinfo=UTC)
TRANSACTION_ID = "toss-transaction-sensitive"
SIGNATURE = "toss-signature-sensitive"


class RecordingSession:

    def __init__(self):
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


class TossIdentityVerificationEvidenceWriterTest(unittest.TestCase):

    def setUp(self):
        self.writer = TossIdentityVerificationEvidenceWriter()
        self.db_session = RecordingSession()
        self.verification_session = IdentityVerificationSession(
            identity_verification_session_id=17,
            verification_session_id=(
                "00000000-0000-4000-8000-000000000017"
            ),
            purpose="android_registration",
            provider="toss",
            status="verified",
            provider_transaction_id=TRANSACTION_ID,
            identity_subject_digest="d" * 64,
            age_eligibility="AGE_14_OR_OVER",
            expires_at=datetime(2026, 10, 9, 1, 10, tzinfo=UTC),
            verified_at=ACTION_TIME,
            created_at=datetime(2026, 10, 9, 1, 0, tzinfo=UTC),
            updated_at=ACTION_TIME,
        )

    def make_toss_result(
        self,
        *,
        provider_transaction_id=TRANSACTION_ID,
        signature=SIGNATURE,
    ):
        return TossVerifiedIdentityResult(
            provider=IdentityVerificationProviderName.TOSS,
            provider_transaction_id=provider_transaction_id,
            birth_date=date(2000, 1, 1),
            identity_subject="identity-subject-sensitive",
            signature=signature,
        )

    def test_writer_implements_provider_neutral_contract(self):
        self.assertIsInstance(
            self.writer,
            VerifiedIdentityEvidenceWriter,
        )

    def test_writer_stages_exact_immutable_evidence_values(self):
        evidence = self.writer.add_evidence(
            self.db_session,
            verification_session=self.verification_session,
            verified_identity_result=self.make_toss_result(),
            action_time=ACTION_TIME,
        )

        self.assertEqual(self.db_session.added, [evidence])
        self.assertEqual(evidence.identity_verification_session_id, 17)
        self.assertEqual(evidence.provider_transaction_id, TRANSACTION_ID)
        self.assertEqual(evidence.signature, SIGNATURE)
        self.assertEqual(evidence.created_at, ACTION_TIME)

    def test_writer_does_not_commit_or_rollback_callers_transaction(self):
        self.writer.add_evidence(
            self.db_session,
            verification_session=self.verification_session,
            verified_identity_result=self.make_toss_result(),
            action_time=ACTION_TIME,
        )

        self.assertEqual(self.db_session.commit_count, 0)
        self.assertEqual(self.db_session.rollback_count, 0)

    def test_writer_rejects_generic_result_with_stable_error(self):
        generic_result = VerifiedIdentityResult(
            provider=IdentityVerificationProviderName.TOSS,
            provider_transaction_id=TRANSACTION_ID,
            birth_date=date(2000, 1, 1),
            identity_subject="identity-subject-sensitive",
        )

        with self.assertRaises(
            IdentityVerificationEvidenceWriterError
        ) as context:
            self.writer.add_evidence(
                self.db_session,
                verification_session=self.verification_session,
                verified_identity_result=generic_result,
                action_time=ACTION_TIME,
            )

        self.assertIs(
            context.exception.code,
            IdentityVerificationEvidenceWriterErrorCode.INVALID_EVIDENCE,
        )
        self.assertEqual(self.db_session.added, [])

    def test_writer_rejects_transaction_mismatch(self):
        with self.assertRaises(IdentityVerificationEvidenceWriterError):
            self.writer.add_evidence(
                self.db_session,
                verification_session=self.verification_session,
                verified_identity_result=self.make_toss_result(
                    provider_transaction_id="different-transaction"
                ),
                action_time=ACTION_TIME,
            )

        self.assertEqual(self.db_session.added, [])

    def test_writer_defensively_rejects_blank_signature(self):
        result = self.make_toss_result()
        object.__setattr__(result, "signature", "   ")

        with self.assertRaises(IdentityVerificationEvidenceWriterError):
            self.writer.add_evidence(
                self.db_session,
                verification_session=self.verification_session,
                verified_identity_result=result,
                action_time=ACTION_TIME,
            )

        self.assertEqual(self.db_session.added, [])

    def test_writer_does_not_persist_birth_date_or_identity_subject(self):
        evidence = self.writer.add_evidence(
            self.db_session,
            verification_session=self.verification_session,
            verified_identity_result=self.make_toss_result(),
            action_time=ACTION_TIME,
        )

        self.assertFalse(hasattr(evidence, "birth_date"))
        self.assertFalse(hasattr(evidence, "identity_subject"))

    def test_writer_has_no_http_client_dependency(self):
        constructor_parameters = inspect.signature(
            TossIdentityVerificationEvidenceWriter
        ).parameters

        self.assertEqual(dict(constructor_parameters), {})

    def test_error_and_evidence_repr_do_not_disclose_values(self):
        evidence = self.writer.add_evidence(
            self.db_session,
            verification_session=self.verification_session,
            verified_identity_result=self.make_toss_result(),
            action_time=ACTION_TIME,
        )
        error = IdentityVerificationEvidenceWriterError(
            IdentityVerificationEvidenceWriterErrorCode.INVALID_EVIDENCE
        )

        for representation in (repr(evidence), repr(error)):
            self.assertNotIn(TRANSACTION_ID, representation)
            self.assertNotIn(SIGNATURE, representation)


if __name__ == "__main__":
    unittest.main()
