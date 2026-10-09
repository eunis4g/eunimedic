import ast
from base64 import b64encode
from datetime import UTC, date, datetime, timedelta
import inspect as python_inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from sqlalchemy import create_engine, func, select

import app as app_module
import routes.api_v1 as api_routes
from models import (
    IdentityVerificationSession,
    TossIdentityVerificationEvidence,
    User,
    db,
)
from services.identity_verification_service import (
    IdentityVerificationServiceError,
    IdentityVerificationServiceErrorCode,
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
    TossIdentityVerificationConfigurationError,
    TossIdentityVerificationConfigurationErrorCode,
    TossIdentityVerificationDependencies,
    read_toss_identity_verification_config,
)


START_URL = "/api/v1/identity-verifications"
TRANSACTION_PREFIX = "synthetic-api-transaction-sensitive"
AUTHENTICATION_URL = "https://example.test/auth-sensitive"
IDENTITY_SUBJECT = "synthetic-api-di-sensitive"
SIGNATURE = "synthetic-api-signature-sensitive"
HMAC_KEY = b"synthetic-stable-api-hmac-key"


class StubStartClient:

    def __init__(self):
        self.call_count = 0

    def start_verification(self):
        self.call_count += 1
        return TossIdentityVerificationStartResult(
            provider_transaction_id=(
                f"{TRANSACTION_PREFIX}-{self.call_count}"
            ),
            authentication_url=(
                f"{AUTHENTICATION_URL}/{self.call_count}"
            ),
        )


class StubResultClient:

    def __init__(self, *, birth_date=date(2000, 1, 1), error=None):
        self.birth_date = birth_date
        self.error = error
        self.call_count = 0
        self.received_transaction_ids = []

    def get_verified_identity(self, provider_transaction_id):
        self.call_count += 1
        self.received_transaction_ids.append(provider_transaction_id)
        if self.error is not None:
            raise self.error
        return TossIdentityVerificationResult(
            provider_transaction_id=provider_transaction_id,
            birth_date=self.birth_date,
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


class FutureDateTime:

    @classmethod
    def now(cls, timezone):
        return datetime.now(UTC) + timedelta(days=1)


class ApiV1IdentityVerificationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = (
            Path(cls.temporary_directory.name)
            / "api-v1-identity-verification.db"
        )
        cls.application = app_module.app
        cls.original_testing = cls.application.config["TESTING"]
        cls.original_dependencies = cls.application.extensions[
            api_routes.API_V1_DEPENDENCIES_EXTENSION_KEY
        ]
        cls.application.config.update(TESTING=True)

        with cls.application.app_context():
            db.session.remove()
            cls.original_engine = db.engines[None]
            cls.test_engine = create_engine(
                "sqlite:///" + cls.database_path.as_posix()
            )
            db.engines[None] = cls.test_engine

    @classmethod
    def tearDownClass(cls):
        with cls.application.app_context():
            db.session.remove()
            db.drop_all()
            db.session.remove()
            db.engines[None] = cls.original_engine

        cls.application.extensions[
            api_routes.API_V1_DEPENDENCIES_EXTENSION_KEY
        ] = cls.original_dependencies
        cls.application.config["TESTING"] = cls.original_testing
        cls.test_engine.dispose()
        cls.temporary_directory.cleanup()

    def setUp(self):
        self.application_context = self.application.app_context()
        self.application_context.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.http_guard = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError(
                "API tests must not make actual Toss HTTP requests."
            ),
        )
        self.http_guard.start()
        self.dependencies, self.start_client, self.result_client = (
            self.make_dependencies()
        )
        self.application.extensions[
            api_routes.API_V1_DEPENDENCIES_EXTENSION_KEY
        ] = self.dependencies
        self.client = self.application.test_client()

    def tearDown(self):
        self.http_guard.stop()
        db.session.remove()
        self.application_context.pop()

    def make_dependencies(
        self,
        *,
        birth_date=date(2000, 1, 1),
        result_error=None,
        writer=None,
    ):
        start_client = StubStartClient()
        result_client = StubResultClient(
            birth_date=birth_date,
            error=result_error,
        )
        dependencies = TossIdentityVerificationDependencies(
            provider=TossIdentityVerificationProvider(
                start_client=start_client,
                result_client=result_client,
            ),
            evidence_writer=(
                writer or TossIdentityVerificationEvidenceWriter()
            ),
            identity_subject_hmac_key=HMAC_KEY,
        )
        return dependencies, start_client, result_client

    def install_dependencies(self, **options):
        dependencies, start_client, result_client = self.make_dependencies(
            **options
        )
        self.application.extensions[
            api_routes.API_V1_DEPENDENCIES_EXTENSION_KEY
        ] = dependencies
        self.dependencies = dependencies
        self.start_client = start_client
        self.result_client = result_client

    def start(self, *, payload=None, **kwargs):
        return self.client.post(
            START_URL,
            json={} if payload is None else payload,
            **kwargs,
        )

    def complete(self, verification_session_id, *, payload=None, **kwargs):
        return self.client.post(
            f"{START_URL}/{verification_session_id}/complete",
            json={} if payload is None else payload,
            **kwargs,
        )

    def start_and_get_id(self):
        response = self.start()
        self.assertEqual(response.status_code, 201)
        return response.get_json()["verification_session_id"]

    def reset_database(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

    def test_start_returns_201_minimal_handoff_and_utc_expiry(self):
        response = self.start()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.content_type, "application/json")
        payload = response.get_json()
        self.assertEqual(
            set(payload),
            {
                "verification_session_id",
                "provider_transaction_id",
                "authentication_url",
                "expires_at",
            },
        )
        self.assertEqual(
            payload["provider_transaction_id"],
            f"{TRANSACTION_PREFIX}-1",
        )
        self.assertEqual(
            payload["authentication_url"],
            f"{AUTHENTICATION_URL}/1",
        )
        self.assertTrue(payload["expires_at"].endswith("Z"))
        parsed_expiry = datetime.fromisoformat(
            payload["expires_at"].replace("Z", "+00:00")
        )
        self.assertEqual(parsed_expiry.utcoffset(), timedelta(0))

    def test_start_calls_orchestration_and_persists_server_transaction(self):
        with patch.object(
            api_routes,
            "start_identity_verification",
            wraps=api_routes.start_identity_verification,
        ) as orchestration:
            response = self.start()

        public_id = response.get_json()["verification_session_id"]
        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(orchestration.call_count, 1)
        self.assertIs(orchestration.call_args.args[0], db.session)
        self.assertIs(
            orchestration.call_args.kwargs["provider"],
            self.dependencies.provider,
        )
        self.assertEqual(stored.verification_session_id, public_id)
        self.assertEqual(
            stored.provider_transaction_id,
            f"{TRANSACTION_PREFIX}-1",
        )
        self.assertEqual(stored.status, "pending")

    def test_start_is_pre_auth_and_api_blueprint_is_csrf_exempt(self):
        response = self.start()

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("WWW-Authenticate", response.headers)
        self.assertEqual(self.client.post("/login", data={}).status_code, 400)
        self.assertEqual(self.client.post("/logout").status_code, 400)

    def test_start_rejects_non_json_malformed_and_unexpected_fields(self):
        non_json = self.client.post(START_URL)
        malformed = self.client.post(
            START_URL,
            data="{",
            content_type="application/json",
        )
        unexpected = self.start(payload={"birth_date": "2000-01-01"})

        self.assertEqual(non_json.status_code, 415)
        self.assertEqual(
            non_json.get_json()["error"]["code"],
            "UNSUPPORTED_MEDIA_TYPE",
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(unexpected.status_code, 400)
        self.assertEqual(self.start_client.call_count, 0)

    def test_complete_uses_path_id_and_returns_minimal_domain_result(self):
        public_id = self.start_and_get_id()
        with patch.object(
            api_routes,
            "complete_identity_verification",
            wraps=api_routes.complete_identity_verification,
        ) as orchestration:
            response = self.complete(public_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "verification_session_id": public_id,
                "status": "verified",
                "age_eligibility": "AGE_14_OR_OVER",
            },
        )
        self.assertEqual(orchestration.call_count, 1)
        self.assertEqual(
            orchestration.call_args.kwargs["verification_session_id"],
            public_id,
        )
        self.assertNotIn(
            "provider_transaction_id",
            orchestration.call_args.kwargs,
        )
        self.assertEqual(
            self.result_client.received_transaction_ids,
            [f"{TRANSACTION_PREFIX}-1"],
        )

    def test_complete_rejects_client_txid_and_sensitive_fields(self):
        public_id = self.start_and_get_id()
        for field_name in (
            "txId",
            "provider_transaction_id",
            "birthDate",
            "birth_date",
            "di",
            "signature",
        ):
            with self.subTest(field_name=field_name):
                response = self.complete(
                    public_id,
                    payload={field_name: "attacker-controlled"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json()["error"]["code"],
                    "INVALID_REQUEST",
                )
        self.assertEqual(self.result_client.call_count, 0)

    def test_success_persists_verified_session_and_toss_evidence(self):
        public_id = self.start_and_get_id()

        response = self.complete(public_id)

        stored = db.session.scalar(select(IdentityVerificationSession))
        evidence = db.session.scalar(select(TossIdentityVerificationEvidence))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(stored.status, "verified")
        self.assertEqual(stored.age_eligibility, "AGE_14_OR_OVER")
        self.assertRegex(stored.identity_subject_digest, r"^[0-9a-f]{64}$")
        self.assertIsNone(stored.completion_claim_token)
        self.assertIsNone(stored.completion_claimed_at)
        self.assertIsNone(stored.failure_code)
        self.assertIsNone(stored.consumed_at)
        self.assertEqual(
            evidence.identity_verification_session_id,
            stored.identity_verification_session_id,
        )
        self.assertEqual(
            evidence.provider_transaction_id,
            stored.provider_transaction_id,
        )
        self.assertEqual(evidence.signature, SIGNATURE)

    def test_complete_response_excludes_all_identity_and_evidence_values(self):
        public_id = self.start_and_get_id()

        response = self.complete(public_id)
        response_text = response.get_data(as_text=True)

        for forbidden_value in (
            f"{TRANSACTION_PREFIX}-1",
            f"{AUTHENTICATION_URL}/1",
            IDENTITY_SUBJECT,
            SIGNATURE,
            HMAC_KEY.decode("ascii"),
            "2000-01-01",
        ):
            self.assertNotIn(forbidden_value, response_text)
        for forbidden_field in (
            "birth_date",
            "birthday",
            "di",
            "ci",
            "identity_subject",
            "identity_subject_digest",
            "signature",
            "provider_transaction_id",
            "authentication_url",
        ):
            self.assertNotIn(forbidden_field, response.get_json())

    def test_complete_does_not_create_user_or_consume_session(self):
        public_id = self.start_and_get_id()
        self.complete(public_id)

        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(User)),
            0,
        )
        self.assertEqual(stored.status, "verified")
        self.assertIsNone(stored.consumed_at)

    def test_under_14_is_returned_as_verified_without_user_creation(self):
        self.install_dependencies(birth_date=date(2020, 1, 1))
        public_id = self.start_and_get_id()

        response = self.complete(public_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["age_eligibility"],
            "UNDER_14",
        )
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(User)),
            0,
        )

    def test_pending_returns_stable_retryable_error_and_releases_claim(self):
        self.install_dependencies(
            result_error=TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode
                .VERIFICATION_PENDING
            )
        )
        public_id = self.start_and_get_id()

        response = self.complete(public_id)

        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "VERIFICATION_NOT_COMPLETED",
        )
        self.assertNotIn("Retry-After", response.headers)
        self.assertEqual(self.result_client.call_count, 1)
        self.assertEqual(stored.status, "pending")
        self.assertIsNone(stored.completion_claim_token)
        self.assertEqual(
            db.session.scalar(
                select(func.count()).select_from(
                    TossIdentityVerificationEvidence
                )
            ),
            0,
        )

    def test_age_restricted_returns_stable_error_and_failed_session(self):
        self.install_dependencies(
            result_error=TossIdentityVerificationResultError(
                TossIdentityVerificationResultErrorCode.AGE_RESTRICTED
            )
        )
        public_id = self.start_and_get_id()

        response = self.complete(public_id)

        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "AGE_REQUIREMENT_NOT_MET",
        )
        self.assertNotIn("CE3006", response.get_data(as_text=True))
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure_code, "age_restricted")

    def test_expired_session_returns_410_without_provider_query(self):
        public_id = self.start_and_get_id()

        with patch.object(api_routes, "datetime", FutureDateTime):
            response = self.complete(public_id)

        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(response.status_code, 410)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "VERIFICATION_EXPIRED",
        )
        self.assertEqual(self.result_client.call_count, 0)
        self.assertEqual(stored.status, "expired")

    def test_unknown_session_returns_stable_404(self):
        response = self.complete(
            "00000000-0000-4000-8000-000000000099"
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "SESSION_NOT_FOUND",
        )

    def test_service_error_mapping_is_stable(self):
        cases = (
            (
                IdentityVerificationServiceErrorCode
                .COMPLETION_IN_PROGRESS,
                409,
            ),
            (
                IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT,
                409,
            ),
            (
                IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE,
                503,
            ),
            (
                IdentityVerificationServiceErrorCode
                .INVALID_PROVIDER_RESPONSE,
                502,
            ),
            (
                IdentityVerificationServiceErrorCode.FINALIZATION_FAILED,
                500,
            ),
        )
        public_id = "00000000-0000-4000-8000-000000000099"
        for code, status_code in cases:
            with self.subTest(code=code):
                with patch.object(
                    api_routes,
                    "complete_identity_verification",
                    side_effect=IdentityVerificationServiceError(code),
                ):
                    response = self.complete(public_id)
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(
                    response.get_json()["error"]["code"],
                    code.value,
                )
                self.assertEqual(
                    set(response.get_json()["error"]),
                    {"code", "message"},
                )

    def test_evidence_insert_failure_rolls_back_verified_update(self):
        self.install_dependencies(writer=InvalidInsertEvidenceWriter())
        public_id = self.start_and_get_id()

        response = self.complete(public_id)

        stored = db.session.scalar(select(IdentityVerificationSession))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "FINALIZATION_FAILED",
        )
        self.assertEqual(stored.status, "pending")
        self.assertIsNone(stored.identity_subject_digest)
        self.assertIsNone(stored.completion_claim_token)
        self.assertEqual(
            db.session.scalar(
                select(func.count()).select_from(
                    TossIdentityVerificationEvidence
                )
            ),
            0,
        )

    def test_unexpected_db_error_is_sanitized(self):
        raw_error = "UNIQUE constraint raw-sql-sensitive"
        with patch.object(
            api_routes,
            "complete_identity_verification",
            side_effect=RuntimeError(raw_error),
        ):
            response = self.complete(
                "00000000-0000-4000-8000-000000000099"
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "INTERNAL_SERVER_ERROR",
        )
        self.assertNotIn(raw_error, response.get_data(as_text=True))

    def test_dependency_bundle_is_reused_across_requests(self):
        installed = self.application.extensions[
            api_routes.API_V1_DEPENDENCIES_EXTENSION_KEY
        ]

        first = self.start()
        second = self.start()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertIs(
            self.application.extensions[
                api_routes.API_V1_DEPENDENCIES_EXTENSION_KEY
            ],
            installed,
        )
        self.assertEqual(self.start_client.call_count, 2)

    def test_api_does_not_enable_cors_wildcard(self):
        response = self.start()

        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_route_source_contains_no_auth_tokens_or_domain_reimplementation(self):
        source = python_inspect.getsource(api_routes)
        tree = ast.parse(source)
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        }

        self.assertIn("start_identity_verification", called_names)
        self.assertIn("complete_identity_verification", called_names)
        for forbidden_text in (
            "login_required",
            "Bearer",
            "refresh_token",
            "access_token",
            "check_age_eligibility",
            "identity_subject_digest",
            "TossIdentityVerificationResultClient",
        ):
            self.assertNotIn(forbidden_text, source)

    def test_config_strictly_decodes_stable_hmac_key_and_hides_values(self):
        environ = self.make_environ()

        config = read_toss_identity_verification_config(environ)
        representation = repr(config)

        self.assertEqual(config.identity_subject_hmac_key, HMAC_KEY)
        for secret_value in (
            environ["TOSS_CERT_CLIENT_ID"],
            environ["TOSS_CERT_CLIENT_SECRET"],
            environ["TOSS_IDENTITY_REQUEST_URL"],
            environ["TOSS_CERT_RSA_PUBLIC_KEY_BASE64"],
            environ["IDENTITY_SUBJECT_HMAC_KEY_BASE64"],
            HMAC_KEY.decode("ascii"),
        ):
            self.assertNotIn(secret_value, representation)

    def test_hmac_key_missing_blank_and_invalid_base64_are_rejected_safely(self):
        invalid_values = (None, "", "   ", "not-base64-sensitive!")
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                environ = self.make_environ()
                if invalid_value is None:
                    del environ["IDENTITY_SUBJECT_HMAC_KEY_BASE64"]
                else:
                    environ["IDENTITY_SUBJECT_HMAC_KEY_BASE64"] = (
                        invalid_value
                    )
                with self.assertRaises(
                    TossIdentityVerificationConfigurationError
                ) as context:
                    read_toss_identity_verification_config(environ)
                error_text = repr(context.exception)
                if invalid_value:
                    self.assertNotIn(invalid_value, error_text)
                self.assertNotIn(HMAC_KEY.decode("ascii"), error_text)
                self.assertIn(
                    context.exception.code,
                    {
                        TossIdentityVerificationConfigurationErrorCode
                        .MISSING_CONFIGURATION,
                        TossIdentityVerificationConfigurationErrorCode
                        .INVALID_HMAC_KEY,
                    },
                )

    def test_app_stores_dependency_bundle_and_exempts_only_api_blueprint(self):
        dependencies = self.application.extensions[
            api_routes.API_V1_DEPENDENCIES_EXTENSION_KEY
        ]

        self.assertIsInstance(
            dependencies,
            TossIdentityVerificationDependencies,
        )
        self.assertIn(
            api_routes.api_v1_blueprint,
            app_module.csrf._exempt_blueprints,
        )
        self.assertEqual(self.client.post("/logout").status_code, 400)

    @staticmethod
    def make_environ(**overrides):
        environ = {
            "TOSS_CERT_CLIENT_ID": "synthetic-client-id",
            "TOSS_CERT_CLIENT_SECRET": "synthetic-client-secret",
            "TOSS_IDENTITY_REQUEST_URL": (
                "https://example.test/toss/complete"
            ),
            "TOSS_CERT_RSA_PUBLIC_KEY_BASE64": "synthetic-rsa-key",
            "TOSS_HTTP_TIMEOUT_SECONDS": "5",
            "TOSS_TOKEN_REFRESH_SKEW_SECONDS": "30",
            "IDENTITY_SUBJECT_HMAC_KEY_BASE64": b64encode(
                HMAC_KEY
            ).decode("ascii"),
        }
        environ.update(overrides)
        return environ


if __name__ == "__main__":
    unittest.main()
