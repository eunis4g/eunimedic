from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
import math
import os
from pathlib import Path
import sys

from dotenv import load_dotenv
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.toss_cert_access_token_client import (  # noqa: E402
    TossCertAccessTokenClient,
)
from services.toss_identity_verification_start_client import (  # noqa: E402
    TossIdentityVerificationStartClient,
    TossIdentityVerificationStartError,
)


class TossIdentityStartSmokeTestErrorCode(str, Enum):
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    NON_TEST_CREDENTIAL = "NON_TEST_CREDENTIAL"
    HTTP_REQUEST_LIMIT_EXCEEDED = "HTTP_REQUEST_LIMIT_EXCEEDED"
    REQUEST_COUNT_MISMATCH = "REQUEST_COUNT_MISMATCH"
    INVALID_START_RESULT = "INVALID_START_RESULT"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


_ERROR_MESSAGES = {
    TossIdentityStartSmokeTestErrorCode.MISSING_CONFIGURATION:
        "Required smoke-test configuration is missing.",
    TossIdentityStartSmokeTestErrorCode.INVALID_CONFIGURATION:
        "Smoke-test configuration is invalid.",
    TossIdentityStartSmokeTestErrorCode.NON_TEST_CREDENTIAL:
        "Only Toss test credentials may be used by this script.",
    TossIdentityStartSmokeTestErrorCode.HTTP_REQUEST_LIMIT_EXCEEDED:
        "A smoke-test endpoint was called more than once.",
    TossIdentityStartSmokeTestErrorCode.REQUEST_COUNT_MISMATCH:
        "The smoke test did not make the expected requests.",
    TossIdentityStartSmokeTestErrorCode.INVALID_START_RESULT:
        "The identity verification start result was invalid.",
    TossIdentityStartSmokeTestErrorCode.UNEXPECTED_ERROR:
        "The smoke test failed unexpectedly.",
}


class TossIdentityStartSmokeTestError(RuntimeError):

    def __init__(self, code: TossIdentityStartSmokeTestErrorCode):
        if type(code) is not TossIdentityStartSmokeTestErrorCode:
            raise TypeError(
                "code must be a TossIdentityStartSmokeTestErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class TossIdentityStartSmokeTestConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    request_url: str = field(repr=False)
    timeout: float
    refresh_skew: timedelta


class _SinglePostHttpSession:

    def __init__(self, delegate):
        if not callable(getattr(delegate, "post", None)):
            raise TypeError("HTTP session must provide a callable post method.")

        self._delegate = delegate
        self.request_count = 0

    def post(self, *args, **kwargs):
        if self.request_count >= 1:
            error_code = TossIdentityStartSmokeTestErrorCode.HTTP_REQUEST_LIMIT_EXCEEDED
            raise TossIdentityStartSmokeTestError(
                error_code
            )

        self.request_count += 1
        return self._delegate.post(*args, **kwargs)

    def close(self):
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()


def run_toss_identity_start_smoke_test(
    *,
    environ,
    http_session_factory=requests.Session,
    access_token_client_factory=TossCertAccessTokenClient,
    start_client_factory=TossIdentityVerificationStartClient,
    output=print,
) -> int:
    """Request one USER_NONE handoff without opening or persisting it."""

    token_http_session = None
    start_http_session = None
    try:
        config = _read_config(environ)
        token_http_session = _SinglePostHttpSession(
            http_session_factory()
        )
        start_http_session = _SinglePostHttpSession(
            http_session_factory()
        )
        access_token_client = access_token_client_factory(
            client_id=config.client_id,
            client_secret=config.client_secret,
            timeout=config.timeout,
            refresh_skew=config.refresh_skew,
            http_session=token_http_session,
        )
        start_client = start_client_factory(
            access_token_client=access_token_client,
            request_url=config.request_url,
            http_session=start_http_session,
            timeout=config.timeout,
        )

        result = start_client.start_verification()
        if (
            not isinstance(result.provider_transaction_id, str)
            or not result.provider_transaction_id.strip()
            or not isinstance(result.authentication_url, str)
            or not result.authentication_url.strip()
        ):
            raise TossIdentityStartSmokeTestError(
                TossIdentityStartSmokeTestErrorCode.INVALID_START_RESULT
            )
        if (
            token_http_session.request_count != 1
            or start_http_session.request_count != 1
        ):
            raise TossIdentityStartSmokeTestError(
                TossIdentityStartSmokeTestErrorCode.REQUEST_COUNT_MISMATCH
            )

        output("Toss identity start smoke test: OK")
        output("Transaction ID received: OK")
        output("Authentication URL received: OK")
        return 0
    except TossIdentityStartSmokeTestError as error:
        _print_failure(output, error.code.value)
        return 1
    except TossIdentityVerificationStartError as error:
        _print_failure(output, error.code.value)
        return 1
    except Exception:
        _print_failure(
            output,
            TossIdentityStartSmokeTestErrorCode.UNEXPECTED_ERROR.value,
        )
        return 1
    finally:
        for http_session in (start_http_session, token_http_session):
            if http_session is not None:
                try:
                    http_session.close()
                except Exception:
                    pass


def main(
    *,
    environ=None,
    http_session_factory=requests.Session,
    access_token_client_factory=TossCertAccessTokenClient,
    start_client_factory=TossIdentityVerificationStartClient,
    output=print,
    load_environment=True,
) -> int:
    if load_environment:
        try:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        except Exception:
            error_code = TossIdentityStartSmokeTestErrorCode.INVALID_CONFIGURATION
            _print_failure(
                output,
                error_code.value,
            )
            return 1

    return run_toss_identity_start_smoke_test(
        environ=os.environ if environ is None else environ,
        http_session_factory=http_session_factory,
        access_token_client_factory=access_token_client_factory,
        start_client_factory=start_client_factory,
        output=output,
    )


def _read_config(environ) -> TossIdentityStartSmokeTestConfig:
    client_id = _read_required_value(environ, "TOSS_CERT_CLIENT_ID")
    client_secret = _read_required_value(
        environ,
        "TOSS_CERT_CLIENT_SECRET",
    )
    request_url = _read_required_value(
        environ,
        "TOSS_IDENTITY_REQUEST_URL",
    )

    if (
        not client_id.startswith("test_")
        or not client_secret.startswith("test_")
    ):
        raise TossIdentityStartSmokeTestError(
            TossIdentityStartSmokeTestErrorCode.NON_TEST_CREDENTIAL
        )

    timeout = _read_seconds(
        environ,
        "TOSS_HTTP_TIMEOUT_SECONDS",
        allow_zero=False,
    )
    refresh_skew_seconds = _read_seconds(
        environ,
        "TOSS_TOKEN_REFRESH_SKEW_SECONDS",
        allow_zero=True,
    )
    try:
        refresh_skew = timedelta(seconds=refresh_skew_seconds)
    except OverflowError:
        raise TossIdentityStartSmokeTestError(
            TossIdentityStartSmokeTestErrorCode.INVALID_CONFIGURATION
        ) from None

    return TossIdentityStartSmokeTestConfig(
        client_id=client_id,
        client_secret=client_secret,
        request_url=request_url,
        timeout=timeout,
        refresh_skew=refresh_skew,
    )


def _read_required_value(environ, name):
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TossIdentityStartSmokeTestError(
            TossIdentityStartSmokeTestErrorCode.MISSING_CONFIGURATION
        )
    return value


def _read_seconds(environ, name, *, allow_zero):
    raw_value = _read_required_value(environ, name)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise TossIdentityStartSmokeTestError(
            TossIdentityStartSmokeTestErrorCode.INVALID_CONFIGURATION
        ) from None

    minimum_is_valid = value >= 0 if allow_zero else value > 0
    if not math.isfinite(value) or not minimum_is_valid:
        raise TossIdentityStartSmokeTestError(
            TossIdentityStartSmokeTestErrorCode.INVALID_CONFIGURATION
        )
    return value


def _print_failure(output, code):
    output("Toss identity start smoke test: FAILED")
    output(f"Error code: {code}")


if __name__ == "__main__":
    raise SystemExit(main())
