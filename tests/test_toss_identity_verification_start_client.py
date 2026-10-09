import ast
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
import inspect
import math
import unittest
from unittest.mock import patch

import requests

import services.toss_identity_verification_start_client as start_service
from services.identity_verification_provider import IdentityVerificationProvider
from services.toss_cert_access_token_client import (
    TossCertAccessToken,
    TossCertAccessTokenError,
    TossCertAccessTokenErrorCode,
)
from services.toss_identity_verification_start_client import (
    TOSS_IDENTITY_VERIFICATION_START_ENDPOINT,
    TossIdentityVerificationStartClient,
    TossIdentityVerificationStartError,
    TossIdentityVerificationStartErrorCode,
    TossIdentityVerificationStartResult,
)


ACCESS_TOKEN = "synthetic-access-token-sensitive"
TRANSACTION_ID = "synthetic-provider-transaction-sensitive"
AUTHENTICATION_URL = "https://auth.example.test/sensitive-handoff"
REQUEST_URL = "https://example.test/toss/return"
RAW_RESPONSE_BODY = "raw-provider-body-sensitive"


class StubAccessTokenClient:

    def __init__(self, *, token_result=None, error=None):
        self.token_result = token_result
        self.error = error
        self.call_count = 0

    def get_access_token(self):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.token_result


class FakeResponse:

    def __init__(self, *, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = RAW_RESPONSE_BODY

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class RecordingSession:

    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        raise AssertionError("authentication_url must not be fetched")


def valid_payload():
    return {
        "resultType": "SUCCESS",
        "success": {
            "txId": TRANSACTION_ID,
            "authUrl": AUTHENTICATION_URL,
            "requestedDt": "2026-10-09T12:00:00+09:00",
        },
    }


def synthetic_token_result():
    return TossCertAccessToken(
        access_token=ACCESS_TOKEN,
        expires_at=datetime(2026, 10, 9, 13, 0, tzinfo=UTC),
    )


class TossIdentityVerificationStartClientTest(unittest.TestCase):

    def test_rejects_empty_request_url(self):
        with self.assertRaises(ValueError):
            self.make_client(request_url="")

    def test_rejects_whitespace_only_request_url(self):
        with self.assertRaises(ValueError):
            self.make_client(request_url="   ")

    def test_rejects_zero_timeout(self):
        with self.assertRaises(ValueError):
            self.make_client(timeout=0)

    def test_rejects_negative_timeout(self):
        with self.assertRaises(ValueError):
            self.make_client(timeout=-0.1)

    def test_rejects_boolean_timeout(self):
        with self.assertRaises(ValueError):
            self.make_client(timeout=True)

    def test_rejects_nan_and_infinite_timeout(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_client(timeout=value)

    def test_requires_access_token_client_contract(self):
        for value in (None, object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    self.make_client(access_token_client=value)

    def test_requires_http_post_contract(self):
        with self.assertRaises(TypeError):
            self.make_client(http_session=object())

    def test_posts_to_exact_identity_verification_endpoint(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertEqual(
            session.post_calls[0][0],
            TOSS_IDENTITY_VERIFICATION_START_ENDPOINT,
        )
        self.assertEqual(
            TOSS_IDENTITY_VERIFICATION_START_ENDPOINT,
            "https://cert.toss.im/api/v2/sign/user/auth/id/request",
        )

    def test_uses_post_only(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.get_calls, [])

    def test_sends_bearer_authorization_header(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertEqual(
            session.post_calls[0][1]["headers"]["Authorization"],
            f"Bearer {ACCESS_TOKEN}",
        )

    def test_sends_json_content_type(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertEqual(
            session.post_calls[0][1]["headers"]["Content-Type"],
            "application/json",
        )

    def test_sends_user_none_request_type(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertEqual(
            session.post_calls[0][1]["json"]["requestType"],
            "USER_NONE",
        )

    def test_sends_injected_request_url_unchanged(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertEqual(
            session.post_calls[0][1]["json"]["requestUrl"],
            REQUEST_URL,
        )

    def test_body_has_no_user_personal_fields(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        body = session.post_calls[0][1]["json"]
        self.assertTrue(
            set(body).isdisjoint(
                {"userName", "userPhone", "userBirthday"}
            )
        )

    def test_body_has_no_session_key(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertNotIn("sessionKey", session.post_calls[0][1]["json"])

    def test_body_has_no_trigger_type(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertNotIn("triggerType", session.post_calls[0][1]["json"])

    def test_body_has_no_nonce_or_expire_seconds(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        body = session.post_calls[0][1]["json"]
        self.assertNotIn("nonce", body)
        self.assertNotIn("expireSeconds", body)

    def test_passes_explicit_timeout(self):
        session, client, _ = self.make_success_client(timeout=7.5)

        client.start_verification()

        self.assertEqual(session.post_calls[0][1]["timeout"], 7.5)

    def test_disables_redirects(self):
        session, client, _ = self.make_success_client()

        client.start_verification()

        self.assertIs(
            session.post_calls[0][1]["allow_redirects"],
            False,
        )

    def test_obtains_access_token_immediately_before_post(self):
        events = []

        class OrderedTokenClient:
            def get_access_token(self):
                events.append("token")
                return synthetic_token_result()

        class OrderedSession(RecordingSession):
            def post(self, url, **kwargs):
                events.append("post")
                return super().post(url, **kwargs)

        session = OrderedSession(FakeResponse(payload=valid_payload()))
        client = self.make_client(
            access_token_client=OrderedTokenClient(),
            http_session=session,
        )

        client.start_verification()

        self.assertEqual(events, ["token", "post"])

    def test_parses_success_response_into_minimal_result(self):
        _, client, _ = self.make_success_client()

        result = client.start_verification()

        self.assertIsInstance(result, TossIdentityVerificationStartResult)
        self.assertEqual(
            {item.name for item in fields(result)},
            {"provider_transaction_id", "authentication_url"},
        )

    def test_parses_provider_transaction_id(self):
        _, client, _ = self.make_success_client()

        result = client.start_verification()

        self.assertEqual(result.provider_transaction_id, TRANSACTION_ID)

    def test_parses_authentication_url(self):
        _, client, _ = self.make_success_client()

        result = client.start_verification()

        self.assertEqual(result.authentication_url, AUTHENTICATION_URL)

    def test_does_not_preserve_requested_datetime(self):
        _, client, _ = self.make_success_client()

        result = client.start_verification()

        self.assertFalse(hasattr(result, "requestedDt"))
        self.assertFalse(hasattr(result, "requested_at"))

    def test_does_not_preserve_raw_response(self):
        _, client, _ = self.make_success_client()

        result = client.start_verification()

        self.assertFalse(hasattr(result, "raw_response"))
        self.assertFalse(hasattr(result, "response"))

    def test_result_is_immutable(self):
        _, client, _ = self.make_success_client()
        result = client.start_verification()

        with self.assertRaises(FrozenInstanceError):
            result.provider_transaction_id = "replacement"

    def test_rejects_non_json_response(self):
        response = FakeResponse(json_error=ValueError("raw JSON failure"))
        self.assert_response_error(
            response,
            TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_non_object_json(self):
        for payload in ([], "value", 123, None):
            with self.subTest(payload_type=type(payload).__name__):
                self.assert_payload_error(
                    payload,
                    TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
                )

    def test_rejects_missing_result_type(self):
        payload = valid_payload()
        del payload["resultType"]
        self.assert_payload_error(
            payload,
            TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_unknown_result_type(self):
        payload = valid_payload()
        payload["resultType"] = "UNKNOWN"
        self.assert_payload_error(
            payload,
            TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_missing_success_object(self):
        payload = valid_payload()
        del payload["success"]
        self.assert_payload_error(
            payload,
            TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_non_object_success(self):
        for value in ([], "value", None):
            with self.subTest(value_type=type(value).__name__):
                payload = valid_payload()
                payload["success"] = value
                self.assert_payload_error(
                    payload,
                    TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
                )

    def test_rejects_missing_transaction_id(self):
        payload = valid_payload()
        del payload["success"]["txId"]
        self.assert_payload_error(
            payload,
            TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_empty_transaction_id(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                payload = valid_payload()
                payload["success"]["txId"] = value
                self.assert_payload_error(
                    payload,
                    TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
                )

    def test_rejects_missing_authentication_url(self):
        payload = valid_payload()
        del payload["success"]["authUrl"]
        self.assert_payload_error(
            payload,
            TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_empty_authentication_url(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                payload = valid_payload()
                payload["success"]["authUrl"] = value
                self.assert_payload_error(
                    payload,
                    TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
                )

    def test_fail_result_maps_to_request_rejected(self):
        payload = {
            "resultType": "FAIL",
            "error": {
                "reason": "raw-rejection-reason-sensitive",
                "data": RAW_RESPONSE_BODY,
            },
        }
        self.assert_payload_error(
            payload,
            TossIdentityVerificationStartErrorCode.REQUEST_REJECTED,
        )

    def test_non_2xx_maps_to_http_error_without_parsing_body(self):
        for status_code in (199, 300, 400, 500):
            with self.subTest(status_code=status_code):
                response = FakeResponse(
                    status_code=status_code,
                    json_error=AssertionError("JSON must not be parsed"),
                )
                self.assert_response_error(
                    response,
                    TossIdentityVerificationStartErrorCode.HTTP_ERROR,
                )

    def test_timeout_maps_to_network_error(self):
        self.assert_network_error(requests.Timeout("raw-timeout-sensitive"))

    def test_connection_failure_maps_to_network_error(self):
        self.assert_network_error(
            requests.ConnectionError("raw-connection-sensitive")
        )

    def test_access_token_failure_maps_to_token_error(self):
        token_error = TossCertAccessTokenError(
            TossCertAccessTokenErrorCode.NETWORK_ERROR
        )
        token_client = StubAccessTokenClient(error=token_error)
        session = RecordingSession(FakeResponse(payload=valid_payload()))
        client = self.make_client(
            access_token_client=token_client,
            http_session=session,
        )

        error = self.capture_error(client)

        self.assertIs(
            error.code,
            TossIdentityVerificationStartErrorCode.TOKEN_ERROR,
        )
        self.assertEqual(session.post_calls, [])

    def test_invalid_token_result_maps_to_token_error(self):
        for token_result in (None, object()):
            with self.subTest(result_type=type(token_result).__name__):
                token_client = StubAccessTokenClient(
                    token_result=token_result
                )
                client = self.make_client(
                    access_token_client=token_client
                )
                error = self.capture_error(client)
                self.assertIs(
                    error.code,
                    TossIdentityVerificationStartErrorCode.TOKEN_ERROR,
                )

    def test_errors_are_not_retried_automatically(self):
        session = RecordingSession(error=requests.ConnectionError())
        client = self.make_client(http_session=session)

        self.capture_error(client)

        self.assertEqual(len(session.post_calls), 1)

    def test_client_and_errors_do_not_expose_access_token(self):
        session = RecordingSession(
            FakeResponse(status_code=500, payload=valid_payload())
        )
        client = self.make_client(http_session=session)

        error = self.capture_error(client)

        for representation in (repr(client), str(client), repr(error), str(error)):
            self.assertNotIn(ACCESS_TOKEN, representation)

    def test_result_repr_hides_transaction_id(self):
        _, client, _ = self.make_success_client()
        result = client.start_verification()

        self.assertNotIn(TRANSACTION_ID, repr(result))

    def test_result_repr_hides_authentication_url(self):
        _, client, _ = self.make_success_client()
        result = client.start_verification()

        self.assertNotIn(AUTHENTICATION_URL, repr(result))

    def test_error_hides_raw_provider_body_and_rejection_fields(self):
        raw_reason = "raw-rejection-reason-sensitive"
        payload = {
            "resultType": "FAIL",
            "error": {"reason": raw_reason, "data": RAW_RESPONSE_BODY},
        }
        client = self.make_client(
            http_session=RecordingSession(FakeResponse(payload=payload))
        )

        error = self.capture_error(client)

        for sensitive_value in (raw_reason, RAW_RESPONSE_BODY):
            self.assertNotIn(sensitive_value, str(error))
            self.assertNotIn(sensitive_value, repr(error))

    def test_client_repr_hides_request_url_and_dependencies(self):
        token_client = StubAccessTokenClient(
            token_result=synthetic_token_result()
        )
        client = self.make_client(access_token_client=token_client)
        representation = repr(client)

        self.assertNotIn(REQUEST_URL, representation)
        self.assertNotIn(ACCESS_TOKEN, representation)
        self.assertNotIn(repr(token_client), representation)

    def test_production_source_has_no_credentials_or_environment_lookup(self):
        source = inspect.getsource(start_service)

        for excluded_text in (
            "client_id",
            "client_secret",
            "dotenv",
            "environ",
        ):
            with self.subTest(excluded_text=excluded_text):
                self.assertNotIn(excluded_text, source)

    def test_service_has_no_database_or_flask_dependency(self):
        imported_roots = self.imported_roots()

        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "app",
                    "flask",
                    "flask_sqlalchemy",
                    "models",
                    "sqlalchemy",
                }
            )
        )

    def test_client_does_not_implement_identity_provider_contract(self):
        client = self.make_client()

        self.assertNotIsInstance(client, IdentityVerificationProvider)
        self.assertFalse(hasattr(client, "get_verified_identity"))

    def test_does_not_fetch_authentication_url(self):
        session, client, _ = self.make_success_client()

        result = client.start_verification()

        self.assertEqual(result.authentication_url, AUTHENTICATION_URL)
        self.assertEqual(session.get_calls, [])

    def test_service_has_no_lock_persistence_or_logging_dependency(self):
        imported_roots = self.imported_roots()

        self.assertTrue(
            imported_roots.isdisjoint(
                {"logging", "pathlib", "pickle", "shelve", "sqlite3", "threading"}
            )
        )

    def test_default_session_can_be_replaced_without_actual_toss_http(self):
        fake_session = RecordingSession(
            FakeResponse(payload=valid_payload())
        )
        with patch.object(
            start_service.requests,
            "Session",
            return_value=fake_session,
        ) as session_factory:
            client = self.make_client(http_session=None)
            result = client.start_verification()

        session_factory.assert_called_once_with()
        self.assertEqual(result.provider_transaction_id, TRANSACTION_ID)
        self.assertEqual(len(fake_session.post_calls), 1)

    def test_error_codes_are_stable_and_minimal(self):
        self.assertEqual(
            {code.value for code in TossIdentityVerificationStartErrorCode},
            {
                "NETWORK_ERROR",
                "HTTP_ERROR",
                "REQUEST_REJECTED",
                "INVALID_RESPONSE",
                "TOKEN_ERROR",
            },
        )

    def make_client(self, **overrides):
        values = {
            "access_token_client": StubAccessTokenClient(
                token_result=synthetic_token_result()
            ),
            "request_url": REQUEST_URL,
            "http_session": RecordingSession(
                FakeResponse(payload=valid_payload())
            ),
            "timeout": 5,
        }
        values.update(overrides)
        return TossIdentityVerificationStartClient(**values)

    def make_success_client(self, **overrides):
        token_client = StubAccessTokenClient(
            token_result=synthetic_token_result()
        )
        session = RecordingSession(FakeResponse(payload=valid_payload()))
        client = self.make_client(
            access_token_client=token_client,
            http_session=session,
            **overrides,
        )
        return session, client, token_client

    def assert_payload_error(self, payload, expected_code):
        self.assert_response_error(
            FakeResponse(payload=payload),
            expected_code,
        )

    def assert_response_error(self, response, expected_code):
        client = self.make_client(
            http_session=RecordingSession(response)
        )
        error = self.capture_error(client)
        self.assertIs(error.code, expected_code)

    def assert_network_error(self, network_error):
        session = RecordingSession(error=network_error)
        client = self.make_client(http_session=session)
        error = self.capture_error(client)
        self.assertIs(
            error.code,
            TossIdentityVerificationStartErrorCode.NETWORK_ERROR,
        )

    def capture_error(self, client):
        with self.assertRaises(
            TossIdentityVerificationStartError
        ) as context:
            client.start_verification()
        return context.exception

    def imported_roots(self):
        source = inspect.getsource(start_service)
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
        return imported_roots


if __name__ == "__main__":
    unittest.main()
