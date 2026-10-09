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
    TossCertAccessTokenError,
)


class TossAccessTokenSmokeTestErrorCode(str, Enum):
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    NON_TEST_CREDENTIAL = "NON_TEST_CREDENTIAL"
    HTTP_REQUEST_LIMIT_EXCEEDED = "HTTP_REQUEST_LIMIT_EXCEEDED"
    CACHE_REUSE_FAILED = "CACHE_REUSE_FAILED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


_ERROR_MESSAGES = {
    TossAccessTokenSmokeTestErrorCode.MISSING_CONFIGURATION:
        "Required smoke-test configuration is missing.",
    TossAccessTokenSmokeTestErrorCode.INVALID_CONFIGURATION:
        "Smoke-test configuration is invalid.",
    TossAccessTokenSmokeTestErrorCode.NON_TEST_CREDENTIAL:
        "Only Toss test credentials may be used by this script.",
    TossAccessTokenSmokeTestErrorCode.HTTP_REQUEST_LIMIT_EXCEEDED:
        "The smoke test attempted more than one HTTP request.",
    TossAccessTokenSmokeTestErrorCode.CACHE_REUSE_FAILED:
        "The access-token cache was not reused.",
    TossAccessTokenSmokeTestErrorCode.UNEXPECTED_ERROR:
        "The smoke test failed unexpectedly.",
}


class TossAccessTokenSmokeTestError(RuntimeError):

    def __init__(self, code: TossAccessTokenSmokeTestErrorCode):
        if type(code) is not TossAccessTokenSmokeTestErrorCode:
            raise TypeError(
                "code must be a TossAccessTokenSmokeTestErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class TossAccessTokenSmokeTestConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    timeout: float
    refresh_skew: timedelta


class _SingleRequestHttpSession:

    def __init__(self, delegate):
        if not callable(getattr(delegate, "post", None)):
            raise TypeError("HTTP session must provide a callable post method.")

        self._delegate = delegate
        self.request_count = 0

    def post(self, *args, **kwargs):
        if self.request_count >= 1:
            raise TossAccessTokenSmokeTestError(
                TossAccessTokenSmokeTestErrorCode.HTTP_REQUEST_LIMIT_EXCEEDED
            )

        self.request_count += 1
        return self._delegate.post(*args, **kwargs)

    def close(self):
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()


def run_toss_access_token_smoke_test(
    *,
    environ,
    http_session_factory=requests.Session,
    output=print,
) -> int:
    """Run one token request and verify in-memory cache reuse."""

    http_session = None
    try:
        config = _read_config(environ)
        http_session = _SingleRequestHttpSession(http_session_factory())
        client = TossCertAccessTokenClient(
            client_id=config.client_id,
            client_secret=config.client_secret,
            timeout=config.timeout,
            refresh_skew=config.refresh_skew,
            http_session=http_session,
        )

        first_token = client.get_access_token()
        second_token = client.get_access_token()

        if first_token is not second_token:
            raise TossAccessTokenSmokeTestError(
                TossAccessTokenSmokeTestErrorCode.CACHE_REUSE_FAILED
            )
        if http_session.request_count != 1:
            raise TossAccessTokenSmokeTestError(
                TossAccessTokenSmokeTestErrorCode.CACHE_REUSE_FAILED
            )

        output("Toss Access Token smoke test: OK")
        output(f"Token expires at: {first_token.expires_at.isoformat()}")
        output("Token cache reuse: OK")
        return 0
    except TossAccessTokenSmokeTestError as error:
        _print_failure(output, error.code.value)
        return 1
    except TossCertAccessTokenError as error:
        _print_failure(output, error.code.value)
        return 1
    except Exception:
        _print_failure(
            output,
            TossAccessTokenSmokeTestErrorCode.UNEXPECTED_ERROR.value,
        )
        return 1
    finally:
        if http_session is not None:
            try:
                http_session.close()
            except Exception:
                pass


def main(
    *,
    environ=None,
    http_session_factory=requests.Session,
    output=print,
    load_environment=True,
) -> int:
    if load_environment:
        try:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        except Exception:
            _print_failure(
                output,
                TossAccessTokenSmokeTestErrorCode.INVALID_CONFIGURATION.value,
            )
            return 1

    return run_toss_access_token_smoke_test(
        environ=os.environ if environ is None else environ,
        http_session_factory=http_session_factory,
        output=output,
    )


def _read_config(environ) -> TossAccessTokenSmokeTestConfig:
    client_id = _read_required_value(environ, "TOSS_CERT_CLIENT_ID")
    client_secret = _read_required_value(
        environ,
        "TOSS_CERT_CLIENT_SECRET",
    )

    if (
        not client_id.startswith("test_")
        or not client_secret.startswith("test_")
    ):
        raise TossAccessTokenSmokeTestError(
            TossAccessTokenSmokeTestErrorCode.NON_TEST_CREDENTIAL
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
        raise TossAccessTokenSmokeTestError(
            TossAccessTokenSmokeTestErrorCode.INVALID_CONFIGURATION
        ) from None

    return TossAccessTokenSmokeTestConfig(
        client_id=client_id,
        client_secret=client_secret,
        timeout=timeout,
        refresh_skew=refresh_skew,
    )


def _read_required_value(environ, name):
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TossAccessTokenSmokeTestError(
            TossAccessTokenSmokeTestErrorCode.MISSING_CONFIGURATION
        )
    return value


def _read_seconds(environ, name, *, allow_zero):
    raw_value = _read_required_value(environ, name)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise TossAccessTokenSmokeTestError(
            TossAccessTokenSmokeTestErrorCode.INVALID_CONFIGURATION
        ) from None

    minimum_is_valid = value >= 0 if allow_zero else value > 0
    if not math.isfinite(value) or not minimum_is_valid:
        raise TossAccessTokenSmokeTestError(
            TossAccessTokenSmokeTestErrorCode.INVALID_CONFIGURATION
        )
    return value


def _print_failure(output, code):
    output("Toss Access Token smoke test: FAILED")
    output(f"Error code: {code}")


if __name__ == "__main__":
    raise SystemExit(main())
