from datetime import datetime
from enum import Enum
from pathlib import Path
import re
import sqlite3
import sys
from threading import Lock
from urllib.parse import quote, urlsplit
from uuid import UUID

from flask import Flask, Response, jsonify, request
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.age_eligibility_service import AgeEligibility  # noqa: E402


HOST = "127.0.0.1"
PORT = 5058
API_BASE_URL = "http://127.0.0.1:5000"
API_START_URL = f"{API_BASE_URL}/api/v1/identity-verifications"
SDK_URL = "https://cdn.toss.im/cert/v1"
HTTP_TIMEOUT_SECONDS = 10.0
OPERATING_DATABASE_PATH = (PROJECT_ROOT / "medicine.db").resolve()
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_AGE_ELIGIBILITY = frozenset(value.value for value in AgeEligibility)
_SAFE_API_ERROR_CODES = frozenset(
    {
        "UNSUPPORTED_MEDIA_TYPE",
        "INVALID_REQUEST",
        "SESSION_NOT_FOUND",
        "PROVIDER_MISMATCH",
        "SESSION_NOT_READY",
        "COMPLETION_IN_PROGRESS",
        "COMPLETION_CONFLICT",
        "SESSION_CONSUMED",
        "PROVIDER_UNAVAILABLE",
        "VERIFICATION_NOT_COMPLETED",
        "AGE_REQUIREMENT_NOT_MET",
        "VERIFICATION_FAILED",
        "VERIFICATION_EXPIRED",
        "INVALID_PROVIDER_RESPONSE",
        "FINALIZATION_FAILED",
        "INTERNAL_SERVER_ERROR",
    }
)
_SAFE_PROXY_STATUS_CODES = frozenset({400, 403, 404, 409, 410, 500, 502, 503})


class TossApiE2ESmokeErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    SESSION_NOT_STARTED = "SESSION_NOT_STARTED"
    COMPLETE_ALREADY_ATTEMPTED = "COMPLETE_ALREADY_ATTEMPTED"
    START_API_FAILED = "START_API_FAILED"
    COMPLETE_API_FAILED = "COMPLETE_API_FAILED"
    INVALID_START_RESPONSE = "INVALID_START_RESPONSE"
    INVALID_COMPLETE_RESPONSE = "INVALID_COMPLETE_RESPONSE"
    DATABASE_VERIFICATION_FAILED = "DATABASE_VERIFICATION_FAILED"
    SERVER_ERROR = "SERVER_ERROR"


class TossApiE2ESmokeError(RuntimeError):

    def __init__(self, code, *, status_code=500):
        if type(code) is not TossApiE2ESmokeErrorCode:
            raise TypeError("code must be a TossApiE2ESmokeErrorCode value.")
        self.code = code
        self.status_code = status_code
        super().__init__(code.value)


class RequestsIdentityApiClient:
    """Make only the two fixed loopback API calls required by the smoke test."""

    def __init__(self, *, session=None, timeout=HTTP_TIMEOUT_SECONDS):
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._timeout = timeout
        self._closed = False

    def start_identity_verification(self):
        return self._session.post(
            API_START_URL,
            json={},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
            allow_redirects=False,
        )

    def complete_identity_verification(self, verification_session_id):
        safe_path_value = quote(verification_session_id, safe="")
        return self._session.post(
            f"{API_START_URL}/{safe_path_value}/complete",
            json={},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
            allow_redirects=False,
        )

    def close(self):
        if not self._closed:
            self._session.close()
            self._closed = True

    def __repr__(self):
        return f"{type(self).__name__}()"


class ReadOnlyIdentityDatabaseVerifier:
    """Read committed identity state without acquiring write capability."""

    def __init__(self, database_path, *, connection_factory=sqlite3.connect):
        self._database_path = Path(database_path).resolve()
        self._connection_factory = connection_factory

    def snapshot_user_count(self):
        connection = self._connect()
        try:
            row = connection.execute("SELECT COUNT(*) FROM users").fetchone()
            if row is None or type(row[0]) is not int:
                raise TossApiE2ESmokeError(
                    TossApiE2ESmokeErrorCode.DATABASE_VERIFICATION_FAILED
                )
            return row[0]
        except TossApiE2ESmokeError:
            raise
        except Exception:
            raise TossApiE2ESmokeError(
                TossApiE2ESmokeErrorCode.DATABASE_VERIFICATION_FAILED
            ) from None
        finally:
            connection.close()

    def verify_completed_session(
        self,
        *,
        verification_session_id,
        expected_age_eligibility,
        expected_user_count,
    ):
        connection = self._connect()
        try:
            session_rows = connection.execute(
                "SELECT identity_verification_session_id, status, "
                "provider_transaction_id, completion_claim_token, "
                "completion_claimed_at, identity_subject_digest, "
                "age_eligibility, failure_code, verified_at, consumed_at "
                "FROM identity_verification_sessions "
                "WHERE verification_session_id = ?",
                (verification_session_id,),
            ).fetchall()
            if len(session_rows) != 1:
                raise TossApiE2ESmokeError(
                    TossApiE2ESmokeErrorCode.DATABASE_VERIFICATION_FAILED
                )

            stored = session_rows[0]
            evidence_rows = connection.execute(
                "SELECT identity_verification_session_id, "
                "provider_transaction_id, signature "
                "FROM toss_identity_verification_evidence "
                "WHERE identity_verification_session_id = ?",
                (stored["identity_verification_session_id"],),
            ).fetchall()
            user_row = connection.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()

            provider_transaction_id = stored["provider_transaction_id"]
            digest = stored["identity_subject_digest"]
            session_is_valid = all(
                (
                    stored["status"] == "verified",
                    isinstance(provider_transaction_id, str),
                    bool(provider_transaction_id),
                    stored["completion_claim_token"] is None,
                    stored["completion_claimed_at"] is None,
                    isinstance(digest, str),
                    _DIGEST_PATTERN.fullmatch(digest) is not None,
                    stored["age_eligibility"] == expected_age_eligibility,
                    stored["failure_code"] is None,
                    stored["verified_at"] is not None,
                    stored["consumed_at"] is None,
                )
            )
            evidence_is_valid = (
                len(evidence_rows) == 1
                and evidence_rows[0]["identity_verification_session_id"]
                == stored["identity_verification_session_id"]
                and evidence_rows[0]["provider_transaction_id"]
                == provider_transaction_id
                and isinstance(evidence_rows[0]["signature"], str)
                and bool(evidence_rows[0]["signature"].strip())
            )
            users_are_unchanged = (
                user_row is not None
                and user_row[0] == expected_user_count
            )
            if not all(
                (session_is_valid, evidence_is_valid, users_are_unchanged)
            ):
                raise TossApiE2ESmokeError(
                    TossApiE2ESmokeErrorCode.DATABASE_VERIFICATION_FAILED
                )
        except TossApiE2ESmokeError:
            raise
        except Exception:
            raise TossApiE2ESmokeError(
                TossApiE2ESmokeErrorCode.DATABASE_VERIFICATION_FAILED
            ) from None
        finally:
            connection.close()

    def _connect(self):
        try:
            database_uri = self._database_path.as_uri() + "?mode=ro"
            connection = self._connection_factory(database_uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if query_only is None or query_only[0] != 1:
                connection.close()
                raise TossApiE2ESmokeError(
                    TossApiE2ESmokeErrorCode.DATABASE_VERIFICATION_FAILED
                )
            return connection
        except TossApiE2ESmokeError:
            raise
        except Exception:
            raise TossApiE2ESmokeError(
                TossApiE2ESmokeErrorCode.DATABASE_VERIFICATION_FAILED
            ) from None

    def __repr__(self):
        return f"{type(self).__name__}()"


class SmokeSessionStore:
    """Keep public IDs and one-shot completion state in process memory only."""

    def __init__(self):
        self._lock = Lock()
        self._sessions = {}

    def register(self, verification_session_id, *, user_count_before):
        with self._lock:
            if verification_session_id in self._sessions:
                raise TossApiE2ESmokeError(
                    TossApiE2ESmokeErrorCode.INVALID_START_RESPONSE,
                    status_code=502,
                )
            self._sessions[verification_session_id] = {
                "state": "started",
                "user_count_before": user_count_before,
            }

    def claim_completion(self, verification_session_id):
        with self._lock:
            stored = self._sessions.get(verification_session_id)
            if stored is None:
                raise TossApiE2ESmokeError(
                    TossApiE2ESmokeErrorCode.SESSION_NOT_STARTED,
                    status_code=400,
                )
            if stored["state"] != "started":
                raise TossApiE2ESmokeError(
                    TossApiE2ESmokeErrorCode.COMPLETE_ALREADY_ATTEMPTED,
                    status_code=409,
                )
            stored["state"] = "completing"
            return stored["user_count_before"]

    def finish_completion(self, verification_session_id):
        with self._lock:
            stored = self._sessions.get(verification_session_id)
            if stored is not None:
                stored["state"] = "finished"

    def __repr__(self):
        return f"{type(self).__name__}()"


_PAGE = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Toss API E2E Smoke Test</title>
  <style>
    body {{
      font-family: sans-serif;
      max-width: 44rem;
      margin: 4rem auto;
      padding: 0 1.25rem;
      line-height: 1.6;
    }}
    button {{ padding: 0.75rem 1rem; font-size: 1rem; cursor: pointer; }}
    #status {{ margin-top: 1rem; min-height: 8rem; white-space: pre-line; }}
    .notice {{ color: #555; }}
  </style>
  <script src="{SDK_URL}"></script>
</head>
<body>
  <main>
    <h1>Toss API E2E Smoke Test</h1>
    <p class="notice">
      실제 localhost API를 통한 본인확인 흐름을 점검합니다.
      본인확인 값은 화면이나 브라우저 저장소에 보관하지 않습니다.
    </p>
    <button id="start-button" type="button">Toss 본인확인 테스트</button>
    <p id="status" role="status" aria-live="polite"></p>
  </main>
  <script>
    (() => {{
      const button = document.getElementById("start-button");
      const status = document.getElementById("status");
      const genericFailure = "처리를 완료하지 못했습니다. 처음부터 다시 시도해 주세요.";
      let runInProgress = false;
      let completeRequested = false;

      const finishWithFailure = (code) => {{
        status.textContent = typeof code === "string" && code
          ? `처리 실패 (${{code}})`
          : genericFailure;
        runInProgress = false;
        button.disabled = false;
      }};

      const readResult = (response) => response.json()
        .catch(() => ({{ error: {{ code: "INVALID_SMOKE_RESPONSE" }} }}))
        .then((payload) => ({{ ok: response.ok, payload }}));

      const completeVerification = (verificationSessionId) => {{
        if (completeRequested) {{
          return;
        }}
        completeRequested = true;
        status.textContent = "API 완료 처리와 DB 검증을 진행하고 있습니다.";
        fetch("/complete", {{
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
          .then(readResult)
          .then((result) => {{
            if (
              !result.ok ||
              result.payload.status !== "verified" ||
              typeof result.payload.age_eligibility !== "string"
            ) {{
              finishWithFailure(result.payload.error?.code);
              return;
            }}
            status.textContent = [
              "API start: OK",
              "Toss verification UI: OK",
              "API complete: OK",
              "Identity session verified: OK",
              `Age eligibility: ${{result.payload.age_eligibility}}`,
              "Evidence finalize: OK"
            ].join("\\n");
            runInProgress = false;
            button.disabled = false;
          }})
          .catch(() => finishWithFailure());
      }};

      button.addEventListener("click", (event) => {{
        event.preventDefault();
        if (runInProgress) {{
          return;
        }}
        runInProgress = true;
        completeRequested = false;
        button.disabled = true;

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

        status.textContent = "API start 요청을 진행하고 있습니다.";
        fetch("/start", {{
          method: "POST",
          headers: {{
            "Accept": "application/json",
            "Content-Type": "application/json"
          }},
          credentials: "omit",
          cache: "no-store",
          body: JSON.stringify({{}})
        }})
          .then(readResult)
          .then((result) => {{
            if (!result.ok) {{
              finishWithFailure(result.payload.error?.code);
              return;
            }}
            const payload = result.payload;
            if (
              typeof payload.txId !== "string" || !payload.txId ||
              typeof payload.authUrl !== "string" || !payload.authUrl ||
              typeof payload.verification_session_id !== "string" ||
              !payload.verification_session_id
            ) {{
              finishWithFailure("INVALID_SMOKE_RESPONSE");
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


def create_toss_api_e2e_smoke_app(
    *,
    http_client=None,
    database_verifier=None,
    session_store=None,
    testing=False,
):
    owns_http_client = http_client is None
    http_client = http_client or RequestsIdentityApiClient()
    database_verifier = database_verifier or ReadOnlyIdentityDatabaseVerifier(
        OPERATING_DATABASE_PATH
    )
    session_store = session_store or SmokeSessionStore()

    application = Flask("toss_api_e2e_smoke_test")
    application.config.update(
        DEBUG=False,
        TESTING=bool(testing),
        PROPAGATE_EXCEPTIONS=False,
    )
    application.extensions["toss_api_e2e_http_client"] = http_client
    application.extensions["toss_api_e2e_database_verifier"] = database_verifier
    application.extensions["toss_api_e2e_session_store"] = session_store
    application.extensions["toss_api_e2e_owns_http_client"] = owns_http_client
    application.extensions["toss_api_e2e_closed"] = False

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
            _require_empty_json_object()
            user_count_before = database_verifier.snapshot_user_count()
            api_response = http_client.start_identity_verification()
            payload = _validated_start_response(api_response)
            session_store.register(
                payload["verification_session_id"],
                user_count_before=user_count_before,
            )
            return jsonify(
                txId=payload["provider_transaction_id"],
                authUrl=payload["authentication_url"],
                verification_session_id=payload["verification_session_id"],
            )
        except TossApiE2ESmokeError as error:
            return _smoke_error_response(error)
        except Exception:
            return _json_error(
                TossApiE2ESmokeErrorCode.SERVER_ERROR.value,
                500,
            )

    @application.post("/complete")
    def complete_identity_verification():
        verification_session_id = None
        claimed = False
        try:
            verification_session_id = _read_complete_session_id()
            user_count_before = session_store.claim_completion(
                verification_session_id
            )
            claimed = True
            api_response = http_client.complete_identity_verification(
                verification_session_id
            )
            payload = _validated_complete_response(
                api_response,
                verification_session_id=verification_session_id,
            )
            database_verifier.verify_completed_session(
                verification_session_id=verification_session_id,
                expected_age_eligibility=payload["age_eligibility"],
                expected_user_count=user_count_before,
            )
            return jsonify(
                status="verified",
                age_eligibility=payload["age_eligibility"],
            )
        except TossApiE2ESmokeError as error:
            return _smoke_error_response(error)
        except Exception:
            return _json_error(
                TossApiE2ESmokeErrorCode.SERVER_ERROR.value,
                500,
            )
        finally:
            if claimed:
                session_store.finish_completion(verification_session_id)

    return application


def close_toss_api_e2e_smoke_app(application):
    if application.extensions.get("toss_api_e2e_closed"):
        return
    application.extensions["toss_api_e2e_closed"] = True
    http_client = application.extensions.get("toss_api_e2e_http_client")
    close_method = getattr(http_client, "close", None)
    if callable(close_method):
        close_method()


def run_toss_api_e2e_smoke_server(
    application,
    *,
    output=print,
    server_runner=None,
):
    output("Toss API E2E smoke test server:")
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
        close_toss_api_e2e_smoke_app(application)


def main(*, output=print, server_runner=None):
    application = None
    try:
        application = create_toss_api_e2e_smoke_app()
        return run_toss_api_e2e_smoke_server(
            application,
            output=output,
            server_runner=server_runner,
        )
    except Exception:
        if application is not None:
            close_toss_api_e2e_smoke_app(application)
        output("Toss API E2E smoke test server failed: SERVER_ERROR")
        return 1


def _validated_start_response(response):
    if getattr(response, "status_code", None) != 201:
        raise _sanitized_api_error(
            response,
            fallback_code=TossApiE2ESmokeErrorCode.START_API_FAILED,
        )
    payload = _read_json_response(
        response,
        invalid_code=TossApiE2ESmokeErrorCode.INVALID_START_RESPONSE,
    )
    required_fields = {
        "verification_session_id",
        "provider_transaction_id",
        "authentication_url",
        "expires_at",
    }
    if set(payload) != required_fields:
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_START_RESPONSE,
            status_code=502,
        )

    verification_session_id = payload["verification_session_id"]
    provider_transaction_id = payload["provider_transaction_id"]
    authentication_url = payload["authentication_url"]
    expires_at = payload["expires_at"]
    if not _is_canonical_uuid(verification_session_id):
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_START_RESPONSE,
            status_code=502,
        )
    if not isinstance(provider_transaction_id, str) or not provider_transaction_id:
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_START_RESPONSE,
            status_code=502,
        )
    if not _is_https_url(authentication_url):
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_START_RESPONSE,
            status_code=502,
        )
    if not _is_timezone_aware_iso8601(expires_at):
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_START_RESPONSE,
            status_code=502,
        )
    return payload


def _validated_complete_response(response, *, verification_session_id):
    if getattr(response, "status_code", None) != 200:
        raise _sanitized_api_error(
            response,
            fallback_code=TossApiE2ESmokeErrorCode.COMPLETE_API_FAILED,
        )
    payload = _read_json_response(
        response,
        invalid_code=TossApiE2ESmokeErrorCode.INVALID_COMPLETE_RESPONSE,
    )
    if set(payload) != {
        "verification_session_id",
        "status",
        "age_eligibility",
    }:
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_COMPLETE_RESPONSE,
            status_code=502,
        )
    if (
        payload["verification_session_id"] != verification_session_id
        or payload["status"] != "verified"
        or payload["age_eligibility"] not in _ALLOWED_AGE_ELIGIBILITY
    ):
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_COMPLETE_RESPONSE,
            status_code=502,
        )
    return payload


def _read_json_response(response, *, invalid_code):
    content_type = getattr(response, "headers", {}).get("Content-Type", "")
    if not content_type.lower().startswith("application/json"):
        raise TossApiE2ESmokeError(
            invalid_code,
            status_code=502,
        )
    try:
        payload = response.json()
    except Exception:
        raise TossApiE2ESmokeError(
            invalid_code,
            status_code=502,
        ) from None
    if not isinstance(payload, dict):
        raise TossApiE2ESmokeError(
            invalid_code,
            status_code=502,
        )
    return payload


def _sanitized_api_error(response, *, fallback_code):
    status_code = getattr(response, "status_code", None)
    safe_status = status_code if status_code in _SAFE_PROXY_STATUS_CODES else 502
    safe_code = fallback_code.value
    try:
        payload = response.json()
        candidate = payload.get("error", {}).get("code")
        if candidate in _SAFE_API_ERROR_CODES:
            safe_code = candidate
    except Exception:
        pass
    error = TossApiE2ESmokeError(fallback_code, status_code=safe_status)
    error.safe_code = safe_code
    return error


def _read_complete_session_id():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {
        "verification_session_id"
    }:
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_REQUEST,
            status_code=400,
        )
    verification_session_id = payload["verification_session_id"]
    if not _is_canonical_uuid(verification_session_id):
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_REQUEST,
            status_code=400,
        )
    return verification_session_id


def _require_empty_json_object():
    payload = request.get_json(silent=True)
    if not request.is_json or payload != {}:
        raise TossApiE2ESmokeError(
            TossApiE2ESmokeErrorCode.INVALID_REQUEST,
            status_code=400,
        )


def _is_canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _is_https_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _is_timezone_aware_iso8601(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _smoke_error_response(error):
    safe_code = getattr(error, "safe_code", error.code.value)
    return _json_error(safe_code, error.status_code)


def _json_error(code, status_code):
    return jsonify(error={"code": code}), status_code


if __name__ == "__main__":
    raise SystemExit(main())
