import ast
import inspect
import unittest
from datetime import date

import services.fake_identity_verification_provider as fake_provider_service
from services.age_eligibility_service import (
    AgeEligibility,
    check_age_eligibility,
)
from services.fake_identity_verification_provider import (
    FakeIdentityVerificationProvider,
)
from services.identity_verification_provider import (
    IdentityVerificationProvider,
    IdentityVerificationProviderError,
    IdentityVerificationProviderErrorCode,
    IdentityVerificationProviderName,
    IdentityVerificationStartResult,
    VerifiedIdentityResult,
)


class FakeIdentityVerificationProviderTest(unittest.TestCase):

    def test_fake_provider_implements_provider_contract(self):
        provider = self._make_provider()

        self.assertIsInstance(provider, IdentityVerificationProvider)
        self.assertIs(provider.provider, IdentityVerificationProviderName.TOSS)

    def test_start_verification_returns_contract_result(self):
        provider = self._make_provider(
            transaction_ids=("fake-transaction-001",)
        )

        result = provider.start_verification()

        self.assertIsInstance(result, IdentityVerificationStartResult)
        self.assertIs(result.provider, provider.provider)

    def test_start_verification_returns_fake_authentication_url(self):
        provider = self._make_provider(
            transaction_ids=("fake-transaction-001",)
        )

        result = provider.start_verification()

        self.assertEqual(
            result.authentication_url,
            "fake://identity-verification/fake-transaction-001",
        )

    def test_injected_factory_makes_transaction_id_deterministic(self):
        provider = self._make_provider(
            transaction_ids=("fixture-transaction-id",)
        )

        result = provider.start_verification()

        self.assertEqual(
            result.provider_transaction_id,
            "fixture-transaction-id",
        )

    def test_default_factory_generates_distinct_transaction_ids(self):
        provider = self._make_provider()

        first = provider.start_verification()
        second = provider.start_verification()

        self.assertNotEqual(
            first.provider_transaction_id,
            second.provider_transaction_id,
        )

    def test_get_verified_identity_returns_contract_result(self):
        provider = self._make_provider()
        start_result = provider.start_verification()

        result = provider.get_verified_identity(
            provider_transaction_id=start_result.provider_transaction_id
        )

        self.assertIsInstance(result, VerifiedIdentityResult)
        self.assertEqual(
            result.provider_transaction_id,
            start_result.provider_transaction_id,
        )

    def test_verified_birth_date_is_returned_as_plain_date(self):
        verified_birth_date = date(2012, 10, 8)
        provider = self._make_provider(
            verified_birth_date=verified_birth_date
        )
        start_result = provider.start_verification()

        result = provider.get_verified_identity(
            provider_transaction_id=start_result.provider_transaction_id
        )

        self.assertIs(type(result.birth_date), date)
        self.assertEqual(result.birth_date, verified_birth_date)

    def test_opaque_identity_subject_is_returned_unchanged(self):
        provider = self._make_provider(
            identity_subject="fake-subject-001"
        )
        start_result = provider.start_verification()

        result = provider.get_verified_identity(
            provider_transaction_id=start_result.provider_transaction_id
        )

        self.assertEqual(result.identity_subject, "fake-subject-001")

    def test_unknown_transaction_is_rejected_without_exposing_it(self):
        provider = self._make_provider()
        unknown_transaction_id = "unknown-sensitive-transaction"

        with self.assertRaises(IdentityVerificationProviderError) as context:
            provider.get_verified_identity(
                provider_transaction_id=unknown_transaction_id
            )

        self.assertIs(
            context.exception.code,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )
        self.assertNotIn(unknown_transaction_id, str(context.exception))

    def test_provider_unavailable_can_be_simulated(self):
        provider = self._make_provider(
            start_error_code=(
                IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE
            )
        )

        with self.assertRaises(IdentityVerificationProviderError) as context:
            provider.start_verification()

        self.assertIs(
            context.exception.code,
            IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_verification_failed_can_be_simulated(self):
        self._assert_verification_error(
            IdentityVerificationProviderErrorCode.VERIFICATION_FAILED
        )

    def test_verification_expired_can_be_simulated(self):
        self._assert_verification_error(
            IdentityVerificationProviderErrorCode.VERIFICATION_EXPIRED
        )

    def test_error_configuration_requires_enum_values(self):
        with self.assertRaises(TypeError):
            self._make_provider(start_error_code="PROVIDER_UNAVAILABLE")

        with self.assertRaises(TypeError):
            self._make_provider(verification_error_code="VERIFICATION_FAILED")

    def test_multiple_transactions_are_recognized_independently(self):
        provider = self._make_provider(
            transaction_ids=(
                "fake-transaction-001",
                "fake-transaction-002",
            )
        )
        first = provider.start_verification()
        second = provider.start_verification()

        first_result = provider.get_verified_identity(
            provider_transaction_id=first.provider_transaction_id
        )
        second_result = provider.get_verified_identity(
            provider_transaction_id=second.provider_transaction_id
        )

        self.assertNotEqual(
            first_result.provider_transaction_id,
            second_result.provider_transaction_id,
        )

    def test_repeated_result_lookup_is_not_consumed(self):
        provider = self._make_provider()
        start_result = provider.start_verification()

        first = provider.get_verified_identity(
            provider_transaction_id=start_result.provider_transaction_id
        )
        second = provider.get_verified_identity(
            provider_transaction_id=start_result.provider_transaction_id
        )

        self.assertEqual(first, second)

    def test_duplicate_generated_transaction_is_rejected(self):
        provider = self._make_provider(
            transaction_ids=("duplicate-id", "duplicate-id")
        )
        provider.start_verification()

        with self.assertRaises(IdentityVerificationProviderError) as context:
            provider.start_verification()

        self.assertIs(
            context.exception.code,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )

    def test_verified_adult_birth_date_connects_to_age_service(self):
        result = self._get_identity_result(
            verified_birth_date=date(2012, 10, 8)
        )

        eligibility = check_age_eligibility(
            birth_date=result.birth_date,
            reference_date=date(2026, 10, 8),
        )

        self.assertIs(eligibility, AgeEligibility.AGE_14_OR_OVER)

    def test_verified_minor_birth_date_connects_to_age_service(self):
        result = self._get_identity_result(
            verified_birth_date=date(2012, 10, 9)
        )

        eligibility = check_age_eligibility(
            birth_date=result.birth_date,
            reference_date=date(2026, 10, 8),
        )

        self.assertIs(eligibility, AgeEligibility.UNDER_14)

    def test_fake_provider_does_not_import_network_clients(self):
        imported_roots = self._imported_roots()
        source = inspect.getsource(fake_provider_service).lower()

        self.assertTrue(
            imported_roots.isdisjoint({"requests", "httpx", "urllib"})
        )
        self.assertNotIn("oauth2.cert.toss.im", source)
        self.assertNotIn("cert.toss.im", source)

    def test_fake_provider_has_no_flask_dependency(self):
        self.assertTrue(
            self._imported_roots().isdisjoint(
                {"flask", "flask_login", "flask_sqlalchemy"}
            )
        )

    def test_fake_provider_has_no_database_dependency(self):
        self.assertTrue(
            self._imported_roots().isdisjoint(
                {"sqlalchemy", "sqlite3", "models"}
            )
        )

    def test_fake_provider_requires_no_toss_credentials(self):
        parameter_names = set(
            inspect.signature(
                FakeIdentityVerificationProvider
            ).parameters
        )

        self.assertTrue(
            parameter_names.isdisjoint(
                {"client_id", "client_secret", "api_key"}
            )
        )

    def test_fake_provider_repr_does_not_expose_fixture_data(self):
        verified_birth_date = date(2012, 10, 8)
        identity_subject = "fake-subject-sensitive"
        transaction_id = "fake-transaction-sensitive"
        provider = self._make_provider(
            verified_birth_date=verified_birth_date,
            identity_subject=identity_subject,
            transaction_ids=(transaction_id,),
        )
        provider.start_verification()
        representation = repr(provider)

        for sensitive_value in (
            verified_birth_date.isoformat(),
            identity_subject,
            transaction_id,
        ):
            with self.subTest(sensitive_value=sensitive_value):
                self.assertNotIn(sensitive_value, representation)

    def test_fake_does_not_add_itself_to_production_provider_enum(self):
        self.assertEqual(
            list(IdentityVerificationProviderName),
            [IdentityVerificationProviderName.TOSS],
        )

    def test_fake_provider_has_no_state_or_nonce_fields(self):
        provider = self._make_provider()

        self.assertFalse(hasattr(provider, "state"))
        self.assertFalse(hasattr(provider, "nonce"))

    def _assert_verification_error(self, error_code):
        provider = self._make_provider(
            verification_error_code=error_code
        )
        start_result = provider.start_verification()

        with self.assertRaises(IdentityVerificationProviderError) as context:
            provider.get_verified_identity(
                provider_transaction_id=start_result.provider_transaction_id
            )

        self.assertIs(context.exception.code, error_code)

    def _get_identity_result(self, *, verified_birth_date):
        provider = self._make_provider(
            verified_birth_date=verified_birth_date
        )
        start_result = provider.start_verification()
        return provider.get_verified_identity(
            provider_transaction_id=start_result.provider_transaction_id
        )

    def _make_provider(
        self,
        *,
        verified_birth_date=date(2012, 10, 8),
        identity_subject="fake-subject-001",
        transaction_ids=None,
        start_error_code=None,
        verification_error_code=None,
    ):
        transaction_id_factory = None
        if transaction_ids is not None:
            transaction_id_iterator = iter(transaction_ids)
            transaction_id_factory = lambda: next(
                transaction_id_iterator
            )

        return FakeIdentityVerificationProvider(
            provider=IdentityVerificationProviderName.TOSS,
            verified_birth_date=verified_birth_date,
            identity_subject=identity_subject,
            transaction_id_factory=transaction_id_factory,
            start_error_code=start_error_code,
            verification_error_code=verification_error_code,
        )

    def _imported_roots(self):
        syntax_tree = ast.parse(inspect.getsource(fake_provider_service))
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
