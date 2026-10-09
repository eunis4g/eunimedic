from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
import requests
from sqlalchemy import func, inspect, select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models import (  # noqa: E402
    IdentityVerificationSession,
    TossIdentityVerificationEvidence,
    User,
    db,
)
from services.identity_verification_service import (  # noqa: E402
    IdentityVerificationServiceError,
    IdentityVerificationServiceErrorCode,
    complete_identity_verification as complete_identity_verification_service,
    start_identity_verification as start_identity_verification_service,
)
from services.toss_identity_verification_wiring import (  # noqa: E402
    TossIdentityVerificationConfig,
    TossIdentityVerificationDependencies,
    build_toss_identity_verification_dependencies,
)


HOST = "127.0.0.1"
PORT = 5057
SDK_URL = "https://cdn.toss.im/cert/v1"
SESSION_TTL = timedelta(minutes=10)
OPERATING_DATABASE_PATH = (PROJECT_ROOT / "medicine.db").resolve()
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_TABLES = (
    IdentityVerificationSession.__tablename__,
    TossIdentityVerificationEvidence.__tablename__,
)
_FORBIDDEN_IDENTITY_COLUMNS = {
    "birthday",
    "birth_date",
    "di",
    "ci",
    "name",
    "phone",
    "gender",
    "nationality",
    "raw_toss_response",
    "auth_url",
    "access_token",
    "session_key",
    "aes_key",
    "iv",
    "ciphertext",
}


class TossIdentityDbSmokeErrorCode(str, Enum):
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    NON_TEST_CREDENTIAL = "NON_TEST_CREDENTIAL"
    UNSAFE_DATABASE_PATH = "UNSAFE_DATABASE_PATH"
    DATABASE_INITIALIZATION_FAILED = "DATABASE_INITIALIZATION_FAILED"
    SERVER_ERROR = "SERVER_ERROR"


class TossIdentityDbSmokeError(RuntimeError):

    def __init__(self, code: TossIdentityDbSmokeErrorCode):
        if type(code) is not TossIdentityDbSmokeErrorCode:
            raise TypeError("code must be a TossIdentityDbSmokeErrorCode value.")
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class TossIdentityDbSmokeConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    request_url: str = field(repr=False)
    rsa_public_key_base64: str = field(repr=False)
    http_timeout: float
    token_refresh_skew: timedelta


class TemporarySmokeDatabase:
    """Own one file-based SQLite database outside the project directory."""

    def __init__(self, *, temporary_directory_factory=tempfile.TemporaryDirectory):
        self._temporary_directory = temporary_directory_factory(
            prefix="medicine_web_toss_smoke_"
        )
        self.path = (
            Path(self._temporary_directory.name) / "smoke.db"
        ).resolve()
        try:
            ensure_safe_database_path(self.path)
        except Exception:
            self._temporary_directory.cleanup()
            raise
        self._cleaned = False

    def cleanup(self):
        if not self._cleaned:
            self._temporary_directory.cleanup()
            self._cleaned = True

    def __repr__(self):
        return f"{type(self).__name__}()"


_PAGE = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Toss Identity DB Smoke Test</title>
  <style>
    body {{
      font-family: sans-serif;
      max-width: 44rem;
      margin: 4rem auto;
      padding: 0 1.25rem;
      line-height: 1.6;
    }}
    button {{ padding: 0.75rem 1rem; font-size: 1rem; cursor: pointer; }}
    #status {{ margin-top: 1rem; min-height: 7rem; white-space: pre-line; }}
    .notice {{ color: #555; }}
  </style>
  <script src="{SDK_URL}"></script>
</head>
<body>
  <main>
    <h1>Toss Identity DB Smoke Test</h1>
    <p class="notice">
      실제 Toss 테스트 인증 결과를 임시 SQLite DB에만 저장해 검증합니다.
      개인정보 원문은 화면이나 브라우저 저장소에 보관하지 않습니다.
    </p>
    <button id="start-button" type="button">Toss 본인확인 테스트</button>
    <p id="status" role="status" aria-live="polite"></p>
  </main>
  <script>
    (() => {{
      const button = document.getElementById("start-button");
      const status = document.getElementById("status");
      const genericFailure = "처리를 완료하지 못했습니다. 처음부터 다시 테스트해 주세요.";

      const finishWithFailure = (message = genericFailure) => {{
        status.textContent = message;
        button.disabled = false;
      }};

      const completeVerification = (verificationSessionId) => {{
        status.textContent = "서버에서 임시 DB 저장 결과를 확인하고 있습니다.";
        return fetch("/complete", {{
          method: "POST",
          headers: {{
            "Accept": "application/json",
            "Content-Type": "application/json"
          }},
          credentials: "omit",
          cache: "no-store",
          body: JSON.stringify({{
            verification_session_id: verificationSessionId
          }})
        }})
          .then((response) => response.json().then((payload) => ({{
            ok: response.ok,
            payload
          }})))
          .then((result) => {{
            if (!result.ok || !Array.isArray(result.payload.checks)) {{
              finishWithFailure(result.payload.message || genericFailure);
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
        status.textContent = "Toss 본인확인 요청을 준비하고 있습니다.";
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
              typeof payload.verification_session_id !== "string" ||
              !payload.verification_session_id
            ) {{
              finishWithFailure();
              return;
            }}
            try {{
              const started = tossCert.start({{
                authUrl: payload.authUrl,
                txId: payload.txId,
                onSuccess: () => completeVerification(
                  payload.verification_session_id
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


def ensure_safe_database_path(
    database_path,
    *,
    operating_database_path=OPERATING_DATABASE_PATH,
    project_root=PROJECT_ROOT,
    temporary_root=None,
):
    database_path = Path(database_path).resolve()
    operating_database_path = Path(operating_database_path).resolve()
    project_root = Path(project_root).resolve()
    temporary_root = Path(
        tempfile.gettempdir() if temporary_root is None else temporary_root
    ).resolve()

    if (
        database_path == operating_database_path
        or _is_relative_to(database_path, project_root)
        or not _is_relative_to(database_path, temporary_root)
        or database_path.name != "smoke.db"
    ):
        raise TossIdentityDbSmokeError(
            TossIdentityDbSmokeErrorCode.UNSAFE_DATABASE_PATH
        )


def create_toss_identity_db_smoke_app(
    config: TossIdentityDbSmokeConfig,
    *,
    dependencies=None,
    dependency_builder=build_toss_identity_verification_dependencies,
    http_session_factory=requests.Session,
    database_owner=None,
    action_time_factory=lambda: datetime.now(UTC),
    testing=False,
) -> Flask:
    """Build an isolated Toss E2E app backed only by a temporary SQLite DB."""

    _validate_config(config)
    owner = database_owner or TemporarySmokeDatabase()
    http_session = None
    application = None
    try:
        ensure_safe_database_path(owner.path)
        if dependencies is None:
            http_session = http_session_factory()
            wiring_config = TossIdentityVerificationConfig(
                client_id=config.client_id,
                client_secret=config.client_secret,
                request_url=config.request_url,
                rsa_public_key_base64=config.rsa_public_key_base64,
                http_timeout=config.http_timeout,
                token_refresh_skew=config.token_refresh_skew,
                identity_subject_hmac_key=secrets.token_bytes(32),
            )
            dependencies = dependency_builder(
                wiring_config,
                http_session=http_session,
            )
        if not isinstance(
            dependencies,
            TossIdentityVerificationDependencies,
        ):
            raise TypeError(
                "dependencies must be TossIdentityVerificationDependencies."
            )

        application = Flask("toss_identity_db_smoke_test")
        application.config.update(
            DEBUG=False,
            TESTING=bool(testing),
            PROPAGATE_EXCEPTIONS=False,
            SQLALCHEMY_DATABASE_URI="sqlite:///" + owner.path.as_posix(),
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(application)
        with application.app_context():
            db.create_all()

        application.extensions["toss_identity_db_dependencies"] = dependencies
        application.extensions["toss_identity_db_database_owner"] = owner
        application.extensions["toss_identity_db_http_session"] = http_session
        application.extensions["toss_identity_db_closed"] = False

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
                action_time = _read_action_time(action_time_factory)
                result = start_identity_verification_service(
                    db.session,
                    provider=dependencies.provider,
                    action_time=action_time,
                    session_ttl=SESSION_TTL,
                )
                return jsonify(
                    txId=result.provider_transaction_id,
                    authUrl=result.authentication_url,
                    verification_session_id=result.verification_session_id,
                )
            except IdentityVerificationServiceError as error:
                return _service_error_response(error.code)
            except Exception:
                db.session.rollback()
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
                or set(payload) != {"verification_session_id"}
                or not isinstance(payload.get("verification_session_id"), str)
                or not payload["verification_session_id"].strip()
            ):
                return _json_error(
                    "INVALID_REQUEST",
                    "올바른 본인확인 세션이 필요합니다.",
                    400,
                )

            verification_session_id = payload["verification_session_id"]
            try:
                action_time = _read_action_time(action_time_factory)
                result = complete_identity_verification_service(
                    db.session,
                    verification_session_id=verification_session_id,
                    provider=dependencies.provider,
                    action_time=action_time,
                    identity_subject_hmac_key=(
                        dependencies.identity_subject_hmac_key
                    ),
                    evidence_writer=dependencies.evidence_writer,
                )
                checks = _verified_database_checks(
                    verification_session_id=verification_session_id,
                    expected_age_eligibility=result.age_eligibility.value,
                )
                return jsonify(status=result.status, checks=checks)
            except IdentityVerificationServiceError as error:
                return _service_error_response(error.code)
            except Exception:
                db.session.rollback()
                return _json_error(
                    "FINALIZATION_CHECK_FAILED",
                    "임시 DB 저장 결과를 확인하지 못했습니다.",
                    500,
                )

        return application
    except Exception:
        if application is not None:
            try:
                with application.app_context():
                    db.session.remove()
                    db.engine.dispose()
            except Exception:
                pass
        if http_session is not None:
            _close_if_possible(http_session)
        owner.cleanup()
        raise


def build_toss_identity_db_smoke_app_from_environ(environ, **factory_options):
    return create_toss_identity_db_smoke_app(
        _read_config(environ),
        **factory_options,
    )


def close_toss_identity_db_smoke_app(application):
    if application.extensions.get("toss_identity_db_closed"):
        return
    application.extensions["toss_identity_db_closed"] = True
    with application.app_context():
        db.session.remove()
        db.engine.dispose()
    _close_if_possible(
        application.extensions.get("toss_identity_db_http_session")
    )
    application.extensions["toss_identity_db_database_owner"].cleanup()


def run_toss_identity_db_smoke_server(
    application,
    *,
    output=print,
    server_runner=None,
) -> int:
    output("Toss identity temporary-DB smoke test server:")
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
        close_toss_identity_db_smoke_app(application)


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
                TossIdentityDbSmokeErrorCode.INVALID_CONFIGURATION,
            )
            return 1

    application = None
    try:
        application = build_toss_identity_db_smoke_app_from_environ(
            os.environ if environ is None else environ
        )
        return run_toss_identity_db_smoke_server(
            application,
            output=output,
            server_runner=server_runner,
        )
    except TossIdentityDbSmokeError as error:
        _print_failure(output, error.code)
        return 1
    except Exception:
        if application is not None:
            close_toss_identity_db_smoke_app(application)
        _print_failure(output, TossIdentityDbSmokeErrorCode.SERVER_ERROR)
        return 1


def _verified_database_checks(*, verification_session_id, expected_age_eligibility):
    db.session.expire_all()
    verification_session = db.session.scalar(
        select(IdentityVerificationSession).where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id
        )
    )
    if verification_session is None:
        raise RuntimeError("Verified session is missing.")

    evidence_rows = db.session.scalars(
        select(TossIdentityVerificationEvidence).where(
            TossIdentityVerificationEvidence.identity_verification_session_id
            == verification_session.identity_verification_session_id
        )
    ).all()
    session_is_valid = all(
        (
            verification_session.status == "verified",
            isinstance(verification_session.provider_transaction_id, str),
            bool(verification_session.provider_transaction_id),
            verification_session.age_eligibility == expected_age_eligibility,
            verification_session.verified_at is not None,
            verification_session.completion_claim_token is None,
            verification_session.completion_claimed_at is None,
            verification_session.failure_code is None,
            verification_session.consumed_at is None,
        )
    )
    digest_is_valid = (
        isinstance(verification_session.identity_subject_digest, str)
        and _DIGEST_PATTERN.fullmatch(
            verification_session.identity_subject_digest
        )
        is not None
    )
    evidence_is_valid = (
        len(evidence_rows) == 1
        and evidence_rows[0].identity_verification_session_id
        == verification_session.identity_verification_session_id
        and evidence_rows[0].provider_transaction_id
        == verification_session.provider_transaction_id
        and isinstance(evidence_rows[0].signature, str)
        and bool(evidence_rows[0].signature.strip())
        and evidence_rows[0].created_at is not None
    )
    schema_is_private = _identity_schema_has_no_plaintext_columns()
    user_count = db.session.scalar(select(func.count()).select_from(User))
    if not all(
        (
            session_is_valid,
            digest_is_valid,
            evidence_is_valid,
            schema_is_private,
            user_count == 0,
        )
    ):
        raise RuntimeError("Temporary database invariant failed.")

    return [
        "Toss verification result: OK",
        "Identity session verified: OK",
        f"Age eligibility: {expected_age_eligibility}",
        "Identity digest stored: OK",
        "Toss evidence stored: OK",
        "Atomic finalize: OK",
    ]


def _identity_schema_has_no_plaintext_columns():
    database_inspector = inspect(db.engine)
    for table_name in _IDENTITY_TABLES:
        column_names = {
            column["name"].lower()
            for column in database_inspector.get_columns(table_name)
        }
        if column_names & _FORBIDDEN_IDENTITY_COLUMNS:
            return False
    return True


def _read_config(environ) -> TossIdentityDbSmokeConfig:
    client_id = _read_required_value(environ, "TOSS_CERT_CLIENT_ID")
    client_secret = _read_required_value(environ, "TOSS_CERT_CLIENT_SECRET")
    request_url = _read_required_value(environ, "TOSS_IDENTITY_REQUEST_URL")
    rsa_public_key_base64 = _read_required_value(
        environ,
        "TOSS_CERT_RSA_PUBLIC_KEY_BASE64",
    )
    if not client_id.startswith("test_") or not client_secret.startswith("test_"):
        raise TossIdentityDbSmokeError(
            TossIdentityDbSmokeErrorCode.NON_TEST_CREDENTIAL
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
        raise TossIdentityDbSmokeError(
            TossIdentityDbSmokeErrorCode.INVALID_CONFIGURATION
        ) from None

    return TossIdentityDbSmokeConfig(
        client_id=client_id,
        client_secret=client_secret,
        request_url=request_url,
        rsa_public_key_base64=rsa_public_key_base64,
        http_timeout=timeout,
        token_refresh_skew=refresh_skew,
    )


def _read_required_value(environ, name):
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TossIdentityDbSmokeError(
            TossIdentityDbSmokeErrorCode.MISSING_CONFIGURATION
        )
    return value


def _read_seconds(environ, name, *, allow_zero):
    raw_value = _read_required_value(environ, name)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise TossIdentityDbSmokeError(
            TossIdentityDbSmokeErrorCode.INVALID_CONFIGURATION
        ) from None
    valid_minimum = value >= 0 if allow_zero else value > 0
    if not math.isfinite(value) or not valid_minimum:
        raise TossIdentityDbSmokeError(
            TossIdentityDbSmokeErrorCode.INVALID_CONFIGURATION
        )
    return value


def _validate_config(config):
    if type(config) is not TossIdentityDbSmokeConfig:
        raise TypeError("config must be TossIdentityDbSmokeConfig.")
    if not config.client_id.startswith("test_") or not config.client_secret.startswith(
        "test_"
    ):
        raise TossIdentityDbSmokeError(
            TossIdentityDbSmokeErrorCode.NON_TEST_CREDENTIAL
        )


def _read_action_time(factory):
    value = factory()
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("action_time_factory must return an aware datetime.")
    return value


def _service_error_response(code):
    mapping = {
        IdentityVerificationServiceErrorCode.VERIFICATION_NOT_COMPLETED: (
            "VERIFICATION_PENDING",
            "결과가 아직 준비되지 않았습니다. 처음부터 다시 테스트해 주세요.",
            409,
        ),
        IdentityVerificationServiceErrorCode.AGE_REQUIREMENT_NOT_MET: (
            "AGE_RESTRICTED",
            "연령 제한으로 본인확인을 완료하지 못했습니다.",
            403,
        ),
        IdentityVerificationServiceErrorCode.COMPLETION_IN_PROGRESS: (
            "COMPLETION_IN_PROGRESS",
            "결과 확인이 이미 진행 중입니다.",
            409,
        ),
        IdentityVerificationServiceErrorCode.SESSION_NOT_FOUND: (
            "SESSION_NOT_FOUND",
            "유효하지 않은 본인확인 세션입니다.",
            404,
        ),
        IdentityVerificationServiceErrorCode.FINALIZATION_FAILED: (
            "FINALIZATION_FAILED",
            "임시 DB 저장을 완료하지 못했습니다.",
            500,
        ),
    }
    response = mapping.get(
        code,
        (
            "VERIFICATION_FAILED",
            "본인확인을 완료하지 못했습니다.",
            409,
        ),
    )
    return _json_error(*response)


def _json_error(code, message, status_code):
    return jsonify(error={"code": code}, message=message), status_code


def _close_if_possible(value):
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _print_failure(output, code):
    output(f"Toss identity temporary-DB smoke test failed: {code.value}")


if __name__ == "__main__":
    raise SystemExit(main())
