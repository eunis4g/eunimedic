from base64 import b64decode, b64encode
import unittest
import uuid

from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA1
from Crypto.PublicKey import RSA
from Crypto.Signature.pss import MGF1

import services.toss_cert_crypto as crypto_service
from services.toss_cert_crypto import (
    TossCertCryptoError,
    TossCertCryptoErrorCode,
    TossCertCryptoSessionGenerator,
)


# Published in toss/toss-cert-examples test_data/aes_test_data.
OFFICIAL_SESSION_ID = "b94ea687-0a72-43f8-be24-7be5029fa998"
OFFICIAL_SECRET_KEY_BASE64 = (
    "KxHeNnbiR5Eh5d/9KJCN6jUW5Md9nkk8jU6WbkmR7Es="
)
OFFICIAL_IV_BASE64 = "jbZIV4aBzrydN0IP"
OFFICIAL_PLAINTEXT = "Layton Nicholson"
OFFICIAL_ENCRYPTED_DATA = (
    "v1$b94ea687-0a72-43f8-be24-7be5029fa998$"
    "LCFncFGpJKMBClC5RHa7zabbPzqsKC51USGG1ZwSBPE="
)
FIXED_SESSION_ID = "2a260a14-7725-4ff4-87b2-1d87ab9187bf"
SENSITIVE_TEXT = "synthetic-sensitive-personal-data"


class QueuedRandomBytes:

    def __init__(self, *values):
        self._values = list(values)
        self.requested_lengths = []

    def __call__(self, length):
        self.requested_lengths.append(length)
        if not self._values:
            raise AssertionError("Unexpected random byte request.")
        value = self._values.pop(0)
        if len(value) != length:
            raise AssertionError(
                f"Expected a {length}-byte test value, got {len(value)}."
            )
        return value


def encrypt_like_official_example(*, session_id, secret_key, iv, plaintext):
    cipher = AES.new(secret_key, AES.MODE_GCM, nonce=iv, mac_len=16)
    cipher.update(secret_key)
    encrypted, tag = cipher.encrypt_and_digest(plaintext)
    combined = b64encode(encrypted + tag).decode("ascii")
    return f"v1${session_id}${combined}"


class TossCertCryptoTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.private_key = RSA.generate(2048)
        public_key_der = cls.private_key.public_key().export_key(format="DER")
        cls.public_key_base64 = b64encode(public_key_der).decode("ascii")

    def test_error_requires_error_code_enum(self):
        with self.assertRaises(TypeError):
            TossCertCryptoError("DECRYPTION_FAILED")

    def test_rejects_non_string_public_key(self):
        with self.assertRaises(TypeError):
            TossCertCryptoSessionGenerator(base64_public_key=None)

    def test_rejects_invalid_public_key_base64_with_stable_error(self):
        self.assert_generation_error("not base64!")

    def test_rejects_non_rsa_public_key_with_stable_error(self):
        self.assert_generation_error(
            b64encode(b"not an RSA key").decode("ascii")
        )

    def test_rejects_private_key_as_public_key_configuration(self):
        private_key_base64 = b64encode(
            self.private_key.export_key(format="DER")
        ).decode("ascii")
        self.assert_generation_error(private_key_base64)

    def test_rejects_non_callable_random_source(self):
        with self.assertRaises(TypeError):
            self.make_generator(random_bytes=b"not-callable")

    def test_rejects_non_callable_session_id_factory(self):
        with self.assertRaises(TypeError):
            self.make_generator(session_id_factory=FIXED_SESSION_ID)

    def test_generates_exact_official_session_key_structure(self):
        session, _, _, _ = self.make_deterministic_session()

        version, session_id, encrypted_key = session.session_key.split("$")

        self.assertEqual(version, "v1")
        self.assertEqual(session_id, FIXED_SESSION_ID)
        self.assertTrue(b64decode(encrypted_key, validate=True))

    def test_rsa_payload_uses_exact_official_aes_key_structure(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_key = b64decode(session.session_key.split("$")[2])
        decryptor = PKCS1_OAEP.new(
            self.private_key,
            hashAlgo=SHA1,
            mgfunc=lambda seed, length: MGF1(seed, length, SHA1),
        )

        decrypted_key = decryptor.decrypt(encrypted_key).decode("utf-8")

        self.assertEqual(
            decrypted_key,
            "AES_GCM$"
            f"{b64encode(secret_key).decode('ascii')}$"
            f"{b64encode(iv).decode('ascii')}",
        )

    def test_generation_requests_32_byte_key_12_byte_iv_and_sha1_seed(self):
        _, _, _, random_source = self.make_deterministic_session()

        self.assertEqual(random_source.requested_lengths, [32, 12, 20])

    def test_broken_random_source_maps_to_generation_error(self):
        generator = self.make_generator(
            random_bytes=lambda length: b"too-short",
        )

        with self.assertRaises(TossCertCryptoError) as context:
            generator.generate()

        self.assertEqual(
            context.exception.code,
            TossCertCryptoErrorCode.SESSION_GENERATION_FAILED,
        )

    def test_random_source_exception_maps_to_generation_error(self):
        def broken_source(length):
            raise RuntimeError(SENSITIVE_TEXT)

        generator = self.make_generator(random_bytes=broken_source)

        with self.assertRaises(TossCertCryptoError) as context:
            generator.generate()

        self.assertNotIn(SENSITIVE_TEXT, str(context.exception))

    def test_invalid_session_id_maps_to_generation_error(self):
        generator = self.make_generator(
            session_id_factory=lambda: "not-a-uuid",
        )

        with self.assertRaises(TossCertCryptoError) as context:
            generator.generate()

        self.assertEqual(
            context.exception.code,
            TossCertCryptoErrorCode.SESSION_GENERATION_FAILED,
        )

    def test_non_uuid4_session_id_maps_to_generation_error(self):
        generator = self.make_generator(
            session_id_factory=lambda: uuid.uuid1(),
        )

        with self.assertRaises(TossCertCryptoError) as context:
            generator.generate()

        self.assertEqual(
            context.exception.code,
            TossCertCryptoErrorCode.SESSION_GENERATION_FAILED,
        )

    def test_two_default_generations_create_different_session_keys(self):
        generator = self.make_generator()

        first = generator.generate()
        second = generator.generate()

        self.assertIsNot(first, second)
        self.assertNotEqual(first.session_key, second.session_key)

    def test_generator_repr_hides_public_key(self):
        generator = self.make_generator()

        representation = repr(generator)

        self.assertEqual(representation, "TossCertCryptoSessionGenerator()")
        self.assertNotIn(self.public_key_base64, representation)

    def test_session_repr_and_str_hide_session_key_and_secret_material(self):
        session, secret_key, iv, _ = self.make_deterministic_session()

        for representation in (repr(session), str(session)):
            self.assertEqual(representation, "TossCertCryptoSession()")
            self.assertNotIn(session.session_key, representation)
            self.assertNotIn(b64encode(secret_key).decode(), representation)
            self.assertNotIn(b64encode(iv).decode(), representation)

    def test_official_aes_vector_decrypts(self):
        key = b64decode(OFFICIAL_SECRET_KEY_BASE64)
        iv = b64decode(OFFICIAL_IV_BASE64)
        random_source = QueuedRandomBytes(key, iv, b"o" * 20)
        session = self.make_generator(
            random_bytes=random_source,
            session_id_factory=lambda: OFFICIAL_SESSION_ID,
        ).generate()

        plaintext = session.decrypt(OFFICIAL_ENCRYPTED_DATA)

        self.assertEqual(plaintext, OFFICIAL_PLAINTEXT)

    def test_test_encryptor_matches_official_aes_vector(self):
        encrypted_data = encrypt_like_official_example(
            session_id=OFFICIAL_SESSION_ID,
            secret_key=b64decode(OFFICIAL_SECRET_KEY_BASE64),
            iv=b64decode(OFFICIAL_IV_BASE64),
            plaintext=OFFICIAL_PLAINTEXT.encode("utf-8"),
        )

        self.assertEqual(encrypted_data, OFFICIAL_ENCRYPTED_DATA)

    def test_synthetic_unicode_round_trip(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        plaintext = "홍길동 / 2000-01-02"
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=secret_key,
            iv=iv,
            plaintext=plaintext.encode("utf-8"),
        )

        self.assertEqual(session.decrypt(encrypted_data), plaintext)

    def test_empty_plaintext_round_trip(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=secret_key,
            iv=iv,
            plaintext=b"",
        )

        self.assertEqual(session.decrypt(encrypted_data), "")

    def test_rejects_non_string_ciphertext(self):
        session, _, _, _ = self.make_deterministic_session()

        for value in (None, b"ciphertext", 123):
            with self.subTest(value_type=type(value).__name__):
                self.assert_crypto_error(
                    session,
                    value,
                    TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
                )

    def test_rejects_empty_ciphertext(self):
        session, _, _, _ = self.make_deterministic_session()
        self.assert_crypto_error(
            session,
            "",
            TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
        )

    def test_rejects_wrong_part_count(self):
        session, _, _, _ = self.make_deterministic_session()

        for value in ("v1", "v1$session", "v1$a$b$c"):
            with self.subTest(part_count=len(value.split("$"))):
                self.assert_crypto_error(
                    session,
                    value,
                    TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
                )

    def test_rejects_empty_ciphertext_part(self):
        session, _, _, _ = self.make_deterministic_session()

        for value in ("$id$payload", "v1$$payload", "v1$id$"):
            with self.subTest(value=value):
                self.assert_crypto_error(
                    session,
                    value,
                    TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
                )

    def test_rejects_wrong_version(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=secret_key,
            iv=iv,
            plaintext=b"value",
        ).replace("v1$", "v2$", 1)
        self.assert_crypto_error(
            session,
            encrypted_data,
            TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
        )

    def test_rejects_different_session_id(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id="9a657978-e837-4be8-8cd2-9d81dab03789",
            secret_key=secret_key,
            iv=iv,
            plaintext=b"value",
        )
        self.assert_crypto_error(
            session,
            encrypted_data,
            TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
        )

    def test_rejects_malformed_base64(self):
        session, _, _, _ = self.make_deterministic_session()
        self.assert_crypto_error(
            session,
            f"v1${FIXED_SESSION_ID}$not-base64!",
            TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
        )

    def test_rejects_payload_shorter_than_gcm_tag(self):
        session, _, _, _ = self.make_deterministic_session()
        short_payload = b64encode(b"x" * 15).decode("ascii")
        self.assert_crypto_error(
            session,
            f"v1${FIXED_SESSION_ID}${short_payload}",
            TossCertCryptoErrorCode.INVALID_CIPHERTEXT,
        )

    def test_tampered_ciphertext_fails_gcm_integrity_check(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=secret_key,
            iv=iv,
            plaintext=b"value",
        )
        tampered = self.flip_combined_byte(encrypted_data, index=0)
        self.assert_crypto_error(
            session,
            tampered,
            TossCertCryptoErrorCode.DECRYPTION_FAILED,
        )

    def test_tampered_tag_fails_gcm_integrity_check(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=secret_key,
            iv=iv,
            plaintext=b"value",
        )
        tampered = self.flip_combined_byte(encrypted_data, index=-1)
        self.assert_crypto_error(
            session,
            tampered,
            TossCertCryptoErrorCode.DECRYPTION_FAILED,
        )

    def test_ciphertext_from_other_crypto_session_cannot_be_decrypted(self):
        first, first_key, first_iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=first_key,
            iv=first_iv,
            plaintext=b"value",
        )
        other_random = QueuedRandomBytes(
            bytes(reversed(first_key)),
            bytes(reversed(first_iv)),
            b"z" * 20,
        )
        second = self.make_generator(
            random_bytes=other_random,
            session_id_factory=lambda: FIXED_SESSION_ID,
        ).generate()

        self.assert_crypto_error(
            second,
            encrypted_data,
            TossCertCryptoErrorCode.DECRYPTION_FAILED,
        )

    def test_invalid_utf8_is_not_silently_replaced(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=secret_key,
            iv=iv,
            plaintext=b"\xff",
        )
        self.assert_crypto_error(
            session,
            encrypted_data,
            TossCertCryptoErrorCode.DECRYPTION_FAILED,
        )

    def test_decryption_error_does_not_include_ciphertext_or_plaintext(self):
        session, secret_key, iv, _ = self.make_deterministic_session()
        encrypted_data = encrypt_like_official_example(
            session_id=FIXED_SESSION_ID,
            secret_key=secret_key,
            iv=iv,
            plaintext=SENSITIVE_TEXT.encode("utf-8"),
        )
        tampered = self.flip_combined_byte(encrypted_data, index=-1)

        with self.assertRaises(TossCertCryptoError) as context:
            session.decrypt(tampered)

        error_text = str(context.exception)
        self.assertNotIn(tampered, error_text)
        self.assertNotIn(SENSITIVE_TEXT, error_text)
        self.assertNotIn(b64encode(secret_key).decode(), error_text)

    def test_invalid_ciphertext_error_message_is_stable(self):
        session, _, _, _ = self.make_deterministic_session()

        with self.assertRaises(TossCertCryptoError) as context:
            session.decrypt("malformed")

        self.assertEqual(
            str(context.exception),
            "The Toss Cert ciphertext format is invalid.",
        )

    def test_session_exposes_no_encrypt_operation(self):
        session, _, _, _ = self.make_deterministic_session()

        self.assertFalse(hasattr(session, "encrypt"))

    def test_module_does_not_import_insecure_random_module(self):
        self.assertFalse(hasattr(crypto_service, "random"))
        self.assertIs(crypto_service.secrets.token_bytes, __import__(
            "secrets"
        ).token_bytes)

    def make_generator(self, **overrides):
        values = {"base64_public_key": self.public_key_base64}
        values.update(overrides)
        return TossCertCryptoSessionGenerator(**values)

    def make_deterministic_session(self):
        secret_key = bytes(range(32))
        iv = bytes(range(12))
        random_source = QueuedRandomBytes(
            secret_key,
            iv,
            b"s" * SHA1.digest_size,
        )
        generator = self.make_generator(
            random_bytes=random_source,
            session_id_factory=lambda: FIXED_SESSION_ID,
        )
        return generator.generate(), secret_key, iv, random_source

    def assert_generation_error(self, base64_public_key):
        with self.assertRaises(TossCertCryptoError) as context:
            TossCertCryptoSessionGenerator(
                base64_public_key=base64_public_key
            )

        self.assertEqual(
            context.exception.code,
            TossCertCryptoErrorCode.SESSION_GENERATION_FAILED,
        )

    def assert_crypto_error(self, session, encrypted_data, expected_code):
        with self.assertRaises(TossCertCryptoError) as context:
            session.decrypt(encrypted_data)

        self.assertEqual(context.exception.code, expected_code)

    @staticmethod
    def flip_combined_byte(encrypted_data, *, index):
        version, session_id, combined = encrypted_data.split("$")
        raw = bytearray(b64decode(combined))
        raw[index] ^= 1
        return f"{version}${session_id}${b64encode(raw).decode('ascii')}"


if __name__ == "__main__":
    unittest.main()
