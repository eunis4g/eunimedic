import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
import inspect
from threading import Event, Lock
import unittest
from unittest.mock import patch

import requests

import services.toss_cert_access_token_client as token_service
from services.identity_verification_provider import IdentityVerificationProvider
from services.toss_cert_access_token_client import (
    TOSS_CERT_TOKEN_ENDPOINT,
    TossCertAccessToken,
    TossCertAccessTokenClient,
    TossCertAccessTokenError,
    TossCertAccessTokenErrorCode,
)


RECEIVED_AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
CLIENT_ID = "client-id-sensitive"
CLIENT_SECRET = "client-secret-sensitive"
ACCESS_TOKEN = "access-token-sensitive"


class FakeResponse:

    def __init__(self, *, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = "raw-response-body-sensitive"

    def json(self):
        if self._json_error is not None:
            raise self._json_error

        return self._payload


class RecordingSession:

    def __init__(self, *responses, error=None, on_post=None):
        self._responses = list(responses)
        self._error = error
        self._on_post = on_post
        self.calls = []
        self._lock = Lock()

    def post(self, url, **kwargs):
        with self._lock:
            self.calls.append((url, kwargs))
            if self._on_post is not None:
                self._on_post()
            if self._error is not None:
                raise self._error
            return self._responses.pop(0)


class BlockingSession:

    def __init__(self, response):
        self._response = response
        self.entered = Event()
        self.release = Event()
        self.call_count = 0
        self._lock = Lock()

    def post(self, url, **kwargs):
        with self._lock:
            self.call_count += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release the blocked request")
        return self._response


class MutableClock:

    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current


def valid_payload(*, access_token=ACCESS_TOKEN, expires_in=3600):
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "scope": "ca",
        "expires_in": expires_in,
    }


class TossCertAccessTokenClientTest(unittest.TestCase):

    def test_rejects_empty_client_id(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValueError):
                    self.make_client(client_id=value)

    def test_rejects_empty_client_secret(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValueError):
                    self.make_client(client_secret=value)

    def test_does_not_impose_a_test_credential_prefix(self):
        client = self.make_client(
            client_id="production-format-id",
            client_secret="production-format-secret",
        )

        self.assertIsInstance(client, TossCertAccessTokenClient)

    def test_rejects_non_positive_or_missing_timeout(self):
        for value in (None, 0, -0.1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_client(timeout=value)

    def test_rejects_boolean_or_non_finite_timeout(self):
        for value in (True, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_client(timeout=value)

    def test_rejects_negative_refresh_skew(self):
        with self.assertRaises(ValueError):
            self.make_client(refresh_skew=timedelta(microseconds=-1))

    def test_rejects_non_timedelta_refresh_skew(self):
        with self.assertRaises(TypeError):
            self.make_client(refresh_skew=60)

    def test_posts_to_the_official_token_endpoint(self):
        session, client = self.make_success_client()

        client.get_access_token()

        self.assertEqual(session.calls[0][0], TOSS_CERT_TOKEN_ENDPOINT)
        self.assertEqual(
            TOSS_CERT_TOKEN_ENDPOINT,
            "https://oauth2.cert.toss.im/token",
        )

    def test_sends_form_urlencoded_content_type(self):
        session, client = self.make_success_client()

        client.get_access_token()

        self.assertEqual(
            session.calls[0][1]["headers"],
            {"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_sends_client_credentials_grant_type(self):
        session, client = self.make_success_client()

        client.get_access_token()

        self.assertEqual(
            session.calls[0][1]["data"]["grant_type"],
            "client_credentials",
        )

    def test_sends_ca_scope(self):
        session, client = self.make_success_client()

        client.get_access_token()

        self.assertEqual(session.calls[0][1]["data"]["scope"], "ca")

    def test_sends_injected_credentials_as_form_values(self):
        session, client = self.make_success_client()

        client.get_access_token()

        data = session.calls[0][1]["data"]
        self.assertEqual(data["client_id"], CLIENT_ID)
        self.assertEqual(data["client_secret"], CLIENT_SECRET)

    def test_passes_explicit_timeout(self):
        session, client = self.make_success_client(timeout=7.5)

        client.get_access_token()

        self.assertEqual(session.calls[0][1]["timeout"], 7.5)

    def test_disables_redirects(self):
        session, client = self.make_success_client()

        client.get_access_token()

        self.assertIs(session.calls[0][1]["allow_redirects"], False)

    def test_parses_the_minimal_token_result(self):
        _, client = self.make_success_client()

        result = client.get_access_token()

        self.assertEqual(result.access_token, ACCESS_TOKEN)
        self.assertEqual(
            {item.name for item in fields(result)},
            {"access_token", "expires_at"},
        )

    def test_accepts_exact_bearer_token_type_and_ca_scope(self):
        _, client = self.make_success_client()

        result = client.get_access_token()

        self.assertIsInstance(result, TossCertAccessToken)

    def test_uses_response_expires_in_without_a_hardcoded_lifetime(self):
        _, short_client = self.make_success_client(expires_in=17)
        _, long_client = self.make_success_client(expires_in=91)

        self.assertEqual(
            short_client.get_access_token().expires_at,
            RECEIVED_AT + timedelta(seconds=17),
        )
        self.assertEqual(
            long_client.get_access_token().expires_at,
            RECEIVED_AT + timedelta(seconds=91),
        )

    def test_bases_expiration_on_the_response_received_time(self):
        clock = MutableClock(RECEIVED_AT - timedelta(minutes=5))
        response_time = RECEIVED_AT
        session = RecordingSession(
            FakeResponse(payload=valid_payload(expires_in=30)),
            on_post=lambda: setattr(clock, "current", response_time),
        )
        client = self.make_client(http_session=session, clock=clock)

        result = client.get_access_token()

        self.assertEqual(
            result.expires_at,
            response_time + timedelta(seconds=30),
        )

    def test_normalizes_response_time_to_utc(self):
        korea_time = datetime(
            2026,
            10,
            9,
            21,
            0,
            tzinfo=timezone(timedelta(hours=9)),
        )
        _, client = self.make_success_client(clock=lambda: korea_time)

        result = client.get_access_token()

        self.assertIs(result.expires_at.tzinfo, UTC)
        self.assertEqual(
            result.expires_at,
            RECEIVED_AT + timedelta(hours=1),
        )

    def test_rejects_boolean_expires_in(self):
        self.assert_invalid_payload(valid_payload(expires_in=True))

    def test_rejects_non_positive_expires_in(self):
        for value in (0, -1):
            with self.subTest(value=value):
                self.assert_invalid_payload(valid_payload(expires_in=value))

    def test_rejects_non_json_response(self):
        response = FakeResponse(json_error=ValueError("raw JSON failure"))
        client = self.make_client(http_session=RecordingSession(response))

        self.assert_error_code(
            client,
            TossCertAccessTokenErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_non_object_json(self):
        for payload in ([], "value", 123, None):
            with self.subTest(payload_type=type(payload).__name__):
                self.assert_invalid_payload(payload)

    def test_rejects_missing_access_token(self):
        payload = valid_payload()
        del payload["access_token"]

        self.assert_invalid_payload(payload)

    def test_rejects_empty_access_token(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                self.assert_invalid_payload(valid_payload(access_token=value))

    def test_rejects_missing_expires_in(self):
        payload = valid_payload()
        del payload["expires_in"]

        self.assert_invalid_payload(payload)

    def test_rejects_wrong_token_type(self):
        for value in ("bearer", "BEARER", None):
            with self.subTest(value=value):
                payload = valid_payload()
                payload["token_type"] = value
                self.assert_invalid_payload(payload)

    def test_rejects_wrong_scope(self):
        for value in ("CA", "ca other", None):
            with self.subTest(value=value):
                payload = valid_payload()
                payload["scope"] = value
                self.assert_invalid_payload(payload)

    def test_reuses_the_same_cached_token_while_valid(self):
        session, client = self.make_success_client()

        first = client.get_access_token()
        second = client.get_access_token()

        self.assertIs(second, first)
        self.assertEqual(len(session.calls), 1)

    def test_reissues_an_expired_token(self):
        clock = MutableClock(RECEIVED_AT)
        session = RecordingSession(
            FakeResponse(payload=valid_payload(
                access_token="first-token",
                expires_in=10,
            )),
            FakeResponse(payload=valid_payload(
                access_token="second-token",
                expires_in=10,
            )),
        )
        client = self.make_client(http_session=session, clock=clock)
        first = client.get_access_token()
        clock.current = first.expires_at

        second = client.get_access_token()

        self.assertEqual(second.access_token, "second-token")
        self.assertEqual(len(session.calls), 2)

    def test_reissues_at_the_refresh_skew_boundary(self):
        clock, session, client = self.make_two_response_client(
            refresh_skew=timedelta(seconds=20)
        )
        first = client.get_access_token()
        clock.current = first.expires_at - timedelta(seconds=20)

        second = client.get_access_token()

        self.assertEqual(second.access_token, "second-token")
        self.assertEqual(len(session.calls), 2)

    def test_reuses_just_before_the_refresh_skew_boundary(self):
        clock, session, client = self.make_two_response_client(
            refresh_skew=timedelta(seconds=20)
        )
        first = client.get_access_token()
        clock.current = (
            first.expires_at
            - timedelta(seconds=20)
            - timedelta(microseconds=1)
        )

        second = client.get_access_token()

        self.assertIs(second, first)
        self.assertEqual(len(session.calls), 1)

    def test_returns_but_does_not_reuse_a_short_lived_token(self):
        clock, session, client = self.make_two_response_client(
            refresh_skew=timedelta(seconds=60),
            expires_in=60,
        )

        first = client.get_access_token()
        second = client.get_access_token()

        self.assertEqual(first.access_token, "first-token")
        self.assertEqual(second.access_token, "second-token")
        self.assertEqual(len(session.calls), 2)

    def test_simultaneous_requests_perform_only_one_refresh(self):
        session = BlockingSession(
            FakeResponse(payload=valid_payload(expires_in=60))
        )
        client = self.make_client(http_session=session)

        with ThreadPoolExecutor(max_workers=12) as executor:
            first_future = executor.submit(client.get_access_token)
            self.assertTrue(session.entered.wait(timeout=2))
            other_futures = [
                executor.submit(client.get_access_token)
                for _ in range(11)
            ]
            session.release.set()
            results = [first_future.result(timeout=2)]
            results.extend(
                future.result(timeout=2) for future in other_futures
            )

        self.assertEqual(session.call_count, 1)
        self.assertTrue(all(result is results[0] for result in results))

    def test_client_repr_and_str_hide_credentials_and_cached_token(self):
        _, client = self.make_success_client()
        client.get_access_token()

        for representation in (repr(client), str(client)):
            with self.subTest(representation=representation):
                self.assertNotIn(CLIENT_ID, representation)
                self.assertNotIn(CLIENT_SECRET, representation)
                self.assertNotIn(ACCESS_TOKEN, representation)

    def test_token_result_is_immutable_and_hides_token_in_repr(self):
        _, client = self.make_success_client()
        result = client.get_access_token()

        with self.assertRaises(FrozenInstanceError):
            result.access_token = "replacement"

        self.assertNotIn(ACCESS_TOKEN, repr(result))

    def test_network_error_hides_credentials_and_original_message(self):
        raw_message = (
            f"failed with {CLIENT_ID} {CLIENT_SECRET} {ACCESS_TOKEN}"
        )
        session = RecordingSession(error=requests.Timeout(raw_message))
        client = self.make_client(http_session=session)

        error = self.capture_error(client)

        self.assertIs(error.code, TossCertAccessTokenErrorCode.NETWORK_ERROR)
        for sensitive_value in (
            CLIENT_ID,
            CLIENT_SECRET,
            ACCESS_TOKEN,
            raw_message,
        ):
            self.assertNotIn(sensitive_value, str(error))
            self.assertNotIn(sensitive_value, repr(error))

    def test_http_error_hides_raw_response_body(self):
        response = FakeResponse(status_code=401, payload=valid_payload())
        client = self.make_client(http_session=RecordingSession(response))

        error = self.capture_error(client)

        self.assertIs(error.code, TossCertAccessTokenErrorCode.HTTP_ERROR)
        self.assertNotIn(response.text, str(error))
        self.assertNotIn(response.text, repr(error))

    def test_invalid_response_error_hides_access_token(self):
        payload = valid_payload()
        payload["scope"] = "wrong"
        client = self.make_client(
            http_session=RecordingSession(FakeResponse(payload=payload))
        )

        error = self.capture_error(client)

        self.assertIs(
            error.code,
            TossCertAccessTokenErrorCode.INVALID_RESPONSE,
        )
        self.assertNotIn(ACCESS_TOKEN, str(error))
        self.assertNotIn(ACCESS_TOKEN, repr(error))

    def test_timeout_maps_to_stable_network_error(self):
        client = self.make_client(
            http_session=RecordingSession(error=requests.Timeout())
        )

        self.assert_error_code(
            client,
            TossCertAccessTokenErrorCode.NETWORK_ERROR,
        )

    def test_connection_failure_maps_to_stable_network_error(self):
        client = self.make_client(
            http_session=RecordingSession(error=requests.ConnectionError())
        )

        self.assert_error_code(
            client,
            TossCertAccessTokenErrorCode.NETWORK_ERROR,
        )

    def test_non_2xx_maps_to_stable_http_error_without_json_parsing(self):
        for status_code in (199, 300, 400, 500):
            with self.subTest(status_code=status_code):
                response = FakeResponse(
                    status_code=status_code,
                    json_error=AssertionError("JSON must not be read"),
                )
                client = self.make_client(
                    http_session=RecordingSession(response)
                )
                self.assert_error_code(
                    client,
                    TossCertAccessTokenErrorCode.HTTP_ERROR,
                )

    def test_failures_are_not_retried_automatically(self):
        for error, response, expected_code in (
            (
                requests.ConnectionError(),
                None,
                TossCertAccessTokenErrorCode.NETWORK_ERROR,
            ),
            (
                None,
                FakeResponse(status_code=503),
                TossCertAccessTokenErrorCode.HTTP_ERROR,
            ),
        ):
            with self.subTest(expected_code=expected_code):
                responses = () if response is None else (response,)
                session = RecordingSession(*responses, error=error)
                client = self.make_client(http_session=session)

                self.assert_error_code(client, expected_code)
                self.assertEqual(len(session.calls), 1)

    def test_cache_is_per_client_instance(self):
        session = RecordingSession(
            FakeResponse(payload=valid_payload(access_token="first-token")),
            FakeResponse(payload=valid_payload(access_token="second-token")),
        )
        first_client = self.make_client(http_session=session)
        second_client = self.make_client(http_session=session)

        first = first_client.get_access_token()
        second = second_client.get_access_token()

        self.assertEqual(first.access_token, "first-token")
        self.assertEqual(second.access_token, "second-token")
        self.assertEqual(len(session.calls), 2)

    def test_service_has_no_database_or_flask_dependency(self):
        imported_roots = self.imported_roots()

        self.assertTrue(
            imported_roots.isdisjoint(
                {"flask", "flask_sqlalchemy", "sqlalchemy", "models"}
            )
        )

    def test_client_does_not_implement_identity_provider_contract(self):
        client = self.make_client()

        self.assertNotIsInstance(client, IdentityVerificationProvider)
        self.assertFalse(hasattr(client, "start_verification"))
        self.assertFalse(hasattr(client, "get_verified_identity"))

    def test_service_has_no_persistence_or_logging_dependency(self):
        imported_roots = self.imported_roots()

        self.assertTrue(
            imported_roots.isdisjoint(
                {"logging", "pathlib", "pickle", "shelve", "sqlite3"}
            )
        )

    def test_default_session_can_be_replaced_without_actual_toss_http(self):
        fake_session = RecordingSession(
            FakeResponse(payload=valid_payload())
        )
        with patch.object(
            token_service.requests,
            "Session",
            return_value=fake_session,
        ) as session_factory:
            client = self.make_client(http_session=None)
            result = client.get_access_token()

        session_factory.assert_called_once_with()
        self.assertEqual(result.access_token, ACCESS_TOKEN)
        self.assertEqual(len(fake_session.calls), 1)

    def make_client(self, **overrides):
        values = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "timeout": 5,
            "refresh_skew": timedelta(0),
            "http_session": RecordingSession(
                FakeResponse(payload=valid_payload())
            ),
            "clock": lambda: RECEIVED_AT,
        }
        values.update(overrides)
        return TossCertAccessTokenClient(**values)

    def make_success_client(self, *, expires_in=3600, **overrides):
        session = RecordingSession(
            FakeResponse(payload=valid_payload(expires_in=expires_in))
        )
        client = self.make_client(http_session=session, **overrides)
        return session, client

    def make_two_response_client(self, *, refresh_skew, expires_in=100):
        clock = MutableClock(RECEIVED_AT)
        session = RecordingSession(
            FakeResponse(payload=valid_payload(
                access_token="first-token",
                expires_in=expires_in,
            )),
            FakeResponse(payload=valid_payload(
                access_token="second-token",
                expires_in=expires_in,
            )),
        )
        client = self.make_client(
            http_session=session,
            clock=clock,
            refresh_skew=refresh_skew,
        )
        return clock, session, client

    def assert_invalid_payload(self, payload):
        client = self.make_client(
            http_session=RecordingSession(FakeResponse(payload=payload))
        )
        self.assert_error_code(
            client,
            TossCertAccessTokenErrorCode.INVALID_RESPONSE,
        )

    def assert_error_code(self, client, expected_code):
        error = self.capture_error(client)
        self.assertIs(error.code, expected_code)

    def capture_error(self, client):
        with self.assertRaises(TossCertAccessTokenError) as context:
            client.get_access_token()
        return context.exception

    def imported_roots(self):
        source = inspect.getsource(token_service)
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
