from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import math

import requests

from services.age_eligibility_service import (
    AgeEligibilityError,
    parse_verified_birth_date,
)
from services.toss_cert_access_token_client import TossCertAccessTokenError
from services.toss_cert_crypto import TossCertCryptoError


TOSS_IDENTITY_VERIFICATION_RESULT_ENDPOINT = (
    "https://cert.toss.im/api/v2/sign/user/auth/id/result"
)


class TossIdentityVerificationResultErrorCode(str, Enum):
    NETWORK_ERROR = "NETWORK_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    TOKEN_ERROR = "TOKEN_ERROR"
    CRYPTO_ERROR = "CRYPTO_ERROR"
    VERIFICATION_PENDING = "VERIFICATION_PENDING"
    VERIFICATION_EXPIRED = "VERIFICATION_EXPIRED"
    AGE_RESTRICTED = "AGE_RESTRICTED"
    RESULT_QUERY_LIMIT_EXCEEDED = "RESULT_QUERY_LIMIT_EXCEEDED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


_ERROR_MESSAGES = {
    TossIdentityVerificationResultErrorCode.NETWORK_ERROR:
        "The Toss identity verification result request could not be completed.",
    TossIdentityVerificationResultErrorCode.HTTP_ERROR:
        "The Toss identity verification result endpoint returned an HTTP error.",
    TossIdentityVerificationResultErrorCode.REQUEST_REJECTED:
        "The Toss identity verification result request was rejected.",
    TossIdentityVerificationResultErrorCode.INVALID_RESPONSE:
        "The Toss identity verification result response is invalid.",
    TossIdentityVerificationResultErrorCode.TOKEN_ERROR:
        "A Toss Cert access token could not be obtained.",
    TossIdentityVerificationResultErrorCode.CRYPTO_ERROR:
        "The Toss identity verification result could not be decrypted.",
    TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING:
        "Toss identity verification is not completed yet.",
    TossIdentityVerificationResultErrorCode.VERIFICATION_EXPIRED:
        "Toss identity verification expired.",
    TossIdentityVerificationResultErrorCode.AGE_RESTRICTED:
        "Toss identity verification rejected the age policy.",
    TossIdentityVerificationResultErrorCode.RESULT_QUERY_LIMIT_EXCEEDED:
        "The Toss identity verification result query limit was exceeded.",
    TossIdentityVerificationResultErrorCode.PROVIDER_UNAVAILABLE:
        "The Toss identity verification provider is unavailable.",
}


_TOSS_FAIL_CODE_MAPPING = {
    "CE3102": TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING,
    "CE3103": TossIdentityVerificationResultErrorCode.VERIFICATION_EXPIRED,
    "CE3006": TossIdentityVerificationResultErrorCode.AGE_RESTRICTED,
    "CE3101": (
        TossIdentityVerificationResultErrorCode.RESULT_QUERY_LIMIT_EXCEEDED
    ),
    "CE1000": TossIdentityVerificationResultErrorCode.TOKEN_ERROR,
    "CE3100": TossIdentityVerificationResultErrorCode.REQUEST_REJECTED,
    "CE0001": TossIdentityVerificationResultErrorCode.PROVIDER_UNAVAILABLE,
    "CE0002": TossIdentityVerificationResultErrorCode.PROVIDER_UNAVAILABLE,
}


class TossIdentityVerificationResultError(RuntimeError):
    """A stable result error without Toss response or identity data."""

    def __init__(self, code: TossIdentityVerificationResultErrorCode):
        if type(code) is not TossIdentityVerificationResultErrorCode:
            raise TypeError(
                "code must be a TossIdentityVerificationResultErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class TossIdentityVerificationResult:
    provider_transaction_id: str = field(repr=False)
    birth_date: date = field(repr=False)
    identity_subject: str = field(repr=False)
    signature: str = field(repr=False)

    def __post_init__(self):
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
        _validate_non_empty_string(
            field_name="signature",
            value=self.signature,
        )


class TossIdentityVerificationResultClient:
    """Fetch and minimally decode one Toss identity verification result."""

    def __init__(
        self,
        *,
        access_token_client,
        crypto_session_generator,
        timeout: float,
        http_session=None,
    ):
        if not callable(
            getattr(access_token_client, "get_access_token", None)
        ):
            raise TypeError(
                "access_token_client must provide a callable "
                "get_access_token method."
            )

        if not callable(getattr(crypto_session_generator, "generate", None)):
            raise TypeError(
                "crypto_session_generator must provide a callable "
                "generate method."
            )

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number.")

        if http_session is not None and not callable(
            getattr(http_session, "post", None)
        ):
            raise TypeError("http_session must provide a callable post method.")

        self._access_token_client = access_token_client
        self._crypto_session_generator = crypto_session_generator
        self._timeout = timeout
        self._http_session = (
            http_session if http_session is not None else requests.Session()
        )

    def get_verified_identity(
        self,
        provider_transaction_id: str,
    ) -> TossIdentityVerificationResult:
        _validate_non_empty_string(
            field_name="provider_transaction_id",
            value=provider_transaction_id,
        )

        access_token = self._get_access_token()
        crypto_session = self._generate_crypto_session()

        try:
            response = self._http_session.post(
                TOSS_IDENTITY_VERIFICATION_RESULT_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "txId": provider_transaction_id,
                    "sessionKey": crypto_session.session_key,
                },
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.NETWORK_ERROR
            ) from None

        status_code = getattr(response, "status_code", None)
        if type(status_code) is not int:
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        try:
            payload = response.json()
        except ValueError:
            error_code = (
                TossIdentityVerificationResultErrorCode.HTTP_ERROR
                if not 200 <= status_code < 300
                else TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )
            raise TossIdentityVerificationResultError(error_code) from None

        if isinstance(payload, dict) and payload.get("resultType") == "FAIL":
            self._raise_fail_response(payload)

        if not 200 <= status_code < 300:
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.HTTP_ERROR
            )

        return self._parse_success_response(
            payload,
            expected_transaction_id=provider_transaction_id,
            crypto_session=crypto_session,
        )

    def __repr__(self):
        return (
            f"{type(self).__name__}("
            f"endpoint={TOSS_IDENTITY_VERIFICATION_RESULT_ENDPOINT!r}, "
            f"timeout={self._timeout!r})"
        )

    def _get_access_token(self):
        try:
            token_result = self._access_token_client.get_access_token()
        except TossCertAccessTokenError:
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.TOKEN_ERROR
            ) from None

        access_token = getattr(token_result, "access_token", None)
        if not isinstance(access_token, str) or not access_token.strip():
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.TOKEN_ERROR
            )

        return access_token

    def _generate_crypto_session(self):
        try:
            crypto_session = self._crypto_session_generator.generate()
        except TossCertCryptoError:
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.CRYPTO_ERROR
            ) from None

        session_key = getattr(crypto_session, "session_key", None)
        decrypt = getattr(crypto_session, "decrypt", None)
        if (
            not isinstance(session_key, str)
            or not session_key.strip()
            or not callable(decrypt)
        ):
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.CRYPTO_ERROR
            )

        return crypto_session

    def _raise_fail_response(self, payload):
        error = payload.get("error")
        if not isinstance(error, dict):
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        toss_error_code = error.get("errorCode")
        if not isinstance(toss_error_code, str) or not toss_error_code.strip():
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        client_error_code = _TOSS_FAIL_CODE_MAPPING.get(
            toss_error_code,
            TossIdentityVerificationResultErrorCode.REQUEST_REJECTED,
        )
        raise TossIdentityVerificationResultError(client_error_code) from None

    def _parse_success_response(
        self,
        payload,
        *,
        expected_transaction_id,
        crypto_session,
    ):
        if not isinstance(payload, dict) or payload.get("resultType") != "SUCCESS":
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        success = payload.get("success")
        if not isinstance(success, dict):
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        provider_transaction_id = success.get("txId")
        status = success.get("status")
        signature = success.get("signature")
        personal_data = success.get("personalData")
        if (
            not isinstance(provider_transaction_id, str)
            or not provider_transaction_id.strip()
            or provider_transaction_id != expected_transaction_id
            or status != "COMPLETED"
            or not isinstance(signature, str)
            or not signature.strip()
            or not isinstance(personal_data, dict)
        ):
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        encrypted_birthday = personal_data.get("birthday")
        encrypted_di = personal_data.get("di")
        if (
            not isinstance(encrypted_birthday, str)
            or not encrypted_birthday.strip()
            or not isinstance(encrypted_di, str)
            or not encrypted_di.strip()
        ):
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        try:
            birthday = crypto_session.decrypt(encrypted_birthday)
            identity_subject = crypto_session.decrypt(encrypted_di)
        except TossCertCryptoError:
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.CRYPTO_ERROR
            ) from None

        try:
            birth_date = parse_verified_birth_date(birthday)
        except AgeEligibilityError:
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            ) from None

        if not isinstance(identity_subject, str) or not identity_subject.strip():
            raise TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
            )

        return TossIdentityVerificationResult(
            provider_transaction_id=provider_transaction_id,
            birth_date=birth_date,
            identity_subject=identity_subject,
            signature=signature,
        )


def _validate_non_empty_string(*, field_name, value):
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")

    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
