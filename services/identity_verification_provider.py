from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class IdentityVerificationProviderName(str, Enum):
    TOSS = "TOSS"


class IdentityVerificationProviderErrorCode(str, Enum):
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    VERIFICATION_PENDING = "VERIFICATION_PENDING"
    AGE_RESTRICTED = "AGE_RESTRICTED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_EXPIRED = "VERIFICATION_EXPIRED"


_ERROR_MESSAGES = {
    IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE:
        "The identity verification provider is unavailable.",
    IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE:
        "The identity verification provider returned an invalid response.",
    IdentityVerificationProviderErrorCode.VERIFICATION_PENDING:
        "Identity verification is not completed yet.",
    IdentityVerificationProviderErrorCode.AGE_RESTRICTED:
        "The identity verification provider rejected the age requirement.",
    IdentityVerificationProviderErrorCode.VERIFICATION_FAILED:
        "Identity verification failed.",
    IdentityVerificationProviderErrorCode.VERIFICATION_EXPIRED:
        "Identity verification expired.",
}


class IdentityVerificationProviderError(RuntimeError):
    """A provider failure whose message never contains provider response data."""

    def __init__(self, code: IdentityVerificationProviderErrorCode):
        if type(code) is not IdentityVerificationProviderErrorCode:
            raise TypeError(
                "code must be an IdentityVerificationProviderErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class IdentityVerificationStartResult:
    provider: IdentityVerificationProviderName
    provider_transaction_id: str = field(repr=False)
    authentication_url: str = field(repr=False)

    def __post_init__(self):
        _validate_provider(self.provider)
        _validate_non_empty_string(
            field_name="provider_transaction_id",
            value=self.provider_transaction_id,
        )
        _validate_non_empty_string(
            field_name="authentication_url",
            value=self.authentication_url,
        )


@dataclass(frozen=True)
class VerifiedIdentityResult:
    provider: IdentityVerificationProviderName
    provider_transaction_id: str = field(repr=False)
    birth_date: date = field(repr=False)
    identity_subject: str = field(repr=False)

    def __post_init__(self):
        _validate_provider(self.provider)
        _validate_non_empty_string(
            field_name="provider_transaction_id",
            value=self.provider_transaction_id,
        )

        if type(self.birth_date) is not date:
            raise TypeError("birth_date must be a datetime.date value.")

        _validate_non_empty_string(
            field_name="identity_subject",
            value=self.identity_subject,
        )

    @property
    def evidence_persistence_required(self) -> bool:
        """Tell orchestration whether verified finalization needs evidence."""

        return False


class IdentityVerificationProvider(ABC):

    @property
    @abstractmethod
    def provider(self) -> IdentityVerificationProviderName:
        """Return the provider represented by this implementation."""

    @abstractmethod
    def start_verification(self) -> IdentityVerificationStartResult:
        """Start provider authentication and return its opaque handoff data."""

    @abstractmethod
    def get_verified_identity(
        self,
        *,
        provider_transaction_id: str,
    ) -> VerifiedIdentityResult:
        """Return verified identity data or raise a provider domain error."""


def _validate_provider(provider):
    if type(provider) is not IdentityVerificationProviderName:
        raise TypeError(
            "provider must be an IdentityVerificationProviderName value."
        )


def _validate_non_empty_string(*, field_name, value):
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")

    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
