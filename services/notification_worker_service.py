import smtplib
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import joinedload, selectinload

from models import (
    MedicationOccurrence,
    MedicationSchedule,
    NotificationDispatch,
    NotificationDispatchMember,
    UserMedicine,
)
from services.email_service import EmailServiceError, send_email
from services.medication_occurrence_service import (
    MedicationOccurrenceError,
    normalize_occurrence_instant,
    validate_schedule_occurrence,
)
from services.notification_service import (
    EMAIL_DELIVERY_CHANNEL,
    PENDING_DISPATCH_STATUS,
    SCHEDULED_NOTIFICATION_TYPE,
)


CLAIMED_DISPATCH_STATUS = "claimed"
SENT_DISPATCH_STATUS = "sent"
FAILED_DISPATCH_STATUS = "failed"
CANCELED_DISPATCH_STATUS = "canceled"
EMAIL_SUBJECT = "복약 일정 알림"
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
)
MAX_DELIVERY_ATTEMPTS = len(RETRY_DELAYS) + 1


class NotificationWorkerError(ValueError):
    """Raised when worker input or persisted delivery data is unsafe."""


@dataclass(frozen=True)
class ScheduledNotificationDeliverySummary:
    claimed_count: int
    sent_count: int
    canceled_count: int
    retry_count: int
    failed_count: int
    skipped_count: int


@dataclass(frozen=True)
class _PreparedDelivery:
    dispatch_id: int
    claim_token: str
    to_email: str
    subject: str
    body: str
    message_id: str
    attempt_count: int


@dataclass(frozen=True)
class _PreparationResult:
    outcome: str
    delivery: _PreparedDelivery | None = None


def _rollback_session_on_error(function):
    @wraps(function)
    def wrapped(session, *args, **kwargs):
        try:
            return function(session, *args, **kwargs)
        except Exception:
            session.rollback()
            raise

    return wrapped


@_rollback_session_on_error
def process_due_scheduled_notifications(
    session,
    *,
    action_time: datetime,
    batch_size: int = 100,
    sender: Callable | None = None,
    token_factory: Callable[[], str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ScheduledNotificationDeliverySummary:
    """Claim and deliver one bounded batch of scheduled email notifications.

    This worker owns its transaction boundaries. Call it with a session that
    has no uncommitted application work. Every SMTP call happens after the
    claim and pre-delivery validation transactions have been committed.
    """

    action_time_utc = _normalize_worker_instant(
        action_time,
        name="action_time",
    )
    _validate_batch_size(batch_size)

    sender = sender or send_email
    token_factory = token_factory or (lambda: uuid4().hex)
    clock = clock or (lambda: datetime.now(UTC))

    dispatch_ids = tuple(
        session.scalars(
            select(NotificationDispatch.dispatch_id)
            .where(
                NotificationDispatch.status == PENDING_DISPATCH_STATUS,
                NotificationDispatch.due_at <= action_time_utc,
                or_(
                    NotificationDispatch.next_attempt_at.is_(None),
                    NotificationDispatch.next_attempt_at <= action_time_utc,
                ),
                NotificationDispatch.delivery_channel
                == EMAIL_DELIVERY_CHANNEL,
                NotificationDispatch.notification_type
                == SCHEDULED_NOTIFICATION_TYPE,
            )
            .order_by(
                NotificationDispatch.due_at.asc(),
                NotificationDispatch.dispatch_id.asc(),
            )
            .limit(batch_size)
        ).all()
    )
    session.commit()

    counts = {
        "claimed_count": 0,
        "sent_count": 0,
        "canceled_count": 0,
        "retry_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
    }

    for dispatch_id in dispatch_ids:
        claim_token = _new_claim_token(token_factory)

        if not _claim_dispatch(
            session,
            dispatch_id=dispatch_id,
            claim_token=claim_token,
            claim_time=action_time_utc,
        ):
            counts["skipped_count"] += 1
            continue

        counts["claimed_count"] += 1
        preparation = _prepare_claimed_dispatch(
            session,
            dispatch_id=dispatch_id,
            claim_token=claim_token,
            action_time=action_time_utc,
        )

        if preparation.outcome == CANCELED_DISPATCH_STATUS:
            counts["canceled_count"] += 1
            continue

        if preparation.outcome == "skipped":
            counts["skipped_count"] += 1
            continue

        delivery = preparation.delivery

        if delivery is None:
            raise NotificationWorkerError(
                "A ready notification has no delivery data."
            )

        try:
            provider_message_id = sender(
                to_email=delivery.to_email,
                subject=delivery.subject,
                body=delivery.body,
                message_id=delivery.message_id,
            )
        except Exception as error:
            error_code, is_permanent = _classify_delivery_error(error)
            finalize_time = _clock_time(clock)
            final_status = _finalize_failure(
                session,
                delivery=delivery,
                finalize_time=finalize_time,
                error_code=error_code,
                is_permanent=is_permanent,
            )

            if final_status == PENDING_DISPATCH_STATUS:
                counts["retry_count"] += 1
            elif final_status == FAILED_DISPATCH_STATUS:
                counts["failed_count"] += 1
            else:
                counts["skipped_count"] += 1

            continue

        finalize_time = _clock_time(clock)

        if _finalize_sent(
            session,
            delivery=delivery,
            finalize_time=finalize_time,
            provider_message_id=provider_message_id,
        ):
            counts["sent_count"] += 1
        else:
            counts["skipped_count"] += 1

    return ScheduledNotificationDeliverySummary(**counts)


def _claim_dispatch(
    session,
    *,
    dispatch_id: int,
    claim_token: str,
    claim_time: datetime,
) -> bool:
    result = session.execute(
        update(NotificationDispatch)
        .where(
            NotificationDispatch.dispatch_id == dispatch_id,
            NotificationDispatch.status == PENDING_DISPATCH_STATUS,
            NotificationDispatch.due_at <= claim_time,
            or_(
                NotificationDispatch.next_attempt_at.is_(None),
                NotificationDispatch.next_attempt_at <= claim_time,
            ),
            NotificationDispatch.delivery_channel == EMAIL_DELIVERY_CHANNEL,
            NotificationDispatch.notification_type
            == SCHEDULED_NOTIFICATION_TYPE,
        )
        .values(
            status=CLAIMED_DISPATCH_STATUS,
            claim_token=claim_token,
            claimed_at=claim_time,
            updated_at=claim_time,
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return False

    session.commit()
    return True


def _prepare_claimed_dispatch(
    session,
    *,
    dispatch_id: int,
    claim_token: str,
    action_time: datetime,
) -> _PreparationResult:
    dispatch = session.scalar(
        select(NotificationDispatch)
        .options(joinedload(NotificationDispatch.user))
        .where(
            NotificationDispatch.dispatch_id == dispatch_id,
            NotificationDispatch.status == CLAIMED_DISPATCH_STATUS,
            NotificationDispatch.claim_token == claim_token,
        )
        .execution_options(populate_existing=True)
    )

    if dispatch is None:
        session.rollback()
        return _PreparationResult(outcome="skipped")

    member_options = (
        joinedload(NotificationDispatchMember.occurrence)
        .joinedload(MedicationOccurrence.schedule)
        .joinedload(MedicationSchedule.user_medicine)
        .joinedload(UserMedicine.user)
    )
    schedule_time_options = (
        joinedload(NotificationDispatchMember.occurrence)
        .joinedload(MedicationOccurrence.schedule)
        .selectinload(MedicationSchedule.times)
    )
    members = session.scalars(
        select(NotificationDispatchMember)
        .options(member_options, schedule_time_options)
        .where(NotificationDispatchMember.dispatch_id == dispatch_id)
        .order_by(NotificationDispatchMember.occurrence_id.asc())
    ).all()
    valid_occurrence_ids = []

    for member in members:
        if _is_deliverable_member(dispatch, member):
            valid_occurrence_ids.append(member.occurrence_id)

    valid_occurrence_id_set = set(valid_occurrence_ids)
    invalid_occurrence_ids = [
        member.occurrence_id
        for member in members
        if member.occurrence_id not in valid_occurrence_id_set
    ]

    if invalid_occurrence_ids:
        session.execute(
            delete(NotificationDispatchMember).where(
                NotificationDispatchMember.dispatch_id == dispatch_id,
                NotificationDispatchMember.occurrence_id.in_(
                    invalid_occurrence_ids
                ),
            ).execution_options(synchronize_session=False)
        )

    if not valid_occurrence_ids:
        result = session.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.dispatch_id == dispatch_id,
                NotificationDispatch.status == CLAIMED_DISPATCH_STATUS,
                NotificationDispatch.claim_token == claim_token,
            )
            .values(
                status=CANCELED_DISPATCH_STATUS,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                sent_at=None,
                provider_message_id=None,
                last_error_code=None,
                updated_at=action_time,
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return _PreparationResult(
            outcome=(
                CANCELED_DISPATCH_STATUS
                if result.rowcount == 1
                else "skipped"
            )
        )

    user = dispatch.user

    if user is None or not user.is_active or not user.email.strip():
        result = session.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.dispatch_id == dispatch_id,
                NotificationDispatch.status == CLAIMED_DISPATCH_STATUS,
                NotificationDispatch.claim_token == claim_token,
            )
            .values(
                status=CANCELED_DISPATCH_STATUS,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                sent_at=None,
                provider_message_id=None,
                last_error_code=None,
                updated_at=action_time,
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return _PreparationResult(
            outcome=(
                CANCELED_DISPATCH_STATUS
                if result.rowcount == 1
                else "skipped"
            )
        )

    due_at_utc = _stored_datetime_as_utc(dispatch.due_at)

    try:
        local_time = due_at_utc.astimezone(ZoneInfo(user.timezone))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        local_time = due_at_utc

    delivery = _PreparedDelivery(
        dispatch_id=dispatch_id,
        claim_token=claim_token,
        to_email=user.email,
        subject=EMAIL_SUBJECT,
        body=(
            f"{local_time:%H:%M}에 예정된 복용 일정이 있었습니다.\n"
            "앱에서 복약 기록을 확인해 주세요."
        ),
        message_id=_notification_message_id(dispatch_id),
        attempt_count=dispatch.attempt_count,
    )
    session.commit()
    return _PreparationResult(outcome="ready", delivery=delivery)


def _is_deliverable_member(dispatch, member) -> bool:
    occurrence = member.occurrence

    if occurrence is None or occurrence.response_status is not None:
        return False

    schedule = occurrence.schedule

    if schedule is None or schedule.plan_id is None:
        return False

    user_medicine = schedule.user_medicine

    if (
        user_medicine is None
        or user_medicine.user_id != dispatch.user_id
        or schedule.plan_id != dispatch.plan_id
        or _stored_datetime_as_utc(occurrence.scheduled_for)
        != _stored_datetime_as_utc(dispatch.due_at)
    ):
        return False

    try:
        validate_schedule_occurrence(
            schedule,
            timezone_name=user_medicine.user.timezone,
            scheduled_for=_stored_datetime_as_utc(occurrence.scheduled_for),
        )
    except (AttributeError, MedicationOccurrenceError):
        return False

    return True


def _finalize_sent(
    session,
    *,
    delivery: _PreparedDelivery,
    finalize_time: datetime,
    provider_message_id,
) -> bool:
    stored_provider_message_id = (
        provider_message_id
        if isinstance(provider_message_id, str) and provider_message_id
        else None
    )
    result = session.execute(
        update(NotificationDispatch)
        .where(
            NotificationDispatch.dispatch_id == delivery.dispatch_id,
            NotificationDispatch.status == CLAIMED_DISPATCH_STATUS,
            NotificationDispatch.claim_token == delivery.claim_token,
        )
        .values(
            status=SENT_DISPATCH_STATUS,
            attempt_count=delivery.attempt_count + 1,
            next_attempt_at=None,
            claim_token=None,
            claimed_at=None,
            sent_at=finalize_time,
            provider_message_id=stored_provider_message_id,
            last_error_code=None,
            updated_at=finalize_time,
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return False

    session.commit()
    return True


def _finalize_failure(
    session,
    *,
    delivery: _PreparedDelivery,
    finalize_time: datetime,
    error_code: str,
    is_permanent: bool,
) -> str | None:
    attempt_count = delivery.attempt_count + 1
    should_fail = is_permanent or attempt_count >= MAX_DELIVERY_ATTEMPTS

    if should_fail:
        status = FAILED_DISPATCH_STATUS
        next_attempt_at = None
    else:
        status = PENDING_DISPATCH_STATUS
        next_attempt_at = finalize_time + RETRY_DELAYS[attempt_count - 1]

    result = session.execute(
        update(NotificationDispatch)
        .where(
            NotificationDispatch.dispatch_id == delivery.dispatch_id,
            NotificationDispatch.status == CLAIMED_DISPATCH_STATUS,
            NotificationDispatch.claim_token == delivery.claim_token,
        )
        .values(
            status=status,
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            claim_token=None,
            claimed_at=None,
            sent_at=None,
            provider_message_id=None,
            last_error_code=error_code,
            updated_at=finalize_time,
        )
        .execution_options(synchronize_session=False)
    )

    if result.rowcount != 1:
        session.rollback()
        return None

    session.commit()
    return status


def _classify_delivery_error(error: Exception) -> tuple[str, bool]:
    underlying = error

    if isinstance(error, EmailServiceError) and error.__cause__ is not None:
        underlying = error.__cause__

    if isinstance(underlying, (socket.timeout, TimeoutError)):
        return "smtp_timeout", False

    if isinstance(underlying, smtplib.SMTPAuthenticationError):
        return "smtp_auth_error", True

    if isinstance(underlying, smtplib.SMTPRecipientsRefused):
        return "smtp_recipient_rejected", True

    if isinstance(underlying, smtplib.SMTPResponseException):
        is_permanent = 500 <= underlying.smtp_code < 600
        return "smtp_unknown_error", is_permanent

    if isinstance(underlying, (OSError, smtplib.SMTPException)):
        return "smtp_connection_error", False

    if isinstance(error, EmailServiceError):
        return "smtp_configuration_error", True

    return "smtp_unknown_error", False


def _notification_message_id(dispatch_id: int) -> str:
    return f"<medicine-web-notification-{dispatch_id}@medicine-web.local>"


def _new_claim_token(token_factory: Callable[[], str]) -> str:
    claim_token = token_factory()

    if (
        not isinstance(claim_token, str)
        or not claim_token
        or len(claim_token) > 64
    ):
        raise NotificationWorkerError(
            "token_factory must return a non-empty string of at most 64 "
            "characters."
        )

    return claim_token


def _clock_time(clock: Callable[[], datetime]) -> datetime:
    return _normalize_worker_instant(clock(), name="clock result")


def _normalize_worker_instant(value: datetime, *, name: str) -> datetime:
    try:
        return normalize_occurrence_instant(value, name=name)
    except MedicationOccurrenceError as error:
        raise NotificationWorkerError(str(error)) from error


def _validate_batch_size(batch_size: int) -> None:
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise NotificationWorkerError("batch_size must be a positive integer.")


def _stored_datetime_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)
