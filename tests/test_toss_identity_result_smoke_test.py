import ast
from datetime import date, timedelta
import inspect
from threading import Event, Thread
import unittest

import scripts.toss_identity_result_smoke_test as result_smoke
from scripts.toss_identity_result_smoke_test import (
    HOST,
    PORT,
    SDK_URL,
    SmokeTransactionState,
    TossIdentityResultSmokeTestConfig,
    TossIdentityResultSmokeTestError,
    TossIdentityResultSmokeTestErrorCode,
    build_toss_identity_result_smoke_app_from_environ,
    create_toss_identity_result_smoke_app,
    main,
    run_toss_identity_result_smoke_server,
)
from services.age_eligibility_service import AgeEligibility
from services.toss_identity_verification_result_client import (
    TossIdentityVerificationResultError,
    TossIdentityVerificationResultErrorCode,
)


CLIENT_ID = "test_synthetic-client-id"
CLIENT_SECRET = "test_synthetic-client-secret"
REQUEST_URL = "https://cert.toss.im"
RSA_PUBLIC_KEY = "synthetic-rsa-public-key-not-used-by-stub"
TRANSACTION_ID = "sensitive-provider-transaction-id"
OTHER_TRANSACTION_ID = "different-sensitive-provider-transaction-id"
AUTHENTICATION_URL = "https://auth.example.test/sensitive-handoff"
SMOKE_SESSION_ID = "opaque-smoke-session-id-1234567890"
BIRTH_DATE = date(2000, 1, 2)
IDENTITY_SUBJECT = "sensitive-decrypted-di"
SIGNATURE = "sensitive-signature"
SESSION_KEY = "sensitive-session-key"
RAW_RESPONSE = "sensitive-raw-provider-response"


class StubStartResult:
    provider_transaction_id = TRANSACTION_ID
    authentication_url = AUTHENTICATION_URL


class StubStartClient:

    def __init__(self, *, result=None, error=None):
        self.result = result or StubStartResult()
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


class StubVerifiedIdentity:

    def __init__(self, *, provider_transaction_id=TRANSACTION_ID):
        self.provider_transaction_id = provider_transaction_id
        self.birth_date = BIRTH_DATE
        self.identity_subject = IDENTITY_SUBJECT
        self.signature = SIGNATURE


class StubResultClient:

    def __init__(self, *, result=None, error=None, entered=None, release=None):
        self.result = result or StubVerifiedIdentity()
        self.error = error
        self.entered = entered
        self.release = release
        self.call_count = 0
        self.transaction_ids = []

    def get_verified_identity(self, provider_transaction_id):
        self.call_count += 1
        self.transaction_ids.append(provider_transaction_id)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return self.result


class StubResultClientFactory:

    def __init__(self, client=None):
        self.client = client or StubResultClient()
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.client


class StubAccessTokenClient:
    pass


class StubAccessTokenClientFactory:

    def __init__(self):
        self.client = StubAccessTokenClient()
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.client


class StubCryptoSessionGenerator:

    def generate(self):
        raise AssertionError("Stub result client must own result behavior.")


class StubCryptoSessionGeneratorFactory:

    def __init__(self):
        self.generator = StubCryptoSessionGenerator()
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.generator


class FakeHttpSession:

    def __init__(self):
        self.post_calls = []
        self.get_calls = []
        self.close_count = 0

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        raise AssertionError("Unit tests must not make external HTTP calls.")

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("Unit tests must not open the authentication URL.")

    def close(self):
        self.close_count += 1


class SessionFactory:

    def __init__(self):
        self.sessions = []

    @property
    def call_count(self):
        return len(self.sessions)

    def __call__(self):
        session = FakeHttpSession()
        self.sessions.append(session)
        return session


class OutputCapture:

    def __init__(self):
        self.lines = []

    def __call__(self, value):
        self.lines.append(value)

    @property
    def text(self):
        return "\n".join(self.lines)


class ServerRunnerSpy:

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class AgeCheckerSpy:

    def __init__(self, result=AgeEligibility.AGE_14_OR_OVER):
        self.result = result
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class TossIdentityResultSmokeTestTest(unittest.TestCase):

    def make_config(self, **overrides):
        values = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "request_url": REQUEST_URL,
            "rsa_public_key_base64": RSA_PUBLIC_KEY,
            "timeout": 5.0,
            "refresh_skew": timedelta(seconds=30),
        }
        values.update(overrides)
        return TossIdentityResultSmokeTestConfig(**values)

    def make_environ(self, **overrides):
        values = {
            "TOSS_CERT_CLIENT_ID": CLIENT_ID,
            "TOSS_CERT_CLIENT_SECRET": CLIENT_SECRET,
            "TOSS_HTTP_TIMEOUT_SECONDS": "5",
            "TOSS_TOKEN_REFRESH_SKEW_SECONDS": "30",
            "TOSS_IDENTITY_REQUEST_URL": REQUEST_URL,
            "TOSS_CERT_RSA_PUBLIC_KEY_BASE64": RSA_PUBLIC_KEY,
        }
        values.update(overrides)
        return values

    def make_app(
        self,
        *,
        start_client=None,
        result_client=None,
        age_checker=None,
        session_id_factory=lambda: SMOKE_SESSION_ID,
    ):
        session_factory = SessionFactory()
        access_token_factory = StubAccessTokenClientFactory()
        start_factory = StubStartClientFactory(start_client)
        crypto_factory = StubCryptoSessionGeneratorFactory()
        result_factory = StubResultClientFactory(result_client)
        age_checker = age_checker or AgeCheckerSpy()
        application = create_toss_identity_result_smoke_app(
            self.make_config(),
            http_session_factory=session_factory,
            access_token_client_factory=access_token_factory,
            start_client_factory=start_factory,
            crypto_session_generator_factory=crypto_factory,
            result_client_factory=result_factory,
            age_eligibility_checker=age_checker,
            reference_date_factory=lambda: date(2026, 10, 9),
            smoke_session_id_factory=session_id_factory,
            testing=True,
        )
        return {
            "app": application,
            "client": application.test_client(),
            "sessions": session_factory,
            "access_factory": access_token_factory,
            "start_factory": start_factory,
            "crypto_factory": crypto_factory,
            "result_factory": result_factory,
            "age_checker": age_checker,
        }

    def start(self, context):
        response = context["client"].post("/start")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def complete(self, context, smoke_session_id=SMOKE_SESSION_ID, **extra):
        payload = {"smoke_session_id": smoke_session_id}
        payload.update(extra)
        return context["client"].post("/complete", json=payload)

    def test_root_page_returns_200_and_loads_official_toss_sdk(self):
        context = self.make_app()

        response = context["client"].get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'<script src="{SDK_URL}"></script>', html)
        self.assertEqual(html.count(SDK_URL), 1)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_browser_uses_on_success_to_call_complete_with_smoke_id_only(self):
        context = self.make_app()
        html = context["client"].get("/").get_data(as_text=True)

        self.assertIn("onSuccess: () => completeVerification(", html)
        self.assertIn('fetch("/complete"', html)
        self.assertIn(
            "body: JSON.stringify({ smoke_session_id: smokeSessionId })",
            html,
        )
        self.assertNotIn("localStorage", html)
        self.assertNotIn("sessionStorage", html)
        self.assertNotIn("document.cookie", html)
        self.assertNotIn("console.", html)

    def test_start_button_click_path_cannot_submit_or_reload_root(self):
        context = self.make_app()
        html = context["client"].get("/").get_data(as_text=True)

        self.assertIn('<button id="start-button" type="button">', html)
        self.assertIn('document.getElementById("start-button")', html)
        self.assertIn('button.addEventListener("click", (event) => {', html)
        self.assertNotIn("<form", html.lower())
        self.assertNotIn('action="/"', html.lower())

        handler_index = html.index(
            'button.addEventListener("click", (event) => {'
        )
        prevent_default_index = html.index("event.preventDefault();", handler_index)
        prepare_popup_index = html.index("tossCert.preparePopup();", handler_index)
        start_request_index = html.index('fetch("/start", {', handler_index)
        toss_start_index = html.index("tossCert.start({", handler_index)

        self.assertLess(handler_index, prevent_default_index)
        self.assertLess(prevent_default_index, prepare_popup_index)
        self.assertLess(prepare_popup_index, start_request_index)
        self.assertLess(start_request_index, toss_start_index)
        self.assertRegex(
            html[start_request_index:toss_start_index],
            r'fetch\("/start",\s*\{\s*method: "POST"',
        )

    def test_generated_javascript_keeps_check_separator_escaped(self):
        context = self.make_app()
        html = context["client"].get("/").get_data(as_text=True)

        self.assertIn('checks.join("\\n")', html)
        self.assertNotIn('checks.join("\n")', html)

    def test_start_creates_opaque_session_and_saves_txid_server_side(self):
        context = self.make_app()

        payload = self.start(context)
        snapshot = context["app"].extensions[
            "toss_result_smoke_transaction_store"
        ].snapshot(payload["smoke_session_id"])

        self.assertEqual(payload["smoke_session_id"], SMOKE_SESSION_ID)
        self.assertEqual(snapshot[0], SmokeTransactionState.PENDING)
        self.assertEqual(snapshot[1], TRANSACTION_ID)
        self.assertIsNone(snapshot[2])

    def test_complete_accepts_no_client_transaction_id(self):
        context = self.make_app()
        self.start(context)

        response = self.complete(context, txId=TRANSACTION_ID)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(context["result_factory"].client.call_count, 0)

    def test_unknown_smoke_session_is_rejected_without_result_query(self):
        context = self.make_app()

        response = self.complete(
            context,
            smoke_session_id="unknown-smoke-session-id-123456789",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(context["result_factory"].client.call_count, 0)

    def test_result_client_receives_only_server_saved_transaction_id_once(self):
        context = self.make_app()
        self.start(context)

        response = self.complete(context)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(context["result_factory"].client.call_count, 1)
        self.assertEqual(
            context["result_factory"].client.transaction_ids,
            [TRANSACTION_ID],
        )

    def test_duplicate_complete_does_not_repeat_result_query(self):
        context = self.make_app()
        self.start(context)

        first = self.complete(context)
        second = self.complete(context)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(context["result_factory"].client.call_count, 1)

    def test_concurrent_complete_does_not_repeat_result_query(self):
        entered = Event()
        release = Event()
        result_client = StubResultClient(entered=entered, release=release)
        context = self.make_app(result_client=result_client)
        self.start(context)
        first_response = []

        def run_first_completion():
            with context["app"].test_client() as client:
                first_response.append(
                    client.post(
                        "/complete",
                        json={"smoke_session_id": SMOKE_SESSION_ID},
                    )
                )

        thread = Thread(target=run_first_completion)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))

        second = self.complete(context)
        release.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(second.status_code, 409)
        self.assertEqual(first_response[0].status_code, 200)
        self.assertEqual(result_client.call_count, 1)

    def test_result_transaction_mismatch_fails_closed(self):
        result_client = StubResultClient(
            result=StubVerifiedIdentity(
                provider_transaction_id=OTHER_TRANSACTION_ID
            )
        )
        context = self.make_app(result_client=result_client)
        self.start(context)

        response = self.complete(context)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_RESULT")
        snapshot = context["app"].extensions[
            "toss_result_smoke_transaction_store"
        ].snapshot(SMOKE_SESSION_ID)
        self.assertEqual(snapshot[0], SmokeTransactionState.FAILED)

    def test_success_calls_age_service_with_decrypted_birth_date(self):
        age_checker = AgeCheckerSpy()
        context = self.make_app(age_checker=age_checker)
        self.start(context)

        response = self.complete(context)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            age_checker.calls,
            [{
                "birth_date": BIRTH_DATE,
                "reference_date": date(2026, 10, 9),
            }],
        )

    def test_success_displays_only_safe_check_statuses(self):
        context = self.make_app()
        self.start(context)

        response = self.complete(context)

        self.assertEqual(
            response.get_json(),
            {
                "status": "completed",
                "checks": [
                    "Toss result query: OK",
                    "Birth date decryption: OK",
                    "Age eligibility: AGE_14_OR_OVER",
                ],
            },
        )

    def test_under_14_is_reported_only_after_successful_birth_date_check(self):
        context = self.make_app(
            age_checker=AgeCheckerSpy(AgeEligibility.UNDER_14)
        )
        self.start(context)

        response = self.complete(context)

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Age eligibility: UNDER_14",
            response.get_json()["checks"],
        )

    def test_pending_is_terminal_and_never_retried(self):
        error = TossIdentityVerificationResultError(
            TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING
        )
        context = self.make_app(result_client=StubResultClient(error=error))
        self.start(context)

        first = self.complete(context)
        second = self.complete(context)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(
            first.get_json()["message"],
            "결과가 아직 준비되지 않았습니다. 새 인증으로 다시 테스트해 주세요.",
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(context["result_factory"].client.call_count, 1)

    def test_age_restricted_is_not_treated_as_under_14_success(self):
        error = TossIdentityVerificationResultError(
            TossIdentityVerificationResultErrorCode.AGE_RESTRICTED
        )
        context = self.make_app(result_client=StubResultClient(error=error))
        self.start(context)

        response = self.complete(context)
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "AGE_RESTRICTED")
        self.assertNotIn("UNDER_14", body)
        self.assertEqual(context["age_checker"].calls, [])

    def test_provider_failures_use_general_message_without_raw_error(self):
        for error_code in (
            TossIdentityVerificationResultErrorCode.NETWORK_ERROR,
            TossIdentityVerificationResultErrorCode.HTTP_ERROR,
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
            TossIdentityVerificationResultErrorCode.CRYPTO_ERROR,
        ):
            with self.subTest(error_code=error_code.value):
                error = TossIdentityVerificationResultError(error_code)
                context = self.make_app(
                    result_client=StubResultClient(error=error)
                )
                self.start(context)

                response = self.complete(context)
                body = response.get_data(as_text=True)

                self.assertEqual(response.status_code, 502)
                self.assertIn("RESULT_CHECK_FAILED", body)
                self.assertNotIn(RAW_RESPONSE, body)

    def test_sensitive_result_values_are_absent_from_complete_response(self):
        context = self.make_app()
        self.start(context)

        body = self.complete(context).get_data(as_text=True)

        for sensitive in (
            CLIENT_ID,
            CLIENT_SECRET,
            RSA_PUBLIC_KEY,
            TRANSACTION_ID,
            AUTHENTICATION_URL,
            BIRTH_DATE.isoformat(),
            IDENTITY_SUBJECT,
            SIGNATURE,
            SESSION_KEY,
            RAW_RESPONSE,
        ):
            self.assertNotIn(sensitive, body)

    def test_sensitive_values_are_absent_from_html_and_server_output(self):
        context = self.make_app()
        html = context["client"].get("/").get_data(as_text=True)
        output = OutputCapture()
        runner = ServerRunnerSpy()

        exit_code = run_toss_identity_result_smoke_server(
            context["app"],
            output=output,
            server_runner=runner,
        )

        self.assertEqual(exit_code, 0)
        for sensitive in (
            CLIENT_ID,
            CLIENT_SECRET,
            RSA_PUBLIC_KEY,
            TRANSACTION_ID,
            AUTHENTICATION_URL,
            BIRTH_DATE.isoformat(),
            IDENTITY_SUBJECT,
            SIGNATURE,
            SESSION_KEY,
        ):
            self.assertNotIn(sensitive, html)
            self.assertNotIn(sensitive, output.text)

    def test_responses_set_no_store_and_no_cookie(self):
        context = self.make_app()
        responses = [
            context["client"].get("/"),
            context["client"].post("/start"),
            self.complete(context),
        ]

        for response in responses:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Pragma"], "no-cache")
            self.assertNotIn("Set-Cookie", response.headers)

    def test_required_configuration_fails_before_http_client_creation(self):
        for name in (
            "TOSS_CERT_CLIENT_ID",
            "TOSS_CERT_CLIENT_SECRET",
            "TOSS_IDENTITY_REQUEST_URL",
            "TOSS_CERT_RSA_PUBLIC_KEY_BASE64",
        ):
            for mode in ("missing", "blank"):
                with self.subTest(name=name, mode=mode):
                    environ = self.make_environ()
                    if mode == "missing":
                        del environ[name]
                    else:
                        environ[name] = "   "
                    session_factory = SessionFactory()

                    with self.assertRaises(TossIdentityResultSmokeTestError):
                        build_toss_identity_result_smoke_app_from_environ(
                            environ,
                            http_session_factory=session_factory,
                        )

                    self.assertEqual(session_factory.call_count, 0)

    def test_non_test_credentials_fail_before_http_client_creation(self):
        for name in ("TOSS_CERT_CLIENT_ID", "TOSS_CERT_CLIENT_SECRET"):
            with self.subTest(name=name):
                environ = self.make_environ()
                environ[name] = "production-credential"
                session_factory = SessionFactory()

                with self.assertRaises(TossIdentityResultSmokeTestError) as context:
                    build_toss_identity_result_smoke_app_from_environ(
                        environ,
                        http_session_factory=session_factory,
                    )

                self.assertIs(
                    context.exception.code,
                    TossIdentityResultSmokeTestErrorCode.NON_TEST_CREDENTIAL,
                )
                self.assertEqual(session_factory.call_count, 0)

    def test_invalid_rsa_key_fails_before_http_client_creation(self):
        session_factory = SessionFactory()

        with self.assertRaises(TossIdentityResultSmokeTestError) as context:
            build_toss_identity_result_smoke_app_from_environ(
                self.make_environ(
                    TOSS_CERT_RSA_PUBLIC_KEY_BASE64="not-valid-base64"
                ),
                http_session_factory=session_factory,
            )

        self.assertIs(
            context.exception.code,
            TossIdentityResultSmokeTestErrorCode.INVALID_RSA_PUBLIC_KEY,
        )
        self.assertEqual(session_factory.call_count, 0)

    def test_existing_production_components_are_wired_with_exact_config(self):
        context = self.make_app()

        self.assertEqual(
            context["crypto_factory"].calls,
            [{"base64_public_key": RSA_PUBLIC_KEY}],
        )
        self.assertIs(
            context["result_factory"].calls[0]["crypto_session_generator"],
            context["crypto_factory"].generator,
        )
        self.assertIs(
            context["result_factory"].calls[0]["access_token_client"],
            context["access_factory"].client,
        )
        self.assertIs(
            context["start_factory"].calls[0]["access_token_client"],
            context["access_factory"].client,
        )

    def test_config_repr_hides_credentials_url_and_rsa_key(self):
        representation = repr(self.make_config())

        for sensitive in (
            CLIENT_ID,
            CLIENT_SECRET,
            REQUEST_URL,
            RSA_PUBLIC_KEY,
        ):
            self.assertNotIn(sensitive, representation)

    def test_server_runs_on_localhost_without_debug_or_reloader(self):
        context = self.make_app()
        runner = ServerRunnerSpy()

        exit_code = run_toss_identity_result_smoke_server(
            context["app"],
            output=OutputCapture(),
            server_runner=runner,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            runner.calls,
            [{
                "host": HOST,
                "port": PORT,
                "debug": False,
                "use_reloader": False,
            }],
        )
        self.assertEqual(HOST, "127.0.0.1")
        self.assertEqual(PORT, 5056)
        self.assertFalse(context["app"].debug)

    def test_server_closes_all_http_sessions(self):
        context = self.make_app()

        run_toss_identity_result_smoke_server(
            context["app"],
            output=OutputCapture(),
            server_runner=ServerRunnerSpy(),
        )

        self.assertEqual(
            [session.close_count for session in context["sessions"].sessions],
            [1, 1, 1],
        )

    def test_main_can_run_without_loading_dotenv(self):
        output = OutputCapture()
        runner = ServerRunnerSpy()
        factories = {
            "http_session_factory": SessionFactory(),
            "access_token_client_factory": StubAccessTokenClientFactory(),
            "start_client_factory": StubStartClientFactory(),
            "crypto_session_generator_factory": (
                StubCryptoSessionGeneratorFactory()
            ),
            "result_client_factory": StubResultClientFactory(),
            "smoke_session_id_factory": lambda: SMOKE_SESSION_ID,
            "testing": True,
        }

        original_builder = result_smoke.build_toss_identity_result_smoke_app_from_environ
        result_smoke.build_toss_identity_result_smoke_app_from_environ = (
            lambda environ: create_toss_identity_result_smoke_app(
                self.make_config(),
                **factories,
            )
        )
        try:
            exit_code = main(
                environ=self.make_environ(),
                output=output,
                load_environment=False,
                server_runner=runner,
            )
        finally:
            result_smoke.build_toss_identity_result_smoke_app_from_environ = (
                original_builder
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(runner.calls), 1)

    def test_root_dotenv_is_loaded_without_override(self):
        source = inspect.getsource(result_smoke)

        self.assertIn('PROJECT_ROOT / ".env"', source)
        self.assertIn("override=False", source)

    def test_script_has_no_database_model_app_or_hmac_dependency(self):
        source = inspect.getsource(result_smoke)
        syntax_tree = ast.parse(source)
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
        )

        self.assertTrue(
            {"app", "models", "sqlalchemy", "flask_sqlalchemy", "hmac"}
            .isdisjoint(imported_roots)
        )
        self.assertNotIn("IDENTITY_SUBJECT_HMAC_KEY", source)
        self.assertNotIn("medicine.db", source)

    def test_unit_flow_makes_zero_http_calls(self):
        context = self.make_app()

        self.start(context)
        self.complete(context)

        self.assertEqual(context["sessions"].call_count, 3)
        for session in context["sessions"].sessions:
            self.assertEqual(session.post_calls, [])
            self.assertEqual(session.get_calls, [])


if __name__ == "__main__":
    unittest.main()
