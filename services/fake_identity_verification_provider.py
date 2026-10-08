from datetime import date
from typing import Callable, Optional
from uuid import uuid4

from services.identity_verification_provider import (
    IdentityVerificationProvider,
    IdentityVerificationProviderError,
    IdentityVerificationProviderErrorCode,
    IdentityVerificationProviderName,
    IdentityVerificationStartResult,
    VerifiedIdentityResult,
)


class FakeIdentityVerificationProvider(IdentityVerificationProvider):
    """An explicitly constructed, in-memory identity provider test double."""

    def __init__(
        self,
        *,
        provider: IdentityVerificationProviderName,
        verified_birth_date: date,
        identity_subject: str,
        transaction_id_factory: Optional[Callable[[], str]] = None,
        start_error_code: Optional[
            IdentityVerificationProviderErrorCode
        ] = None,
        verification_error_code: Optional[
            IdentityVerificationProviderErrorCode
        ] = None,
    ):
        if type(provider) is not IdentityVerificationProviderName:
            raise TypeError(
                "provider must be an IdentityVerificationProviderName value."
            )

        if type(verified_birth_date) is not date:
            raise TypeError(
                "verified_birth_date must be a datetime.date value."
            )

        if not isinstance(identity_subject, str):
            raise TypeError("identity_subject must be a string.")

        if not identity_subject.strip():
            raise ValueError("identity_subject must not be empty.")

        if transaction_id_factory is not None and not callable(
            transaction_id_factory
        ):
            raise TypeError("transaction_id_factory must be callable.")

        if start_error_code is not None:
            if type(start_error_code) is not (
                IdentityVerificationProviderErrorCode
            ):
                raise TypeError(
                    "start_error_code must be a provider error code."
                )

            if start_error_code is not (
                IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE
            ):
                raise ValueError(
                    "start_error_code only supports PROVIDER_UNAVAILABLE."
                )

        if verification_error_code is not None:
            if type(verification_error_code) is not (
                IdentityVerificationProviderErrorCode
            ):
                raise TypeError(
                    "verification_error_code must be a provider error code."
                )

            if verification_error_code not in (
                IdentityVerificationProviderErrorCode.VERIFICATION_FAILED,
                IdentityVerificationProviderErrorCode.VERIFICATION_EXPIRED,
            ):
                raise ValueError(
                    "verification_error_code only supports verification failures."
                )

        self._provider = provider
        self._verified_birth_date = verified_birth_date
        self._identity_subject = identity_subject
        self._transaction_id_factory = transaction_id_factory
        if self._transaction_id_factory is None:
            self._transaction_id_factory = _generate_transaction_id
        self._start_error_code = start_error_code
        self._verification_error_code = verification_error_code
        self._started_transaction_ids = set()

    @property
    def provider(self) -> IdentityVerificationProviderName:
        return self._provider

    def start_verification(self) -> IdentityVerificationStartResult:
        if self._start_error_code is not None:
            raise IdentityVerificationProviderError(self._start_error_code)

        try:
            provider_transaction_id = self._transaction_id_factory()
        except Exception:
            raise IdentityVerificationProviderError(
                IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE
            ) from None

        if (
            not isinstance(provider_transaction_id, str)
            or not provider_transaction_id.strip()
            or provider_transaction_id in self._started_transaction_ids
        ):
            raise IdentityVerificationProviderError(
                IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE
            )

        result = IdentityVerificationStartResult(
            provider=self.provider,
            provider_transaction_id=provider_transaction_id,
            authentication_url=(
                "fake://identity-verification/"
                f"{provider_transaction_id}"
            ),
        )
        self._started_transaction_ids.add(provider_transaction_id)
        return result

    def get_verified_identity(
        self,
        *,
        provider_transaction_id: str,
    ) -> VerifiedIdentityResult:
        if (
            not isinstance(provider_transaction_id, str)
            or provider_transaction_id not in self._started_transaction_ids
        ):
            raise IdentityVerificationProviderError(
                IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE
            )

        if self._verification_error_code is not None:
            raise IdentityVerificationProviderError(
                self._verification_error_code
            )

        return VerifiedIdentityResult(
            provider=self.provider,
            provider_transaction_id=provider_transaction_id,
            birth_date=self._verified_birth_date,
            identity_subject=self._identity_subject,
        )

    def __repr__(self):
        return (
            f"{type(self).__name__}("
            f"provider={self.provider!r}, "
            f"transaction_count={len(self._started_transaction_ids)})"
        )


def _generate_transaction_id():
    return uuid4().hex
