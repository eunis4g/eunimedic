import ast
from dataclasses import FrozenInstanceError
from datetime import date
import inspect
import unittest

from services.identity_verification_provider import (
    IdentityVerificationProvider,
    IdentityVerificationProviderError,
    IdentityVerificationProviderErrorCode,
    IdentityVerificationProviderName,
    IdentityVerificationStartResult,
    VerifiedIdentityResult,
)
from services.toss_identity_verification_result_client import (
    TossIdentityVerificationResult,
    TossIdentityVerificationResultError,
    TossIdentityVerificationResultErrorCode,
)
from services.toss_identity_verification_start_client import (
    TossIdentityVerificationStartError,
    TossIdentityVerificationStartErrorCode,
    TossIdentityVerificationStartResult,
)
import services.toss_identity_verification_provider as provider_service
from services.toss_identity_verification_provider import (
    TossIdentityVerificationProvider,
    TossVerifiedIdentityResult,
)


TRANSACTION_ID = "synthetic-provider-transaction-sensitive"
AUTHENTICATION_URL = "https://auth.example.test/sensitive-handoff"
IDENTITY_SUBJECT = "synthetic-identity-subject-sensitive"
SIGNATURE = "synthetic-signature-sensitive"
BIRTH_DATE = date(2000, 1, 2)
RAW_TOSS_DATA = "raw-toss-error-sensitive"


class StubStartClient:

    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.call_count = 0

    def start_verification(self):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.result


class StubResultClient:

    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.call_count = 0
        self.transaction_ids = []

    def get_verified_identity(self, provider_transaction_id):
        self.call_count += 1
        self.transaction_ids.append(provider_transaction_id)
        if self.error is not None:
            raise self.error
        return self.result


def valid_start_result():
    return TossIdentityVerificationStartResult(
        provider_transaction_id=TRANSACTION_ID,
        authentication_url=AUTHENTICATION_URL,
    )


def valid_result():
    return TossIdentityVerificationResult(
        provider_transaction_id=TRANSACTION_ID,
        birth_date=BIRTH_DATE,
        identity_subject=IDENTITY_SUBJECT,
        signature=SIGNATURE,
    )


class TossIdentityVerificationProviderTest(unittest.TestCase):

    def test_implements_provider_contract(self):
        self.assertIsInstance(self.make_provider()[0], IdentityVerificationProvider)

    def test_provider_name_is_toss(self):
        provider, _, _ = self.make_provider()
        self.assertIs(provider.provider, IdentityVerificationProviderName.TOSS)

    def test_requires_start_client_contract(self):
        for value in (None, object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    TossIdentityVerificationProvider(
                        start_client=value,
                        result_client=StubResultClient(result=valid_result()),
                    )

    def test_requires_result_client_contract(self):
        for value in (None, object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    TossIdentityVerificationProvider(
                        start_client=StubStartClient(
                            result=valid_start_result()
                        ),
                        result_client=value,
                    )

    def test_start_calls_start_client_exactly_once(self):
        provider, start_client, _ = self.make_provider()
        provider.start_verification()
        self.assertEqual(start_client.call_count, 1)

    def test_start_returns_provider_neutral_result(self):
        provider, _, _ = self.make_provider()
        result = provider.start_verification()
        self.assertIs(type(result), IdentityVerificationStartResult)
        self.assertIs(result.provider, IdentityVerificationProviderName.TOSS)

    def test_start_preserves_transaction_id(self):
        provider, _, _ = self.make_provider()
        self.assertEqual(
            provider.start_verification().provider_transaction_id,
            TRANSACTION_ID,
        )

    def test_start_preserves_authentication_url(self):
        provider, _, _ = self.make_provider()
        self.assertEqual(
            provider.start_verification().authentication_url,
            AUTHENTICATION_URL,
        )

    def test_start_does_not_call_result_client(self):
        provider, _, result_client = self.make_provider()
        provider.start_verification()
        self.assertEqual(result_client.call_count, 0)

    def test_get_calls_result_client_exactly_once(self):
        provider, _, result_client = self.make_provider()
        provider.get_verified_identity(provider_transaction_id=TRANSACTION_ID)
        self.assertEqual(result_client.call_count, 1)
        self.assertEqual(result_client.transaction_ids, [TRANSACTION_ID])

    def test_get_does_not_call_start_client(self):
        provider, start_client, _ = self.make_provider()
        provider.get_verified_identity(provider_transaction_id=TRANSACTION_ID)
        self.assertEqual(start_client.call_count, 0)

    def test_get_preserves_birth_date(self):
        provider, _, _ = self.make_provider()
        result = provider.get_verified_identity(
            provider_transaction_id=TRANSACTION_ID
        )
        self.assertEqual(result.birth_date, BIRTH_DATE)

    def test_get_preserves_identity_subject(self):
        provider, _, _ = self.make_provider()
        result = provider.get_verified_identity(
            provider_transaction_id=TRANSACTION_ID
        )
        self.assertEqual(result.identity_subject, IDENTITY_SUBJECT)

    def test_get_preserves_provider_transaction_id(self):
        provider, _, _ = self.make_provider()
        result = provider.get_verified_identity(
            provider_transaction_id=TRANSACTION_ID
        )
        self.assertEqual(result.provider_transaction_id, TRANSACTION_ID)

    def test_get_preserves_signature(self):
        provider, _, _ = self.make_provider()
        result = provider.get_verified_identity(
            provider_transaction_id=TRANSACTION_ID
        )
        self.assertEqual(result.signature, SIGNATURE)

    def test_toss_result_is_verified_identity_compatible(self):
        provider, _, _ = self.make_provider()
        result = provider.get_verified_identity(
            provider_transaction_id=TRANSACTION_ID
        )
        self.assertIsInstance(result, VerifiedIdentityResult)
        self.assertIs(type(result), TossVerifiedIdentityResult)
        self.assertTrue(result.evidence_persistence_required)

    def test_toss_result_is_immutable(self):
        provider, _, _ = self.make_provider()
        result = provider.get_verified_identity(
            provider_transaction_id=TRANSACTION_ID
        )
        with self.assertRaises(FrozenInstanceError):
            result.signature = "replacement"

    def test_toss_result_rejects_empty_signature(self):
        with self.assertRaises(ValueError):
            TossVerifiedIdentityResult(
                provider=IdentityVerificationProviderName.TOSS,
                provider_transaction_id=TRANSACTION_ID,
                birth_date=BIRTH_DATE,
                identity_subject=IDENTITY_SUBJECT,
                signature=" ",
            )

    def test_invalid_start_result_maps_to_invalid_provider_response(self):
        provider, _, _ = self.make_provider(start_result=object())
        self.assert_start_error(
            provider,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )

    def test_invalid_result_maps_to_invalid_provider_response(self):
        provider, _, _ = self.make_provider(result=object())
        self.assert_result_error(
            provider,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )

    def test_start_network_maps_to_provider_unavailable(self):
        self.assert_start_mapping(
            TossIdentityVerificationStartErrorCode.NETWORK_ERROR,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_start_http_maps_to_provider_unavailable(self):
        self.assert_start_mapping(
            TossIdentityVerificationStartErrorCode.HTTP_ERROR,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_start_token_maps_to_provider_unavailable(self):
        self.assert_start_mapping(
            TossIdentityVerificationStartErrorCode.TOKEN_ERROR,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_start_invalid_response_maps_to_invalid_provider_response(self):
        self.assert_start_mapping(
            TossIdentityVerificationStartErrorCode.INVALID_RESPONSE,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )

    def test_start_rejected_maps_to_verification_failed(self):
        self.assert_start_mapping(
            TossIdentityVerificationStartErrorCode.REQUEST_REJECTED,
            IdentityVerificationProviderErrorCode.VERIFICATION_FAILED,
        )

    def test_result_pending_maps_to_verification_pending(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING,
            IdentityVerificationProviderErrorCode.VERIFICATION_PENDING,
        )

    def test_result_expired_maps_to_verification_expired(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.VERIFICATION_EXPIRED,
            IdentityVerificationProviderErrorCode.VERIFICATION_EXPIRED,
        )

    def test_result_age_restricted_maps_to_age_restricted(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.AGE_RESTRICTED,
            IdentityVerificationProviderErrorCode.AGE_RESTRICTED,
        )

    def test_result_provider_unavailable_maps_to_provider_unavailable(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.PROVIDER_UNAVAILABLE,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_result_network_maps_to_provider_unavailable(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.NETWORK_ERROR,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_result_http_maps_to_provider_unavailable(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.HTTP_ERROR,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_result_token_maps_to_provider_unavailable(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.TOKEN_ERROR,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_result_crypto_maps_to_invalid_provider_response(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.CRYPTO_ERROR,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )

    def test_result_invalid_response_maps_to_invalid_provider_response(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )

    def test_result_query_limit_maps_to_verification_failed(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.RESULT_QUERY_LIMIT_EXCEEDED,
            IdentityVerificationProviderErrorCode.VERIFICATION_FAILED,
        )

    def test_result_rejected_maps_to_verification_failed(self):
        self.assert_result_mapping(
            TossIdentityVerificationResultErrorCode.REQUEST_REJECTED,
            IdentityVerificationProviderErrorCode.VERIFICATION_FAILED,
        )

    def test_start_error_does_not_retry(self):
        error = TossIdentityVerificationStartError(
            TossIdentityVerificationStartErrorCode.NETWORK_ERROR
        )
        start_client = StubStartClient(error=error)
        provider, _, _ = self.make_provider(start_client=start_client)
        self.assert_start_error(
            provider,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )
        self.assertEqual(start_client.call_count, 1)

    def test_result_error_does_not_retry(self):
        error = TossIdentityVerificationResultError(
            TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING
        )
        result_client = StubResultClient(error=error)
        provider, _, _ = self.make_provider(result_client=result_client)
        self.assert_result_error(
            provider,
            IdentityVerificationProviderErrorCode.VERIFICATION_PENDING,
        )
        self.assertEqual(result_client.call_count, 1)

    def test_toss_result_repr_hides_all_sensitive_values(self):
        provider, _, _ = self.make_provider()
        result = provider.get_verified_identity(
            provider_transaction_id=TRANSACTION_ID
        )
        representation = repr(result)
        for sensitive in (
            TRANSACTION_ID,
            BIRTH_DATE.isoformat(),
            IDENTITY_SUBJECT,
            SIGNATURE,
        ):
            self.assertNotIn(sensitive, representation)

    def test_provider_error_does_not_copy_raw_toss_data(self):
        error = TossIdentityVerificationResultError(
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE
        )
        error.args = (RAW_TOSS_DATA,)
        provider, _, _ = self.make_provider(
            result_client=StubResultClient(error=error)
        )
        with self.assertRaises(IdentityVerificationProviderError) as context:
            provider.get_verified_identity(
                provider_transaction_id=TRANSACTION_ID
            )
        self.assertNotIn(RAW_TOSS_DATA, repr(context.exception))

    def test_provider_repr_hides_client_state(self):
        provider, _, _ = self.make_provider()
        representation = repr(provider)
        self.assertEqual(representation, "TossIdentityVerificationProvider()")
        self.assertNotIn(TRANSACTION_ID, representation)

    def test_provider_has_no_database_or_flask_dependency(self):
        syntax_tree = ast.parse(inspect.getsource(provider_service))
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
            {"flask", "sqlalchemy", "models", "app"}.isdisjoint(
                imported_roots
            )
        )

    def test_provider_does_not_store_credentials(self):
        provider, _, _ = self.make_provider()
        for attribute in (
            "client_id",
            "client_secret",
            "access_token",
            "crypto_key",
        ):
            self.assertFalse(hasattr(provider, attribute))

    def make_provider(
        self,
        *,
        start_client=None,
        result_client=None,
        start_result=None,
        result=None,
    ):
        if start_client is None:
            start_client = StubStartClient(
                result=(
                    valid_start_result()
                    if start_result is None
                    else start_result
                )
            )
        if result_client is None:
            result_client = StubResultClient(
                result=valid_result() if result is None else result
            )
        provider = TossIdentityVerificationProvider(
            start_client=start_client,
            result_client=result_client,
        )
        return provider, start_client, result_client

    def assert_start_mapping(self, toss_code, provider_code):
        error = TossIdentityVerificationStartError(toss_code)
        start_client = StubStartClient(error=error)
        provider, _, _ = self.make_provider(start_client=start_client)
        self.assert_start_error(provider, provider_code)
        self.assertEqual(start_client.call_count, 1)

    def assert_result_mapping(self, toss_code, provider_code):
        error = TossIdentityVerificationResultError(toss_code)
        result_client = StubResultClient(error=error)
        provider, _, _ = self.make_provider(result_client=result_client)
        self.assert_result_error(provider, provider_code)
        self.assertEqual(result_client.call_count, 1)

    def assert_start_error(self, provider, expected_code):
        with self.assertRaises(IdentityVerificationProviderError) as context:
            provider.start_verification()
        self.assertIs(context.exception.code, expected_code)

    def assert_result_error(self, provider, expected_code):
        with self.assertRaises(IdentityVerificationProviderError) as context:
            provider.get_verified_identity(
                provider_transaction_id=TRANSACTION_ID
            )
        self.assertIs(context.exception.code, expected_code)


if __name__ == "__main__":
    unittest.main()
