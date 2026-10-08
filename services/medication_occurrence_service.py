from datetime import UTC, date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from models import MedicationOccurrence, MedicationSchedule
from services.medication_schedule_service import (
    MedicationScheduleCalculationError,
    _load_timezone,
    _resolve_local_wall_time,
    generate_occurrences_in_window,
)


class MedicationOccurrenceError(ValueError):
    """Raised when a reliable scheduled occurrence cannot be produced."""


def local_date_to_utc_window(
    local_date: date,
    *,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    """Return the UTC half-open window for one date in a user timezone."""

    if not isinstance(local_date, date) or isinstance(local_date, datetime):
        raise MedicationOccurrenceError(
            "local_date must be a datetime.date value."
        )

    try:
        next_local_date = local_date + timedelta(days=1)
    except OverflowError as error:
        raise MedicationOccurrenceError(
            "local_date is outside the supported date range."
        ) from error

    try:
        user_timezone = _load_timezone(timezone_name)
        day_start = _resolve_local_wall_time(
            local_date=local_date,
            local_time=time.min,
            timezone=user_timezone,
        )
        day_end = _resolve_local_wall_time(
            local_date=next_local_date,
            local_time=time.min,
            timezone=user_timezone,
        )
    except MedicationScheduleCalculationError as error:
        raise MedicationOccurrenceError(str(error)) from error

    return day_start.astimezone(UTC), day_end.astimezone(UTC)


def normalize_occurrence_instant(
    value: datetime,
    *,
    name: str = "scheduled_for",
) -> datetime:
    """Validate an external datetime and return its canonical UTC instant."""

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MedicationOccurrenceError(
            f"{name} must be a timezone-aware datetime."
        )

    return value.astimezone(UTC)


def list_schedule_occurrences_in_window(
    schedule: MedicationSchedule,
    *,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, ...]:
    """Return this schedule version's valid UTC slots in [start, end)."""

    _validate_plan_bound_schedule(schedule)
    window_start_utc = normalize_occurrence_instant(
        window_start,
        name="window_start",
    )
    window_end_utc = normalize_occurrence_instant(
        window_end,
        name="window_end",
    )

    if window_start_utc >= window_end_utc:
        raise MedicationOccurrenceError(
            "window_start must be earlier than window_end."
        )

    effective_end_utc = window_end_utc

    if schedule.closed_at is not None:
        effective_end_utc = min(
            effective_end_utc,
            _stored_datetime_as_utc("closed_at", schedule.closed_at),
        )

    if window_start_utc >= effective_end_utc:
        return ()

    try:
        occurrences = generate_occurrences_in_window(
            start_date=schedule.start_date,
            course_days=schedule.course_days,
            times=(value.time_of_day for value in schedule.times),
            reported_doses_taken_before_tracking=(
                schedule.reported_doses_taken_before_tracking
            ),
            accounted_occurrence_count=(
                schedule.accounted_occurrence_count
            ),
            reminder_tracking_started_at=_stored_datetime_as_utc(
                "reminder_tracking_started_at",
                schedule.reminder_tracking_started_at,
            ),
            window_start=window_start_utc,
            window_end=effective_end_utc,
            timezone_name=timezone_name,
        )
    except MedicationScheduleCalculationError as error:
        raise MedicationOccurrenceError(str(error)) from error

    return tuple(value.astimezone(UTC) for value in occurrences)


def validate_schedule_occurrence(
    schedule: MedicationSchedule,
    *,
    timezone_name: str,
    scheduled_for: datetime,
) -> datetime:
    """Return canonical UTC when the instant is a valid schedule slot."""

    scheduled_for_utc = normalize_occurrence_instant(scheduled_for)

    try:
        validation_end = scheduled_for_utc + timedelta(microseconds=1)
    except OverflowError as error:
        raise MedicationOccurrenceError(
            "scheduled_for is outside the supported datetime range."
        ) from error

    occurrences = list_schedule_occurrences_in_window(
        schedule,
        timezone_name=timezone_name,
        window_start=scheduled_for_utc,
        window_end=validation_end,
    )

    if scheduled_for_utc not in occurrences:
        raise MedicationOccurrenceError(
            "scheduled_for is not a valid occurrence for this schedule."
        )

    return scheduled_for_utc


def get_or_create_medication_occurrence(
    session,
    schedule: MedicationSchedule,
    *,
    timezone_name: str,
    scheduled_for: datetime,
    action_time: datetime,
) -> MedicationOccurrence:
    """Validate and materialize one row without committing the transaction."""

    scheduled_for_utc = validate_schedule_occurrence(
        schedule,
        timezone_name=timezone_name,
        scheduled_for=scheduled_for,
    )
    action_time_utc = normalize_occurrence_instant(
        action_time,
        name="action_time",
    )

    statement = (
        sqlite_insert(MedicationOccurrence)
        .values(
            schedule_id=schedule.schedule_id,
            scheduled_for=scheduled_for_utc,
            response_status=None,
            responded_at=None,
            created_at=action_time_utc,
            updated_at=action_time_utc,
        )
        .on_conflict_do_nothing(
            index_elements=("schedule_id", "scheduled_for"),
        )
    )
    session.execute(statement)

    occurrence = session.scalar(
        select(MedicationOccurrence).where(
            MedicationOccurrence.schedule_id == schedule.schedule_id,
            MedicationOccurrence.scheduled_for == scheduled_for_utc,
        )
    )

    if occurrence is None:
        raise MedicationOccurrenceError(
            "The medication occurrence could not be materialized."
        )

    return occurrence


def list_existing_medication_occurrences(
    session,
    occurrence_keys: Iterable[tuple[int, datetime]],
) -> dict[tuple[int, datetime], MedicationOccurrence]:
    """Fetch materialized rows for exact keys in one query.

    Missing rows are intentionally omitted: a valid virtual occurrence can
    exist without a materialized row and therefore still mean "unanswered".
    """

    normalized_keys = set()

    try:
        keys = tuple(occurrence_keys)
    except TypeError as error:
        raise MedicationOccurrenceError(
            "occurrence_keys must be an iterable of key pairs."
        ) from error

    for key in keys:
        if not isinstance(key, tuple) or len(key) != 2:
            raise MedicationOccurrenceError(
                "Each occurrence key must contain schedule_id and "
                "scheduled_for."
            )

        schedule_id, scheduled_for = key

        if (
            isinstance(schedule_id, bool)
            or not isinstance(schedule_id, int)
            or schedule_id < 1
        ):
            raise MedicationOccurrenceError(
                "schedule_id must be a positive integer."
            )

        normalized_keys.add(
            (
                schedule_id,
                normalize_occurrence_instant(scheduled_for),
            )
        )

    if not normalized_keys:
        return {}

    statement = select(MedicationOccurrence).where(
        tuple_(
            MedicationOccurrence.schedule_id,
            MedicationOccurrence.scheduled_for,
        ).in_(normalized_keys)
    )
    occurrences = session.scalars(statement).all()

    return {
        (
            occurrence.schedule_id,
            _stored_datetime_as_utc(
                "scheduled_for",
                occurrence.scheduled_for,
            ),
        ): occurrence
        for occurrence in occurrences
    }


def _validate_plan_bound_schedule(schedule: MedicationSchedule) -> None:
    if not isinstance(schedule, MedicationSchedule):
        raise MedicationOccurrenceError(
            "schedule must be a MedicationSchedule instance."
        )

    if schedule.plan_id is None:
        raise MedicationOccurrenceError(
            "Medication occurrences currently require a plan-bound "
            "schedule."
        )


def _stored_datetime_as_utc(name: str, value: datetime) -> datetime:
    """Interpret SQLite-naive persisted datetimes as canonical UTC."""

    if not isinstance(value, datetime):
        raise MedicationOccurrenceError(f"{name} must be a datetime.")

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    if value.utcoffset() is None:
        raise MedicationOccurrenceError(
            f"{name} must contain a usable UTC offset."
        )

    return value.astimezone(UTC)
