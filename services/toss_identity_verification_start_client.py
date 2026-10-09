from dataclasses import dataclass, field
from enum import Enum
import math

import requests

from services.toss_cert_access_token_client import TossCertAccessTokenError


TOSS_IDENTITY_VERIFICATION_START_ENDPOINT = (
    "https://cert.toss.im/api/v2/sign/user/auth/id/request"
)


class TossIdentityVerificationStartErrorCode(str, Enum):
    NETWORK_ERROR = "NETWORK_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    TOKEN_ERROR = "TOKEN_ERROR"


_ERROR_MESSAGES = {
    TossIdentityVerificationStartErrorCode.NETWORK_ERROR:
        "The Toss identity verification request could not be completed.",
    TossIdentityVerificationStartErrorCode.HTTP_ERROR:
        "The Toss identity verification endpoint returned an HTTP error.",
    TossIdentityVerificationStartErrorCode.REQUEST_REJECTED:
        "The Toss identity verification request was rejected.",
    TossIdentityVerificationStartErrorCode.INVALID_RESPONSE:
        "The Toss identity verification endpoint returned an invalid response.",
    TossIdentityVerificationStartErrorCode.TOKEN_ERROR:
        "A Toss Cert access token could not be obtained.",
}


class TossIdentityVerificationStartError(RuntimeError):
    """A stable start error that never contains request or response data."""

    def __init__(self, code: TossIdentityVerificationStartErrorCode):
        if type(code) is not TossIdentityVerificationStartErrorCode:
            raise TypeError(
                "code must be a TossIdentityVerificationStartErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class TossIdentityVerificationStartResult:
    provider_transaction_id: str = field(repr=False)
    authentication_url: str = field(repr=False)

    def __post_init__(self):
        _validate_non_empty_string(
            field_name="provider_transaction_id",
            value=self.provider_transaction_id,
        )
        _validate_non_empty_string(
            field_name="authentication_url",
            value=self.authentication_url,
        )


class TossIdentityVerificationStartClient:
    """Start one Toss USER_NONE identity verification request."""

    def __init__(
        self,
        *,
        access_token_client,
        request_url: str,
        http_session=None,
        timeout: float,
    ):
        if not callable(
            getattr(access_token_client, "get_access_token", None)
        ):
            raise TypeError(
                "access_token_client must provide a callable "
                "get_access_token method."
            )

        _validate_non_empty_string(
            field_name="request_url",
            value=request_url,
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
        self._request_url = request_url
        self._http_session = (
            http_session if http_session is not None else requests.Session()
        )
        self._timeout = timeout

    def start_verification(self) -> TossIdentityVerificationStartResult:
        access_token = self._get_access_token()

        try:
            response = self._http_session.post(
                TOSS_IDENTITY_VERIFICATION_START_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "requestType": "USER_NONE",
                    "requestUrl": self._request_url,
                },
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.NETWORK_ERROR
            ) from None

        status_code = getattr(response, "status_code", None)
        if type(status_code) is not int:
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.INVALID_RESPONSE
            )

        if not 200 <= status_code < 300:
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.HTTP_ERROR
            )

        try:
            payload = response.json()
        except ValueError:
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.INVALID_RESPONSE
            ) from None

        if not isinstance(payload, dict):
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.INVALID_RESPONSE
            )

        result_type = payload.get("resultType")
        if result_type == "FAIL":
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.REQUEST_REJECTED
            )

        if result_type != "SUCCESS":
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.INVALID_RESPONSE
            )

        success = payload.get("success")
        if not isinstance(success, dict):
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.INVALID_RESPONSE
            )

        provider_transaction_id = success.get("txId")
        authentication_url = success.get("authUrl")
        if (
            not isinstance(provider_transaction_id, str)
            or not provider_transaction_id.strip()
            or not isinstance(authentication_url, str)
            or not authentication_url.strip()
        ):
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.INVALID_RESPONSE
            )

        return TossIdentityVerificationStartResult(
            provider_transaction_id=provider_transaction_id,
            authentication_url=authentication_url,
        )

    def __repr__(self):
        return (
            f"{type(self).__name__}("
            f"endpoint={TOSS_IDENTITY_VERIFICATION_START_ENDPOINT!r}, "
            f"timeout={self._timeout!r})"
        )

    def _get_access_token(self):
        try:
            token_result = self._access_token_client.get_access_token()
        except TossCertAccessTokenError:
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.TOKEN_ERROR
            ) from None

        access_token = getattr(token_result, "access_token", None)
        if not isinstance(access_token, str) or not access_token.strip():
            raise TossIdentityVerificationStartError(
                TossIdentityVerificationStartErrorCode.TOKEN_ERROR
            )

        return access_token


def _validate_non_empty_string(*, field_name, value):
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")

    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
