from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
import math
from threading import Lock
from typing import Callable, Optional

import requests


TOSS_CERT_TOKEN_ENDPOINT = "https://oauth2.cert.toss.im/token"


class TossCertAccessTokenErrorCode(str, Enum):
    NETWORK_ERROR = "NETWORK_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"


_ERROR_MESSAGES = {
    TossCertAccessTokenErrorCode.NETWORK_ERROR:
        "The Toss Cert token request could not be completed.",
    TossCertAccessTokenErrorCode.HTTP_ERROR:
        "The Toss Cert token endpoint returned an HTTP error.",
    TossCertAccessTokenErrorCode.INVALID_RESPONSE:
        "The Toss Cert token endpoint returned an invalid response.",
}


class TossCertAccessTokenError(RuntimeError):
    """A stable token error that never contains request or response data."""

    def __init__(self, code: TossCertAccessTokenErrorCode):
        if type(code) is not TossCertAccessTokenErrorCode:
            raise TypeError(
                "code must be a TossCertAccessTokenErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class TossCertAccessToken:
    access_token: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self):
        _validate_non_empty_string(
            field_name="access_token",
            value=self.access_token,
        )

        if not isinstance(self.expires_at, datetime):
            raise TypeError("expires_at must be a datetime value.")

        if (
            self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must be timezone-aware.")


class TossCertAccessTokenClient:
    """Issue and cache Toss Cert client-credentials access tokens in memory."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout: float,
        refresh_skew: timedelta = timedelta(0),
        http_session=None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        _validate_non_empty_string(field_name="client_id", value=client_id)
        _validate_non_empty_string(
            field_name="client_secret",
            value=client_secret,
        )

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number.")

        if not isinstance(refresh_skew, timedelta):
            raise TypeError("refresh_skew must be a datetime.timedelta value.")

        if refresh_skew < timedelta(0):
            raise ValueError("refresh_skew must not be negative.")

        if http_session is not None and not callable(
            getattr(http_session, "post", None)
        ):
            raise TypeError("http_session must provide a callable post method.")

        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable.")

        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._refresh_skew = refresh_skew
        self._http_session = http_session or requests.Session()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cached_token = None
        self._refresh_lock = Lock()

    def get_access_token(self) -> TossCertAccessToken:
        cached_token = self._cached_token
        if self._is_reusable(cached_token, now=self._read_clock()):
            return cached_token

        with self._refresh_lock:
            cached_token = self._cached_token
            if self._is_reusable(cached_token, now=self._read_clock()):
                return cached_token

            token = self._request_access_token()
            self._cached_token = token
            return token

    def __repr__(self):
        return (
            f"{type(self).__name__}("
            f"endpoint={TOSS_CERT_TOKEN_ENDPOINT!r}, "
            f"timeout={self._timeout!r}, "
            f"refresh_skew={self._refresh_skew!r}, "
            f"has_cached_token={self._cached_token is not None})"
        )

    def _is_reusable(self, token, *, now):
        if token is None:
            return False

        return now < token.expires_at - self._refresh_skew

    def _request_access_token(self):
        try:
            response = self._http_session.post(
                TOSS_CERT_TOKEN_ENDPOINT,
                data={
                    "grant_type": "client_credentials",
                    "scope": "ca",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TossCertAccessTokenError(
                TossCertAccessTokenErrorCode.NETWORK_ERROR
            ) from None

        received_at = self._read_clock()
        status_code = getattr(response, "status_code", None)
        if type(status_code) is not int:
            raise TossCertAccessTokenError(
                TossCertAccessTokenErrorCode.INVALID_RESPONSE
            )

        if not 200 <= status_code < 300:
            raise TossCertAccessTokenError(
                TossCertAccessTokenErrorCode.HTTP_ERROR
            )

        try:
            payload = response.json()
        except ValueError:
            raise TossCertAccessTokenError(
                TossCertAccessTokenErrorCode.INVALID_RESPONSE
            ) from None

        if not isinstance(payload, dict):
            raise TossCertAccessTokenError(
                TossCertAccessTokenErrorCode.INVALID_RESPONSE
            )

        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        scope = payload.get("scope")
        expires_in = payload.get("expires_in")

        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or token_type != "Bearer"
            or scope != "ca"
            or type(expires_in) is not int
            or expires_in <= 0
        ):
            raise TossCertAccessTokenError(
                TossCertAccessTokenErrorCode.INVALID_RESPONSE
            )

        try:
            expires_at = received_at + timedelta(seconds=expires_in)
        except OverflowError:
            raise TossCertAccessTokenError(
                TossCertAccessTokenErrorCode.INVALID_RESPONSE
            ) from None

        return TossCertAccessToken(
            access_token=access_token,
            expires_at=expires_at,
        )

    def _read_clock(self):
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("clock must return a datetime value.")

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime.")

        return now.astimezone(UTC)


def _validate_non_empty_string(*, field_name, value):
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")

    if not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
