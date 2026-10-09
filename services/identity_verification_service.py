import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from models import IdentityVerificationSession
from services.age_eligibility_service import (
    AgeEligibility,
    AgeEligibilityError,
    check_age_eligibility,
)
from services.identity_verification_provider import (
    IdentityVerificationProvider,
    IdentityVerificationProviderError,
    IdentityVerificationProviderErrorCode,
    IdentityVerificationStartResult,
    VerifiedIdentityResult,
)


ANDROID_REGISTRATION_PURPOSE = "android_registration"
PENDING_STATUS = "pending"
VERIFIED_STATUS = "verified"
FAILED_STATUS = "failed"
EXPIRED_STATUS = "expired"
CONSUMED_STATUS = "consumed"
SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")
_HMAC_DOMAIN = b"medicine-web.identity-subject.v1"


class IdentityVerificationServiceErrorCode(str, Enum):
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    SESSION_NOT_READY = "SESSION_NOT_READY"
    COMPLETION_IN_PROGRESS = "COMPLETION_IN_PROGRESS"
    COMPLETION_CONFLICT = "COMPLETION_CONFLICT"
    SESSION_CONSUMED = "SESSION_CONSUMED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_EXPIRED = "VERIFICATION_EXPIRED"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"


_ERROR_MESSAGES = {
    IdentityVerificationServiceErrorCode.SESSION_NOT_FOUND:
        "The identity verification session was not found.",
    IdentityVerificationServiceErrorCode.PROVIDER_MISMATCH:
        "The identity verification provider does not match the session.",
    IdentityVerificationServiceErrorCode.SESSION_NOT_READY:
        "The identity verification session is not ready for completion.",
    IdentityVerificationServiceErrorCode.COMPLETION_IN_PROGRESS:
        "Identity verification completion is already in progress.",
    IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT:
        "Identity verification completion ownership was lost.",
    IdentityVerificationServiceErrorCode.SESSION_CONSUMED:
        "The identity verification session has already been consumed.",
    IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE:
        "The identity verification provider is unavailable.",
    IdentityVerificationServiceErrorCode.VERIFICATION_FAILED:
        "Identity verification failed.",
    IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED:
        "Identity verification expired.",
    IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE:
        "The identity verification provider returned an invalid response.",
}


class IdentityVerificationServiceError(RuntimeError):
    """A stable orchestration error without provider or identity payloads."""

    def __init__(self, code: IdentityVerificationServiceErrorCode):
        if type(code) is not IdentityVerificationServiceErrorCode:
            raise TypeError(
                "code must be an IdentityVerificationServiceErrorCode value."
            )

        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class IdentityVerificationSessionStartResult:
    verification_session_id: str
    provider_transaction_id: str = field(repr=False)
    authentication_url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class IdentityVerificationCompletionResult:
    verification_session_id: str
    status: str
    age_eligibility: AgeEligibility


@dataclass(frozen=True)
class IdentityVerificationClaimRecoveryResult:
    recovered_count: int


class _InvalidProviderResult(RuntimeError):
    pass


def start_identity_verification(
    session,
    *,
    provider: IdentityVerificationProvider,
    action_time: datetime,
    session_ttl: timedelta,
    verification_session_id_factory: Callable[[], str] | None = None,
) -> IdentityVerificationSessionStartResult:
    """Create a session before calling the provider outside a transaction."""

    provider_name = _provider_storage_name(provider)
    action_time_utc = _normalize_action_time(action_time)
    expires_at = _calculate_expiry(action_time_utc, session_ttl)
    verification_session_id = _new_verification_session_id(
        verification_session_id_factory
    )

    verification_session = IdentityVerificationSession(
        verification_session_id=verification_session_id,
        purpose=ANDROID_REGISTRATION_PURPOSE,
        provider=provider_name,
        status=PENDING_STATUS,
        expires_at=expires_at,
        created_at=action_time_utc,
        updated_at=action_time_utc,
    )
    session.add(verification_session)

    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    try:
        provider_result = provider.start_verification()
        _validate_start_result(provider, provider_result)
    except IdentityVerificationProviderError as error:
        service_code = _service_code_for_provider_error(error.code)
        _finalize_start_failure(
            session,
            verification_session_id=verification_session_id,
            action_time=action_time_utc,
            failure_code=_stored_failure_code(service_code),
        )
        raise IdentityVerificationServiceError(service_code) from None
    except _InvalidProviderResult:
        _finalize_start_failure(
            session,
            verification_session_id=verification_session_id,
            action_time=action_time_utc,
            failure_code="invalid_provider_response",
        )
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE
        ) from None
    except Exception:
        _finalize_start_failure(
            session,
            verification_session_id=verification_session_id,
            action_time=action_time_utc,
            failure_code="invalid_provider_response",
        )
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE
        ) from None

    try:
        result = session.execute(
            update(IdentityVerificationSession)
            .where(
                IdentityVerificationSession.verification_session_id
                == verification_session_id,
                IdentityVerificationSession.status == PENDING_STATUS,
                IdentityVerificationSession.provider_transaction_id.is_(None),
                IdentityVerificationSession.completion_claim_token.is_(None),
                IdentityVerificationSession.completion_claimed_at.is_(None),
            )
            .values(
                provider_transaction_id=(
                    provider_result.provider_transaction_id
                ),
                updated_at=action_time_utc,
            )
            .execution_options(synchronize_session=False)
        )
    except Exception:
        session.rollback()
        _finalize_start_failure(
            session,
            verification_session_id=verification_session_id,
            action_time=action_time_utc,
            failure_code="invalid_provider_response",
        )
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE
        ) from None

    if result.rowcount != 1:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
        )

    session.commit()
    return IdentityVerificationSessionStartResult(
        verification_session_id=verification_session_id,
        provider_transaction_id=provider_result.provider_transaction_id,
        authentication_url=provider_result.authentication_url,
        expires_at=expires_at,
    )


def complete_identity_verification(
    session,
    *,
    verification_session_id: str,
    provider: IdentityVerificationProvider,
    action_time: datetime,
    identity_subject_hmac_key: bytes,
    claim_token_factory: Callable[[], str] | None = None,
) -> IdentityVerificationCompletionResult:
    """Claim, query, and finalize one identity verification session."""

    _validate_public_session_id(verification_session_id)
    provider_name = _provider_storage_name(provider)
    action_time_utc = _normalize_action_time(action_time)
    _validate_hmac_key(identity_subject_hmac_key)

    verification_session = session.scalar(
        select(IdentityVerificationSession).where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id
        )
    )

    if verification_session is None:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.SESSION_NOT_FOUND
        )

    if verification_session.provider != provider_name:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.PROVIDER_MISMATCH
        )

    if verification_session.status == VERIFIED_STATUS:
        result = _completion_result(verification_session)
        session.commit()
        return result

    if verification_session.status == CONSUMED_STATUS:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.SESSION_CONSUMED
        )

    if verification_session.status == FAILED_STATUS:
        session.rollback()
        raise IdentityVerificationServiceError(
            _service_code_for_stored_failure(
                verification_session.failure_code,
                fallback=(
                    IdentityVerificationServiceErrorCode.VERIFICATION_FAILED
                ),
            )
        )

    if verification_session.status == EXPIRED_STATUS:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED
        )

    if verification_session.status != PENDING_STATUS:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
        )

    if verification_session.completion_claim_token is not None:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_IN_PROGRESS
        )

    if verification_session.provider_transaction_id is None:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.SESSION_NOT_READY
        )

    if _stored_datetime_as_utc(verification_session.expires_at) <= action_time_utc:
        if _expire_pending_session(
            session,
            verification_session_id=verification_session_id,
            action_time=action_time_utc,
        ):
            raise IdentityVerificationServiceError(
                IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED
            )

        return _raise_current_completion_state(
            session,
            verification_session_id=verification_session_id,
        )

    provider_transaction_id = verification_session.provider_transaction_id
    claim_token = _new_claim_token(claim_token_factory)

    if not _claim_completion(
        session,
        verification_session_id=verification_session_id,
        claim_token=claim_token,
        action_time=action_time_utc,
    ):
        return _raise_current_completion_state(
            session,
            verification_session_id=verification_session_id,
        )

    try:
        provider_result = provider.get_verified_identity(
            provider_transaction_id=provider_transaction_id
        )
        _validate_verified_result(
            provider,
            provider_result,
            expected_transaction_id=provider_transaction_id,
        )
        age_eligibility = check_age_eligibility(
            birth_date=provider_result.birth_date,
            reference_date=action_time_utc.astimezone(
                SEOUL_TIMEZONE
            ).date(),
        )
        identity_subject_digest = _identity_subject_digest(
            provider_name=provider_name,
            identity_subject=provider_result.identity_subject,
            key=identity_subject_hmac_key,
        )
    except IdentityVerificationProviderError as error:
        _handle_provider_completion_error(
            session,
            verification_session_id=verification_session_id,
            claim_token=claim_token,
            action_time=action_time_utc,
            provider_error_code=error.code,
        )
    except (_InvalidProviderResult, AgeEligibilityError):
        _finalize_invalid_provider_response(
            session,
            verification_session_id=verification_session_id,
            claim_token=claim_token,
            action_time=action_time_utc,
        )
    except Exception:
        try:
            _release_completion_claim(
                session,
                verification_session_id=verification_session_id,
                claim_token=claim_token,
                action_time=action_time_utc,
            )
        except Exception:
            session.rollback()
        raise

    if not _finalize_verified_completion(
        session,
        verification_session_id=verification_session_id,
        claim_token=claim_token,
        action_time=action_time_utc,
        age_eligibility=age_eligibility,
        identity_subject_digest=identity_subject_digest,
    ):
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
        )

    return IdentityVerificationCompletionResult(
        verification_session_id=verification_session_id,
        status=VERIFIED_STATUS,
        age_eligibility=age_eligibility,
    )


def recover_stale_identity_verification_claims(
    session,
    *,
    action_time: datetime,
    claim_timeout: timedelta,
) -> IdentityVerificationClaimRecoveryResult:
    """Release stale completion leases without committing or changing status."""

    action_time_utc = _normalize_action_time(action_time)

    if (
        not isinstance(claim_timeout, timedelta)
        or claim_timeout <= timedelta(0)
    ):
        raise ValueError(
            "claim_timeout must be a positive datetime.timedelta value."
        )

    try:
        stale_before = action_time_utc - claim_timeout
    except OverflowError as error:
        raise ValueError(
            "The stale claim cutoff is outside the supported datetime range."
        ) from error

    result = session.execute(
        update(IdentityVerificationSession)
        .where(
            IdentityVerificationSession.status == PENDING_STATUS,
            IdentityVerificationSession.completion_claim_token.is_not(None),
            IdentityVerificationSession.completion_claimed_at.is_not(None),
            IdentityVerificationSession.completion_claimed_at < stale_before,
        )
        .values(
            completion_claim_token=None,
            completion_claimed_at=None,
            updated_at=action_time_utc,
        )
        .execution_options(synchronize_session=False)
    )
    return IdentityVerificationClaimRecoveryResult(
        recovered_count=result.rowcount or 0
    )


def _claim_completion(
    session,
    *,
    verification_session_id: str,
    claim_token: str,
    action_time: datetime,
) -> bool:
    result = session.execute(
        update(IdentityVerificationSession)
        .where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id,
            IdentityVerificationSession.status == PENDING_STATUS,
            IdentityVerificationSession.provider_transaction_id.is_not(None),
            IdentityVerificationSession.completion_claim_token.is_(None),
            IdentityVerificationSession.completion_claimed_at.is_(None),
            IdentityVerificationSession.expires_at > action_time,
        )
        .values(
            completion_claim_token=claim_token,
            completion_claimed_at=action_time,
            updated_at=action_time,
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return False

    session.commit()
    return True


def _finalize_verified_completion(
    session,
    *,
    verification_session_id: str,
    claim_token: str,
    action_time: datetime,
    age_eligibility: AgeEligibility,
    identity_subject_digest: str,
) -> bool:
    result = session.execute(
        update(IdentityVerificationSession)
        .where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id,
            IdentityVerificationSession.status == PENDING_STATUS,
            IdentityVerificationSession.completion_claim_token == claim_token,
        )
        .values(
            status=VERIFIED_STATUS,
            age_eligibility=age_eligibility.value,
            identity_subject_digest=identity_subject_digest,
            verified_at=action_time,
            completion_claim_token=None,
            completion_claimed_at=None,
            failure_code=None,
            updated_at=action_time,
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return False

    session.commit()
    return True


def _release_completion_claim(
    session,
    *,
    verification_session_id: str,
    claim_token: str,
    action_time: datetime,
    failure_code: str | None = None,
) -> bool:
    values = {
        "completion_claim_token": None,
        "completion_claimed_at": None,
        "updated_at": action_time,
    }
    if failure_code is not None:
        values["failure_code"] = failure_code

    result = session.execute(
        update(IdentityVerificationSession)
        .where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id,
            IdentityVerificationSession.status == PENDING_STATUS,
            IdentityVerificationSession.completion_claim_token == claim_token,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return False

    session.commit()
    return True


def _handle_provider_completion_error(
    session,
    *,
    verification_session_id: str,
    claim_token: str,
    action_time: datetime,
    provider_error_code: IdentityVerificationProviderErrorCode,
):
    service_code = _service_code_for_provider_error(provider_error_code)

    if service_code is IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE:
        if not _release_completion_claim(
            session,
            verification_session_id=verification_session_id,
            claim_token=claim_token,
            action_time=action_time,
            failure_code="provider_unavailable",
        ):
            raise IdentityVerificationServiceError(
                IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
            )
        raise IdentityVerificationServiceError(service_code) from None

    status = (
        EXPIRED_STATUS
        if service_code
        is IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED
        else FAILED_STATUS
    )
    if not _finalize_provider_failure(
        session,
        verification_session_id=verification_session_id,
        claim_token=claim_token,
        action_time=action_time,
        status=status,
        failure_code=_stored_failure_code(service_code),
    ):
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
        )
    raise IdentityVerificationServiceError(service_code) from None


def _finalize_invalid_provider_response(
    session,
    *,
    verification_session_id: str,
    claim_token: str,
    action_time: datetime,
):
    if not _finalize_provider_failure(
        session,
        verification_session_id=verification_session_id,
        claim_token=claim_token,
        action_time=action_time,
        status=FAILED_STATUS,
        failure_code="invalid_provider_response",
    ):
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
        )
    raise IdentityVerificationServiceError(
        IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE
    ) from None


def _finalize_provider_failure(
    session,
    *,
    verification_session_id: str,
    claim_token: str,
    action_time: datetime,
    status: str,
    failure_code: str,
) -> bool:
    result = session.execute(
        update(IdentityVerificationSession)
        .where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id,
            IdentityVerificationSession.status == PENDING_STATUS,
            IdentityVerificationSession.completion_claim_token == claim_token,
        )
        .values(
            status=status,
            completion_claim_token=None,
            completion_claimed_at=None,
            failure_code=failure_code,
            updated_at=action_time,
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return False

    session.commit()
    return True


def _expire_pending_session(
    session,
    *,
    verification_session_id: str,
    action_time: datetime,
) -> bool:
    result = session.execute(
        update(IdentityVerificationSession)
        .where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id,
            IdentityVerificationSession.status == PENDING_STATUS,
            IdentityVerificationSession.completion_claim_token.is_(None),
            IdentityVerificationSession.completion_claimed_at.is_(None),
            IdentityVerificationSession.expires_at <= action_time,
        )
        .values(
            status=EXPIRED_STATUS,
            failure_code="session_expired",
            updated_at=action_time,
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return False

    session.commit()
    return True


def _finalize_start_failure(
    session,
    *,
    verification_session_id: str,
    action_time: datetime,
    failure_code: str,
):
    try:
        session.execute(
            update(IdentityVerificationSession)
            .where(
                IdentityVerificationSession.verification_session_id
                == verification_session_id,
                IdentityVerificationSession.status == PENDING_STATUS,
                IdentityVerificationSession.provider_transaction_id.is_(None),
            )
            .values(
                status=FAILED_STATUS,
                failure_code=failure_code,
                updated_at=action_time,
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception:
        session.rollback()


def _raise_current_completion_state(
    session,
    *,
    verification_session_id: str,
):
    current = session.scalar(
        select(IdentityVerificationSession).where(
            IdentityVerificationSession.verification_session_id
            == verification_session_id
        )
    )

    if current is None:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.SESSION_NOT_FOUND
        )

    if current.status == VERIFIED_STATUS:
        result = _completion_result(current)
        session.commit()
        return result

    if current.status == CONSUMED_STATUS:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.SESSION_CONSUMED
        )

    if current.status == FAILED_STATUS:
        code = _service_code_for_stored_failure(
            current.failure_code,
            fallback=IdentityVerificationServiceErrorCode.VERIFICATION_FAILED,
        )
        session.rollback()
        raise IdentityVerificationServiceError(code)

    if current.status == EXPIRED_STATUS:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED
        )

    if current.completion_claim_token is not None:
        session.rollback()
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_IN_PROGRESS
        )

    session.rollback()
    raise IdentityVerificationServiceError(
        IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
    )


def _completion_result(
    verification_session: IdentityVerificationSession,
) -> IdentityVerificationCompletionResult:
    try:
        age_eligibility = AgeEligibility(
            verification_session.age_eligibility
        )
    except (TypeError, ValueError):
        raise IdentityVerificationServiceError(
            IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT
        ) from None

    return IdentityVerificationCompletionResult(
        verification_session_id=(
            verification_session.verification_session_id
        ),
        status=verification_session.status,
        age_eligibility=age_eligibility,
    )


def _identity_subject_digest(
    *,
    provider_name: str,
    identity_subject: str,
    key: bytes,
) -> str:
    message = b"\x00".join(
        (
            _HMAC_DOMAIN,
            provider_name.encode("utf-8"),
            identity_subject.encode("utf-8"),
        )
    )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _provider_storage_name(provider: IdentityVerificationProvider) -> str:
    if not isinstance(provider, IdentityVerificationProvider):
        raise TypeError(
            "provider must implement IdentityVerificationProvider."
        )

    return provider.provider.value.lower()


def _validate_start_result(provider, result):
    if (
        not isinstance(result, IdentityVerificationStartResult)
        or result.provider is not provider.provider
    ):
        raise _InvalidProviderResult()


def _validate_verified_result(
    provider,
    result,
    *,
    expected_transaction_id: str,
):
    if (
        not isinstance(result, VerifiedIdentityResult)
        or result.provider is not provider.provider
        or result.provider_transaction_id != expected_transaction_id
    ):
        raise _InvalidProviderResult()


def _normalize_action_time(value: datetime) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("action_time must be a timezone-aware datetime.")

    return value.astimezone(UTC)


def _stored_datetime_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


def _calculate_expiry(
    action_time: datetime,
    session_ttl: timedelta,
) -> datetime:
    if (
        not isinstance(session_ttl, timedelta)
        or session_ttl <= timedelta(0)
    ):
        raise ValueError(
            "session_ttl must be a positive datetime.timedelta value."
        )

    try:
        return action_time + session_ttl
    except OverflowError as error:
        raise ValueError(
            "The session expiry is outside the supported range."
        ) from error


def _new_verification_session_id(factory):
    factory = factory or (lambda: str(uuid4()))

    try:
        value = factory()
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(
            "verification_session_id_factory must return a UUID4 string."
        ) from None

    if parsed.version != 4 or str(parsed) != value.lower():
        raise ValueError(
            "verification_session_id_factory must return a UUID4 string."
        )

    return str(parsed)


def _validate_public_session_id(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("verification_session_id must be a non-empty string.")


def _new_claim_token(factory):
    factory = factory or (lambda: uuid4().hex)

    try:
        value = factory()
    except Exception:
        raise ValueError(
            "claim_token_factory must return a 32-character hex string."
        ) from None

    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise ValueError(
            "claim_token_factory must return a 32-character hex string."
        )

    return value.lower()


def _validate_hmac_key(value):
    if type(value) is not bytes or not value:
        raise ValueError("identity_subject_hmac_key must be non-empty bytes.")


def _service_code_for_provider_error(code):
    mapping = {
        IdentityVerificationProviderErrorCode.PROVIDER_UNAVAILABLE:
            IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE,
        IdentityVerificationProviderErrorCode.INVALID_PROVIDER_RESPONSE:
            IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE,
        IdentityVerificationProviderErrorCode.VERIFICATION_FAILED:
            IdentityVerificationServiceErrorCode.VERIFICATION_FAILED,
        IdentityVerificationProviderErrorCode.VERIFICATION_EXPIRED:
            IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED,
    }
    return mapping[code]


def _service_code_for_stored_failure(failure_code, *, fallback):
    mapping = {
        "provider_unavailable": (
            IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE
        ),
        "invalid_provider_response": (
            IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE
        ),
        "verification_failed": (
            IdentityVerificationServiceErrorCode.VERIFICATION_FAILED
        ),
        "verification_expired": (
            IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED
        ),
        "session_expired": (
            IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED
        ),
    }
    return mapping.get(failure_code, fallback)


def _stored_failure_code(code):
    return code.value.lower()
