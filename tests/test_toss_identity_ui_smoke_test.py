import ast
import inspect
import unittest

import scripts.toss_identity_ui_smoke_test as ui_smoke
from scripts.toss_identity_start_smoke_test import (
    TossIdentityStartSmokeTestConfig,
)
from scripts.toss_identity_ui_smoke_test import (
    HOST,
    PORT,
    SDK_URL,
    build_toss_identity_ui_smoke_app_from_environ,
    create_toss_identity_ui_smoke_app,
    run_toss_identity_ui_smoke_server,
)
from services.toss_cert_access_token_client import (
    TossCertAccessTokenClient,
)
from services.toss_identity_verification_start_client import (
    TossIdentityVerificationStartClient,
)


CLIENT_ID = "test_synthetic-client-id"
CLIENT_SECRET = "test_synthetic-client-secret"
REQUEST_URL = "https://cert.toss.im"
TRANSACTION_ID = "sensitive-provider-transaction-id"
AUTHENTICATION_URL = "https://auth.example.test/sensitive-handoff"
ACCESS_TOKEN = "sensitive-access-token"
RAW_PROVIDER_BODY = "raw-sensitive-provider-response"


class StubResult:
    provider_transaction_id = TRANSACTION_ID
    authentication_url = AUTHENTICATION_URL


class StubStartClient:

    def __init__(self, *, result=None, error=None):
        self.result = result or StubResult()
        self.error = error
        self.call_count = 0

    def start_verification(self):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.result


class StubStartClientFactory:

    def __init__(self, client=None):
        self.client = client or StubStartClient()
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.client


class StubAccessTokenClient:

    def get_access_token(self):
        raise AssertionError("Stub start client must not request a token.")


class StubAccessTokenClientFactory:

    def __init__(self):
        self.calls = []
        self.client = StubAccessTokenClient()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.client


class FakeHttpSession:

    def __init__(self):
        self.post_calls = []
        self.get_calls = []
        self.close_count = 0

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        raise AssertionError("Unit tests must not make HTTP requests.")

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("The authentication URL must not be fetched.")

    def close(self):
        self.close_count += 1


class QueuedSessionFactory:

    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        return self.sessions.pop(0)


class FactorySpy:

    def __init__(self, factory):
        self.factory = factory
        self.calls = []
        self.instances = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        instance = self.factory(**kwargs)
        self.instances.append(instance)
        return instance


class OutputCapture:

    def __init__(self):
        self.lines = []

    def __call__(self, value):
        self.lines.append(value)


class ServerRunnerSpy:

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class TossIdentityUiSmokeTestTest(unittest.TestCase):

    def make_config(self, **overrides):
        values = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "request_url": REQUEST_URL,
            "timeout": 5.0,
            "refresh_skew": __import__("datetime").timedelta(seconds=30),
        }
        values.update(overrides)
        return TossIdentityStartSmokeTestConfig(**values)

    def make_environ(self, **overrides):
        values = {
            "TOSS_CERT_CLIENT_ID": CLIENT_ID,
            "TOSS_CERT_CLIENT_SECRET": CLIENT_SECRET,
            "TOSS_HTTP_TIMEOUT_SECONDS": "5",
            "TOSS_TOKEN_REFRESH_SKEW_SECONDS": "30",
            "TOSS_IDENTITY_REQUEST_URL": REQUEST_URL,
        }
        values.update(overrides)
        return values

    def make_app(
        self,
        *,
        config=None,
        access_token_factory=None,
        start_factory=None,
    ):
        token_session = FakeHttpSession()
        start_session = FakeHttpSession()
        session_factory = QueuedSessionFactory(
            token_session,
            start_session,
        )
        access_token_factory = (
            access_token_factory or StubAccessTokenClientFactory()
        )
        start_factory = start_factory or StubStartClientFactory()
        application = create_toss_identity_ui_smoke_app(
            config or self.make_config(),
            http_session_factory=session_factory,
            access_token_client_factory=access_token_factory,
            start_client_factory=start_factory,
            testing=True,
        )
        return (
            application,
            application.test_client(),
            access_token_factory,
            start_factory,
            token_session,
            start_session,
        )

    def test_root_page_returns_200_and_no_store(self):
        _, client, _, _, _, _ = self.make_app()

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")

    def test_root_page_loads_only_official_toss_sdk_url(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertIn(f'<script src="{SDK_URL}"></script>', html)
        self.assertEqual(html.count(SDK_URL), 1)

    def test_page_uses_toss_cert_prepare_popup_and_start(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertIn("TossCert()", html)
        self.assertIn("tossCert.preparePopup()", html)
        self.assertIn("tossCert.start({", html)

    def test_prepare_popup_occurs_before_asynchronous_start_request(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertLess(
            html.index("tossCert.preparePopup()"),
            html.index('fetch("/start"'),
        )

    def test_start_endpoint_is_post_only(self):
        _, client, _, _, _, _ = self.make_app()

        self.assertEqual(client.get("/start").status_code, 405)
        self.assertEqual(client.post("/start").status_code, 200)

    def test_production_client_classes_can_be_created_by_harness(self):
        token_factory = FactorySpy(TossCertAccessTokenClient)
        start_factory = FactorySpy(TossIdentityVerificationStartClient)
        application, _, _, _, _, _ = self.make_app(
            access_token_factory=token_factory,
            start_factory=start_factory,
        )

        self.assertIsInstance(
            token_factory.instances[0],
            TossCertAccessTokenClient,
        )
        self.assertIsInstance(
            start_factory.instances[0],
            TossIdentityVerificationStartClient,
        )
        self.assertIs(
            application.extensions["toss_ui_smoke_start_client"],
            start_factory.instances[0],
        )

    def test_start_verification_is_called_once_per_endpoint_request(self):
        start_factory = StubStartClientFactory()
        _, client, _, _, _, _ = self.make_app(
            start_factory=start_factory
        )

        response = client.post("/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(start_factory.client.call_count, 1)

    def test_start_response_contains_only_required_handoff_fields(self):
        _, client, _, _, _, _ = self.make_app()

        response = client.post("/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"txId": TRANSACTION_ID, "authUrl": AUTHENTICATION_URL},
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_start_response_is_not_cached_and_sets_no_cookie(self):
        _, client, _, _, _, _ = self.make_app()

        response = client.post("/start")

        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertNotIn("Set-Cookie", response.headers)

    def test_handoff_values_are_not_rendered_in_html(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertNotIn(TRANSACTION_ID, html)
        self.assertNotIn(AUTHENTICATION_URL, html)
        self.assertNotIn(ACCESS_TOKEN, html)

    def test_page_does_not_log_or_persist_handoff_values(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertNotIn("console.", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("sessionStorage", html)
        self.assertNotIn("document.cookie", html)

    def test_sdk_receives_handoff_fields_directly_from_memory(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertIn("authUrl: payload.authUrl", html)
        self.assertIn("txId: payload.txId", html)

    def test_success_failure_and_close_callbacks_show_generic_messages(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertIn("토스 본인확인 UI 진행 완료", html)
        self.assertIn("토스 본인확인을 완료하지 않았습니다", html)
        self.assertIn("onFail: showFailure", html)
        self.assertIn("onClose: showFailure", html)

    def test_page_has_no_result_or_status_query(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertNotIn("/result", html)
        self.assertNotIn("/status", html)
        self.assertNotIn("get_verified_identity", html)

    def test_authentication_url_is_not_opened_directly(self):
        source = inspect.getsource(ui_smoke)

        self.assertNotIn("webbrowser", source)
        self.assertNotIn("window.open", source)
        self.assertNotIn("location.href", source)
        self.assertNotIn("requests.get", source)

    def test_test_credentials_are_required_before_client_creation(self):
        for field_name in (
            "TOSS_CERT_CLIENT_ID",
            "TOSS_CERT_CLIENT_SECRET",
        ):
            with self.subTest(field_name=field_name):
                environ = self.make_environ()
                environ[field_name] = "production-credential"
                session_factory = QueuedSessionFactory()

                with self.assertRaises(Exception):
                    build_toss_identity_ui_smoke_app_from_environ(
                        environ,
                        http_session_factory=session_factory,
                    )

                self.assertEqual(session_factory.call_count, 0)

    def test_missing_request_url_is_rejected_before_client_creation(self):
        environ = self.make_environ()
        del environ["TOSS_IDENTITY_REQUEST_URL"]
        session_factory = QueuedSessionFactory()

        with self.assertRaises(Exception):
            build_toss_identity_ui_smoke_app_from_environ(
                environ,
                http_session_factory=session_factory,
            )

        self.assertEqual(session_factory.call_count, 0)

    def test_provider_error_response_is_stable_and_redacted(self):
        from services.toss_identity_verification_start_client import (
            TossIdentityVerificationStartError,
            TossIdentityVerificationStartErrorCode,
        )

        start_factory = StubStartClientFactory(
            StubStartClient(
                error=TossIdentityVerificationStartError(
                    TossIdentityVerificationStartErrorCode.REQUEST_REJECTED
                )
            )
        )
        _, client, _, _, _, _ = self.make_app(
            start_factory=start_factory
        )

        response = client.post("/start")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {"error": {"code": "REQUEST_REJECTED"}},
        )
        self.assertNotIn(RAW_PROVIDER_BODY, body)
        self.assertNotIn(TRANSACTION_ID, body)
        self.assertNotIn(AUTHENTICATION_URL, body)

    def test_unexpected_start_error_response_is_generic_and_redacted(self):
        raw_error = "unexpected-sensitive-provider-detail"
        start_factory = StubStartClientFactory(
            StubStartClient(error=RuntimeError(raw_error))
        )
        _, client, _, _, _, _ = self.make_app(
            start_factory=start_factory
        )

        response = client.post("/start")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(),
            {"error": {"code": "UNEXPECTED_ERROR"}},
        )
        self.assertNotIn(raw_error, body)

    def test_harness_has_no_result_crypto_hmac_model_or_database_dependency(self):
        source = inspect.getsource(ui_smoke)
        syntax_tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
        }
        imported_roots = {
            module.split(".", 1)[0]
            for module in imported_modules
        }

        self.assertNotIn("models", imported_roots)
        self.assertNotIn("sqlalchemy", imported_roots)
        self.assertNotIn("Crypto", imported_roots)
        self.assertFalse(
            any("result_client" in module for module in imported_modules)
        )
        self.assertNotIn("identity_subject_hmac", source)

    def test_unit_requests_make_no_external_http_calls(self):
        _, client, _, _, token_session, start_session = self.make_app()

        response = client.post("/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(token_session.post_calls, [])
        self.assertEqual(start_session.post_calls, [])
        self.assertEqual(token_session.get_calls, [])
        self.assertEqual(start_session.get_calls, [])

    def test_application_debug_is_always_false(self):
        application, _, _, _, _, _ = self.make_app()

        self.assertFalse(application.debug)
        self.assertFalse(application.config["DEBUG"])

    def test_server_binds_only_to_localhost_without_debug_or_reloader(self):
        application, _, _, _, token_session, start_session = self.make_app()
        output = OutputCapture()
        runner = ServerRunnerSpy()

        exit_code = run_toss_identity_ui_smoke_server(
            application,
            output=output,
            server_runner=runner,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            runner.calls,
            [
                {
                    "host": "127.0.0.1",
                    "port": 5055,
                    "debug": False,
                    "use_reloader": False,
                }
            ],
        )
        self.assertEqual(
            output.lines,
            [
                "Toss identity UI smoke test server:",
                "http://127.0.0.1:5055",
            ],
        )
        self.assertEqual(token_session.close_count, 1)
        self.assertEqual(start_session.close_count, 1)
        self.assertEqual(HOST, "127.0.0.1")
        self.assertEqual(PORT, 5055)

    def test_page_title_and_button_are_smoke_test_specific(self):
        _, client, _, _, _, _ = self.make_app()
        html = client.get("/").get_data(as_text=True)

        self.assertIn("Toss Identity UI Smoke Test", html)
        self.assertIn("토스로 본인확인 테스트", html)


if __name__ == "__main__":
    unittest.main()
