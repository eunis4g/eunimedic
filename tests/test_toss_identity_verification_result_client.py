import ast
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, date, datetime
import inspect
import math
import unittest

import requests

import services.toss_identity_verification_result_client as result_service
from services.toss_cert_access_token_client import (
    TossCertAccessToken,
    TossCertAccessTokenError,
    TossCertAccessTokenErrorCode,
)
from services.toss_cert_crypto import (
    TossCertCryptoError,
    TossCertCryptoErrorCode,
)
from services.toss_identity_verification_result_client import (
    TOSS_IDENTITY_VERIFICATION_RESULT_ENDPOINT,
    TossIdentityVerificationResult,
    TossIdentityVerificationResultClient,
    TossIdentityVerificationResultError,
    TossIdentityVerificationResultErrorCode,
)


ACCESS_TOKEN = "synthetic-access-token-sensitive"
TRANSACTION_ID = "synthetic-provider-transaction-sensitive"
OTHER_TRANSACTION_ID = "other-provider-transaction-sensitive"
SESSION_KEY_PREFIX = "synthetic-session-key-sensitive"
BIRTHDAY_CIPHERTEXT = "encrypted-birthday-sensitive"
DI_CIPHERTEXT = "encrypted-di-sensitive"
SIGNATURE = "synthetic-base64-der-signature-sensitive"
IDENTITY_SUBJECT = "synthetic-decrypted-di-sensitive"
RAW_RESPONSE_BODY = "raw-provider-response-sensitive"


class StubAccessTokenClient:

    def __init__(self, *, token_result=None, error=None):
        self.token_result = token_result
        self.error = error
        self.call_count = 0

    def get_access_token(self):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.token_result


class FakeCryptoSession:

    def __init__(self, *, session_key, decryptions=None, errors=None):
        self.session_key = session_key
        self.decryptions = decryptions or {}
        self.errors = errors or {}
        self.decrypt_calls = []

    def decrypt(self, ciphertext):
        self.decrypt_calls.append(ciphertext)
        if ciphertext in self.errors:
            raise self.errors[ciphertext]
        return self.decryptions[ciphertext]

    def __repr__(self):
        return "FakeCryptoSession()"


class FakeCryptoSessionGenerator:

    def __init__(self, *, error=None, session_factory=None):
        self.error = error
        self.session_factory = session_factory
        self.call_count = 0
        self.sessions = []

    def generate(self):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        if self.session_factory is None:
            session = make_crypto_session(self.call_count)
        else:
            session = self.session_factory(self.call_count)
        self.sessions.append(session)
        return session


class FakeResponse:

    def __init__(self, *, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = RAW_RESPONSE_BODY

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class RecordingSession:

    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        raise AssertionError("The result endpoint must use POST.")


def make_crypto_session(sequence=1, *, errors=None, decryptions=None):
    values = {
        BIRTHDAY_CIPHERTEXT: "20000102",
        DI_CIPHERTEXT: IDENTITY_SUBJECT,
    }
    if decryptions is not None:
        values.update(decryptions)
    return FakeCryptoSession(
        session_key=f"{SESSION_KEY_PREFIX}-{sequence}",
        decryptions=values,
        errors=errors,
    )


def valid_payload():
    return {
        "resultType": "SUCCESS",
        "success": {
            "txId": TRANSACTION_ID,
            "status": "COMPLETED",
            "signature": SIGNATURE,
            "completedDt": "2026-10-09T12:01:00+09:00",
            "requestedDt": "2026-10-09T12:00:00+09:00",
            "personalData": {
                "ci": "encrypted-ci-must-not-decrypt",
                "name": "encrypted-name-must-not-decrypt",
                "birthday": BIRTHDAY_CIPHERTEXT,
                "gender": "MALE",
                "nationality": "LOCAL",
                "di": DI_CIPHERTEXT,
                "ageGroup": "ADULT",
            },
        },
    }


def fail_payload(error_code):
    return {
        "resultType": "FAIL",
        "error": {
            "errorType": 400,
            "errorCode": error_code,
            "reason": RAW_RESPONSE_BODY,
            "title": "sensitive-title",
            "data": {"sensitive": "value"},
        },
    }


def synthetic_token_result():
    return TossCertAccessToken(
        access_token=ACCESS_TOKEN,
        expires_at=datetime(2026, 10, 9, 13, 0, tzinfo=UTC),
    )


class TossIdentityVerificationResultClientTest(unittest.TestCase):

    def test_endpoint_is_exact_official_result_endpoint(self):
        self.assertEqual(
            TOSS_IDENTITY_VERIFICATION_RESULT_ENDPOINT,
            "https://cert.toss.im/api/v2/sign/user/auth/id/result",
        )

    def test_requires_access_token_client_contract(self):
        for value in (None, object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    self.make_client(access_token_client=value)

    def test_requires_crypto_session_generator_contract(self):
        for value in (None, object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    self.make_client(crypto_session_generator=value)

    def test_requires_http_post_contract(self):
        with self.assertRaises(TypeError):
            self.make_client(http_session=object())

    def test_rejects_zero_timeout(self):
        with self.assertRaises(ValueError):
            self.make_client(timeout=0)

    def test_rejects_negative_timeout(self):
        with self.assertRaises(ValueError):
            self.make_client(timeout=-0.1)

    def test_rejects_boolean_timeout(self):
        with self.assertRaises(ValueError):
            self.make_client(timeout=True)

    def test_rejects_nan_and_infinite_timeout(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_client(timeout=value)

    def test_rejects_non_string_transaction_id(self):
        _, client, _, _ = self.make_success_client()
        with self.assertRaises(TypeError):
            client.get_verified_identity(None)

    def test_rejects_empty_transaction_id(self):
        _, client, _, _ = self.make_success_client()
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(ValueError):
                    client.get_verified_identity(value)

    def test_uses_post_only(self):
        session, client, _, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.get_calls, [])

    def test_posts_to_result_endpoint(self):
        session, client, _, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            session.post_calls[0][0],
            TOSS_IDENTITY_VERIFICATION_RESULT_ENDPOINT,
        )

    def test_sends_bearer_access_token(self):
        session, client, _, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            session.post_calls[0][1]["headers"]["Authorization"],
            f"Bearer {ACCESS_TOKEN}",
        )

    def test_sends_json_content_type(self):
        session, client, _, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            session.post_calls[0][1]["headers"]["Content-Type"],
            "application/json",
        )

    def test_sends_transaction_id_in_json_body(self):
        session, client, _, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            session.post_calls[0][1]["json"]["txId"],
            TRANSACTION_ID,
        )

    def test_sends_generated_session_key_in_json_body(self):
        session, client, _, generator = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            session.post_calls[0][1]["json"]["sessionKey"],
            generator.sessions[0].session_key,
        )

    def test_json_body_contains_only_required_fields(self):
        session, client, _, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            set(session.post_calls[0][1]["json"]),
            {"txId", "sessionKey"},
        )

    def test_passes_explicit_timeout(self):
        session, client, _, _ = self.make_success_client(timeout=7.5)
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(session.post_calls[0][1]["timeout"], 7.5)

    def test_disables_redirects(self):
        session, client, _, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertIs(session.post_calls[0][1]["allow_redirects"], False)

    def test_obtains_token_once_per_call(self):
        _, client, token_client, _ = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(token_client.call_count, 1)

    def test_generates_one_fresh_crypto_session_per_call(self):
        session = RecordingSession(FakeResponse(payload=valid_payload()))
        generator = FakeCryptoSessionGenerator()
        client = self.make_client(
            http_session=session,
            crypto_session_generator=generator,
        )

        client.get_verified_identity(TRANSACTION_ID)
        client.get_verified_identity(TRANSACTION_ID)

        self.assertEqual(generator.call_count, 2)
        self.assertIsNot(generator.sessions[0], generator.sessions[1])
        self.assertNotEqual(
            session.post_calls[0][1]["json"]["sessionKey"],
            session.post_calls[1][1]["json"]["sessionKey"],
        )

    def test_same_session_that_supplies_key_decrypts_birthday_and_di(self):
        _, client, _, generator = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            generator.sessions[0].decrypt_calls,
            [BIRTHDAY_CIPHERTEXT, DI_CIPHERTEXT],
        )

    def test_success_returns_exact_result_type(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        self.assertIs(type(result), TossIdentityVerificationResult)

    def test_success_preserves_transaction_id(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(result.provider_transaction_id, TRANSACTION_ID)

    def test_success_parses_birth_date(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(result.birth_date, date(2000, 1, 2))

    def test_success_uses_decrypted_di_as_identity_subject(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(result.identity_subject, IDENTITY_SUBJECT)

    def test_success_preserves_signature_without_validation(self):
        payload = valid_payload()
        payload["success"]["signature"] = "opaque-not-validated-here"
        _, client, _, _ = self.make_success_client(payload=payload)
        result = client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(result.signature, "opaque-not-validated-here")

    def test_result_is_immutable(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        with self.assertRaises(FrozenInstanceError):
            result.identity_subject = "replacement"

    def test_result_contains_only_minimal_fields(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        self.assertEqual(
            {item.name for item in fields(result)},
            {
                "provider_transaction_id",
                "birth_date",
                "identity_subject",
                "signature",
            },
        )

    def test_result_does_not_retain_timestamps_or_raw_response(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        for excluded in (
            "completedDt",
            "requestedDt",
            "completed_at",
            "requested_at",
            "raw_response",
            "personal_data",
        ):
            self.assertFalse(hasattr(result, excluded))

    def test_does_not_decrypt_unneeded_personal_data(self):
        _, client, _, generator = self.make_success_client()
        client.get_verified_identity(TRANSACTION_ID)
        calls = generator.sessions[0].decrypt_calls
        self.assertNotIn("encrypted-ci-must-not-decrypt", calls)
        self.assertNotIn("encrypted-name-must-not-decrypt", calls)
        self.assertEqual(len(calls), 2)

    def test_ignores_gender_nationality_and_age_group(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        for excluded in ("gender", "nationality", "age_group"):
            self.assertFalse(hasattr(result, excluded))

    def test_ce3102_maps_to_verification_pending(self):
        self.assert_fail_mapping(
            "CE3102",
            TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING,
        )

    def test_ce3103_maps_to_verification_expired(self):
        self.assert_fail_mapping(
            "CE3103",
            TossIdentityVerificationResultErrorCode.VERIFICATION_EXPIRED,
        )

    def test_ce3006_maps_to_age_restricted(self):
        self.assert_fail_mapping(
            "CE3006",
            TossIdentityVerificationResultErrorCode.AGE_RESTRICTED,
        )

    def test_ce3101_maps_to_result_query_limit_exceeded(self):
        self.assert_fail_mapping(
            "CE3101",
            TossIdentityVerificationResultErrorCode.RESULT_QUERY_LIMIT_EXCEEDED,
        )

    def test_ce1000_maps_to_token_error(self):
        self.assert_fail_mapping(
            "CE1000",
            TossIdentityVerificationResultErrorCode.TOKEN_ERROR,
        )

    def test_ce3100_maps_to_request_rejected(self):
        self.assert_fail_mapping(
            "CE3100",
            TossIdentityVerificationResultErrorCode.REQUEST_REJECTED,
        )

    def test_ce0001_maps_to_provider_unavailable(self):
        self.assert_fail_mapping(
            "CE0001",
            TossIdentityVerificationResultErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_ce0002_maps_to_provider_unavailable(self):
        self.assert_fail_mapping(
            "CE0002",
            TossIdentityVerificationResultErrorCode.PROVIDER_UNAVAILABLE,
        )

    def test_unknown_fail_maps_to_request_rejected(self):
        self.assert_fail_mapping(
            "CE9999",
            TossIdentityVerificationResultErrorCode.REQUEST_REJECTED,
        )

    def test_non_2xx_structured_pending_is_domain_error(self):
        self.assert_fail_mapping(
            "CE3102",
            TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING,
            status_code=400,
        )

    def test_non_2xx_structured_expiry_is_domain_error(self):
        self.assert_fail_mapping(
            "CE3103",
            TossIdentityVerificationResultErrorCode.VERIFICATION_EXPIRED,
            status_code=400,
        )

    def test_2xx_fail_uses_domain_mapping(self):
        self.assert_fail_mapping(
            "CE3006",
            TossIdentityVerificationResultErrorCode.AGE_RESTRICTED,
            status_code=200,
        )

    def test_non_json_non_2xx_maps_to_http_error(self):
        response = FakeResponse(
            status_code=502,
            json_error=ValueError(RAW_RESPONSE_BODY),
        )
        self.assert_response_error(
            response,
            TossIdentityVerificationResultErrorCode.HTTP_ERROR,
        )

    def test_non_json_2xx_maps_to_invalid_response(self):
        response = FakeResponse(
            status_code=200,
            json_error=ValueError(RAW_RESPONSE_BODY),
        )
        self.assert_response_error(
            response,
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_non_object_2xx_maps_to_invalid_response(self):
        for payload in ([], None, "value"):
            with self.subTest(payload_type=type(payload).__name__):
                self.assert_payload_error(
                    payload,
                    TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
                )

    def test_non_object_non_2xx_maps_to_http_error(self):
        response = FakeResponse(status_code=500, payload=[])
        self.assert_response_error(
            response,
            TossIdentityVerificationResultErrorCode.HTTP_ERROR,
        )

    def test_fail_without_error_maps_to_invalid_response(self):
        self.assert_payload_error(
            {"resultType": "FAIL"},
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_fail_with_non_object_error_maps_to_invalid_response(self):
        self.assert_payload_error(
            {"resultType": "FAIL", "error": []},
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_fail_without_error_code_maps_to_invalid_response(self):
        self.assert_payload_error(
            {"resultType": "FAIL", "error": {}},
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_fail_with_empty_error_code_maps_to_invalid_response(self):
        self.assert_payload_error(
            {"resultType": "FAIL", "error": {"errorCode": "   "}},
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_missing_result_type_is_invalid_response(self):
        self.assert_payload_error(
            {"success": valid_payload()["success"]},
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_unknown_result_type_is_invalid_response(self):
        self.assert_payload_error(
            {"resultType": "UNKNOWN"},
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_missing_success_is_invalid_response(self):
        self.assert_payload_error(
            {"resultType": "SUCCESS"},
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_non_object_success_is_invalid_response(self):
        payload = valid_payload()
        payload["success"] = []
        self.assert_payload_error(
            payload,
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def test_missing_transaction_id_is_invalid_response(self):
        payload = valid_payload()
        del payload["success"]["txId"]
        self.assert_invalid_payload(payload)

    def test_transaction_mismatch_is_invalid_response(self):
        payload = valid_payload()
        payload["success"]["txId"] = OTHER_TRANSACTION_ID
        self.assert_invalid_payload(payload)

    def test_status_must_be_completed(self):
        for status in (None, "REQUESTED", "IN_PROGRESS", "EXPIRED"):
            with self.subTest(status=status):
                payload = valid_payload()
                payload["success"]["status"] = status
                self.assert_invalid_payload(payload)

    def test_missing_signature_is_invalid_response(self):
        payload = valid_payload()
        del payload["success"]["signature"]
        self.assert_invalid_payload(payload)

    def test_empty_signature_is_invalid_response(self):
        payload = valid_payload()
        payload["success"]["signature"] = " "
        self.assert_invalid_payload(payload)

    def test_missing_personal_data_is_invalid_response(self):
        payload = valid_payload()
        del payload["success"]["personalData"]
        self.assert_invalid_payload(payload)

    def test_non_object_personal_data_is_invalid_response(self):
        payload = valid_payload()
        payload["success"]["personalData"] = []
        self.assert_invalid_payload(payload)

    def test_missing_birthday_is_invalid_response(self):
        payload = valid_payload()
        del payload["success"]["personalData"]["birthday"]
        self.assert_invalid_payload(payload)

    def test_empty_encrypted_birthday_is_invalid_response(self):
        payload = valid_payload()
        payload["success"]["personalData"]["birthday"] = ""
        self.assert_invalid_payload(payload)

    def test_missing_di_is_invalid_response(self):
        payload = valid_payload()
        del payload["success"]["personalData"]["di"]
        self.assert_invalid_payload(payload)

    def test_empty_encrypted_di_is_invalid_response(self):
        payload = valid_payload()
        payload["success"]["personalData"]["di"] = " "
        self.assert_invalid_payload(payload)

    def test_birthday_decrypt_failure_maps_to_crypto_error(self):
        error = TossCertCryptoError(TossCertCryptoErrorCode.DECRYPTION_FAILED)
        crypto_session = make_crypto_session(
            errors={BIRTHDAY_CIPHERTEXT: error}
        )
        self.assert_crypto_session_error(crypto_session)

    def test_di_decrypt_failure_maps_to_crypto_error(self):
        error = TossCertCryptoError(TossCertCryptoErrorCode.DECRYPTION_FAILED)
        crypto_session = make_crypto_session(errors={DI_CIPHERTEXT: error})
        self.assert_crypto_session_error(crypto_session)

    def test_malformed_decrypted_birthday_is_invalid_response(self):
        for value in ("2000-01-02", "20001340", "abcdefgh"):
            with self.subTest(value=value):
                crypto_session = make_crypto_session(
                    decryptions={BIRTHDAY_CIPHERTEXT: value}
                )
                self.assert_crypto_session_error(
                    crypto_session,
                    TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
                )

    def test_empty_decrypted_di_is_invalid_response(self):
        for value in ("", "   "):
            with self.subTest(value_length=len(value)):
                crypto_session = make_crypto_session(
                    decryptions={DI_CIPHERTEXT: value}
                )
                self.assert_crypto_session_error(
                    crypto_session,
                    TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
                )

    def test_di_format_is_not_overvalidated(self):
        opaque_di = "opaque DI/value:+_="
        crypto_session = make_crypto_session(
            decryptions={DI_CIPHERTEXT: opaque_di}
        )
        generator = FakeCryptoSessionGenerator(
            session_factory=lambda sequence: crypto_session
        )
        _, client, _, _ = self.make_success_client(
            crypto_session_generator=generator
        )
        self.assertEqual(
            client.get_verified_identity(TRANSACTION_ID).identity_subject,
            opaque_di,
        )

    def test_token_client_error_maps_to_token_error_without_http_call(self):
        token_error = TossCertAccessTokenError(
            TossCertAccessTokenErrorCode.NETWORK_ERROR
        )
        token_client = StubAccessTokenClient(error=token_error)
        session = RecordingSession(FakeResponse(payload=valid_payload()))
        client = self.make_client(
            access_token_client=token_client,
            http_session=session,
        )
        self.assert_client_error(
            client,
            TossIdentityVerificationResultErrorCode.TOKEN_ERROR,
        )
        self.assertEqual(session.post_calls, [])

    def test_invalid_token_result_maps_to_token_error(self):
        token_client = StubAccessTokenClient(token_result=object())
        client = self.make_client(access_token_client=token_client)
        self.assert_client_error(
            client,
            TossIdentityVerificationResultErrorCode.TOKEN_ERROR,
        )

    def test_crypto_generation_error_maps_to_crypto_error(self):
        error = TossCertCryptoError(
            TossCertCryptoErrorCode.SESSION_GENERATION_FAILED
        )
        generator = FakeCryptoSessionGenerator(error=error)
        session = RecordingSession(FakeResponse(payload=valid_payload()))
        client = self.make_client(
            crypto_session_generator=generator,
            http_session=session,
        )
        self.assert_client_error(
            client,
            TossIdentityVerificationResultErrorCode.CRYPTO_ERROR,
        )
        self.assertEqual(session.post_calls, [])

    def test_invalid_generated_crypto_session_maps_to_crypto_error(self):
        generator = FakeCryptoSessionGenerator(
            session_factory=lambda sequence: object()
        )
        client = self.make_client(crypto_session_generator=generator)
        self.assert_client_error(
            client,
            TossIdentityVerificationResultErrorCode.CRYPTO_ERROR,
        )

    def test_network_error_maps_without_automatic_retry(self):
        session = RecordingSession(error=requests.Timeout(RAW_RESPONSE_BODY))
        client = self.make_client(http_session=session)
        self.assert_client_error(
            client,
            TossIdentityVerificationResultErrorCode.NETWORK_ERROR,
        )
        self.assertEqual(len(session.post_calls), 1)

    def test_domain_error_does_not_retry(self):
        session = RecordingSession(
            FakeResponse(status_code=400, payload=fail_payload("CE3102"))
        )
        client = self.make_client(http_session=session)
        self.assert_client_error(
            client,
            TossIdentityVerificationResultErrorCode.VERIFICATION_PENDING,
        )
        self.assertEqual(len(session.post_calls), 1)

    def test_token_error_response_does_not_retry(self):
        session = RecordingSession(
            FakeResponse(status_code=401, payload=fail_payload("CE1000"))
        )
        client = self.make_client(http_session=session)
        self.assert_client_error(
            client,
            TossIdentityVerificationResultErrorCode.TOKEN_ERROR,
        )
        self.assertEqual(len(session.post_calls), 1)

    def test_result_repr_hides_all_sensitive_fields(self):
        _, client, _, _ = self.make_success_client()
        result = client.get_verified_identity(TRANSACTION_ID)
        representation = repr(result)
        for sensitive in (
            TRANSACTION_ID,
            result.birth_date.isoformat(),
            IDENTITY_SUBJECT,
            SIGNATURE,
        ):
            self.assertNotIn(sensitive, representation)

    def test_client_repr_hides_token_and_session_key(self):
        _, client, _, _ = self.make_success_client()
        representation = repr(client)
        self.assertNotIn(ACCESS_TOKEN, representation)
        self.assertNotIn(SESSION_KEY_PREFIX, representation)

    def test_error_hides_raw_fail_fields(self):
        response = FakeResponse(status_code=400, payload=fail_payload("CE3102"))
        with self.assertRaises(TossIdentityVerificationResultError) as context:
            self.make_client(
                http_session=RecordingSession(response)
            ).get_verified_identity(TRANSACTION_ID)
        representation = repr(context.exception)
        self.assertNotIn(RAW_RESPONSE_BODY, representation)
        self.assertNotIn("sensitive-title", representation)
        self.assertNotIn("CE3102", representation)

    def test_crypto_error_hides_ciphertext_and_plaintext(self):
        error = TossCertCryptoError(TossCertCryptoErrorCode.DECRYPTION_FAILED)
        crypto_session = make_crypto_session(
            errors={BIRTHDAY_CIPHERTEXT: error}
        )
        generator = FakeCryptoSessionGenerator(
            session_factory=lambda sequence: crypto_session
        )
        _, client, _, _ = self.make_success_client(
            crypto_session_generator=generator
        )
        with self.assertRaises(TossIdentityVerificationResultError) as context:
            client.get_verified_identity(TRANSACTION_ID)
        representation = repr(context.exception)
        self.assertNotIn(BIRTHDAY_CIPHERTEXT, representation)
        self.assertNotIn("20000102", representation)
        self.assertNotIn(DI_CIPHERTEXT, representation)

    def test_error_enum_is_stable_and_minimal(self):
        self.assertEqual(
            {code.value for code in TossIdentityVerificationResultErrorCode},
            {
                "NETWORK_ERROR",
                "HTTP_ERROR",
                "REQUEST_REJECTED",
                "INVALID_RESPONSE",
                "TOKEN_ERROR",
                "CRYPTO_ERROR",
                "VERIFICATION_PENDING",
                "VERIFICATION_EXPIRED",
                "AGE_RESTRICTED",
                "RESULT_QUERY_LIMIT_EXCEEDED",
                "PROVIDER_UNAVAILABLE",
            },
        )

    def test_error_requires_controlled_enum(self):
        with self.assertRaises(TypeError):
            TossIdentityVerificationResultError("VERIFICATION_PENDING")

    def test_service_has_no_database_flask_or_hmac_dependency(self):
        syntax_tree = ast.parse(inspect.getsource(result_service))
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
            {"flask", "sqlalchemy", "models", "hmac"}.isdisjoint(
                imported_roots
            )
        )

    def test_service_does_not_implement_provider_contract(self):
        source = inspect.getsource(result_service)
        self.assertNotIn("IdentityVerificationProvider", source)
        self.assertNotIn("VerifiedIdentityResult", source)

    def test_service_contains_no_logging_or_print_calls(self):
        syntax_tree = ast.parse(inspect.getsource(result_service))
        called_names = {
            node.func.id
            for node in ast.walk(syntax_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("print", called_names)
        self.assertNotIn("log", called_names)

    def make_client(self, **overrides):
        values = {
            "access_token_client": StubAccessTokenClient(
                token_result=synthetic_token_result()
            ),
            "crypto_session_generator": FakeCryptoSessionGenerator(),
            "http_session": RecordingSession(
                FakeResponse(payload=valid_payload())
            ),
            "timeout": 5.0,
        }
        values.update(overrides)
        return TossIdentityVerificationResultClient(**values)

    def make_success_client(self, *, payload=None, **overrides):
        token_client = overrides.pop(
            "access_token_client",
            StubAccessTokenClient(token_result=synthetic_token_result()),
        )
        generator = overrides.pop(
            "crypto_session_generator",
            FakeCryptoSessionGenerator(),
        )
        session = overrides.pop(
            "http_session",
            RecordingSession(
                FakeResponse(payload=payload if payload is not None else valid_payload())
            ),
        )
        client = self.make_client(
            access_token_client=token_client,
            crypto_session_generator=generator,
            http_session=session,
            **overrides,
        )
        return session, client, token_client, generator

    def assert_fail_mapping(self, toss_code, expected_code, *, status_code=400):
        response = FakeResponse(
            status_code=status_code,
            payload=fail_payload(toss_code),
        )
        self.assert_response_error(response, expected_code)

    def assert_invalid_payload(self, payload):
        self.assert_payload_error(
            payload,
            TossIdentityVerificationResultErrorCode.INVALID_RESPONSE,
        )

    def assert_payload_error(self, payload, expected_code):
        self.assert_response_error(FakeResponse(payload=payload), expected_code)

    def assert_response_error(self, response, expected_code):
        session = RecordingSession(response)
        client = self.make_client(http_session=session)
        self.assert_client_error(client, expected_code)

    def assert_client_error(self, client, expected_code):
        with self.assertRaises(TossIdentityVerificationResultError) as context:
            client.get_verified_identity(TRANSACTION_ID)
        self.assertIs(context.exception.code, expected_code)

    def assert_crypto_session_error(
        self,
        crypto_session,
        expected_code=TossIdentityVerificationResultErrorCode.CRYPTO_ERROR,
    ):
        generator = FakeCryptoSessionGenerator(
            session_factory=lambda sequence: crypto_session
        )
        _, client, _, _ = self.make_success_client(
            crypto_session_generator=generator
        )
        self.assert_client_error(client, expected_code)


if __name__ == "__main__":
    unittest.main()
