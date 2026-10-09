import ast
import inspect
import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime

import services.identity_verification_provider as provider_service
from services.identity_verification_provider import (
    IdentityVerificationProvider,
    IdentityVerificationProviderError,
    IdentityVerificationProviderErrorCode,
    IdentityVerificationProviderName,
    IdentityVerificationStartResult,
    VerifiedIdentityResult,
)


class InTestIdentityVerificationProvider(IdentityVerificationProvider):

    @property
    def provider(self):
        return IdentityVerificationProviderName.TOSS

    def start_verification(self):
        return IdentityVerificationStartResult(
            provider=self.provider,
            provider_transaction_id="test-transaction-id",
            authentication_url="https://auth.example.test/start",
        )

    def get_verified_identity(self, *, provider_transaction_id):
        return VerifiedIdentityResult(
            provider=self.provider,
            provider_transaction_id=provider_transaction_id,
            birth_date=date(2012, 10, 9),
            identity_subject="test-identity-subject",
        )


class IncompleteIdentityVerificationProvider(IdentityVerificationProvider):
    pass


class IdentityVerificationProviderContractTest(unittest.TestCase):

    def test_production_provider_enum_contains_only_toss(self):
        self.assertEqual(
            list(IdentityVerificationProviderName),
            [IdentityVerificationProviderName.TOSS],
        )

    def test_start_result_preserves_opaque_provider_values(self):
        result = self._make_start_result()

        self.assertIs(result.provider, IdentityVerificationProviderName.TOSS)
        self.assertEqual(result.provider_transaction_id, "tx-sensitive-123")
        self.assertEqual(
            result.authentication_url,
            "https://auth.example.test/sensitive-path",
        )

    def test_start_result_is_immutable(self):
        result = self._make_start_result()

        with self.assertRaises(FrozenInstanceError):
            result.provider_transaction_id = "replacement"

    def test_start_result_rejects_unknown_provider_representation(self):
        with self.assertRaises(TypeError):
            self._make_start_result(provider="TOSS")

    def test_start_result_rejects_empty_transaction_id(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValueError):
                    self._make_start_result(provider_transaction_id=value)

    def test_start_result_rejects_non_string_transaction_id(self):
        with self.assertRaises(TypeError):
            self._make_start_result(provider_transaction_id=123)

    def test_start_result_rejects_empty_authentication_url(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValueError):
                    self._make_start_result(authentication_url=value)

    def test_start_result_repr_redacts_transaction_id_and_url(self):
        result = self._make_start_result()
        representation = repr(result)

        self.assertNotIn(result.provider_transaction_id, representation)
        self.assertNotIn(result.authentication_url, representation)
        self.assertIn("IdentityVerificationProviderName.TOSS", representation)

    def test_verified_result_preserves_only_required_identity_values(self):
        result = self._make_verified_result()

        self.assertIs(result.provider, IdentityVerificationProviderName.TOSS)
        self.assertEqual(result.provider_transaction_id, "tx-sensitive-123")
        self.assertEqual(result.birth_date, date(2012, 10, 9))
        self.assertEqual(result.identity_subject, "identity-sensitive-456")
        self.assertEqual(
            {item.name for item in fields(result)},
            {
                "provider",
                "provider_transaction_id",
                "birth_date",
                "identity_subject",
            },
        )

    def test_verified_result_is_immutable(self):
        result = self._make_verified_result()

        with self.assertRaises(FrozenInstanceError):
            result.identity_subject = "replacement"

    def test_verified_result_requires_plain_date(self):
        for value in ("2012-10-09", datetime(2012, 10, 9, 0, 0)):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    self._make_verified_result(birth_date=value)

    def test_verified_result_rejects_empty_identity_subject(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValueError):
                    self._make_verified_result(identity_subject=value)

    def test_verified_result_rejects_empty_transaction_id(self):
        with self.assertRaises(ValueError):
            self._make_verified_result(provider_transaction_id="")

    def test_verified_result_repr_redacts_all_sensitive_values(self):
        result = self._make_verified_result()
        representation = repr(result)

        for sensitive_value in (
            result.provider_transaction_id,
            result.birth_date.isoformat(),
            result.identity_subject,
        ):
            with self.subTest(sensitive_value=sensitive_value):
                self.assertNotIn(sensitive_value, representation)

    def test_verified_result_has_no_identity_profile_or_age_fields(self):
        result = self._make_verified_result()

        for excluded_field in (
            "name",
            "gender",
            "carrier",
            "address",
            "ci",
            "phone",
            "age_group",
            "age_eligibility",
        ):
            with self.subTest(excluded_field=excluded_field):
                self.assertFalse(hasattr(result, excluded_field))

    def test_error_codes_are_stable_and_minimal(self):
        self.assertEqual(
            {code.value for code in IdentityVerificationProviderErrorCode},
            {
                "PROVIDER_UNAVAILABLE",
                "INVALID_PROVIDER_RESPONSE",
                "VERIFICATION_PENDING",
                "VERIFICATION_FAILED",
                "VERIFICATION_EXPIRED",
            },
        )

    def test_provider_error_uses_generic_message_without_raw_data(self):
        raw_provider_data = "raw-sensitive-provider-response"
        error = IdentityVerificationProviderError(
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE
        )

        self.assertIs(
            error.code,
            IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE,
        )
        self.assertNotIn(raw_provider_data, str(error))
        self.assertNotIn(raw_provider_data, repr(error))

    def test_provider_error_rejects_uncontrolled_error_code(self):
        with self.assertRaises(TypeError):
            IdentityVerificationProviderError("PROVIDER_UNAVAILABLE")

    def test_provider_interface_requires_all_contract_methods(self):
        with self.assertRaises(TypeError):
            IncompleteIdentityVerificationProvider()

    def test_in_test_fake_can_implement_the_provider_contract(self):
        provider = InTestIdentityVerificationProvider()
        start_result = provider.start_verification()
        verified_result = provider.get_verified_identity(
            provider_transaction_id=start_result.provider_transaction_id
        )

        self.assertIs(start_result.provider, provider.provider)
        self.assertIs(verified_result.provider, provider.provider)
        self.assertEqual(
            verified_result.provider_transaction_id,
            start_result.provider_transaction_id,
        )

    def test_service_has_only_standard_library_dependencies(self):
        source = inspect.getsource(provider_service)
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

        self.assertEqual(
            imported_roots,
            {"abc", "dataclasses", "datetime", "enum"},
        )

    def test_production_module_has_no_fake_provider_implementation(self):
        class_names = {
            node.name
            for node in ast.walk(
                ast.parse(inspect.getsource(provider_service))
            )
            if isinstance(node, ast.ClassDef)
        }

        self.assertFalse(
            any("fake" in class_name.lower() for class_name in class_names)
        )

    def _make_start_result(
        self,
        *,
        provider=IdentityVerificationProviderName.TOSS,
        provider_transaction_id="tx-sensitive-123",
        authentication_url="https://auth.example.test/sensitive-path",
    ):
        return IdentityVerificationStartResult(
            provider=provider,
            provider_transaction_id=provider_transaction_id,
            authentication_url=authentication_url,
        )

    def _make_verified_result(
        self,
        *,
        provider=IdentityVerificationProviderName.TOSS,
        provider_transaction_id="tx-sensitive-123",
        birth_date=date(2012, 10, 9),
        identity_subject="identity-sensitive-456",
    ):
        return VerifiedIdentityResult(
            provider=provider,
            provider_transaction_id=provider_transaction_id,
            birth_date=birth_date,
            identity_subject=identity_subject,
        )


if __name__ == "__main__":
    unittest.main()
