from abc import ABC, abstractmethod
from enum import Enum


class IdentityVerificationEvidenceWriterErrorCode(str, Enum):
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


_ERROR_MESSAGES = {
    IdentityVerificationEvidenceWriterErrorCode.INVALID_EVIDENCE:
        "Identity verification evidence is invalid.",
}


class IdentityVerificationEvidenceWriterError(RuntimeError):
    """A stable evidence error without identity or provider payloads."""

    def __init__(self, code: IdentityVerificationEvidenceWriterErrorCode):
        if type(code) is not IdentityVerificationEvidenceWriterErrorCode:
            raise TypeError(
                "code must be an "
                "IdentityVerificationEvidenceWriterErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class VerifiedIdentityEvidenceWriter(ABC):

    @abstractmethod
    def add_evidence(
        self,
        session,
        *,
        verification_session,
        verified_identity_result,
        action_time,
    ):
        """Stage evidence in the caller's transaction without committing."""
