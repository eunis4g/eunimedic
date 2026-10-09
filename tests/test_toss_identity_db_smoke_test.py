import ast
from datetime import UTC, date, datetime, timedelta
import inspect as python_inspect
from threading import Event, Thread
import unittest
from unittest.mock import patch

import requests
from sqlalchemy import func, inspect, select

from models import (
    IdentityVerificationSession,
    TossIdentityVerificationEvidence,
    User,
    db,
)
from scripts import toss_identity_db_smoke_test as smoke
from services.identity_verification_provider import (
    IdentityVerificationProviderErrorCode,
)
from services.toss_identity_verification_evidence_writer import (
    TossIdentityVerificationEvidenceWriter,
)
from services.toss_identity_verification_provider import (
    TossIdentityVerificationProvider,
)
from services.toss_identity_verification_result_client import (
    TossIdentityVerificationResult,
    TossIdentityVerificationResultError,
    TossIdentityVerificationResultErrorCode,
)
from services.toss_identity_verification_start_client import (
    TossIdentityVerificationStartResult,
)
from services.toss_identity_verification_wiring import (
    TossIdentityVerificationDependencies,
)


ACTION_TIME = datetime(2026, 10, 9, 6, tzinfo=UTC)
CLIENT_ID = "test_synthetic-client-id"
CLIENT_SECRET = "test_synthetic-client-secret"
REQUEST_URL = "https://example.test/toss/complete"
RSA_PUBLIC_KEY = "synthetic-rsa-public-key"
TRANSACTION_ID = "synthetic-provider-transaction-sensitive"
AUTHENTICATION_URL = "https://example.test/auth-sensitive"
IDENTITY_SUBJECT = "synthetic-di-sensitive"
SIGNATURE = "synthetic-signature-sensitive"
HMAC_KEY = b"synthetic-db-smoke-hmac-key"


class StubStartClient:

    def __init__(self):
        self.call_count = 0

    def start_verification(self):
        self.call_count += 1
        return TossIdentityVerificationStartResult(
            provider_transaction_id=TRANSACTION_ID,
            authentication_url=AUTHENTICATION_URL,
        )


class StubResultClient:

    def __init__(self, *, error=None, entered=None, release=None):
        self.error = error
        self.entered = entered
        self.release = release
        self.call_count = 0
        self.received_transaction_ids = []

    def get_verified_identity(self, provider_transaction_id):
        self.call_count += 1
        self.received_transaction_ids.append(provider_transaction_id)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return TossIdentityVerificationResult(
            provider_transaction_id=provider_transaction_id,
            birth_date=date(2000, 1, 1),
            identity_subject=IDENTITY_SUBJECT,
            signature=SIGNATURE,
        )


class InvalidInsertEvidenceWriter(TossIdentityVerificationEvidenceWriter):

    def add_evidence(
        self,
        session,
        *,
        verification_session,
        verified_identity_result,
        action_time,
    ):
        evidence = TossIdentityVerificationEvidence(
            identity_verification_session_id=(
                verification_session.identity_verification_session_id
            ),
            provider_transaction_id=(
                verified_identity_result.provider_transaction_id
            ),
            signature="",
            created_at=action_time,
        )
        session.add(evidence)
        return evidence


class TossIdentityDbSmokeTest(unittest.TestCase):

    def setUp(self):
        self.applications = []
        self.http_guard = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError(
                "Unit tests must not make actual Toss HTTP requests."
            ),
        )
        self.http_guard.start()

    def tearDown(self):
        for application in reversed(self.applications):
            smoke.close_toss_identity_db_smoke_app(application)
        self.http_guard.stop()

    def make_config(self):
        return smoke.TossIdentityDbSmokeConfig(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            request_url=REQUEST_URL,
            rsa_public_key_base64=RSA_PUBLIC_KEY,
            http_timeout=5,
            token_refresh_skew=timedelta(seconds=30),
        )

    def make_dependencies(self, *, result_client=None, writer=None, key=HMAC_KEY):
        start_client = StubStartClient()
        result_client = result_client or StubResultClient()
        provider = TossIdentityVerificationProvider(
            start_client=start_client,
            result_client=result_client,
        )
        dependencies = TossIdentityVerificationDependencies(
            provider=provider,
            evidence_writer=writer or TossIdentityVerificationEvidenceWriter(),
            identity_subject_hmac_key=key,
        )
        return dependencies, start_client, result_client

    def make_app(self, *, result_client=None, writer=None, key=HMAC_KEY):
        dependencies, start_client, result_client = self.make_dependencies(
            result_client=result_client,
            writer=writer,
            key=key,
        )
        application = smoke.create_toss_identity_db_smoke_app(
            self.make_config(),
            dependencies=dependencies,
            action_time_factory=lambda: ACTION_TIME,
            testing=True,
        )
        self.applications.append(application)
        return application, start_client, result_client

    def start(self, application):
        with application.test_client() as client:
            return client.post("/start")

    def complete(self, application, verification_session_id, **payload):
        body = {"verification_session_id": verification_session_id}
        body.update(payload)
        with application.test_client() as client:
            return client.post("/complete", json=body)

    def get_started_id(self, application):
        response = self.start(application)
        self.assertEqual(response.status_code, 200)
        return response.get_json()["verification_session_id"]

    def test_temp_file_database_is_created_outside_project_and_cleaned(self):
        application, _, _ = self.make_app()
        owner = application.extensions["toss_identity_db_database_owner"]
        database_path = owner.path

        self.assertTrue(database_path.exists())
        self.assertEqual(database_path.name, "smoke.db")
        self.assertFalse(database_path.is_relative_to(smoke.PROJECT_ROOT))
        smoke.close_toss_identity_db_smoke_app(application)

        self.assertFalse(database_path.exists())

    def test_operating_database_path_is_rejected_before_initialization(self):
        with self.assertRaises(smoke.TossIdentityDbSmokeError) as context:
            smoke.ensure_safe_database_path(smoke.OPERATING_DATABASE_PATH)

        self.assertIs(
            context.exception.code,
            smoke.TossIdentityDbSmokeErrorCode.UNSAFE_DATABASE_PATH,
        )

    def test_factory_uses_orm_schema_without_alembic_or_app_import(self):
        source = python_inspect.getsource(smoke)
        tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        self.assertNotIn("app", imported_modules | imported_names)
        self.assertFalse(
            any(name.startswith("flask_migrate") for name in imported_modules)
        )
        self.assertNotIn("upgrade", source)

    def test_runtime_wiring_receives_exact_config_and_ephemeral_hmac_key(self):
        captured = {}
        dependencies, _, _ = self.make_dependencies()

        def dependency_builder(config, *, http_session):
            captured["config"] = config
            captured["http_session"] = http_session
            return dependencies

        with patch.object(smoke.secrets, "token_bytes", return_value=b"e" * 32):
            application = smoke.create_toss_identity_db_smoke_app(
                self.make_config(),
                dependency_builder=dependency_builder,
                testing=True,
            )
        self.applications.append(application)

        config = captured["config"]
        self.assertEqual(config.client_id, CLIENT_ID)
        self.assertEqual(config.client_secret, CLIENT_SECRET)
        self.assertEqual(config.request_url, REQUEST_URL)
        self.assertEqual(config.rsa_public_key_base64, RSA_PUBLIC_KEY)
        self.assertEqual(config.http_timeout, 5)
        self.assertEqual(config.token_refresh_skew, timedelta(seconds=30))
        self.assertEqual(config.identity_subject_hmac_key, b"e" * 32)
        self.assertIsNotNone(captured["http_session"])

    def test_start_uses_orchestration_and_persists_transaction_server_side(self):
        application, start_client, _ = self.make_app()

        response = self.start(application)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(start_client.call_count, 1)
        self.assertEqual(
            set(payload),
            {"txId", "authUrl", "verification_session_id"},
        )
        with application.app_context():
            stored = db.session.scalar(select(IdentityVerificationSession))
            self.assertEqual(stored.status, "pending")
            self.assertEqual(stored.provider_transaction_id, TRANSACTION_ID)
            self.assertEqual(
                stored.verification_session_id,
                payload["verification_session_id"],
            )

    def test_browser_handoff_is_transient_and_complete_sends_public_id_only(self):
        application, _, _ = self.make_app()
        page = application.test_client().get("/").get_data(as_text=True)

        self.assertIn(smoke.SDK_URL, page)
        self.assertIn("preparePopup()", page)
        self.assertIn("tossCert.start", page)
        self.assertIn("onSuccess", page)
        self.assertIn("verification_session_id: verificationSessionId", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("sessionStorage", page)
        self.assertNotIn("document.cookie", page)
        self.assertNotIn("console.", page)
        self.assertTrue(smoke.SDK_URL.startswith("https://cdn.toss.im/cert/v1"))

    def test_complete_rejects_client_transaction_id_without_provider_call(self):
        application, _, result_client = self.make_app()
        verification_session_id = self.get_started_id(application)

        response = self.complete(
            application,
            verification_session_id,
            txId="attacker-controlled-transaction",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(result_client.call_count, 0)

    def test_success_persists_verified_session_digest_and_one_evidence(self):
        application, _, result_client = self.make_app()
        verification_session_id = self.get_started_id(application)

        response = self.complete(application, verification_session_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "status": "verified",
                "checks": [
                    "Toss verification result: OK",
                    "Identity session verified: OK",
                    "Age eligibility: AGE_14_OR_OVER",
                    "Identity digest stored: OK",
                    "Toss evidence stored: OK",
                    "Atomic finalize: OK",
                ],
            },
        )
        self.assertEqual(result_client.received_transaction_ids, [TRANSACTION_ID])
        with application.app_context():
            stored = db.session.scalar(select(IdentityVerificationSession))
            evidence = db.session.scalar(select(TossIdentityVerificationEvidence))
            self.assertEqual(stored.status, "verified")
            self.assertEqual(stored.age_eligibility, "AGE_14_OR_OVER")
            self.assertRegex(stored.identity_subject_digest, r"^[0-9a-f]{64}$")
            self.assertIsNotNone(stored.verified_at)
            self.assertIsNone(stored.completion_claim_token)
            self.assertIsNone(stored.completion_claimed_at)
            self.assertIsNone(stored.failure_code)
            self.assertIsNone(stored.consumed_at)
            self.assertEqual(
                evidence.identity_verification_session_id,
                stored.identity_verification_session_id,
            )
            self.assertEqual(evidence.provider_transaction_id, TRANSACTION_ID)
            self.assertTrue(evidence.signature)
            self.assertIsNotNone(evidence.created_at)

    def test_success_persists_no_raw_identity_and_creates_no_user(self):
        application, _, _ = self.make_app()
        verification_session_id = self.get_started_id(application)
        self.complete(application, verification_session_id)

        with application.app_context():
            identity_columns = set()
            database_inspector = inspect(db.engine)
            for table_name in smoke._IDENTITY_TABLES:
                identity_columns.update(
                    column["name"].lower()
                    for column in database_inspector.get_columns(table_name)
                )
            self.assertTrue(
                identity_columns.isdisjoint(smoke._FORBIDDEN_IDENTITY_COLUMNS)
            )
            self.assertEqual(
                db.session.scalar(select(func.count()).select_from(User)),
                0,
            )
            stored = db.session.scalar(select(IdentityVerificationSession))
            evidence = db.session.scalar(select(TossIdentityVerificationEvidence))
            stored_text = " ".join(
                str(value)
                for value in (*stored.__dict__.values(), *evidence.__dict__.values())
            )
            self.assertNotIn(IDENTITY_SUBJECT, stored_text)
            self.assertNotIn("2000-01-01", stored_text)

    def test_evidence_insert_failure_rolls_back_verified_update_and_releases_claim(self):
        application, _, _ = self.make_app(writer=InvalidInsertEvidenceWriter())
        verification_session_id = self.get_started_id(application)

        response = self.complete(application, verification_session_id)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"]["code"], "FINALIZATION_FAILED")
        with application.app_context():
            stored = db.session.scalar(select(IdentityVerificationSession))
            self.assertEqual(stored.status, "pending")
            self.assertIsNone(stored.identity_subject_digest)
            self.assertIsNone(stored.verified_at)
            self.assertIsNone(stored.completion_claim_token)
            self.assertIsNone(stored.completion_claimed_at)
            self.assertEqual(
                db.session.scalar(
                    select(func.count()).select_from(TossIdentityVerificationEvidence)
                ),
                0,
            )

    def test_duplicate_complete_is_idempotent_without_second_result_query(self):
        application, _, result_client = self.make_app()
        verification_session_id = self.get_started_id(application)

        first = self.complete(application, verification_session_id)
        second = self.complete(application, verification_session_id)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(result_client.call_count, 1)
        with application.app_context():
            self.assertEqual(
                db.session.scalar(
                    select(func.count()).select_from(TossIdentityVerificationEvidence)
                ),
                1,
            )

    def test_concurrent_complete_uses_claim_and_queries_provider_once(self):
        entered = Event()
        release = Event()
        result_client = StubResultClient(entered=entered, release=release)
        application, _, result_client = self.make_app(result_client=result_client)
        verification_session_id = self.get_started_id(application)
        first_response = []

        def run_first():
            first_response.append(
                self.complete(application, verification_session_id)
            )

        thread = Thread(target=run_first)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        second = self.complete(application, verification_session_id)
        release.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_response[0].status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.get_json()["error"]["code"],
            "COMPLETION_IN_PROGRESS",
        )
        self.assertEqual(result_client.call_count, 1)

    def test_pending_releases_claim_without_evidence_or_automatic_retry(self):
        error = TossIdentityVerificationResultError(
            TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING
        )
        application, _, result_client = self.make_app(
            result_client=StubResultClient(error=error)
        )
        verification_session_id = self.get_started_id(application)

        response = self.complete(application, verification_session_id)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "VERIFICATION_PENDING")
        self.assertEqual(result_client.call_count, 1)
        with application.app_context():
            stored = db.session.scalar(select(IdentityVerificationSession))
            self.assertEqual(stored.status, "pending")
            self.assertIsNone(stored.failure_code)
            self.assertIsNone(stored.completion_claim_token)
            self.assertEqual(
                db.session.scalar(
                    select(func.count()).select_from(TossIdentityVerificationEvidence)
                ),
                0,
            )

    def test_age_restricted_is_failed_without_evidence_or_under_14_success(self):
        error = TossIdentityVerificationResultError(
            TossIdentityVerificationResultErrorCode.AGE_RESTRICTED
        )
        application, _, _ = self.make_app(result_client=StubResultClient(error=error))
        verification_session_id = self.get_started_id(application)

        response = self.complete(application, verification_session_id)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "AGE_RESTRICTED")
        self.assertNotIn("UNDER_14", response.get_data(as_text=True))
        with application.app_context():
            stored = db.session.scalar(select(IdentityVerificationSession))
            self.assertEqual(stored.status, "failed")
            self.assertEqual(stored.failure_code, "age_restricted")
            self.assertIsNone(stored.completion_claim_token)
            self.assertEqual(
                db.session.scalar(
                    select(func.count()).select_from(TossIdentityVerificationEvidence)
                ),
                0,
            )

    def test_responses_do_not_cache_set_cookie_or_disclose_sensitive_values(self):
        application, _, _ = self.make_app()
        start_response = self.start(application)
        verification_session_id = start_response.get_json()[
            "verification_session_id"
        ]
        complete_response = self.complete(application, verification_session_id)

        for response in (application.test_client().get("/"), complete_response):
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("Set-Cookie", response.headers)
        safe_output = complete_response.get_data(as_text=True)
        for sensitive_value in (
            TRANSACTION_ID,
            AUTHENTICATION_URL,
            verification_session_id,
            IDENTITY_SUBJECT,
            SIGNATURE,
            HMAC_KEY.decode("ascii"),
        ):
            self.assertNotIn(sensitive_value, safe_output)

    def test_server_is_localhost_only_without_debug_and_cleans_database(self):
        application, _, _ = self.make_app()
        owner = application.extensions["toss_identity_db_database_owner"]
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)

        result = smoke.run_toss_identity_db_smoke_server(
            application,
            output=lambda _message: None,
            server_runner=runner,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [{
                "host": "127.0.0.1",
                "port": 5057,
                "debug": False,
                "use_reloader": False,
            }],
        )
        self.assertFalse(application.debug)
        self.assertFalse(owner.path.exists())

    def test_pytest_flow_uses_no_http_and_main_loads_dotenv_without_override(self):
        application, _, _ = self.make_app()
        self.get_started_id(application)
        with patch.object(smoke, "load_dotenv") as load_environment, patch.object(
            smoke,
            "build_toss_identity_db_smoke_app_from_environ",
            side_effect=smoke.TossIdentityDbSmokeError(
                smoke.TossIdentityDbSmokeErrorCode.INVALID_CONFIGURATION
            ),
        ):
            result = smoke.main(environ={}, output=lambda _message: None)

        self.assertEqual(result, 1)
        load_environment.assert_called_once_with(
            smoke.PROJECT_ROOT / ".env",
            override=False,
        )

    def test_config_repr_and_failures_hide_credentials_and_keys(self):
        representation = repr(self.make_config())
        for sensitive_value in (
            CLIENT_ID,
            CLIENT_SECRET,
            REQUEST_URL,
            RSA_PUBLIC_KEY,
        ):
            self.assertNotIn(sensitive_value, representation)

        provider_code = IdentityVerificationProviderErrorCode.AGE_RESTRICTED
        self.assertNotIn(IDENTITY_SUBJECT, repr(provider_code))


if __name__ == "__main__":
    unittest.main()
