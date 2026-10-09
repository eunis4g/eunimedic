from enum import Enum
import os
from pathlib import Path
import sys

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.toss_cert_crypto import (  # noqa: E402
    TossCertCryptoError,
    TossCertCryptoSession,
    TossCertCryptoSessionGenerator,
)


class TossRsaPublicKeyCheckErrorCode(str, Enum):
    INVALID_RSA_PUBLIC_KEY = "INVALID_RSA_PUBLIC_KEY"
    CRYPTO_SESSION_GENERATION_FAILED = "CRYPTO_SESSION_GENERATION_FAILED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


def run_toss_rsa_public_key_check(
    *,
    environ,
    generator_factory=TossCertCryptoSessionGenerator,
    output=print,
) -> int:
    """Validate one configured Toss RSA public key entirely in memory."""

    public_key = environ.get("TOSS_CERT_RSA_PUBLIC_KEY_BASE64")
    if not isinstance(public_key, str) or not public_key.strip():
        _print_failure(
            output,
            TossRsaPublicKeyCheckErrorCode.INVALID_RSA_PUBLIC_KEY,
        )
        return 1

    try:
        generator = generator_factory(base64_public_key=public_key)
    except (TossCertCryptoError, TypeError, ValueError):
        _print_failure(
            output,
            TossRsaPublicKeyCheckErrorCode.INVALID_RSA_PUBLIC_KEY,
        )
        return 1
    except Exception:
        _print_failure(
            output,
            TossRsaPublicKeyCheckErrorCode.UNEXPECTED_ERROR,
        )
        return 1

    try:
        crypto_session = generator.generate()
        if not isinstance(crypto_session, TossCertCryptoSession):
            raise TypeError("Unexpected crypto session type.")
    except Exception:
        error_code = (
            TossRsaPublicKeyCheckErrorCode.CRYPTO_SESSION_GENERATION_FAILED
        )
        _print_failure(
            output,
            error_code,
        )
        return 1

    output("Toss RSA public key check: OK")
    output("RSA public key parsed: OK")
    output("Crypto session generation: OK")
    return 0


def main(
    *,
    environ=None,
    generator_factory=TossCertCryptoSessionGenerator,
    output=print,
    load_environment=True,
) -> int:
    if load_environment:
        try:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        except Exception:
            _print_failure(
                output,
                TossRsaPublicKeyCheckErrorCode.INVALID_RSA_PUBLIC_KEY,
            )
            return 1

    return run_toss_rsa_public_key_check(
        environ=os.environ if environ is None else environ,
        generator_factory=generator_factory,
        output=output,
    )


def _print_failure(output, code):
    output("Toss RSA public key check: FAILED")
    output(f"Error code: {code.value}")


if __name__ == "__main__":
    raise SystemExit(main())
