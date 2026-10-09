from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
import math
import os
from pathlib import Path
import secrets
import sys
from threading import Lock

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.age_eligibility_service import (  # noqa: E402
    AgeEligibility,
    AgeEligibilityError,
    check_age_eligibility,
)
from services.toss_cert_access_token_client import (  # noqa: E402
    TossCertAccessTokenClient,
)
from services.toss_cert_crypto import (  # noqa: E402
    TossCertCryptoError,
    TossCertCryptoSessionGenerator,
)
from services.toss_identity_verification_result_client import (  # noqa: E402
    TossIdentityVerificationResultClient,
    TossIdentityVerificationResultError,
    TossIdentityVerificationResultErrorCode,
)
from services.toss_identity_verification_start_client import (  # noqa: E402
    TossIdentityVerificationStartClient,
    TossIdentityVerificationStartError,
)


HOST = "127.0.0.1"
PORT = 5056
SDK_URL = "https://cdn.toss.im/cert/v1"


class TossIdentityResultSmokeTestErrorCode(str, Enum):
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    NON_TEST_CREDENTIAL = "NON_TEST_CREDENTIAL"
    INVALID_RSA_PUBLIC_KEY = "INVALID_RSA_PUBLIC_KEY"
    SERVER_ERROR = "SERVER_ERROR"


class TossIdentityResultSmokeTestError(RuntimeError):

    def __init__(self, code: TossIdentityResultSmokeTestErrorCode):
        if type(code) is not TossIdentityResultSmokeTestErrorCode:
            raise TypeError(
                "code must be a TossIdentityResultSmokeTestErrorCode value."
            )

        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class TossIdentityResultSmokeTestConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    request_url: str = field(repr=False)
    rsa_public_key_base64: str = field(repr=False)
    timeout: float
    refresh_skew: timedelta


class SmokeTransactionState(str, Enum):
    PENDING = "pending"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class _SmokeTransaction:
    provider_transaction_id: str = field(repr=False)
    state: SmokeTransactionState = SmokeTransactionState.PENDING
    age_eligibility: AgeEligibility | None = field(default=None, repr=False)


class _SmokeTransactionStore:

    def __init__(self, *, session_id_factory=None):
        self._records = {}
        self._lock = Lock()
        self._session_id_factory = (
            session_id_factory or (lambda: secrets.token_urlsafe(32))
        )

    def create(self, provider_transaction_id):
        if (
            not isinstance(provider_transaction_id, str)
            or not provider_transaction_id.strip()
        ):
            raise ValueError("provider_transaction_id must not be empty.")

        with self._lock:
            for _attempt in range(3):
                smoke_session_id = self._session_id_factory()
                if (
                    not isinstance(smoke_session_id, str)
                    or len(smoke_session_id) < 32
                    or smoke_session_id.strip() != smoke_session_id
                ):
                    raise ValueError("Invalid smoke session id.")
                if smoke_session_id not in self._records:
                    self._records[smoke_session_id] = _SmokeTransaction(
                        provider_transaction_id=provider_transaction_id,
                    )
                    return smoke_session_id

        raise RuntimeError("Could not allocate a smoke session id.")

    def begin_completion(self, smoke_session_id):
        with self._lock:
            transaction = self._records.get(smoke_session_id)
            if transaction is None:
                return None, None

            previous_state = transaction.state
            if previous_state is SmokeTransactionState.PENDING:
                transaction.state = SmokeTransactionState.COMPLETING
                return previous_state, transaction.provider_transaction_id

            return previous_state, None

    def mark_completed(self, smoke_session_id, age_eligibility):
        with self._lock:
            transaction = self._records.get(smoke_session_id)
            if (
                transaction is None
                or transaction.state is not SmokeTransactionState.COMPLETING
            ):
                raise RuntimeError("Invalid completion transition.")
            transaction.age_eligibility = age_eligibility
            transaction.state = SmokeTransactionState.COMPLETED

    def mark_failed(self, smoke_session_id):
        with self._lock:
            transaction = self._records.get(smoke_session_id)
            if (
                transaction is not None
                and transaction.state is SmokeTransactionState.COMPLETING
            ):
                transaction.state = SmokeTransactionState.FAILED

    def snapshot(self, smoke_session_id):
        with self._lock:
            transaction = self._records.get(smoke_session_id)
            if transaction is None:
                return None
            return (
                transaction.state,
                transaction.provider_transaction_id,
                transaction.age_eligibility,
            )

    def __repr__(self):
        return f"{type(self).__name__}(record_count={len(self._records)})"


_PAGE = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Toss Identity Result Smoke Test</title>
  <style>
    body {{
      font-family: sans-serif;
      max-width: 42rem;
      margin: 4rem auto;
      padding: 0 1.25rem;
      line-height: 1.6;
    }}
    button {{
      padding: 0.75rem 1rem;
      font-size: 1rem;
      cursor: pointer;
    }}
    #status {{
      margin-top: 1rem;
      min-height: 4.8rem;
      white-space: pre-line;
    }}
    .notice {{ color: #555; }}
  </style>
  <script src="{SDK_URL}"></script>
</head>
<body>
  <main>
    <h1>Toss Identity Result Smoke Test</h1>
    <p class="notice">
      테스트 인증 완료 후 서버가 결과를 한 번만 조회합니다.
      개인정보 원문은 화면이나 브라우저 저장소에 보관하지 않습니다.
    </p>
    <button id="start-button" type="button">토스 본인확인 테스트</button>
    <p id="status" role="status" aria-live="polite"></p>
  </main>
  <script>
    (() => {{
      const button = document.getElementById("start-button");
      const status = document.getElementById("status");
      const genericFailure = "결과 확인을 완료하지 못했습니다. 새 인증으로 다시 테스트해 주세요.";

      const finishWithFailure = (message = genericFailure) => {{
        status.textContent = message;
        button.disabled = false;
      }};

      const completeVerification = (smokeSessionId) => {{
        status.textContent = "서버에서 인증 결과를 확인하고 있습니다.";
        return fetch("/complete", {{
          method: "POST",
          headers: {{
            "Accept": "application/json",
            "Content-Type": "application/json"
          }},
          credentials: "omit",
          cache: "no-store",
          body: JSON.stringify({{ smoke_session_id: smokeSessionId }})
        }})
          .then((response) => response.json().then((payload) => ({{
            ok: response.ok,
            payload
          }})))
          .then((result) => {{
            if (!result.ok) {{
              finishWithFailure(result.payload.message || genericFailure);
              return;
            }}
            if (!Array.isArray(result.payload.checks)) {{
              finishWithFailure();
              return;
            }}
            status.textContent = result.payload.checks.join("\\n");
            button.disabled = false;
          }})
          .catch(() => finishWithFailure());
      }};

      button.addEventListener("click", (event) => {{
        event.preventDefault();
        let tossCert;
        try {{
          if (typeof TossCert !== "function") {{
            finishWithFailure();
            return;
          }}
          tossCert = TossCert();
          tossCert.preparePopup();
        }} catch (_error) {{
          finishWithFailure();
          return;
        }}

        button.disabled = true;
        status.textContent = "토스 본인확인 요청을 준비하고 있습니다.";

        fetch("/start", {{
          method: "POST",
          headers: {{ "Accept": "application/json" }},
          credentials: "omit",
          cache: "no-store"
        }})
          .then((response) => {{
            if (!response.ok) {{
              throw new Error("start failed");
            }}
            return response.json();
          }})
          .then((payload) => {{
            if (
              typeof payload.txId !== "string" || !payload.txId ||
              typeof payload.authUrl !== "string" || !payload.authUrl ||
              typeof payload.smoke_session_id !== "string" ||
              !payload.smoke_session_id
            ) {{
              finishWithFailure();
              return;
            }}

            try {{
              const started = tossCert.start({{
                authUrl: payload.authUrl,
                txId: payload.txId,
                onSuccess: () => completeVerification(
                  payload.smoke_session_id
                ),
                onFail: () => finishWithFailure(),
                onClose: () => finishWithFailure()
              }});
              Promise.resolve(started).catch(() => finishWithFailure());
            }} catch (_error) {{
              finishWithFailure();
            }}
          }})
          .catch(() => finishWithFailure());
      }});
    }})();
  </script>
</body>
</html>
"""


def create_toss_identity_result_smoke_app(
    config: TossIdentityResultSmokeTestConfig,
    *,
    http_session_factory=requests.Session,
    access_token_client_factory=TossCertAccessTokenClient,
    start_client_factory=TossIdentityVerificationStartClient,
    crypto_session_generator_factory=TossCertCryptoSessionGenerator,
    result_client_factory=TossIdentityVerificationResultClient,
    age_eligibility_checker=check_age_eligibility,
    reference_date_factory=date.today,
    smoke_session_id_factory=None,
    testing=False,
) -> Flask:
    """Create an isolated one-query Toss result smoke harness."""

    _validate_config(config)
    try:
        crypto_session_generator = crypto_session_generator_factory(
            base64_public_key=config.rsa_public_key_base64,
        )
    except (TossCertCryptoError, TypeError, ValueError):
        raise TossIdentityResultSmokeTestError(
            TossIdentityResultSmokeTestErrorCode.INVALID_RSA_PUBLIC_KEY
        ) from None

    token_http_session = http_session_factory()
    start_http_session = http_session_factory()
    result_http_session = http_session_factory()
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
    result_client = result_client_factory(
        access_token_client=access_token_client,
        crypto_session_generator=crypto_session_generator,
        http_session=result_http_session,
        timeout=config.timeout,
    )
    transaction_store = _SmokeTransactionStore(
        session_id_factory=smoke_session_id_factory,
    )

    application = Flask("toss_identity_result_smoke_test")
    application.config.update(
        DEBUG=False,
        TESTING=bool(testing),
        PROPAGATE_EXCEPTIONS=False,
    )
    application.extensions["toss_result_smoke_start_client"] = start_client
    application.extensions["toss_result_smoke_result_client"] = result_client
    application.extensions["toss_result_smoke_transaction_store"] = (
        transaction_store
    )
    application.extensions["toss_result_smoke_http_sessions"] = (
        token_http_session,
        start_http_session,
        result_http_session,
    )

    @application.after_request
    def prevent_sensitive_response_caching(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    @application.get("/")
    def index():
        return Response(_PAGE, mimetype="text/html")

    @application.post("/start")
    def start_identity_verification():
        try:
            result = start_client.start_verification()
            smoke_session_id = transaction_store.create(
                result.provider_transaction_id
            )
            return jsonify(
                txId=result.provider_transaction_id,
                authUrl=result.authentication_url,
                smoke_session_id=smoke_session_id,
            )
        except TossIdentityVerificationStartError:
            return _json_error(
                "START_FAILED",
                "본인확인 요청을 시작하지 못했습니다.",
                502,
            )
        except Exception:
            return _json_error(
                "START_FAILED",
                "본인확인 요청을 시작하지 못했습니다.",
                500,
            )

    @application.post("/complete")
    def complete_identity_verification():
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"smoke_session_id"}
            or not isinstance(payload.get("smoke_session_id"), str)
            or not payload["smoke_session_id"].strip()
        ):
            return _json_error(
                "INVALID_REQUEST",
                "올바른 테스트 세션이 필요합니다.",
                400,
            )

        smoke_session_id = payload["smoke_session_id"]
        previous_state, provider_transaction_id = (
            transaction_store.begin_completion(smoke_session_id)
        )
        if previous_state is None:
            return _json_error(
                "UNKNOWN_SMOKE_SESSION",
                "유효하지 않은 테스트 세션입니다.",
                404,
            )
        if previous_state is SmokeTransactionState.COMPLETING:
            return _json_error(
                "COMPLETION_IN_PROGRESS",
                "결과 확인이 이미 진행 중입니다.",
                409,
            )
        if previous_state is not SmokeTransactionState.PENDING:
            return _json_error(
                "REPLAY_NOT_ALLOWED",
                "이 테스트 세션의 결과 확인은 이미 처리되었습니다.",
                409,
            )

        try:
            result = result_client.get_verified_identity(
                provider_transaction_id
            )
            if result.provider_transaction_id != provider_transaction_id:
                raise ValueError("Provider transaction mismatch.")

            reference_date = reference_date_factory()
            if type(reference_date) is not date:
                raise AgeEligibilityError("Invalid reference date.")
            age_eligibility = age_eligibility_checker(
                birth_date=result.birth_date,
                reference_date=reference_date,
            )
            if type(age_eligibility) is not AgeEligibility:
                raise AgeEligibilityError("Invalid eligibility result.")

            transaction_store.mark_completed(
                smoke_session_id,
                age_eligibility,
            )
            return jsonify(
                status=SmokeTransactionState.COMPLETED.value,
                checks=[
                    "Toss result query: OK",
                    "Birth date decryption: OK",
                    f"Age eligibility: {age_eligibility.value}",
                ],
            )
        except TossIdentityVerificationResultError as error:
            transaction_store.mark_failed(smoke_session_id)
            return _result_error_response(error.code)
        except (AgeEligibilityError, TypeError, ValueError):
            transaction_store.mark_failed(smoke_session_id)
            return _json_error(
                "INVALID_RESULT",
                "인증 결과를 안전하게 확인하지 못했습니다.",
                502,
            )
        except Exception:
            transaction_store.mark_failed(smoke_session_id)
            return _json_error(
                "RESULT_CHECK_FAILED",
                "결과 확인을 완료하지 못했습니다. 새 인증으로 다시 테스트해 주세요.",
                502,
            )

    return application


def build_toss_identity_result_smoke_app_from_environ(
    environ,
    **factory_options,
) -> Flask:
    config = _read_config(environ)
    return create_toss_identity_result_smoke_app(
        config,
        **factory_options,
    )


def run_toss_identity_result_smoke_server(
    application,
    *,
    output=print,
    server_runner=None,
) -> int:
    output("Toss identity result smoke test server:")
    output(f"http://{HOST}:{PORT}")
    runner = server_runner or application.run
    try:
        runner(
            host=HOST,
            port=PORT,
            debug=False,
            use_reloader=False,
        )
        return 0
    finally:
        _close_http_sessions(application)


def main(
    *,
    environ=None,
    output=print,
    load_environment=True,
    server_runner=None,
) -> int:
    if load_environment:
        try:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        except Exception:
            _print_failure(
                output,
                TossIdentityResultSmokeTestErrorCode.INVALID_CONFIGURATION,
            )
            return 1

    try:
        application = build_toss_identity_result_smoke_app_from_environ(
            os.environ if environ is None else environ
        )
        return run_toss_identity_result_smoke_server(
            application,
            output=output,
            server_runner=server_runner,
        )
    except TossIdentityResultSmokeTestError as error:
        _print_failure(output, error.code)
        return 1
    except Exception:
        _print_failure(
            output,
            TossIdentityResultSmokeTestErrorCode.SERVER_ERROR,
        )
        return 1


def _read_config(environ) -> TossIdentityResultSmokeTestConfig:
    client_id = _read_required_value(environ, "TOSS_CERT_CLIENT_ID")
    client_secret = _read_required_value(
        environ,
        "TOSS_CERT_CLIENT_SECRET",
    )
    request_url = _read_required_value(
        environ,
        "TOSS_IDENTITY_REQUEST_URL",
    )
    rsa_public_key_base64 = _read_required_value(
        environ,
        "TOSS_CERT_RSA_PUBLIC_KEY_BASE64",
    )

    if (
        not client_id.startswith("test_")
        or not client_secret.startswith("test_")
    ):
        raise TossIdentityResultSmokeTestError(
            TossIdentityResultSmokeTestErrorCode.NON_TEST_CREDENTIAL
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
        raise TossIdentityResultSmokeTestError(
            TossIdentityResultSmokeTestErrorCode.INVALID_CONFIGURATION
        ) from None

    return TossIdentityResultSmokeTestConfig(
        client_id=client_id,
        client_secret=client_secret,
        request_url=request_url,
        rsa_public_key_base64=rsa_public_key_base64,
        timeout=timeout,
        refresh_skew=refresh_skew,
    )


def _read_required_value(environ, name):
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TossIdentityResultSmokeTestError(
            TossIdentityResultSmokeTestErrorCode.MISSING_CONFIGURATION
        )
    return value


def _read_seconds(environ, name, *, allow_zero):
    raw_value = _read_required_value(environ, name)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise TossIdentityResultSmokeTestError(
            TossIdentityResultSmokeTestErrorCode.INVALID_CONFIGURATION
        ) from None

    minimum_is_valid = value >= 0 if allow_zero else value > 0
    if not math.isfinite(value) or not minimum_is_valid:
        raise TossIdentityResultSmokeTestError(
            TossIdentityResultSmokeTestErrorCode.INVALID_CONFIGURATION
        )
    return value


def _validate_config(config):
    if type(config) is not TossIdentityResultSmokeTestConfig:
        raise TypeError(
            "config must be a TossIdentityResultSmokeTestConfig value."
        )
    if (
        not config.client_id.startswith("test_")
        or not config.client_secret.startswith("test_")
    ):
        raise TossIdentityResultSmokeTestError(
            TossIdentityResultSmokeTestErrorCode.NON_TEST_CREDENTIAL
        )


def _result_error_response(code):
    if code is TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING:
        return _json_error(
            "VERIFICATION_PENDING",
            "결과가 아직 준비되지 않았습니다. 새 인증으로 다시 테스트해 주세요.",
            409,
        )
    if code is TossIdentityVerificationResultErrorCode.AGE_RESTRICTED:
        return _json_error(
            "AGE_RESTRICTED",
            "연령 제한으로 인증 결과를 완료할 수 없습니다.",
            403,
        )
    if code is TossIdentityVerificationResultErrorCode.VERIFICATION_EXPIRED:
        return _json_error(
            "VERIFICATION_EXPIRED",
            "인증이 만료되었습니다. 새 인증으로 다시 테스트해 주세요.",
            409,
        )
    return _json_error(
        "RESULT_CHECK_FAILED",
        "결과 확인을 완료하지 못했습니다. 새 인증으로 다시 테스트해 주세요.",
        502,
    )


def _json_error(code, message, status_code):
    return jsonify(error={"code": code}, message=message), status_code


def _close_http_sessions(application):
    sessions = application.extensions.get(
        "toss_result_smoke_http_sessions",
        (),
    )
    for session in sessions:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _print_failure(output, code):
    output("Toss identity result smoke test server: FAILED")
    output(f"Error code: {code.value}")


if __name__ == "__main__":
    raise SystemExit(main())
