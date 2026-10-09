from datetime import UTC, datetime

from models import IdentityVerificationSession, TossIdentityVerificationEvidence
from services.identity_verification_evidence_writer import (
    IdentityVerificationEvidenceWriterError,
    IdentityVerificationEvidenceWriterErrorCode,
    VerifiedIdentityEvidenceWriter,
)
from services.toss_identity_verification_provider import (
    TossVerifiedIdentityResult,
)


class TossIdentityVerificationEvidenceWriter(
    VerifiedIdentityEvidenceWriter
):
    """Stage immutable Toss evidence in an existing DB transaction."""

    def add_evidence(
        self,
        session,
        *,
        verification_session,
        verified_identity_result,
        action_time,
    ) -> TossIdentityVerificationEvidence:
        if (
            not isinstance(
                verification_session,
                IdentityVerificationSession,
            )
            or type(verified_identity_result)
            is not TossVerifiedIdentityResult
            or type(action_time) is not datetime
            or action_time.tzinfo is None
            or action_time.utcoffset() is None
            or verification_session.identity_verification_session_id is None
            or verification_session.provider != "toss"
            or verification_session.status != "verified"
            or verification_session.completion_claim_token is not None
            or verification_session.completion_claimed_at is not None
            or verification_session.provider_transaction_id
            != verified_identity_result.provider_transaction_id
            or not isinstance(verified_identity_result.signature, str)
            or not verified_identity_result.signature.strip()
        ):
            raise IdentityVerificationEvidenceWriterError(
                IdentityVerificationEvidenceWriterErrorCode.INVALID_EVIDENCE
            )

        evidence = TossIdentityVerificationEvidence(
            identity_verification_session_id=(
                verification_session.identity_verification_session_id
            ),
            provider_transaction_id=(
                verified_identity_result.provider_transaction_id
            ),
            signature=verified_identity_result.signature,
            created_at=action_time.astimezone(UTC),
        )
        session.add(evidence)
        return evidence
