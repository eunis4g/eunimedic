import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import click

from models import db, utc_now
from services.notification_service import (
    ScheduledDispatchCreationResult,
    create_scheduled_notification_dispatches,
    list_scheduled_notification_candidates,
)
from services.notification_worker_service import (
    ScheduledNotificationDeliverySummary,
    StaleNotificationClaimRecoveryResult,
    process_due_scheduled_notifications,
    recover_stale_notification_claims,
)


DEFAULT_SCHEDULED_GRACE_MINUTES = 15
DEFAULT_CLAIM_TIMEOUT_MINUTES = 10
DEFAULT_NOTIFICATION_BATCH_SIZE = 50


class NotificationCliConfigurationError(ValueError):
    """Raised before DB work when notification CLI settings are invalid."""


class NotificationCliStageError(RuntimeError):
    """Raised when a database or system-level pipeline stage fails."""


@dataclass(frozen=True)
class NotificationCliSettings:
    rollout_at: datetime
    scheduled_grace: timedelta
    claim_timeout: timedelta
    batch_size: int


@dataclass(frozen=True)
class NotificationCliResult:
    recovery: StaleNotificationClaimRecoveryResult
    candidate_count: int
    creation: ScheduledDispatchCreationResult
    delivery: ScheduledNotificationDeliverySummary


def register_notification_cli(app):
    app.cli.add_command(process_medication_notifications)


@click.command("process-medication-notifications")
def process_medication_notifications():
    """Run the scheduled notification pipeline once."""

    try:
        settings = load_notification_cli_settings()
    except NotificationCliConfigurationError as error:
        raise click.ClickException(str(error)) from error

    action_time = utc_now()

    try:
        result = run_scheduled_notification_pipeline(
            db.session,
            action_time=action_time,
            settings=settings,
        )
    except NotificationCliStageError as error:
        raise click.ClickException(str(error)) from error

    _print_summary(result)


def load_notification_cli_settings() -> NotificationCliSettings:
    rollout_at = _parse_rollout_at(os.getenv("NOTIFICATION_ROLLOUT_AT"))
    scheduled_grace_minutes = _parse_integer_setting(
        "NOTIFICATION_SCHEDULED_GRACE_MINUTES",
        default=DEFAULT_SCHEDULED_GRACE_MINUTES,
        minimum=0,
    )
    claim_timeout_minutes = _parse_integer_setting(
        "NOTIFICATION_CLAIM_TIMEOUT_MINUTES",
        default=DEFAULT_CLAIM_TIMEOUT_MINUTES,
        minimum=1,
    )
    batch_size = _parse_integer_setting(
        "NOTIFICATION_BATCH_SIZE",
        default=DEFAULT_NOTIFICATION_BATCH_SIZE,
        minimum=1,
    )
    return NotificationCliSettings(
        rollout_at=rollout_at,
        scheduled_grace=_minutes_to_timedelta(
            "NOTIFICATION_SCHEDULED_GRACE_MINUTES",
            scheduled_grace_minutes,
        ),
        claim_timeout=_minutes_to_timedelta(
            "NOTIFICATION_CLAIM_TIMEOUT_MINUTES",
            claim_timeout_minutes,
        ),
        batch_size=batch_size,
    )


def run_scheduled_notification_pipeline(
    session,
    *,
    action_time: datetime,
    settings: NotificationCliSettings,
) -> NotificationCliResult:
    try:
        recovery = recover_stale_notification_claims(
            session,
            action_time=action_time,
            claim_timeout=settings.claim_timeout,
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise NotificationCliStageError(
            "Stale notification claim recovery failed."
        ) from error

    try:
        candidates = list_scheduled_notification_candidates(
            session,
            action_time=action_time,
            rollout_at=settings.rollout_at,
            scheduled_grace=settings.scheduled_grace,
        )
        creation = create_scheduled_notification_dispatches(
            session,
            candidates,
            action_time=action_time,
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise NotificationCliStageError(
            "Scheduled notification preparation failed."
        ) from error

    try:
        delivery = process_due_scheduled_notifications(
            session,
            action_time=action_time,
            batch_size=settings.batch_size,
        )
    except Exception as error:
        session.rollback()
        raise NotificationCliStageError(
            "Scheduled notification delivery failed."
        ) from error

    return NotificationCliResult(
        recovery=recovery,
        candidate_count=len(candidates),
        creation=creation,
        delivery=delivery,
    )


def _parse_rollout_at(value: str | None) -> datetime:
    if value is None or not value.strip():
        raise NotificationCliConfigurationError(
            "NOTIFICATION_ROLLOUT_AT is required as a timezone-aware "
            "ISO-8601 datetime, for example 2026-10-08T00:00:00+00:00."
        )

    normalized_text = value.strip()

    if normalized_text.endswith(("Z", "z")):
        normalized_text = normalized_text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized_text)
    except ValueError as error:
        raise NotificationCliConfigurationError(
            "NOTIFICATION_ROLLOUT_AT must be a valid timezone-aware "
            "ISO-8601 datetime."
        ) from error

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NotificationCliConfigurationError(
            "NOTIFICATION_ROLLOUT_AT must include a UTC offset."
        )

    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise NotificationCliConfigurationError(
            "NOTIFICATION_ROLLOUT_AT is outside the supported datetime "
            "range."
        ) from error


def _parse_integer_setting(
    name: str,
    *,
    default: int,
    minimum: int,
) -> int:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        parsed = int(value.strip())
    except ValueError as error:
        raise NotificationCliConfigurationError(
            f"{name} must be an integer greater than or equal to {minimum}."
        ) from error

    if parsed < minimum:
        raise NotificationCliConfigurationError(
            f"{name} must be an integer greater than or equal to {minimum}."
        )

    return parsed


def _minutes_to_timedelta(name: str, value: int) -> timedelta:
    try:
        return timedelta(minutes=value)
    except OverflowError as error:
        raise NotificationCliConfigurationError(
            f"{name} is outside the supported range."
        ) from error


def _print_summary(result: NotificationCliResult) -> None:
    creation = result.creation
    delivery = result.delivery
    summary_lines = (
        ("Recovered stale claims", result.recovery.recovered_count),
        ("Scheduled candidates", result.candidate_count),
        ("Created dispatches", len(creation.created_dispatch_ids)),
        ("Reused dispatches", len(creation.reused_dispatch_ids)),
        ("Created members", creation.created_member_count),
        ("Materialized occurrences", creation.materialized_occurrence_count),
        ("Skipped responded", creation.skipped_responded_count),
        ("Skipped invalid", creation.skipped_invalid_count),
        ("Skipped non-pending", creation.skipped_non_pending_count),
        ("Claimed", delivery.claimed_count),
        ("Sent", delivery.sent_count),
        ("Canceled", delivery.canceled_count),
        ("Retried", delivery.retry_count),
        ("Failed", delivery.failed_count),
        ("Skipped delivery", delivery.skipped_count),
    )

    for label, count in summary_lines:
        click.echo(f"{label}: {count}")
