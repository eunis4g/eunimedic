import ast
from base64 import b64encode
from dataclasses import FrozenInstanceError
from datetime import timedelta
import inspect
import math
import unittest

from Crypto.PublicKey import RSA

import services.toss_identity_verification_wiring as wiring_service
from services.toss_cert_access_token_client import TossCertAccessTokenClient
from services.toss_cert_crypto import (
    TossCertCryptoError,
    TossCertCryptoSessionGenerator,
)
from services.toss_identity_verification_evidence_writer import (
    TossIdentityVerificationEvidenceWriter,
)
from services.toss_identity_verification_provider import (
    TossIdentityVerificationProvider,
)
from services.toss_identity_verification_result_client import (
    TossIdentityVerificationResultClient,
)
from services.toss_identity_verification_start_client import (
    TossIdentityVerificationStartClient,
)
from services.toss_identity_verification_wiring import (
    TossIdentityVerificationConfig,
    TossIdentityVerificationDependencies,
    build_toss_identity_verification_dependencies,
)


CLIENT_ID = "synthetic-client-id"
CLIENT_SECRET = "synthetic-client-secret"
REQUEST_URL = "https://app.example.test/toss/complete"
HMAC_KEY = b"synthetic-identity-subject-hmac-key"
HTTP_TIMEOUT = 4.5
TOKEN_REFRESH_SKEW = timedelta(seconds=30)


class NoCallHttpSession:

    def __init__(self):
        self.post_call_count = 0

    def post(self, *args, **kwargs):
        self.post_call_count += 1
        raise AssertionError("Wiring creation must not make HTTP requests.")


class TossIdentityVerificationWiringTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        public_key_der = (
            RSA.generate(2048).public_key().export_key(format="DER")
        )
        cls.public_key_base64 = b64encode(public_key_der).decode("ascii")

    def make_config(self, **overrides):
        values = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "request_url": REQUEST_URL,
            "rsa_public_key_base64": self.public_key_base64,
            "http_timeout": HTTP_TIMEOUT,
            "token_refresh_skew": TOKEN_REFRESH_SKEW,
            "identity_subject_hmac_key": HMAC_KEY,
        }
        values.update(overrides)
        return TossIdentityVerificationConfig(**values)

    def build(self, config=None):
        http_session = NoCallHttpSession()
        dependencies = build_toss_identity_verification_dependencies(
            config or self.make_config(),
            http_session=http_session,
        )
        return dependencies, http_session

    def test_valid_config_is_immutable(self):
        config = self.make_config()

        with self.assertRaises(FrozenInstanceError):
            config.client_id = "replacement"

    def test_valid_config_preserves_explicit_boundary_values(self):
        config = self.make_config()

        self.assertEqual(config.client_id, CLIENT_ID)
        self.assertEqual(config.client_secret, CLIENT_SECRET)
        self.assertEqual(config.request_url, REQUEST_URL)
        self.assertEqual(
            config.rsa_public_key_base64,
            self.public_key_base64,
        )
        self.assertEqual(config.http_timeout, HTTP_TIMEOUT)
        self.assertEqual(config.token_refresh_skew, TOKEN_REFRESH_SKEW)
        self.assertIs(config.identity_subject_hmac_key, HMAC_KEY)

    def test_required_string_configuration_is_rejected(self):
        for field_name in (
            "client_id",
            "client_secret",
            "request_url",
            "rsa_public_key_base64",
        ):
            for invalid_value in (None, "", "   "):
                with self.subTest(
                    field_name=field_name,
                    invalid_value=invalid_value,
                ):
                    with self.assertRaises((TypeError, ValueError)):
                        self.make_config(**{field_name: invalid_value})

    def test_hmac_key_requires_non_empty_bytes(self):
        for invalid_value in (None, "text-key", b""):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises((TypeError, ValueError)):
                    self.make_config(
                        identity_subject_hmac_key=invalid_value
                    )

    def test_timeout_requires_positive_finite_number(self):
        for invalid_value in (
            None,
            True,
            0,
            -1,
            math.inf,
            -math.inf,
            math.nan,
        ):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises((TypeError, ValueError)):
                    self.make_config(http_timeout=invalid_value)

    def test_refresh_skew_requires_non_negative_timedelta(self):
        for invalid_value in (None, 30, timedelta(seconds=-1)):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises((TypeError, ValueError)):
                    self.make_config(token_refresh_skew=invalid_value)

    def test_factory_rejects_non_config_value(self):
        with self.assertRaises(TypeError):
            build_toss_identity_verification_dependencies({})

    def test_invalid_rsa_key_fails_during_wiring_without_http(self):
        http_session = NoCallHttpSession()

        with self.assertRaises(TossCertCryptoError) as context:
            build_toss_identity_verification_dependencies(
                self.make_config(rsa_public_key_base64="not-a-key"),
                http_session=http_session,
            )

        self.assertEqual(http_session.post_call_count, 0)
        self.assertNotIn("not-a-key", repr(context.exception))

    def test_bundle_contains_provider_writer_and_hmac_key(self):
        dependencies, _ = self.build()

        self.assertIsInstance(
            dependencies,
            TossIdentityVerificationDependencies,
        )
        self.assertIsInstance(
            dependencies.provider,
            TossIdentityVerificationProvider,
        )
        self.assertIsInstance(
            dependencies.evidence_writer,
            TossIdentityVerificationEvidenceWriter,
        )
        self.assertIs(
            dependencies.identity_subject_hmac_key,
            HMAC_KEY,
        )

    def test_evidence_writer_is_created_without_a_db_session(self):
        dependencies, _ = self.build()

        self.assertEqual(
            dict(
                inspect.signature(
                    type(dependencies.evidence_writer)
                ).parameters
            ),
            {},
        )

    def test_bundle_is_immutable(self):
        dependencies, _ = self.build()

        with self.assertRaises(FrozenInstanceError):
            dependencies.provider = None

    def test_provider_uses_start_and_result_clients(self):
        dependencies, _ = self.build()
        provider = dependencies.provider

        self.assertIsInstance(
            provider._start_client,
            TossIdentityVerificationStartClient,
        )
        self.assertIsInstance(
            provider._result_client,
            TossIdentityVerificationResultClient,
        )

    def test_start_and_result_clients_share_one_token_client(self):
        dependencies, _ = self.build()
        start_client = dependencies.provider._start_client
        result_client = dependencies.provider._result_client

        self.assertIsInstance(
            start_client._access_token_client,
            TossCertAccessTokenClient,
        )
        self.assertIs(
            start_client._access_token_client,
            result_client._access_token_client,
        )

    def test_factory_does_not_precreate_or_cache_access_token(self):
        dependencies, _ = self.build()
        token_client = (
            dependencies.provider._start_client._access_token_client
        )

        self.assertIsNone(token_client._cached_token)

    def test_result_client_reuses_generator_not_crypto_session(self):
        dependencies, _ = self.build()
        result_client = dependencies.provider._result_client
        generator = result_client._crypto_session_generator

        self.assertIsInstance(generator, TossCertCryptoSessionGenerator)
        self.assertFalse(hasattr(generator, "crypto_session"))
        self.assertFalse(hasattr(generator, "_crypto_session"))

    def test_request_url_and_common_timeout_are_wired_exactly(self):
        dependencies, _ = self.build()
        start_client = dependencies.provider._start_client
        result_client = dependencies.provider._result_client
        token_client = start_client._access_token_client

        self.assertEqual(start_client._request_url, REQUEST_URL)
        self.assertEqual(token_client._timeout, HTTP_TIMEOUT)
        self.assertEqual(start_client._timeout, HTTP_TIMEOUT)
        self.assertEqual(result_client._timeout, HTTP_TIMEOUT)
        self.assertEqual(token_client._refresh_skew, TOKEN_REFRESH_SKEW)

    def test_explicit_http_session_is_shared_without_being_called(self):
        dependencies, http_session = self.build()
        start_client = dependencies.provider._start_client
        result_client = dependencies.provider._result_client
        token_client = start_client._access_token_client

        self.assertIs(token_client._http_session, http_session)
        self.assertIs(start_client._http_session, http_session)
        self.assertIs(result_client._http_session, http_session)
        self.assertEqual(http_session.post_call_count, 0)

    def test_each_factory_call_builds_a_distinct_dependency_graph(self):
        first, _ = self.build()
        second, _ = self.build()

        self.assertIsNot(first, second)
        self.assertIsNot(first.provider, second.provider)
        self.assertIsNot(
            first.provider._start_client._access_token_client,
            second.provider._start_client._access_token_client,
        )

    def test_config_repr_redacts_all_credentials_and_keys(self):
        config = self.make_config()
        representation = repr(config)

        for sensitive_value in (
            CLIENT_ID,
            CLIENT_SECRET,
            REQUEST_URL,
            self.public_key_base64,
            HMAC_KEY.decode("ascii"),
        ):
            with self.subTest(sensitive_value=sensitive_value[:20]):
                self.assertNotIn(sensitive_value, representation)

    def test_dependency_repr_redacts_hmac_and_nested_credentials(self):
        dependencies, _ = self.build()
        representation = repr(dependencies)

        self.assertNotIn(CLIENT_ID, representation)
        self.assertNotIn(CLIENT_SECRET, representation)
        self.assertNotIn(HMAC_KEY.decode("ascii"), representation)
        self.assertNotIn(self.public_key_base64, representation)

    def test_wiring_creation_performs_no_http_or_crypto_generation(self):
        dependencies, http_session = self.build()
        generator = (
            dependencies.provider
            ._result_client
            ._crypto_session_generator
        )

        self.assertEqual(http_session.post_call_count, 0)
        self.assertFalse(hasattr(generator, "_cached_session"))

    def test_wiring_module_has_no_flask_sqlalchemy_or_model_dependency(self):
        source = inspect.getsource(wiring_service)
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
        self.assertNotIn("flask_sqlalchemy", imported_roots)
        self.assertNotIn("sqlalchemy", imported_roots)
        self.assertNotIn("models", imported_roots)
        self.assertNotIn("app", imported_roots)

    def test_production_module_contains_no_synthetic_credentials(self):
        source = inspect.getsource(wiring_service)

        self.assertNotIn(CLIENT_ID, source)
        self.assertNotIn(CLIENT_SECRET, source)
        self.assertNotIn(HMAC_KEY.decode("ascii"), source)


if __name__ == "__main__":
    unittest.main()
