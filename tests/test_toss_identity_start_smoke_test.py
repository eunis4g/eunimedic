import ast
import inspect
import unittest

import requests

import scripts.toss_identity_start_smoke_test as smoke_test
from scripts.toss_identity_start_smoke_test import (
    TossIdentityStartSmokeTestError,
    TossIdentityStartSmokeTestErrorCode,
    _read_config,
    main,
    run_toss_identity_start_smoke_test,
)
from services.toss_cert_access_token_client import (
    TOSS_CERT_TOKEN_ENDPOINT,
    TossCertAccessTokenClient,
)
from services.toss_identity_verification_start_client import (
    TOSS_IDENTITY_VERIFICATION_START_ENDPOINT,
    TossIdentityVerificationStartClient,
)


CLIENT_ID = "test_synthetic-client-id"
CLIENT_SECRET = "test_synthetic-client-secret"
ACCESS_TOKEN = "sensitive-synthetic-access-token"
REQUEST_URL = "https://cert.toss.im"
TRANSACTION_ID = "sensitive-provider-transaction-id"
AUTHENTICATION_URL = "https://auth.example.test/sensitive-handoff"
RAW_PROVIDER_BODY = "raw-sensitive-provider-response"


class FakeResponse:

    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = RAW_PROVIDER_BODY

    def json(self):
        return self._payload


def token_response(*, status_code=200):
    return FakeResponse(
        status_code=status_code,
        payload={
            "access_token": ACCESS_TOKEN,
            "token_type": "Bearer",
            "scope": "ca",
            "expires_in": 3600,
        },
    )


def successful_start_response():
    return FakeResponse(
        payload={
            "resultType": "SUCCESS",
            "success": {
                "txId": TRANSACTION_ID,
                "authUrl": AUTHENTICATION_URL,
                "requestedDt": "2026-10-09T12:00:00+09:00",
            },
        }
    )


class FakeHttpSession:

    def __init__(self, response, *, error=None):
        self.response = response
        self.error = error
        self.post_calls = []
        self.get_calls = []
        self.close_count = 0

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        raise AssertionError("The authentication URL must not be fetched.")

    def close(self):
        self.close_count += 1


class QueuedSessionFactory:

    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        if not self.sessions:
            raise AssertionError("Unexpected HTTP session creation.")
        return self.sessions.pop(0)


class OutputCapture:

    def __init__(self):
        self.lines = []

    def __call__(self, value):
        self.lines.append(value)

    @property
    def text(self):
        return "\n".join(self.lines)


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


class CountingStartClient:

    def __init__(self, delegate):
        self.delegate = delegate
        self.call_count = 0

    def start_verification(self):
        self.call_count += 1
        return self.delegate.start_verification()


class CountingStartClientFactory:

    def __init__(self):
        self.instance = None

    def __call__(self, **kwargs):
        self.instance = CountingStartClient(
            TossIdentityVerificationStartClient(**kwargs)
        )
        return self.instance


class TossIdentityStartSmokeTestTest(unittest.TestCase):

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

    def make_sessions(self, *, token=None, start=None):
        return (
            token or FakeHttpSession(token_response()),
            start or FakeHttpSession(successful_start_response()),
        )

    def run_smoke(
        self,
        *,
        environ=None,
        token_session=None,
        start_session=None,
        access_token_client_factory=TossCertAccessTokenClient,
        start_client_factory=TossIdentityVerificationStartClient,
    ):
        token_session, start_session = self.make_sessions(
            token=token_session,
            start=start_session,
        )
        session_factory = QueuedSessionFactory(
            token_session,
            start_session,
        )
        output = OutputCapture()
        exit_code = run_toss_identity_start_smoke_test(
            environ=(
                self.make_environ() if environ is None else environ
            ),
            http_session_factory=session_factory,
            access_token_client_factory=access_token_client_factory,
            start_client_factory=start_client_factory,
            output=output,
        )
        return (
            exit_code,
            output,
            session_factory,
            token_session,
            start_session,
        )

    def assert_config_failure_without_http(self, environ, expected_code):
        result = self.run_smoke(environ=environ)
        exit_code, output, session_factory = result[:3]

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(session_factory.call_count, 0)
        self.assertIn(expected_code, output.text)

    def test_missing_client_id_is_rejected(self):
        environ = self.make_environ()
        del environ["TOSS_CERT_CLIENT_ID"]
        self.assert_config_failure_without_http(
            environ,
            "MISSING_CONFIGURATION",
        )

    def test_missing_client_secret_is_rejected(self):
        environ = self.make_environ()
        del environ["TOSS_CERT_CLIENT_SECRET"]
        self.assert_config_failure_without_http(
            environ,
            "MISSING_CONFIGURATION",
        )

    def test_non_test_client_id_is_rejected(self):
        self.assert_config_failure_without_http(
            self.make_environ(
                TOSS_CERT_CLIENT_ID="production-client-id"
            ),
            "NON_TEST_CREDENTIAL",
        )

    def test_non_test_client_secret_is_rejected(self):
        self.assert_config_failure_without_http(
            self.make_environ(
                TOSS_CERT_CLIENT_SECRET="production-client-secret"
            ),
            "NON_TEST_CREDENTIAL",
        )

    def test_missing_request_url_is_rejected(self):
        environ = self.make_environ()
        del environ["TOSS_IDENTITY_REQUEST_URL"]
        self.assert_config_failure_without_http(
            environ,
            "MISSING_CONFIGURATION",
        )

    def test_invalid_timeout_is_rejected(self):
        for value in ("", "abc", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                self.assert_config_failure_without_http(
                    self.make_environ(TOSS_HTTP_TIMEOUT_SECONDS=value),
                    (
                        "MISSING_CONFIGURATION"
                        if value == ""
                        else "INVALID_CONFIGURATION"
                    ),
                )

    def test_invalid_refresh_skew_is_rejected(self):
        for value in ("", "abc", "-1", "nan", "inf"):
            with self.subTest(value=value):
                self.assert_config_failure_without_http(
                    self.make_environ(
                        TOSS_TOKEN_REFRESH_SKEW_SECONDS=value
                    ),
                    (
                        "MISSING_CONFIGURATION"
                        if value == ""
                        else "INVALID_CONFIGURATION"
                    ),
                )

    def test_production_access_token_client_is_created(self):
        factory = FactorySpy(TossCertAccessTokenClient)

        result = self.run_smoke(access_token_client_factory=factory)

        self.assertEqual(result[0], 0)
        self.assertEqual(len(factory.instances), 1)
        self.assertIsInstance(
            factory.instances[0],
            TossCertAccessTokenClient,
        )

    def test_production_start_client_is_created(self):
        factory = FactorySpy(TossIdentityVerificationStartClient)

        result = self.run_smoke(start_client_factory=factory)

        self.assertEqual(result[0], 0)
        self.assertEqual(len(factory.instances), 1)
        self.assertIsInstance(
            factory.instances[0],
            TossIdentityVerificationStartClient,
        )

    def test_start_verification_is_called_exactly_once(self):
        factory = CountingStartClientFactory()

        result = self.run_smoke(start_client_factory=factory)

        self.assertEqual(result[0], 0)
        self.assertEqual(factory.instance.call_count, 1)

    def test_success_returns_zero_and_safe_status_only(self):
        exit_code, output, _, token_session, start_session = (
            self.run_smoke()
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.lines,
            [
                "Toss identity start smoke test: OK",
                "Transaction ID received: OK",
                "Authentication URL received: OK",
            ],
        )
        for sensitive_value in (
            ACCESS_TOKEN,
            CLIENT_ID,
            CLIENT_SECRET,
            TRANSACTION_ID,
            AUTHENTICATION_URL,
            REQUEST_URL,
        ):
            self.assertNotIn(sensitive_value, output.text)
        self.assertEqual(len(token_session.post_calls), 1)
        self.assertEqual(len(start_session.post_calls), 1)

    def test_requests_use_exact_production_endpoints_once(self):
        result = self.run_smoke()
        token_session, start_session = result[3:5]

        self.assertEqual(result[0], 0)
        self.assertEqual(
            [call[0] for call in token_session.post_calls],
            [TOSS_CERT_TOKEN_ENDPOINT],
        )
        self.assertEqual(
            [call[0] for call in start_session.post_calls],
            [TOSS_IDENTITY_VERIFICATION_START_ENDPOINT],
        )

    def test_start_request_uses_user_none_and_configured_request_url(self):
        result = self.run_smoke()
        _, request = result[4].post_calls[0]

        self.assertEqual(result[0], 0)
        self.assertEqual(request["json"]["requestType"], "USER_NONE")
        self.assertEqual(request["json"]["requestUrl"], REQUEST_URL)
        self.assertFalse(request["allow_redirects"])

    def test_authentication_url_is_never_fetched_or_opened(self):
        result = self.run_smoke()
        token_session, start_session = result[3:5]

        self.assertEqual(result[0], 0)
        self.assertEqual(token_session.get_calls, [])
        self.assertEqual(start_session.get_calls, [])

    def test_provider_rejection_returns_nonzero_without_raw_response(self):
        start_session = FakeHttpSession(
            FakeResponse(
                payload={
                    "resultType": "FAIL",
                    "error": {
                        "errorCode": "CE3100",
                        "reason": RAW_PROVIDER_BODY,
                    },
                }
            )
        )

        exit_code, output, _, _, _ = self.run_smoke(
            start_session=start_session
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("REQUEST_REJECTED", output.text)
        self.assertNotIn(RAW_PROVIDER_BODY, output.text)
        self.assertNotIn(TRANSACTION_ID, output.text)
        self.assertNotIn(AUTHENTICATION_URL, output.text)

    def test_token_failure_is_reported_as_stable_start_error(self):
        token_session = FakeHttpSession(token_response(status_code=401))

        exit_code, output, _, _, start_session = self.run_smoke(
            token_session=token_session
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("TOKEN_ERROR", output.text)
        self.assertEqual(start_session.post_calls, [])
        self.assertNotIn(ACCESS_TOKEN, output.text)

    def test_network_exception_repr_is_not_printed(self):
        raw_error = "network-error-with-sensitive-data"
        start_session = FakeHttpSession(
            successful_start_response(),
            error=requests.ConnectionError(raw_error),
        )

        exit_code, output, _, _, _ = self.run_smoke(
            start_session=start_session
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("NETWORK_ERROR", output.text)
        self.assertNotIn(raw_error, output.text)

    def test_config_and_error_repr_redact_sensitive_values(self):
        config = _read_config(self.make_environ())
        error = TossIdentityStartSmokeTestError(
            TossIdentityStartSmokeTestErrorCode.NON_TEST_CREDENTIAL
        )

        for representation in (repr(config), repr(error)):
            self.assertNotIn(CLIENT_ID, representation)
            self.assertNotIn(CLIENT_SECRET, representation)
            self.assertNotIn(REQUEST_URL, representation)

    def test_main_can_run_without_loading_dotenv_in_unit_test(self):
        token_session, start_session = self.make_sessions()
        session_factory = QueuedSessionFactory(
            token_session,
            start_session,
        )
        output = OutputCapture()

        exit_code = main(
            environ=self.make_environ(),
            http_session_factory=session_factory,
            output=output,
            load_environment=False,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(session_factory.call_count, 2)

    def test_rsa_hmac_crypto_and_database_configuration_are_not_required(self):
        exit_code = self.run_smoke(environ=self.make_environ())[0]

        self.assertEqual(exit_code, 0)

    def test_script_has_no_flask_model_database_or_crypto_dependency(self):
        source = inspect.getsource(smoke_test)
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

        self.assertNotIn("flask", imported_roots)
        self.assertNotIn("models", imported_roots)
        self.assertNotIn("sqlalchemy", imported_roots)
        self.assertNotIn("app", imported_roots)
        self.assertNotIn("Crypto", imported_roots)

    def test_unit_tests_use_fake_transport_not_external_http(self):
        result = self.run_smoke()
        token_session, start_session = result[3:5]

        self.assertEqual(result[0], 0)
        self.assertIsInstance(token_session, FakeHttpSession)
        self.assertIsInstance(start_session, FakeHttpSession)


if __name__ == "__main__":
    unittest.main()
