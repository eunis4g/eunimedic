from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# This is a resource-protection limit for one materialized result, not a
# medical limit on how many doses a user may configure.
MAX_MATERIALIZED_OCCURRENCES = 100_000
MAX_SCANNED_DAYS = 366_000


class MedicationScheduleCalculationError(ValueError):
    """Raised when schedule input cannot produce a reliable calculation."""


@dataclass(frozen=True)
class OccurrenceSummary:
    daily_frequency: int
    planned_total: int
    reported_doses_taken_before_tracking: int
    accounted_occurrence_count: int
    elapsed_in_current_segment: int
    already_accounted: int
    remaining: int


def normalize_medication_times(times: Iterable[time]) -> tuple[time, ...]:
    """Validate local wall-clock times and return them in time order."""

    try:
        normalized_times = tuple(times)
    except TypeError as error:
        raise MedicationScheduleCalculationError(
            "Medication times must be an iterable of datetime.time values."
        ) from error

    if not normalized_times:
        raise MedicationScheduleCalculationError(
            "At least one medication time is required."
        )

    for medication_time in normalized_times:
        if not isinstance(medication_time, time):
            raise MedicationScheduleCalculationError(
                "Every medication time must be a datetime.time value."
            )

        if medication_time.tzinfo is not None:
            raise MedicationScheduleCalculationError(
                "Medication times must be timezone-naive local wall times."
            )

    sorted_times = tuple(
        sorted(
            normalized_times,
            key=lambda value: (
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
            ),
        )
    )
    time_keys = {
        (
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
        )
        for value in sorted_times
    }

    if len(time_keys) != len(sorted_times):
        raise MedicationScheduleCalculationError(
            "Duplicate medication times are not allowed."
        )

    return sorted_times


def calculate_daily_frequency(times: Iterable[time]) -> int:
    return len(normalize_medication_times(times))


def calculate_planned_total(
    *,
    times: Iterable[time],
    course_days: int,
) -> int:
    _validate_positive_integer("course_days", course_days)
    return len(normalize_medication_times(times)) * course_days


def validate_plan_capacity(
    *,
    times: Iterable[time],
    course_days: int,
    reported_doses_taken_before_tracking: int,
    accounted_occurrence_count: int,
) -> int:
    """Validate stored counts and return the derived planned total."""

    normalized_times = normalize_medication_times(times)
    _validate_positive_integer("course_days", course_days)
    _validate_non_negative_integer(
        "reported_doses_taken_before_tracking",
        reported_doses_taken_before_tracking,
    )
    _validate_non_negative_integer(
        "accounted_occurrence_count",
        accounted_occurrence_count,
    )

    planned_total = len(normalized_times) * course_days
    previously_accounted = (
        reported_doses_taken_before_tracking
        + accounted_occurrence_count
    )

    if previously_accounted > planned_total:
        raise MedicationScheduleCalculationError(
            "Reported doses and accounted occurrences cannot exceed the "
            "planned total."
        )

    return planned_total


def count_elapsed_occurrences(
    *,
    start_date: date,
    times: Iterable[time],
    reminder_tracking_started_at: datetime,
    reference_at: datetime,
    timezone_name: str,
    maximum_count: int | None = None,
) -> int:
    """Count occurrence slots in [segment start, reference_at).

    The count describes elapsed schedule slots. It does not say that a dose
    was taken, that a notification was sent, or that a user confirmed it.
    """

    normalized_times = normalize_medication_times(times)
    normalized_start_date = _validate_start_date(start_date)
    tracking_utc = _normalize_aware_datetime(
        "reminder_tracking_started_at",
        reminder_tracking_started_at,
    )
    reference_utc = _normalize_aware_datetime(
        "reference_at",
        reference_at,
    )
    timezone = _load_timezone(timezone_name)

    if maximum_count is not None:
        _validate_non_negative_integer("maximum_count", maximum_count)

        if maximum_count == 0:
            return 0

    segment_start_utc = _calculate_segment_start_utc(
        start_date=normalized_start_date,
        tracking_utc=tracking_utc,
        timezone=timezone,
    )

    if reference_utc <= segment_start_utc:
        return 0

    first_local_date = segment_start_utc.astimezone(timezone).date()
    last_local_date = reference_utc.astimezone(timezone).date()
    day_span = (last_local_date - first_local_date).days + 1

    if day_span > MAX_SCANNED_DAYS:
        raise MedicationScheduleCalculationError(
            "The elapsed occurrence interval is too large to calculate "
            "safely in one request."
        )

    elapsed_count = 0
    cursor_date = first_local_date

    while cursor_date <= last_local_date:
        for occurrence in _occurrences_for_local_date(
            local_date=cursor_date,
            times=normalized_times,
            timezone=timezone,
        ):
            occurrence_utc = occurrence.astimezone(UTC)

            if segment_start_utc <= occurrence_utc < reference_utc:
                elapsed_count += 1

                if (
                    maximum_count is not None
                    and elapsed_count >= maximum_count
                ):
                    return elapsed_count

        cursor_date = _next_date(cursor_date)

    return elapsed_count


def calculate_occurrence_summary(
    *,
    start_date: date,
    course_days: int,
    times: Iterable[time],
    reported_doses_taken_before_tracking: int,
    accounted_occurrence_count: int,
    reminder_tracking_started_at: datetime,
    reference_at: datetime,
    timezone_name: str,
) -> OccurrenceSummary:
    normalized_times = normalize_medication_times(times)
    planned_total = validate_plan_capacity(
        times=normalized_times,
        course_days=course_days,
        reported_doses_taken_before_tracking=(
            reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=accounted_occurrence_count,
    )
    available_in_current_segment = planned_total - (
        reported_doses_taken_before_tracking
        + accounted_occurrence_count
    )
    elapsed_in_current_segment = count_elapsed_occurrences(
        start_date=start_date,
        times=normalized_times,
        reminder_tracking_started_at=reminder_tracking_started_at,
        reference_at=reference_at,
        timezone_name=timezone_name,
        maximum_count=available_in_current_segment,
    )
    already_accounted = (
        reported_doses_taken_before_tracking
        + accounted_occurrence_count
        + elapsed_in_current_segment
    )
    remaining = max(0, planned_total - already_accounted)

    return OccurrenceSummary(
        daily_frequency=len(normalized_times),
        planned_total=planned_total,
        reported_doses_taken_before_tracking=(
            reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=accounted_occurrence_count,
        elapsed_in_current_segment=elapsed_in_current_segment,
        already_accounted=already_accounted,
        remaining=remaining,
    )


def generate_future_occurrences(
    *,
    start_date: date,
    course_days: int,
    times: Iterable[time],
    reported_doses_taken_before_tracking: int,
    accounted_occurrence_count: int,
    reminder_tracking_started_at: datetime,
    reference_at: datetime,
    timezone_name: str,
) -> tuple[datetime, ...]:
    """Return exactly the remaining occurrence slots at/after reference_at."""

    normalized_times = normalize_medication_times(times)
    summary = calculate_occurrence_summary(
        start_date=start_date,
        course_days=course_days,
        times=normalized_times,
        reported_doses_taken_before_tracking=(
            reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=accounted_occurrence_count,
        reminder_tracking_started_at=reminder_tracking_started_at,
        reference_at=reference_at,
        timezone_name=timezone_name,
    )

    if summary.remaining == 0:
        return ()

    if summary.remaining > MAX_MATERIALIZED_OCCURRENCES:
        raise MedicationScheduleCalculationError(
            "Too many future occurrences were requested for one "
            "materialized result."
        )

    normalized_start_date = _validate_start_date(start_date)
    tracking_utc = _normalize_aware_datetime(
        "reminder_tracking_started_at",
        reminder_tracking_started_at,
    )
    reference_utc = _normalize_aware_datetime(
        "reference_at",
        reference_at,
    )
    timezone = _load_timezone(timezone_name)
    segment_start_utc = _calculate_segment_start_utc(
        start_date=normalized_start_date,
        tracking_utc=tracking_utc,
        timezone=timezone,
    )
    future_start_utc = max(segment_start_utc, reference_utc)
    cursor_date = future_start_utc.astimezone(timezone).date()
    occurrences = []
    scanned_days = 0

    while len(occurrences) < summary.remaining:
        scanned_days += 1

        if scanned_days > MAX_SCANNED_DAYS:
            raise MedicationScheduleCalculationError(
                "Future occurrence generation exceeded the safe date range."
            )

        for occurrence in _occurrences_for_local_date(
            local_date=cursor_date,
            times=normalized_times,
            timezone=timezone,
        ):
            if occurrence.astimezone(UTC) >= future_start_utc:
                occurrences.append(occurrence)

                if len(occurrences) == summary.remaining:
                    return tuple(occurrences)

        cursor_date = _next_date(cursor_date)

    return tuple(occurrences)


def generate_occurrences_in_window(
    *,
    start_date: date,
    course_days: int,
    times: Iterable[time],
    reported_doses_taken_before_tracking: int,
    accounted_occurrence_count: int,
    reminder_tracking_started_at: datetime,
    window_start: datetime,
    window_end: datetime,
    timezone_name: str,
) -> tuple[datetime, ...]:
    """Return capacity-limited slots in the UTC window [start, end)."""

    normalized_times = normalize_medication_times(times)
    window_start_utc = _normalize_aware_datetime(
        "window_start",
        window_start,
    )
    window_end_utc = _normalize_aware_datetime(
        "window_end",
        window_end,
    )

    if window_start_utc >= window_end_utc:
        raise MedicationScheduleCalculationError(
            "window_start must be earlier than window_end."
        )

    summary = calculate_occurrence_summary(
        start_date=start_date,
        course_days=course_days,
        times=normalized_times,
        reported_doses_taken_before_tracking=(
            reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=accounted_occurrence_count,
        reminder_tracking_started_at=reminder_tracking_started_at,
        reference_at=window_start_utc,
        timezone_name=timezone_name,
    )

    if summary.remaining == 0:
        return ()

    normalized_start_date = _validate_start_date(start_date)
    tracking_utc = _normalize_aware_datetime(
        "reminder_tracking_started_at",
        reminder_tracking_started_at,
    )
    timezone = _load_timezone(timezone_name)
    segment_start_utc = _calculate_segment_start_utc(
        start_date=normalized_start_date,
        tracking_utc=tracking_utc,
        timezone=timezone,
    )
    bounded_start_utc = max(segment_start_utc, window_start_utc)

    if bounded_start_utc >= window_end_utc:
        return ()

    first_local_date = bounded_start_utc.astimezone(timezone).date()
    last_local_date = (
        window_end_utc - timedelta(microseconds=1)
    ).astimezone(timezone).date()
    day_span = (last_local_date - first_local_date).days + 1

    if day_span > MAX_SCANNED_DAYS:
        raise MedicationScheduleCalculationError(
            "The occurrence window is too large to calculate safely in "
            "one request."
        )

    occurrences = []
    cursor_date = first_local_date

    while cursor_date <= last_local_date:
        for occurrence in _occurrences_for_local_date(
            local_date=cursor_date,
            times=normalized_times,
            timezone=timezone,
        ):
            occurrence_utc = occurrence.astimezone(UTC)

            if bounded_start_utc <= occurrence_utc < window_end_utc:
                if len(occurrences) >= MAX_MATERIALIZED_OCCURRENCES:
                    raise MedicationScheduleCalculationError(
                        "Too many occurrences were requested for one "
                        "materialized result."
                    )

                occurrences.append(occurrence)

                if len(occurrences) == summary.remaining:
                    return tuple(occurrences)

        cursor_date = _next_date(cursor_date)

    return tuple(occurrences)


def calculate_last_scheduled_occurrence(
    *,
    start_date: date,
    course_days: int,
    times: Iterable[time],
    reported_doses_taken_before_tracking: int,
    accounted_occurrence_count: int,
    reminder_tracking_started_at: datetime,
    timezone_name: str,
) -> datetime | None:
    """Return the final app-managed slot in the latest tracking segment.

    A schedule with no slots left in its latest segment cannot provide an
    exact historical timestamp from the current aggregate fields alone.
    """

    normalized_times = normalize_medication_times(times)
    planned_total = validate_plan_capacity(
        times=normalized_times,
        course_days=course_days,
        reported_doses_taken_before_tracking=(
            reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=accounted_occurrence_count,
    )
    slots_in_latest_segment = planned_total - (
        reported_doses_taken_before_tracking
        + accounted_occurrence_count
    )

    if slots_in_latest_segment == 0:
        return None

    occurrences = generate_future_occurrences(
        start_date=start_date,
        course_days=course_days,
        times=normalized_times,
        reported_doses_taken_before_tracking=(
            reported_doses_taken_before_tracking
        ),
        accounted_occurrence_count=accounted_occurrence_count,
        reminder_tracking_started_at=reminder_tracking_started_at,
        reference_at=reminder_tracking_started_at,
        timezone_name=timezone_name,
    )

    return occurrences[-1] if occurrences else None


def _calculate_segment_start_utc(
    *,
    start_date: date,
    tracking_utc: datetime,
    timezone: ZoneInfo,
) -> datetime:
    start_midnight = _resolve_local_wall_time(
        local_date=start_date,
        local_time=time.min,
        timezone=timezone,
    )
    return max(start_midnight.astimezone(UTC), tracking_utc)


def _occurrences_for_local_date(
    *,
    local_date: date,
    times: tuple[time, ...],
    timezone: ZoneInfo,
) -> tuple[datetime, ...]:
    occurrences = [
        _resolve_local_wall_time(
            local_date=local_date,
            local_time=local_time,
            timezone=timezone,
        )
        for local_time in times
    ]
    occurrences.sort(key=lambda value: value.astimezone(UTC))
    return tuple(occurrences)


def _resolve_local_wall_time(
    *,
    local_date: date,
    local_time: time,
    timezone: ZoneInfo,
) -> datetime:
    """Resolve DST gaps and overlaps without a third-party dependency.

    Ambiguous local times use the first occurrence (fold=0). A nonexistent
    local time is shifted forward by the DST gap, so 02:30 in a one-hour
    spring-forward gap becomes 03:30. The returned value is always aware.
    """

    naive_value = datetime.combine(local_date, local_time).replace(fold=0)
    fold_zero = naive_value.replace(tzinfo=timezone, fold=0)
    fold_one = naive_value.replace(tzinfo=timezone, fold=1)
    round_trip_zero = fold_zero.astimezone(UTC).astimezone(timezone)
    round_trip_one = fold_one.astimezone(UTC).astimezone(timezone)
    zero_is_valid = round_trip_zero.replace(tzinfo=None) == naive_value
    one_is_valid = round_trip_one.replace(tzinfo=None) == naive_value

    if zero_is_valid:
        return fold_zero

    if one_is_valid:
        return fold_one

    forward_candidates = [
        candidate
        for candidate in (round_trip_zero, round_trip_one)
        if candidate.replace(tzinfo=None) > naive_value
    ]

    if not forward_candidates:
        raise MedicationScheduleCalculationError(
            "The local medication time could not be resolved in the "
            "selected timezone."
        )

    return min(
        forward_candidates,
        key=lambda value: value.replace(tzinfo=None),
    )


def _load_timezone(timezone_name: str) -> ZoneInfo:
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise MedicationScheduleCalculationError(
            "timezone_name must be a non-empty IANA timezone name."
        )

    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise MedicationScheduleCalculationError(
            "Timezone data is unavailable or the timezone name is unknown: "
            f"{timezone_name}"
        ) from error


def _validate_start_date(value: date) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise MedicationScheduleCalculationError(
            "start_date must be a datetime.date value."
        )

    return value


def _normalize_aware_datetime(name: str, value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MedicationScheduleCalculationError(
            f"{name} must be a timezone-aware datetime."
        )

    return value.astimezone(UTC)


def _validate_positive_integer(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MedicationScheduleCalculationError(
            f"{name} must be an integer greater than or equal to 1."
        )


def _validate_non_negative_integer(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MedicationScheduleCalculationError(
            f"{name} must be a non-negative integer."
        )


def _next_date(value: date) -> date:
    try:
        return value + timedelta(days=1)
    except OverflowError as error:
        raise MedicationScheduleCalculationError(
            "Occurrence generation exceeded the supported date range."
        ) from error
