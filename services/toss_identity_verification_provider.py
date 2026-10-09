from dataclasses import dataclass, field

from services.identity_verification_provider import (
    IdentityVerificationProvider,
    IdentityVerificationProviderError,
    IdentityVerificationProviderErrorCode,
    IdentityVerificationProviderName,
    IdentityVerificationStartResult,
    VerifiedIdentityResult,
)
from services.toss_identity_verification_result_client import (
    TossIdentityVerificationResultError,
    TossIdentityVerificationResultErrorCode,
)
from services.toss_identity_verification_start_client import (
    TossIdentityVerificationStartError,
    TossIdentityVerificationStartErrorCode,
)


_START_ERROR_MAPPING = {
    TossIdentityVerificationStartErrorCode.NETWORK_ERROR:
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
    TossIdentityVerificationStartErrorCode.HTTP_ERROR:
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
    TossIdentityVerificationStartErrorCode.TOKEN_ERROR:
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
    TossIdentityVerificationStartErrorCode.INVALID_RESPONSE:
        IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
    TossIdentityVerificationStartErrorCode.REQUEST_REJECTED:
        IdentityVerificationProviderErrorCode.VERIFICATION_FAILED,
}


_RESULT_ERROR_MAPPING = {
    TossIdentityVerificationResultErrorCode.NETWORK_ERROR:
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
    TossIdentityVerificationResultErrorCode.HTTP_ERROR:
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
    TossIdentityVerificationResultErrorCode.TOKEN_ERROR:
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
    TossIdentityVerificationResultErrorCode.PROVIDER_UNAVAILABLE:
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
    TossIdentityVerificationResultErrorCode.CRYPTO_ERROR:
        IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
    TossIdentityVerificationResultErrorCode.INVALID_RESPONSE:
        IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
    TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING:
        IdentityVerificationProviderErrorCode.VERIFICATION_PENDING,
    TossIdentityVerificationResultErrorCode.VERIFICATION_EXPIRED:
        IdentityVerificationProviderErrorCode.VERIFICATION_EXPIRED,
    TossIdentityVerificationResultErrorCode.AGE_RESTRICTED:
        IdentityVerificationProviderErrorCode.AGE_RESTRICTED,
    TossIdentityVerificationResultErrorCode.RESULT_QUERY_LIMIT_EXCEEDED:
        IdentityVerificationProviderErrorCode.VERIFICATION_FAILED,
    TossIdentityVerificationResultErrorCode.REQUEST_REJECTED:
        IdentityVerificationProviderErrorCode.VERIFICATION_FAILED,
}


@dataclass(frozen=True)
class TossVerifiedIdentityResult(VerifiedIdentityResult):
    signature: str = field(repr=False)

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.signature, str):
            raise TypeError("signature must be a string.")
        if not self.signature.strip():
            raise ValueError("signature must not be empty.")


class TossIdentityVerificationProvider(IdentityVerificationProvider):
    """Adapt Toss-specific clients to the provider-neutral contract."""

    def __init__(self, *, start_client, result_client):
        if not callable(getattr(start_client, "start_verification", None)):
            raise TypeError(
                "start_client must provide a callable start_verification method."
            )
        if not callable(
            getattr(result_client, "get_verified_identity", None)
        ):
            raise TypeError(
                "result_client must provide a callable "
                "get_verified_identity method."
            )

        self._start_client = start_client
        self._result_client = result_client

    @property
    def provider(self) -> IdentityVerificationProviderName:
        return IdentityVerificationProviderName.TOSS

    def start_verification(self) -> IdentityVerificationStartResult:
        try:
            result = self._start_client.start_verification()
        except TossIdentityVerificationStartError as error:
            raise IdentityVerificationProviderError(
                _START_ERROR_MAPPING[error.code]
            ) from None

        try:
            return IdentityVerificationStartResult(
                provider=self.provider,
                provider_transaction_id=result.provider_transaction_id,
                authentication_url=result.authentication_url,
            )
        except (AttributeError, TypeError, ValueError):
            raise IdentityVerificationProviderError(
                IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE
            ) from None

    def get_verified_identity(
        self,
        *,
        provider_transaction_id: str,
    ) -> TossVerifiedIdentityResult:
        try:
            result = self._result_client.get_verified_identity(
                provider_transaction_id
            )
        except TossIdentityVerificationResultError as error:
            raise IdentityVerificationProviderError(
                _RESULT_ERROR_MAPPING[error.code]
            ) from None

        try:
            return TossVerifiedIdentityResult(
                provider=self.provider,
                provider_transaction_id=result.provider_transaction_id,
                birth_date=result.birth_date,
                identity_subject=result.identity_subject,
                signature=result.signature,
            )
        except (AttributeError, TypeError, ValueError):
            raise IdentityVerificationProviderError(
                IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE
            ) from None

    def __repr__(self):
        return f"{type(self).__name__}()"
