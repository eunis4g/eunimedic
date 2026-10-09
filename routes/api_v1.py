from datetime import UTC, datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from models import db
from services.identity_verification_service import (
    IdentityVerificationServiceError,
    IdentityVerificationServiceErrorCode,
    complete_identity_verification,
    start_identity_verification,
)
from services.toss_identity_verification_wiring import (
    TossIdentityVerificationDependencies,
)


API_V1_DEPENDENCIES_EXTENSION_KEY = (
    "toss_identity_verification_dependencies"
)
IDENTITY_VERIFICATION_SESSION_TTL = timedelta(minutes=10)

api_v1_blueprint = Blueprint(
    "api_v1",
    __name__,
    url_prefix="/api/v1",
)


class _ApiRequestError(ValueError):

    def __init__(self, *, code, message, status_code):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(code)


_SERVICE_ERROR_RESPONSES = {
    IdentityVerificationServiceErrorCode.SESSION_NOT_FOUND: (
        404,
        "본인확인 세션을 찾을 수 없습니다.",
    ),
    IdentityVerificationServiceErrorCode.PROVIDER_MISMATCH: (
        409,
        "본인확인 세션 상태가 올바르지 않습니다.",
    ),
    IdentityVerificationServiceErrorCode.SESSION_NOT_READY: (
        409,
        "본인확인 세션이 아직 준비되지 않았습니다.",
    ),
    IdentityVerificationServiceErrorCode.COMPLETION_IN_PROGRESS: (
        409,
        "본인확인 완료 처리가 이미 진행 중입니다.",
    ),
    IdentityVerificationServiceErrorCode.COMPLETION_CONFLICT: (
        409,
        "본인확인 완료 요청이 충돌했습니다.",
    ),
    IdentityVerificationServiceErrorCode.SESSION_CONSUMED: (
        409,
        "본인확인 세션이 이미 사용되었습니다.",
    ),
    IdentityVerificationServiceErrorCode.PROVIDER_UNAVAILABLE: (
        503,
        "본인확인 제공자를 일시적으로 사용할 수 없습니다.",
    ),
    IdentityVerificationServiceErrorCode.VERIFICATION_NOT_COMPLETED: (
        409,
        "본인확인이 아직 완료되지 않았습니다.",
    ),
    IdentityVerificationServiceErrorCode.AGE_REQUIREMENT_NOT_MET: (
        403,
        "연령 요건을 충족하지 못했습니다.",
    ),
    IdentityVerificationServiceErrorCode.VERIFICATION_FAILED: (
        409,
        "본인확인에 실패했습니다.",
    ),
    IdentityVerificationServiceErrorCode.VERIFICATION_EXPIRED: (
        410,
        "본인확인 세션이 만료되었습니다.",
    ),
    IdentityVerificationServiceErrorCode.INVALID_PROVIDER_RESPONSE: (
        502,
        "본인확인 결과를 확인할 수 없습니다.",
    ),
    IdentityVerificationServiceErrorCode.FINALIZATION_FAILED: (
        500,
        "본인확인 결과를 저장하지 못했습니다.",
    ),
}


@api_v1_blueprint.post("/identity-verifications")
def start_identity_verification_endpoint():
    try:
        _require_empty_json_object()
        dependencies = _get_dependencies()
        result = start_identity_verification(
            db.session,
            provider=dependencies.provider,
            action_time=datetime.now(UTC),
            session_ttl=IDENTITY_VERIFICATION_SESSION_TTL,
        )
        return (
            jsonify(
                verification_session_id=result.verification_session_id,
                provider_transaction_id=result.provider_transaction_id,
                authentication_url=result.authentication_url,
                expires_at=_utc_isoformat(result.expires_at),
            ),
            201,
        )
    except _ApiRequestError as error:
        return _error_response(
            code=error.code,
            message=error.message,
            status_code=error.status_code,
        )
    except IdentityVerificationServiceError as error:
        return _service_error_response(error.code)
    except Exception:
        _rollback_safely()
        return _error_response(
            code="INTERNAL_SERVER_ERROR",
            message="본인확인 요청을 처리하지 못했습니다.",
            status_code=500,
        )


@api_v1_blueprint.post(
    "/identity-verifications/<verification_session_id>/complete"
)
def complete_identity_verification_endpoint(verification_session_id):
    try:
        _require_empty_json_object()
        dependencies = _get_dependencies()
        result = complete_identity_verification(
            db.session,
            verification_session_id=verification_session_id,
            provider=dependencies.provider,
            action_time=datetime.now(UTC),
            identity_subject_hmac_key=(
                dependencies.identity_subject_hmac_key
            ),
            evidence_writer=dependencies.evidence_writer,
        )
        return jsonify(
            verification_session_id=result.verification_session_id,
            status=result.status,
            age_eligibility=result.age_eligibility.value,
        )
    except _ApiRequestError as error:
        return _error_response(
            code=error.code,
            message=error.message,
            status_code=error.status_code,
        )
    except IdentityVerificationServiceError as error:
        return _service_error_response(error.code)
    except Exception:
        _rollback_safely()
        return _error_response(
            code="INTERNAL_SERVER_ERROR",
            message="본인확인 완료 요청을 처리하지 못했습니다.",
            status_code=500,
        )


def _require_empty_json_object():
    if not request.is_json:
        raise _ApiRequestError(
            code="UNSUPPORTED_MEDIA_TYPE",
            message="application/json 요청이 필요합니다.",
            status_code=415,
        )

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload:
        raise _ApiRequestError(
            code="INVALID_REQUEST",
            message="요청 본문은 빈 JSON 객체여야 합니다.",
            status_code=400,
        )


def _get_dependencies() -> TossIdentityVerificationDependencies:
    dependencies = current_app.extensions.get(
        API_V1_DEPENDENCIES_EXTENSION_KEY
    )
    if not isinstance(
        dependencies,
        TossIdentityVerificationDependencies,
    ):
        raise RuntimeError(
            "Toss identity verification dependencies are unavailable."
        )
    return dependencies


def _utc_isoformat(value):
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("expires_at must be timezone-aware.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _service_error_response(code):
    status_code, message = _SERVICE_ERROR_RESPONSES[code]
    return _error_response(
        code=code.value,
        message=message,
        status_code=status_code,
    )


def _error_response(*, code, message, status_code):
    return jsonify(error={"code": code, "message": message}), status_code


def _rollback_safely():
    try:
        db.session.rollback()
    except Exception:
        pass
