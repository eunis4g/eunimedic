from base64 import b64decode, b64encode
import binascii
from enum import Enum
import hmac
import secrets
import uuid

from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA1
from Crypto.PublicKey import RSA
from Crypto.Signature.pss import MGF1


_SESSION_KEY_VERSION = "v1"
_AES_ALGORITHM = "AES_GCM"
_AES_KEY_LENGTH = 32
_AES_IV_LENGTH = 12
_AES_TAG_LENGTH = 16


class TossCertCryptoErrorCode(str, Enum):
    INVALID_CIPHERTEXT = "INVALID_CIPHERTEXT"
    DECRYPTION_FAILED = "DECRYPTION_FAILED"
    SESSION_GENERATION_FAILED = "SESSION_GENERATION_FAILED"


_ERROR_MESSAGES = {
    TossCertCryptoErrorCode.INVALID_CIPHERTEXT:
        "The Toss Cert ciphertext format is invalid.",
    TossCertCryptoErrorCode.DECRYPTION_FAILED:
        "The Toss Cert ciphertext could not be decrypted.",
    TossCertCryptoErrorCode.SESSION_GENERATION_FAILED:
        "The Toss Cert crypto session could not be generated.",
}


class TossCertCryptoError(RuntimeError):
    """A stable crypto error that never contains key or payload data."""

    def __init__(self, code: TossCertCryptoErrorCode):
        if type(code) is not TossCertCryptoErrorCode:
            raise TypeError("code must be a TossCertCryptoErrorCode value.")

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class TossCertCryptoSession:
    """Hold one result request's session key and AES-GCM state."""

    __slots__ = ("_session_key", "_session_id", "_secret_key", "_iv")

    def __init__(self, *, session_key, session_id, secret_key, iv):
        self._session_key = session_key
        self._session_id = session_id
        self._secret_key = secret_key
        self._iv = iv

    @property
    def session_key(self) -> str:
        return self._session_key

    def decrypt(self, encrypted_data: str) -> str:
        version, session_id, combined = self._parse_encrypted_data(
            encrypted_data
        )

        if (
            version != _SESSION_KEY_VERSION
            or not hmac.compare_digest(session_id, self._session_id)
        ):
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.INVALID_CIPHERTEXT
            )

        try:
            encrypted_and_tag = b64decode(combined, validate=True)
        except (binascii.Error, ValueError):
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.INVALID_CIPHERTEXT
            ) from None

        if len(encrypted_and_tag) < _AES_TAG_LENGTH:
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.INVALID_CIPHERTEXT
            )

        ciphertext = encrypted_and_tag[:-_AES_TAG_LENGTH]
        tag = encrypted_and_tag[-_AES_TAG_LENGTH:]

        try:
            cipher = AES.new(
                self._secret_key,
                AES.MODE_GCM,
                nonce=self._iv,
                mac_len=_AES_TAG_LENGTH,
            )
            cipher.update(self._secret_key)
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            return plaintext.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.DECRYPTION_FAILED
            ) from None

    def __repr__(self):
        return f"{type(self).__name__}()"

    @staticmethod
    def _parse_encrypted_data(encrypted_data):
        if not isinstance(encrypted_data, str) or not encrypted_data:
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.INVALID_CIPHERTEXT
            )

        parts = encrypted_data.split("$")
        if len(parts) != 3 or not all(parts):
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.INVALID_CIPHERTEXT
            )

        return parts


class TossCertCryptoSessionGenerator:
    """Generate a fresh Toss Cert result crypto session per request."""

    __slots__ = ("_public_key", "_random_bytes", "_session_id_factory")

    def __init__(
        self,
        *,
        base64_public_key: str,
        random_bytes=None,
        session_id_factory=None,
    ):
        if not isinstance(base64_public_key, str):
            raise TypeError("base64_public_key must be a string.")

        if random_bytes is not None and not callable(random_bytes):
            raise TypeError("random_bytes must be callable.")

        if session_id_factory is not None and not callable(
            session_id_factory
        ):
            raise TypeError("session_id_factory must be callable.")

        try:
            public_key_bytes = b64decode(
                base64_public_key.strip(),
                validate=True,
            )
            public_key = RSA.import_key(public_key_bytes)
            if public_key.has_private():
                raise ValueError("A public RSA key is required.")
        except (binascii.Error, IndexError, TypeError, ValueError):
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.SESSION_GENERATION_FAILED
            ) from None

        self._public_key = public_key
        self._random_bytes = random_bytes or secrets.token_bytes
        self._session_id_factory = session_id_factory or uuid.uuid4

    def generate(self) -> TossCertCryptoSession:
        try:
            session_id = self._generate_session_id()
            secret_key = self._read_random_bytes(_AES_KEY_LENGTH)
            iv = self._read_random_bytes(_AES_IV_LENGTH)

            secret_key_base64 = b64encode(secret_key).decode("ascii")
            iv_base64 = b64encode(iv).decode("ascii")
            session_aes_key = (
                f"{_AES_ALGORITHM}${secret_key_base64}${iv_base64}"
            ).encode("utf-8")

            cipher = PKCS1_OAEP.new(
                self._public_key,
                hashAlgo=SHA1,
                mgfunc=_mgf1_sha1,
                randfunc=self._read_random_bytes,
            )
            encrypted_session_aes_key = cipher.encrypt(session_aes_key)
            encrypted_session_aes_key_base64 = b64encode(
                encrypted_session_aes_key
            ).decode("ascii")
            session_key = (
                f"{_SESSION_KEY_VERSION}${session_id}$"
                f"{encrypted_session_aes_key_base64}"
            )
        except Exception:
            raise TossCertCryptoError(
                TossCertCryptoErrorCode.SESSION_GENERATION_FAILED
            ) from None

        return TossCertCryptoSession(
            session_key=session_key,
            session_id=session_id,
            secret_key=secret_key,
            iv=iv,
        )

    def __repr__(self):
        return f"{type(self).__name__}()"

    def _generate_session_id(self):
        session_id = str(self._session_id_factory())
        parsed_session_id = uuid.UUID(session_id)
        if (
            parsed_session_id.version != 4
            or str(parsed_session_id) != session_id
        ):
            raise ValueError("session_id_factory must return a UUID4 value.")
        return session_id

    def _read_random_bytes(self, length):
        value = self._random_bytes(length)
        if not isinstance(value, bytes) or len(value) != length:
            raise ValueError(
                "random_bytes must return the requested number of bytes."
            )
        return value


def _mgf1_sha1(seed, length):
    return MGF1(seed, length, SHA1)
