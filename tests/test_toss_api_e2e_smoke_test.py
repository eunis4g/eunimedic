import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "toss_api_e2e_smoke_test.py"
SPEC = importlib.util.spec_from_file_location(
    "toss_api_e2e_smoke_test",
    SCRIPT_PATH,
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


VERIFICATION_SESSION_ID = "11111111-1111-4111-8111-111111111111"
OLD_PENDING_SESSION_ID = "22222222-2222-4222-8222-222222222222"
TRANSACTION_ID = "synthetic-api-transaction-sensitive"
AUTHENTICATION_URL = "https://example.test/auth-sensitive"
IDENTITY_SUBJECT_DIGEST = "a" * 64
SIGNATURE = "synthetic-api-signature-sensitive"
RAW_API_SECRET = "synthetic-raw-api-response-sensitive"


class FakeResponse:

    def __init__(self, status_code, payload, *, content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeIdentityApiClient:

    def __init__(self, *, start_response=None, complete_response=None):
        self.start_response = start_response or valid_start_response()
        self.complete_response = complete_response or valid_complete_response()
        self.start_call_count = 0
        self.complete_session_ids = []
        self.closed = False

    def start_identity_verification(self):
        self.start_call_count += 1
        return self.start_response

    def complete_identity_verification(self, verification_session_id):
        self.complete_session_ids.append(verification_session_id)
        return self.complete_response

    def close(self):
        self.closed = True

    def __repr__(self):
        return f"{type(self).__name__}()"


class FakeRequestsSession:

    def __init__(self):
        self.calls = []
        self.closed = False
        self.trust_env = True

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(200, {})

    def close(self):
        self.closed = True


def valid_start_response(**overrides):
    payload = {
        "verification_session_id": VERIFICATION_SESSION_ID,
        "provider_transaction_id": TRANSACTION_ID,
        "authentication_url": AUTHENTICATION_URL,
        "expires_at": "2026-10-10T12:10:00Z",
    }
    payload.update(overrides)
    return FakeResponse(201, payload)


def valid_complete_response(**overrides):
    payload = {
        "verification_session_id": VERIFICATION_SESSION_ID,
        "status": "verified",
        "age_eligibility": "AGE_14_OR_OVER",
    }
    payload.update(overrides)
    return FakeResponse(200, payload)


class TossApiE2ESmokeTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "api-e2e-smoke.db"
        )
        self._create_database()
        self.http_guard = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError(
                "Smoke unit tests must not make actual network requests."
            ),
        )
        self.http_guard.start()
        self.api_client = FakeIdentityApiClient()
        self.database_verifier = smoke.ReadOnlyIdentityDatabaseVerifier(
            self.database_path
        )
        self.application = smoke.create_toss_api_e2e_smoke_app(
            http_client=self.api_client,
            database_verifier=self.database_verifier,
            testing=True,
        )
        self.client = self.application.test_client()

    def tearDown(self):
        smoke.close_toss_api_e2e_smoke_app(self.application)
        self.http_guard.stop()
        self.temporary_directory.cleanup()

    def test_server_is_fixed_to_loopback_without_debug_or_reloader(self):
        calls = []
        output = []

        result = smoke.run_toss_api_e2e_smoke_server(
            self.application,
            output=output.append,
            server_runner=lambda **kwargs: calls.append(kwargs),
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [{
                "host": "127.0.0.1",
                "port": 5058,
                "debug": False,
                "use_reloader": False,
            }],
        )
        self.assertFalse(self.application.debug)
        self.assertTrue(self.api_client.closed)
        self.assertEqual(output[-1], "http://127.0.0.1:5058")

    def test_harness_uses_fixed_loopback_api_base(self):
        self.assertEqual(smoke.API_BASE_URL, "http://127.0.0.1:5000")
        self.assertEqual(
            smoke.API_START_URL,
            "http://127.0.0.1:5000/api/v1/identity-verifications",
        )

    def test_responses_are_no_store_without_cors_or_cookie(self):
        responses = (
            self.client.get("/"),
            self.client.post("/start", json={"unexpected": True}),
            self.client.post("/complete", json={}),
        )

        for response in responses:
            with self.subTest(status_code=response.status_code):
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["Pragma"], "no-cache")
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)

    def test_requests_client_start_contract_is_post_json_empty_object(self):
        session = FakeRequestsSession()
        client = smoke.RequestsIdentityApiClient(session=session, timeout=7.5)

        client.start_identity_verification()

        self.assertFalse(session.trust_env)
        self.assertEqual(len(session.calls), 1)
        url, options = session.calls[0]
        self.assertEqual(url, smoke.API_START_URL)
        self.assertEqual(options["json"], {})
        self.assertEqual(options["headers"]["Accept"], "application/json")
        self.assertEqual(
            options["headers"]["Content-Type"],
            "application/json",
        )
        self.assertEqual(options["timeout"], 7.5)
        self.assertFalse(options["allow_redirects"])

    def test_start_proxy_validates_and_returns_only_transient_sdk_values(self):
        response = self.client.post("/start", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.api_client.start_call_count, 1)
        self.assertEqual(
            response.get_json(),
            {
                "verification_session_id": VERIFICATION_SESSION_ID,
                "txId": TRANSACTION_ID,
                "authUrl": AUTHENTICATION_URL,
            },
        )
        self.assertNotIn("expires_at", response.get_json())

    def test_start_rejects_nonempty_or_non_json_body_before_api_call(self):
        responses = (
            self.client.post("/start"),
            self.client.post("/start", json={"unexpected": True}),
        )

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.get_json()["error"]["code"],
                "INVALID_REQUEST",
            )
        self.assertEqual(self.api_client.start_call_count, 0)

    def test_start_rejects_missing_or_invalid_required_api_fields(self):
        invalid_payloads = (
            {"verification_session_id": None},
            {"provider_transaction_id": ""},
            {"authentication_url": "javascript:unsafe"},
            {"expires_at": "not-a-date"},
        )
        for override in invalid_payloads:
            with self.subTest(field=next(iter(override))):
                api_client = FakeIdentityApiClient(
                    start_response=valid_start_response(**override)
                )
                application = smoke.create_toss_api_e2e_smoke_app(
                    http_client=api_client,
                    database_verifier=self.database_verifier,
                    testing=True,
                )
                response = application.test_client().post("/start", json={})
                self.assertEqual(response.status_code, 502)
                self.assertEqual(
                    response.get_json()["error"]["code"],
                    "INVALID_START_RESPONSE",
                )
                smoke.close_toss_api_e2e_smoke_app(application)

    def test_prepare_popup_precedes_asynchronous_start_request(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertLess(page.index("preparePopup()"), page.index('fetch("/start"'))
        self.assertIn("TossCert()", page)
        self.assertIn("tossCert.start", page)
        self.assertIn(smoke.SDK_URL, page)

    def test_page_uses_no_storage_cookie_console_or_direct_navigation(self):
        page = self.client.get("/").get_data(as_text=True)

        for forbidden in (
            "localStorage",
            "sessionStorage",
            "document.cookie",
            "console.",
            "window.location",
            "location.href",
        ):
            self.assertNotIn(forbidden, page)
        for sensitive_value in (
            VERIFICATION_SESSION_ID,
            TRANSACTION_ID,
            AUTHENTICATION_URL,
            IDENTITY_SUBJECT_DIGEST,
            SIGNATURE,
        ):
            self.assertNotIn(sensitive_value, page)

    def test_client_has_double_click_and_single_complete_guards(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("if (runInProgress)", page)
        self.assertIn("if (completeRequested)", page)
        self.assertIn("completeRequested = true", page)
        self.assertEqual(page.count('fetch("/complete"'), 1)
        self.assertEqual(page.count("onSuccess:"), 1)

    def test_complete_requires_only_a_started_public_session_id(self):
        unknown = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )
        self.client.post("/start", json={})
        transaction_injection = self.client.post(
            "/complete",
            json={
                "verification_session_id": VERIFICATION_SESSION_ID,
                "txId": "attacker-controlled",
            },
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(
            unknown.get_json()["error"]["code"],
            "SESSION_NOT_STARTED",
        )
        self.assertEqual(transaction_injection.status_code, 400)
        self.assertEqual(self.api_client.complete_session_ids, [])

    def test_requests_client_complete_contract_uses_path_id_and_empty_body(self):
        session = FakeRequestsSession()
        client = smoke.RequestsIdentityApiClient(session=session)

        client.complete_identity_verification(VERIFICATION_SESSION_ID)

        self.assertEqual(len(session.calls), 1)
        url, options = session.calls[0]
        self.assertEqual(
            url,
            f"{smoke.API_START_URL}/{VERIFICATION_SESSION_ID}/complete",
        )
        self.assertEqual(options["json"], {})
        self.assertEqual(
            options["headers"]["Content-Type"],
            "application/json",
        )
        self.assertNotIn("txId", str(options))
        self.assertNotIn(TRANSACTION_ID, str(options))

    def test_success_returns_safe_status_after_read_only_db_verification(self):
        self.client.post("/start", json={})
        self._insert_verified_session()

        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "status": "verified",
                "age_eligibility": "AGE_14_OR_OVER",
            },
        )
        self.assertEqual(
            self.api_client.complete_session_ids,
            [VERIFICATION_SESSION_ID],
        )

    def test_age_eligibility_uses_domain_values_without_fixed_test_identity(self):
        self.api_client.complete_response = valid_complete_response(
            age_eligibility="UNDER_14"
        )
        self.client.post("/start", json={})
        self._insert_verified_session(age_eligibility="UNDER_14")

        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["age_eligibility"], "UNDER_14")

    def test_invalid_complete_api_result_is_rejected_without_db_values(self):
        self.api_client.complete_response = valid_complete_response(
            age_eligibility="UNTRUSTED_VALUE"
        )
        self.client.post("/start", json={})

        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "INVALID_COMPLETE_RESPONSE",
        )
        self.assertNotIn("UNTRUSTED_VALUE", response.get_data(as_text=True))

    def test_api_errors_forward_only_allowlisted_code_and_safe_status(self):
        cases = (
            (409, "VERIFICATION_NOT_COMPLETED"),
            (403, "AGE_REQUIREMENT_NOT_MET"),
            (410, "VERIFICATION_EXPIRED"),
            (503, "PROVIDER_UNAVAILABLE"),
            (500, "FINALIZATION_FAILED"),
        )
        for status_code, error_code in cases:
            with self.subTest(status_code=status_code, error_code=error_code):
                api_client = FakeIdentityApiClient(
                    complete_response=FakeResponse(
                        status_code,
                        {
                            "error": {
                                "code": error_code,
                                "message": RAW_API_SECRET,
                            }
                        },
                    )
                )
                application = smoke.create_toss_api_e2e_smoke_app(
                    http_client=api_client,
                    database_verifier=self.database_verifier,
                    testing=True,
                )
                client = application.test_client()
                client.post("/start", json={})
                response = client.post(
                    "/complete",
                    json={"verification_session_id": VERIFICATION_SESSION_ID},
                )
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.get_json(), {"error": {"code": error_code}})
                self.assertNotIn(RAW_API_SECRET, response.get_data(as_text=True))
                self.assertEqual(len(api_client.complete_session_ids), 1)
                smoke.close_toss_api_e2e_smoke_app(application)

    def test_unknown_api_error_and_raw_body_are_not_disclosed(self):
        self.api_client.start_response = FakeResponse(
            418,
            {"error": {"code": RAW_API_SECRET, "message": RAW_API_SECRET}},
        )

        response = self.client.post("/start", json={})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {"error": {"code": "START_API_FAILED"}},
        )
        self.assertNotIn(RAW_API_SECRET, response.get_data(as_text=True))

    def test_server_guard_allows_only_one_complete_attempt_per_session(self):
        self.client.post("/start", json={})
        self._insert_verified_session()

        first = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )
        second = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.get_json()["error"]["code"],
            "COMPLETE_ALREADY_ATTEMPTED",
        )
        self.assertEqual(len(self.api_client.complete_session_ids), 1)

    def test_no_automatic_retry_or_polling_is_present(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        page = self.client.get("/").get_data(as_text=True)

        self.assertNotIn("setInterval", page)
        self.assertNotIn("setTimeout", page)
        self.assertNotIn("HTTPAdapter", source)
        self.assertNotIn("Retry(", source)

    def test_database_connections_are_mode_ro_and_query_only(self):
        calls = []
        statements = []

        def recording_connect(database, **kwargs):
            calls.append((database, kwargs))
            connection = sqlite3.connect(database, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        verifier = smoke.ReadOnlyIdentityDatabaseVerifier(
            self.database_path,
            connection_factory=recording_connect,
        )

        self.assertEqual(verifier.snapshot_user_count(), 2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("?mode=ro"))
        self.assertTrue(calls[0][1]["uri"])
        self.assertIn("PRAGMA query_only=ON", statements)
        for statement in statements:
            self.assertNotRegex(statement.upper(), r"\b(INSERT|UPDATE|DELETE)\b")

    def test_db_verifier_requires_verified_digest_claims_and_one_evidence(self):
        self._insert_verified_session()

        self.database_verifier.verify_completed_session(
            verification_session_id=VERIFICATION_SESSION_ID,
            expected_age_eligibility="AGE_14_OR_OVER",
            expected_user_count=2,
        )

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE identity_verification_sessions "
                "SET consumed_at = ? WHERE verification_session_id = ?",
                ("2026-10-10 12:05:00", VERIFICATION_SESSION_ID),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(smoke.TossApiE2ESmokeError):
            self.database_verifier.verify_completed_session(
                verification_session_id=VERIFICATION_SESSION_ID,
                expected_age_eligibility="AGE_14_OR_OVER",
                expected_user_count=2,
            )

    def test_user_count_is_unchanged_and_session_is_not_consumed(self):
        self.client.post("/start", json={})
        self._insert_verified_session()
        before_users = self._scalar("SELECT COUNT(*) FROM users")

        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM users"), before_users)
        self.assertIsNone(
            self._scalar(
                "SELECT consumed_at FROM identity_verification_sessions "
                "WHERE verification_session_id = ?",
                (VERIFICATION_SESSION_ID,),
            )
        )

    def test_changed_user_count_fails_final_read_only_check(self):
        self.client.post("/start", json={})
        self._insert_verified_session()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("INSERT INTO users (user_id) VALUES (3)")
            connection.commit()
        finally:
            connection.close()

        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "DATABASE_VERIFICATION_FAILED",
        )

    def test_existing_pending_session_is_not_selected_or_modified(self):
        before = self._pending_snapshot()
        self.client.post("/start", json={})
        self._insert_verified_session()

        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._pending_snapshot(), before)

    def test_sensitive_values_are_absent_from_safe_output_and_repr(self):
        self.client.post("/start", json={})
        self._insert_verified_session()
        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )
        combined = " ".join(
            (
                response.get_data(as_text=True),
                repr(self.database_verifier),
                repr(self.application.extensions["toss_api_e2e_session_store"]),
            )
        )

        for sensitive_value in (
            VERIFICATION_SESSION_ID,
            TRANSACTION_ID,
            AUTHENTICATION_URL,
            IDENTITY_SUBJECT_DIGEST,
            SIGNATURE,
            RAW_API_SECRET,
        ):
            self.assertNotIn(sensitive_value, combined)

    def test_unit_flow_uses_fake_http_and_global_network_guard(self):
        self.client.post("/start", json={})
        self._insert_verified_session()
        response = self.client.post(
            "/complete",
            json={"verification_session_id": VERIFICATION_SESSION_ID},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.api_client.start_call_count, 1)
        self.assertEqual(len(self.api_client.complete_session_ids), 1)

    def _create_database(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY
                );
                CREATE TABLE identity_verification_sessions (
                    identity_verification_session_id INTEGER PRIMARY KEY,
                    verification_session_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    provider_transaction_id TEXT,
                    completion_claim_token TEXT,
                    completion_claimed_at TEXT,
                    identity_subject_digest TEXT,
                    age_eligibility TEXT,
                    failure_code TEXT,
                    verified_at TEXT,
                    consumed_at TEXT
                );
                CREATE TABLE toss_identity_verification_evidence (
                    toss_identity_verification_evidence_id INTEGER PRIMARY KEY,
                    identity_verification_session_id INTEGER NOT NULL,
                    provider_transaction_id TEXT NOT NULL,
                    signature TEXT NOT NULL
                );
                INSERT INTO users (user_id) VALUES (1), (2);
                INSERT INTO identity_verification_sessions (
                    identity_verification_session_id,
                    verification_session_id,
                    status,
                    provider_transaction_id
                ) VALUES (
                    1,
                    '22222222-2222-4222-8222-222222222222',
                    'pending',
                    'old-pending-transaction'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _insert_verified_session(self, *, age_eligibility="AGE_14_OR_OVER"):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO identity_verification_sessions ("
                "identity_verification_session_id, verification_session_id, "
                "status, provider_transaction_id, identity_subject_digest, "
                "age_eligibility, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    2,
                    VERIFICATION_SESSION_ID,
                    "verified",
                    TRANSACTION_ID,
                    IDENTITY_SUBJECT_DIGEST,
                    age_eligibility,
                    "2026-10-10 12:05:00",
                ),
            )
            connection.execute(
                "INSERT INTO toss_identity_verification_evidence ("
                "toss_identity_verification_evidence_id, "
                "identity_verification_session_id, provider_transaction_id, "
                "signature) VALUES (?, ?, ?, ?)",
                (1, 2, TRANSACTION_ID, SIGNATURE),
            )
            connection.commit()
        finally:
            connection.close()

    def _pending_snapshot(self):
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(
                "SELECT status, provider_transaction_id, "
                "completion_claim_token, completion_claimed_at, "
                "identity_subject_digest, age_eligibility, failure_code, "
                "verified_at, consumed_at "
                "FROM identity_verification_sessions "
                "WHERE verification_session_id = ?",
                (OLD_PENDING_SESSION_ID,),
            ).fetchone()
        finally:
            connection.close()

    def _scalar(self, statement, parameters=()):
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(statement, parameters).fetchone()[0]
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
