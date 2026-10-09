import ast
import inspect
import unittest

import requests

import scripts.toss_access_token_smoke_test as smoke_test
from scripts.toss_access_token_smoke_test import (
    TossAccessTokenSmokeTestError,
    TossAccessTokenSmokeTestErrorCode,
    _read_config,
    main,
    run_toss_access_token_smoke_test,
)
from services.toss_cert_access_token_client import (
    TOSS_CERT_TOKEN_ENDPOINT,
)


CLIENT_ID = "test_synthetic-client-id"
CLIENT_SECRET = "test_synthetic-client-secret"
ACCESS_TOKEN = "sensitive-synthetic-access-token"


class FakeResponse:

    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {
            "access_token": ACCESS_TOKEN,
            "token_type": "Bearer",
            "scope": "ca",
            "expires_in": 3600,
        }

    def json(self):
        return self._payload


class FakeHttpSession:

    def __init__(self, *, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.post_calls = []
        self.close_count = 0

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.close_count += 1


class SessionFactorySpy:

    def __init__(self, session=None):
        self.session = session or FakeHttpSession()
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        return self.session


class OutputCapture:

    def __init__(self):
        self.lines = []

    def __call__(self, value):
        self.lines.append(value)

    @property
    def text(self):
        return "\n".join(self.lines)


class TossAccessTokenSmokeTestTest(unittest.TestCase):

    def make_environ(self, **overrides):
        values = {
            "TOSS_CERT_CLIENT_ID": CLIENT_ID,
            "TOSS_CERT_CLIENT_SECRET": CLIENT_SECRET,
            "TOSS_HTTP_TIMEOUT_SECONDS": "5",
            "TOSS_TOKEN_REFRESH_SKEW_SECONDS": "30",
        }
        values.update(overrides)
        return values

    def run_smoke(self, *, environ=None, session=None):
        output = OutputCapture()
        session_factory = SessionFactorySpy(session)
        exit_code = run_toss_access_token_smoke_test(
            environ=(
                self.make_environ() if environ is None else environ
            ),
            http_session_factory=session_factory,
            output=output,
        )
        return exit_code, output, session_factory

    def test_missing_client_id_fails_before_http(self):
        environ = self.make_environ()
        del environ["TOSS_CERT_CLIENT_ID"]

        exit_code, output, session_factory = self.run_smoke(
            environ=environ
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(session_factory.call_count, 0)
        self.assertIn("MISSING_CONFIGURATION", output.text)

    def test_missing_client_secret_fails_before_http(self):
        environ = self.make_environ()
        del environ["TOSS_CERT_CLIENT_SECRET"]

        exit_code, output, session_factory = self.run_smoke(
            environ=environ
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(session_factory.call_count, 0)
        self.assertIn("MISSING_CONFIGURATION", output.text)

    def test_non_test_client_id_is_rejected_before_http(self):
        exit_code, output, session_factory = self.run_smoke(
            environ=self.make_environ(
                TOSS_CERT_CLIENT_ID="production-client-id"
            )
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(session_factory.call_count, 0)
        self.assertIn("NON_TEST_CREDENTIAL", output.text)

    def test_non_test_client_secret_is_rejected_before_http(self):
        exit_code, output, session_factory = self.run_smoke(
            environ=self.make_environ(
                TOSS_CERT_CLIENT_SECRET="production-client-secret"
            )
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(session_factory.call_count, 0)
        self.assertIn("NON_TEST_CREDENTIAL", output.text)

    def test_invalid_timeout_is_rejected_before_http(self):
        for invalid_value in ("", "abc", "0", "-1", "nan", "inf"):
            with self.subTest(invalid_value=invalid_value):
                exit_code, output, session_factory = self.run_smoke(
                    environ=self.make_environ(
                        TOSS_HTTP_TIMEOUT_SECONDS=invalid_value
                    )
                )

                self.assertNotEqual(exit_code, 0)
                self.assertEqual(session_factory.call_count, 0)
                self.assertIn(
                    (
                        "MISSING_CONFIGURATION"
                        if invalid_value == ""
                        else "INVALID_CONFIGURATION"
                    ),
                    output.text,
                )

    def test_invalid_refresh_skew_is_rejected_before_http(self):
        for invalid_value in ("", "abc", "-1", "nan", "inf"):
            with self.subTest(invalid_value=invalid_value):
                exit_code, output, session_factory = self.run_smoke(
                    environ=self.make_environ(
                        TOSS_TOKEN_REFRESH_SKEW_SECONDS=invalid_value
                    )
                )

                self.assertNotEqual(exit_code, 0)
                self.assertEqual(session_factory.call_count, 0)
                self.assertIn(
                    (
                        "MISSING_CONFIGURATION"
                        if invalid_value == ""
                        else "INVALID_CONFIGURATION"
                    ),
                    output.text,
                )

    def test_config_repr_redacts_client_id_and_secret(self):
        config = _read_config(self.make_environ())
        representation = repr(config)

        self.assertNotIn(CLIENT_ID, representation)
        self.assertNotIn(CLIENT_SECRET, representation)

    def test_success_returns_zero_and_prints_only_safe_metadata(self):
        exit_code, output, session_factory = self.run_smoke()
        session = session_factory.session

        self.assertEqual(exit_code, 0)
        self.assertIn("Toss Access Token smoke test: OK", output.text)
        self.assertIn("Token expires at:", output.text)
        self.assertIn("Token cache reuse: OK", output.text)
        self.assertNotIn(ACCESS_TOKEN, output.text)
        self.assertNotIn(CLIENT_ID, output.text)
        self.assertNotIn(CLIENT_SECRET, output.text)
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.close_count, 1)

    def test_success_uses_exact_production_client_request_contract(self):
        exit_code, _, session_factory = self.run_smoke()
        url, request = session_factory.session.post_calls[0]

        self.assertEqual(exit_code, 0)
        self.assertEqual(url, TOSS_CERT_TOKEN_ENDPOINT)
        self.assertEqual(request["timeout"], 5.0)
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["data"]["client_id"], CLIENT_ID)
        self.assertEqual(request["data"]["client_secret"], CLIENT_SECRET)

    def test_second_get_access_token_reuses_cache_without_second_post(self):
        exit_code, output, session_factory = self.run_smoke()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(session_factory.session.post_calls), 1)
        self.assertIn("Token cache reuse: OK", output.text)

    def test_second_network_attempt_is_blocked_before_transport(self):
        exit_code, output, session_factory = self.run_smoke(
            environ=self.make_environ(
                TOSS_TOKEN_REFRESH_SKEW_SECONDS="4000"
            )
        )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(len(session_factory.session.post_calls), 1)
        self.assertIn("HTTP_REQUEST_LIMIT_EXCEEDED", output.text)
        self.assertNotIn(ACCESS_TOKEN, output.text)

    def test_http_failure_returns_nonzero_with_stable_code_only(self):
        raw_body = "raw-sensitive-response"
        session = FakeHttpSession(
            response=FakeResponse(
                status_code=401,
                payload={"raw": raw_body},
            )
        )

        exit_code, output, session_factory = self.run_smoke(
            session=session
        )

        self.assertNotEqual(exit_code, 0)
        self.assertIn("Toss Access Token smoke test: FAILED", output.text)
        self.assertIn("HTTP_ERROR", output.text)
        self.assertNotIn(raw_body, output.text)
        self.assertNotIn(CLIENT_ID, output.text)
        self.assertNotIn(CLIENT_SECRET, output.text)
        self.assertEqual(len(session_factory.session.post_calls), 1)

    def test_network_exception_repr_is_not_printed(self):
        raw_error = "network-error-with-sensitive-data"
        session = FakeHttpSession(
            error=requests.ConnectionError(raw_error)
        )

        exit_code, output, _ = self.run_smoke(session=session)

        self.assertNotEqual(exit_code, 0)
        self.assertIn("NETWORK_ERROR", output.text)
        self.assertNotIn(raw_error, output.text)

    def test_error_repr_never_contains_credentials(self):
        error = TossAccessTokenSmokeTestError(
            TossAccessTokenSmokeTestErrorCode.NON_TEST_CREDENTIAL
        )

        self.assertNotIn(CLIENT_ID, repr(error))
        self.assertNotIn(CLIENT_SECRET, repr(error))

    def test_main_can_run_without_loading_dotenv_in_unit_test(self):
        output = OutputCapture()
        session_factory = SessionFactorySpy()

        exit_code = main(
            environ=self.make_environ(),
            http_session_factory=session_factory,
            output=output,
            load_environment=False,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(session_factory.session.post_calls), 1)

    def test_script_does_not_require_identity_or_database_configuration(self):
        exit_code, _, _ = self.run_smoke(environ=self.make_environ())

        self.assertEqual(exit_code, 0)

    def test_script_has_no_flask_model_or_database_dependency(self):
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

    def test_unit_tests_use_fake_transport_not_external_http(self):
        session = FakeHttpSession()
        exit_code, _, session_factory = self.run_smoke(session=session)

        self.assertEqual(exit_code, 0)
        self.assertIs(session_factory.session, session)
        self.assertEqual(len(session.post_calls), 1)

    def test_token_metadata_uses_timezone_aware_expiry(self):
        response = FakeResponse(
            payload={
                "access_token": ACCESS_TOKEN,
                "token_type": "Bearer",
                "scope": "ca",
                "expires_in": 3600,
            }
        )
        session = FakeHttpSession(response=response)

        exit_code, output, _ = self.run_smoke(session=session)

        self.assertEqual(exit_code, 0)
        expiry_line = next(
            line
            for line in output.lines
            if line.startswith("Token expires at:")
        )
        self.assertIn("+00:00", expiry_line)


if __name__ == "__main__":
    unittest.main()
