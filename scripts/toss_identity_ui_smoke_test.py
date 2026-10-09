import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from flask import Flask, Response, jsonify
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.toss_identity_start_smoke_test import (  # noqa: E402
    TossIdentityStartSmokeTestConfig,
    TossIdentityStartSmokeTestError,
    TossIdentityStartSmokeTestErrorCode,
    _read_config,
)
from services.toss_cert_access_token_client import (  # noqa: E402
    TossCertAccessTokenClient,
)
from services.toss_identity_verification_start_client import (  # noqa: E402
    TossIdentityVerificationStartClient,
    TossIdentityVerificationStartError,
)


HOST = "127.0.0.1"
PORT = 5055
SDK_URL = "https://cdn.toss.im/cert/v1"
_SERVER_ERROR_CODE = "SERVER_ERROR"
_UNEXPECTED_ERROR_CODE = "UNEXPECTED_ERROR"


_PAGE = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Toss Identity UI Smoke Test</title>
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
      min-height: 1.6rem;
    }}
    .notice {{ color: #555; }}
  </style>
  <script src="{SDK_URL}"></script>
</head>
<body>
  <main>
    <h1>Toss Identity UI Smoke Test</h1>
    <p class="notice">
      이 화면은 테스트 환경의 토스 본인확인 표준창만 확인합니다.
      성공 문구는 서버의 본인확인 완료 판정이 아닙니다.
    </p>
    <button id="start-button" type="button">토스로 본인확인 테스트</button>
    <p id="status" role="status" aria-live="polite"></p>
  </main>
  <script>
    (() => {{
      const button = document.getElementById("start-button");
      const status = document.getElementById("status");
      const failedMessage = "토스 본인확인을 완료하지 않았습니다";

      const showFailure = () => {{
        status.textContent = failedMessage;
        button.disabled = false;
      }};

      button.addEventListener("click", () => {{
        let tossCert;
        try {{
          if (typeof TossCert !== "function") {{
            showFailure();
            return;
          }}
          tossCert = TossCert();
          tossCert.preparePopup();
        }} catch (_error) {{
          showFailure();
          return;
        }}

        button.disabled = true;
        status.textContent = "토스 본인확인 표준창을 준비하고 있습니다";

        fetch("/start", {{
          method: "POST",
          headers: {{ "Accept": "application/json" }},
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
              typeof payload.authUrl !== "string" || !payload.authUrl
            ) {{
              showFailure();
              return;
            }}

            try {{
              const started = tossCert.start({{
                authUrl: payload.authUrl,
                txId: payload.txId,
                onSuccess: () => {{
                  status.textContent = "토스 본인확인 UI 진행 완료";
                  button.disabled = false;
                }},
                onFail: showFailure,
                onClose: showFailure
              }});
              Promise.resolve(started).catch(showFailure);
            }} catch (_error) {{
              showFailure();
            }}
          }})
          .catch(showFailure);
      }});
    }})();
  </script>
</body>
</html>
"""


def create_toss_identity_ui_smoke_app(
    config: TossIdentityStartSmokeTestConfig,
    *,
    http_session_factory=requests.Session,
    access_token_client_factory=TossCertAccessTokenClient,
    start_client_factory=TossIdentityVerificationStartClient,
    testing=False,
) -> Flask:
    """Create an isolated localhost UI harness without DB dependencies."""

    _validate_config(config)
    token_http_session = http_session_factory()
    start_http_session = http_session_factory()
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

    application = Flask("toss_identity_ui_smoke_test")
    application.config.update(
        DEBUG=False,
        TESTING=bool(testing),
        PROPAGATE_EXCEPTIONS=False,
    )
    application.extensions["toss_ui_smoke_start_client"] = start_client
    application.extensions["toss_ui_smoke_http_sessions"] = (
        token_http_session,
        start_http_session,
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
            return jsonify(
                txId=result.provider_transaction_id,
                authUrl=result.authentication_url,
            )
        except TossIdentityVerificationStartError as error:
            return jsonify(error={"code": error.code.value}), 502
        except Exception:
            return jsonify(
                error={"code": _UNEXPECTED_ERROR_CODE}
            ), 500

    return application


def build_toss_identity_ui_smoke_app_from_environ(
    environ,
    **factory_options,
) -> Flask:
    config = _read_config(environ)
    return create_toss_identity_ui_smoke_app(
        config,
        **factory_options,
    )


def run_toss_identity_ui_smoke_server(
    application,
    *,
    output=print,
    server_runner=None,
) -> int:
    output("Toss identity UI smoke test server:")
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
            _print_failure(output, "INVALID_CONFIGURATION")
            return 1

    try:
        application = build_toss_identity_ui_smoke_app_from_environ(
            os.environ if environ is None else environ
        )
        return run_toss_identity_ui_smoke_server(
            application,
            output=output,
            server_runner=server_runner,
        )
    except TossIdentityStartSmokeTestError as error:
        _print_failure(output, error.code.value)
        return 1
    except Exception:
        _print_failure(output, _SERVER_ERROR_CODE)
        return 1


def _validate_config(config):
    if type(config) is not TossIdentityStartSmokeTestConfig:
        raise TypeError(
            "config must be a TossIdentityStartSmokeTestConfig value."
        )
    if (
        not config.client_id.startswith("test_")
        or not config.client_secret.startswith("test_")
    ):
        raise TossIdentityStartSmokeTestError(
            TossIdentityStartSmokeTestErrorCode.NON_TEST_CREDENTIAL
        )


def _close_http_sessions(application):
    sessions = application.extensions.get(
        "toss_ui_smoke_http_sessions",
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
    output("Toss identity UI smoke test server: FAILED")
    output(f"Error code: {code}")


if __name__ == "__main__":
    raise SystemExit(main())
