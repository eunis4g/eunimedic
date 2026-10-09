from dataclasses import dataclass, field
from datetime import timedelta
import math

from services.toss_cert_access_token_client import TossCertAccessTokenClient
from services.toss_cert_crypto import TossCertCryptoSessionGenerator
from services.toss_identity_verification_evidence_writer import (
    TossIdentityVerificationEvidenceWriter,
)
from services.toss_identity_verification_provider import (
    TossIdentityVerificationProvider,
)
from services.toss_identity_verification_result_client import (
    TossIdentityVerificationResultClient,
)
from services.toss_identity_verification_start_client import (
    TossIdentityVerificationStartClient,
)


@dataclass(frozen=True)
class TossIdentityVerificationConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    request_url: str = field(repr=False)
    rsa_public_key_base64: str = field(repr=False)
    http_timeout: float
    token_refresh_skew: timedelta
    identity_subject_hmac_key: bytes = field(repr=False)

    def __post_init__(self):
        for field_name, value in (
            ("client_id", self.client_id),
            ("client_secret", self.client_secret),
            ("request_url", self.request_url),
            ("rsa_public_key_base64", self.rsa_public_key_base64),
        ):
            _validate_required_string(field_name=field_name, value=value)

        if (
            isinstance(self.http_timeout, bool)
            or not isinstance(self.http_timeout, (int, float))
            or not math.isfinite(self.http_timeout)
            or self.http_timeout <= 0
        ):
            raise ValueError(
                "http_timeout must be a positive finite number."
            )

        if not isinstance(self.token_refresh_skew, timedelta):
            raise TypeError(
                "token_refresh_skew must be a datetime.timedelta value."
            )
        if self.token_refresh_skew < timedelta(0):
            raise ValueError("token_refresh_skew must not be negative.")

        _validate_hmac_key(self.identity_subject_hmac_key)


@dataclass(frozen=True)
class TossIdentityVerificationDependencies:
    provider: TossIdentityVerificationProvider
    evidence_writer: TossIdentityVerificationEvidenceWriter
    identity_subject_hmac_key: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.provider, TossIdentityVerificationProvider):
            raise TypeError(
                "provider must be a TossIdentityVerificationProvider."
            )
        if not isinstance(
            self.evidence_writer,
            TossIdentityVerificationEvidenceWriter,
        ):
            raise TypeError(
                "evidence_writer must be a "
                "TossIdentityVerificationEvidenceWriter."
            )
        _validate_hmac_key(self.identity_subject_hmac_key)


def build_toss_identity_verification_dependencies(
    config: TossIdentityVerificationConfig,
    *,
    http_session=None,
) -> TossIdentityVerificationDependencies:
    """Build one reusable, process-local Toss dependency graph."""

    if type(config) is not TossIdentityVerificationConfig:
        raise TypeError(
            "config must be a TossIdentityVerificationConfig value."
        )

    access_token_client = TossCertAccessTokenClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        timeout=config.http_timeout,
        refresh_skew=config.token_refresh_skew,
        http_session=http_session,
    )
    crypto_session_generator = TossCertCryptoSessionGenerator(
        base64_public_key=config.rsa_public_key_base64,
    )
    start_client = TossIdentityVerificationStartClient(
        access_token_client=access_token_client,
        request_url=config.request_url,
        http_session=http_session,
        timeout=config.http_timeout,
    )
    result_client = TossIdentityVerificationResultClient(
        access_token_client=access_token_client,
        crypto_session_generator=crypto_session_generator,
        timeout=config.http_timeout,
        http_session=http_session,
    )

    return TossIdentityVerificationDependencies(
        provider=TossIdentityVerificationProvider(
            start_client=start_client,
            result_client=result_client,
        ),
        evidence_writer=TossIdentityVerificationEvidenceWriter(),
        identity_subject_hmac_key=config.identity_subject_hmac_key,
    )


def _validate_required_string(*, field_name, value):
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")


def _validate_hmac_key(value):
    if not isinstance(value, bytes):
        raise TypeError("identity_subject_hmac_key must be bytes.")
    if not value:
        raise ValueError("identity_subject_hmac_key must not be empty.")
