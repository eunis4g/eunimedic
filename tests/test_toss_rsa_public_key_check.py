import ast
from base64 import b64encode
import inspect
import unittest

from Crypto.PublicKey import RSA

import scripts.toss_rsa_public_key_check as key_check
from scripts.toss_rsa_public_key_check import (
    main,
    run_toss_rsa_public_key_check,
)
from services.toss_cert_crypto import (
    TossCertCryptoError,
    TossCertCryptoErrorCode,
    TossCertCryptoSessionGenerator,
)


class OutputCapture:

    def __init__(self):
        self.lines = []

    def __call__(self, value):
        self.lines.append(value)

    @property
    def text(self):
        return "\n".join(self.lines)


class CapturingGenerator:

    def __init__(self, delegate):
        self.delegate = delegate
        self.generate_call_count = 0
        self.generated_session = None

    def generate(self):
        self.generate_call_count += 1
        self.generated_session = self.delegate.generate()
        return self.generated_session


class GeneratorFactorySpy:

    def __init__(self):
        self.calls = []
        self.instance = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.instance = CapturingGenerator(
            TossCertCryptoSessionGenerator(**kwargs)
        )
        return self.instance


class FailingGenerator:

    def generate(self):
        raise TossCertCryptoError(
            TossCertCryptoErrorCode.SESSION_GENERATION_FAILED
        )


class TossRsaPublicKeyCheckTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.private_key = RSA.generate(2048)
        public_key_der = cls.private_key.public_key().export_key(format="DER")
        cls.public_key_base64 = b64encode(public_key_der).decode("ascii")

    def run_check(self, *, environ=None, generator_factory=None):
        output = OutputCapture()
        options = {
            "environ": (
                {"TOSS_CERT_RSA_PUBLIC_KEY_BASE64": self.public_key_base64}
                if environ is None
                else environ
            ),
            "output": output,
        }
        if generator_factory is not None:
            options["generator_factory"] = generator_factory
        exit_code = run_toss_rsa_public_key_check(**options)
        return exit_code, output

    def test_valid_synthetic_public_key_succeeds(self):
        exit_code, output = self.run_check()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.lines,
            [
                "Toss RSA public key check: OK",
                "RSA public key parsed: OK",
                "Crypto session generation: OK",
            ],
        )

    def test_missing_key_fails_with_safe_code(self):
        exit_code, output = self.run_check(environ={})

        self.assertEqual(exit_code, 1)
        self.assertIn("INVALID_RSA_PUBLIC_KEY", output.text)

    def test_blank_key_fails_with_safe_code(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                exit_code, output = self.run_check(
                    environ={"TOSS_CERT_RSA_PUBLIC_KEY_BASE64": value}
                )

                self.assertEqual(exit_code, 1)
                self.assertIn("INVALID_RSA_PUBLIC_KEY", output.text)

    def test_invalid_base64_fails_without_echoing_value(self):
        invalid_value = "not-valid-base64-sensitive"

        exit_code, output = self.run_check(
            environ={"TOSS_CERT_RSA_PUBLIC_KEY_BASE64": invalid_value}
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("INVALID_RSA_PUBLIC_KEY", output.text)
        self.assertNotIn(invalid_value, output.text)

    def test_non_rsa_data_is_rejected(self):
        invalid_value = b64encode(b"not an RSA key").decode("ascii")

        exit_code, output = self.run_check(
            environ={"TOSS_CERT_RSA_PUBLIC_KEY_BASE64": invalid_value}
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("INVALID_RSA_PUBLIC_KEY", output.text)
        self.assertNotIn(invalid_value, output.text)

    def test_private_key_is_rejected(self):
        private_key_base64 = b64encode(
            self.private_key.export_key(format="DER")
        ).decode("ascii")

        exit_code, output = self.run_check(
            environ={
                "TOSS_CERT_RSA_PUBLIC_KEY_BASE64": private_key_base64
            }
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("INVALID_RSA_PUBLIC_KEY", output.text)
        self.assertNotIn(private_key_base64, output.text)

    def test_production_generator_receives_exact_configured_key(self):
        factory = GeneratorFactorySpy()

        exit_code, _ = self.run_check(generator_factory=factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            factory.calls,
            [{"base64_public_key": self.public_key_base64}],
        )

    def test_crypto_session_is_generated_exactly_once(self):
        factory = GeneratorFactorySpy()

        exit_code, _ = self.run_check(generator_factory=factory)

        self.assertEqual(exit_code, 0)
        self.assertEqual(factory.instance.generate_call_count, 1)

    def test_generated_crypto_material_is_never_printed(self):
        factory = GeneratorFactorySpy()

        exit_code, output = self.run_check(generator_factory=factory)
        crypto_session = factory.instance.generated_session

        self.assertEqual(exit_code, 0)
        self.assertNotIn(self.public_key_base64, output.text)
        self.assertNotIn(crypto_session.session_key, output.text)
        self.assertNotIn(
            b64encode(crypto_session._secret_key).decode("ascii"),
            output.text,
        )
        self.assertNotIn(
            b64encode(crypto_session._iv).decode("ascii"),
            output.text,
        )

    def test_generation_failure_returns_nonzero_safe_code(self):
        exit_code, output = self.run_check(
            generator_factory=lambda **_kwargs: FailingGenerator()
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("CRYPTO_SESSION_GENERATION_FAILED", output.text)
        self.assertNotIn("TossCertCryptoError", output.text)

    def test_unexpected_constructor_error_is_redacted(self):
        raw_error = "unexpected-sensitive-key-detail"

        def failing_factory(**_kwargs):
            raise RuntimeError(raw_error)

        exit_code, output = self.run_check(
            generator_factory=failing_factory
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("UNEXPECTED_ERROR", output.text)
        self.assertNotIn(raw_error, output.text)

    def test_main_can_run_without_loading_dotenv_in_unit_test(self):
        output = OutputCapture()

        exit_code = main(
            environ={
                "TOSS_CERT_RSA_PUBLIC_KEY_BASE64": self.public_key_base64
            },
            output=output,
            load_environment=False,
        )

        self.assertEqual(exit_code, 0)

    def test_script_loads_root_dotenv_without_override(self):
        source = inspect.getsource(key_check)

        self.assertIn('PROJECT_ROOT / ".env"', source)
        self.assertIn("override=False", source)

    def test_script_has_no_http_flask_database_or_toss_client_dependency(self):
        source = inspect.getsource(key_check)
        syntax_tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
        }
        imported_roots = {
            module.split(".", 1)[0]
            for module in imported_modules
        }

        for forbidden_root in (
            "flask",
            "models",
            "requests",
            "sqlalchemy",
        ):
            self.assertNotIn(forbidden_root, imported_roots)
        self.assertFalse(
            any("identity_verification" in item for item in imported_modules)
        )
        self.assertFalse(any("access_token" in item for item in imported_modules))


if __name__ == "__main__":
    unittest.main()
