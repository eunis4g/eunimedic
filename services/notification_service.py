from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import joinedload, selectinload

from models import (
    MedicationOccurrence,
    MedicationPlan,
    MedicationSchedule,
    NotificationDispatch,
    NotificationDispatchMember,
    UserMedicine,
)
from services.medication_occurrence_service import (
    MedicationOccurrenceError,
    get_or_create_medication_occurrence,
    list_existing_medication_occurrences,
    list_schedule_occurrences_in_window,
    normalize_occurrence_instant,
    validate_schedule_occurrence,
)


SCHEDULED_NOTIFICATION_TYPE = "scheduled_occurrence"
EMAIL_DELIVERY_CHANNEL = "email"
PENDING_DISPATCH_STATUS = "pending"


class NotificationServiceError(ValueError):
    """Raised when scheduled notification preparation cannot be trusted."""


@dataclass(frozen=True)
class ScheduledOccurrenceCandidate:
    schedule_id: int
    scheduled_for: datetime


@dataclass(frozen=True)
class ScheduledNotificationCandidate:
    user_id: int
    plan_id: int
    timezone_name: str
    due_at: datetime
    occurrences: tuple[ScheduledOccurrenceCandidate, ...]


@dataclass(frozen=True)
class ScheduledDispatchCreationResult:
    created_dispatch_ids: tuple[int, ...]
    reused_dispatch_ids: tuple[int, ...]
    created_member_count: int
    materialized_occurrence_count: int
    skipped_responded_count: int
    skipped_invalid_count: int
    skipped_non_pending_count: int


def list_scheduled_notification_candidates(
    session,
    *,
    action_time: datetime,
    rollout_at: datetime,
    scheduled_grace: timedelta,
) -> tuple[ScheduledNotificationCandidate, ...]:
    """Return read-only scheduled-notification groups due in the window."""

    window_start, window_end = _scheduled_candidate_window(
        action_time=action_time,
        rollout_at=rollout_at,
        scheduled_grace=scheduled_grace,
    )

    if window_start >= window_end:
        return ()

    schedules = session.scalars(
        select(MedicationSchedule)
        .join(MedicationSchedule.user_medicine)
        .join(MedicationSchedule.plan)
        .options(
            joinedload(MedicationSchedule.user_medicine).joinedload(
                UserMedicine.user
            ),
            joinedload(MedicationSchedule.plan),
            selectinload(MedicationSchedule.times),
        )
        .where(
            MedicationSchedule.plan_id.is_not(None),
            MedicationPlan.user_id == UserMedicine.user_id,
            MedicationSchedule.reminder_tracking_started_at < window_end,
            (
                MedicationSchedule.closed_at.is_(None)
                | (MedicationSchedule.closed_at > window_start)
            ),
        )
        .order_by(MedicationSchedule.schedule_id.asc())
        .execution_options(autoflush=False)
    ).all()

    virtual_occurrences = []

    for schedule in schedules:
        timezone_name = schedule.user_medicine.user.timezone

        try:
            scheduled_instants = list_schedule_occurrences_in_window(
                schedule,
                timezone_name=timezone_name,
                window_start=window_start,
                window_end=window_end,
            )
        except MedicationOccurrenceError:
            continue

        virtual_occurrences.extend(
            (
                schedule,
                timezone_name,
                scheduled_for,
            )
            for scheduled_for in scheduled_instants
        )

    occurrence_keys = [
        (schedule.schedule_id, scheduled_for)
        for schedule, _, scheduled_for in virtual_occurrences
    ]
    with session.no_autoflush:
        existing_occurrences = list_existing_medication_occurrences(
            session,
            occurrence_keys,
        )
    grouped_occurrences = {}

    for schedule, timezone_name, scheduled_for in virtual_occurrences:
        occurrence = existing_occurrences.get(
            (schedule.schedule_id, scheduled_for)
        )

        if occurrence is not None and occurrence.response_status is not None:
            continue

        group_key = (
            schedule.user_medicine.user_id,
            schedule.plan_id,
            scheduled_for,
        )
        group = grouped_occurrences.setdefault(
            group_key,
            {
                "timezone_name": timezone_name,
                "occurrences": [],
            },
        )
        group["occurrences"].append(
            ScheduledOccurrenceCandidate(
                schedule_id=schedule.schedule_id,
                scheduled_for=scheduled_for,
            )
        )

    candidates = []

    for (user_id, plan_id, due_at), group in grouped_occurrences.items():
        occurrences = tuple(
            sorted(
                group["occurrences"],
                key=lambda value: value.schedule_id,
            )
        )
        candidates.append(
            ScheduledNotificationCandidate(
                user_id=user_id,
                plan_id=plan_id,
                timezone_name=group["timezone_name"],
                due_at=due_at,
                occurrences=occurrences,
            )
        )

    candidates.sort(
        key=lambda value: (
            value.due_at,
            value.user_id,
            value.plan_id,
        )
    )
    return tuple(candidates)


def create_scheduled_notification_dispatches(
    session,
    candidates: Iterable[ScheduledNotificationCandidate],
    *,
    action_time: datetime,
) -> ScheduledDispatchCreationResult:
    """Materialize candidates and add pending dispatches without committing."""

    action_time_utc = _normalize_service_instant(
        action_time,
        name="action_time",
    )
    normalized_candidates = _normalize_candidates(candidates)

    if not normalized_candidates:
        return _empty_creation_result()

    occurrence_candidates = {
        (
            occurrence.schedule_id,
            occurrence.scheduled_for,
        ): candidate
        for candidate in normalized_candidates
        for occurrence in candidate.occurrences
    }
    schedule_ids = sorted(
        {
            schedule_id
            for schedule_id, _ in occurrence_candidates
        }
    )
    schedules = session.scalars(
        select(MedicationSchedule)
        .options(
            joinedload(MedicationSchedule.user_medicine).joinedload(
                UserMedicine.user
            ),
            selectinload(MedicationSchedule.times),
        )
        .where(MedicationSchedule.schedule_id.in_(schedule_ids))
        .order_by(MedicationSchedule.schedule_id.asc())
    ).all()
    schedules_by_id = {
        schedule.schedule_id: schedule
        for schedule in schedules
    }
    existing_before = list_existing_medication_occurrences(
        session,
        occurrence_candidates,
    )
    valid_occurrence_groups = {}
    materialized_occurrence_count = 0
    skipped_invalid_count = 0

    for occurrence_key, candidate in occurrence_candidates.items():
        schedule_id, scheduled_for = occurrence_key
        schedule = schedules_by_id.get(schedule_id)

        if (
            schedule is None
            or schedule.plan_id != candidate.plan_id
            or schedule.user_medicine.user_id != candidate.user_id
        ):
            skipped_invalid_count += 1
            continue

        timezone_name = schedule.user_medicine.user.timezone

        try:
            validate_schedule_occurrence(
                schedule,
                timezone_name=timezone_name,
                scheduled_for=scheduled_for,
            )

            if occurrence_key not in existing_before:
                get_or_create_medication_occurrence(
                    session,
                    schedule,
                    timezone_name=timezone_name,
                    scheduled_for=scheduled_for,
                    action_time=action_time_utc,
                )
                materialized_occurrence_count += 1
        except MedicationOccurrenceError:
            skipped_invalid_count += 1
            continue

        group_key = (
            candidate.user_id,
            candidate.plan_id,
            candidate.due_at,
        )
        valid_occurrence_groups.setdefault(group_key, set()).add(
            occurrence_key
        )

    valid_occurrence_keys = {
        occurrence_key
        for keys in valid_occurrence_groups.values()
        for occurrence_key in keys
    }
    current_occurrences = _list_current_occurrences(
        session,
        valid_occurrence_keys,
    )
    final_group_occurrence_ids = {}
    skipped_responded_count = 0

    for group_key, occurrence_keys in valid_occurrence_groups.items():
        for occurrence_key in occurrence_keys:
            occurrence = current_occurrences.get(occurrence_key)

            if occurrence is None:
                skipped_invalid_count += 1
                continue

            if occurrence.response_status is not None:
                skipped_responded_count += 1
                continue

            final_group_occurrence_ids.setdefault(group_key, set()).add(
                occurrence.occurrence_id
            )

    final_group_occurrence_ids = {
        group_key: occurrence_ids
        for group_key, occurrence_ids in final_group_occurrence_ids.items()
        if occurrence_ids
    }

    if not final_group_occurrence_ids:
        return ScheduledDispatchCreationResult(
            created_dispatch_ids=(),
            reused_dispatch_ids=(),
            created_member_count=0,
            materialized_occurrence_count=materialized_occurrence_count,
            skipped_responded_count=skipped_responded_count,
            skipped_invalid_count=skipped_invalid_count,
            skipped_non_pending_count=0,
        )

    dispatch_values = [
        {
            "user_id": user_id,
            "plan_id": plan_id,
            "course_root_schedule_id": None,
            "notification_type": SCHEDULED_NOTIFICATION_TYPE,
            "delivery_channel": EMAIL_DELIVERY_CHANNEL,
            "due_at": due_at,
            "status": PENDING_DISPATCH_STATUS,
            "attempt_count": 0,
            "next_attempt_at": None,
            "created_at": action_time_utc,
            "updated_at": action_time_utc,
        }
        for user_id, plan_id, due_at in sorted(
            final_group_occurrence_ids,
            key=lambda value: (value[2], value[0], value[1]),
        )
    ]
    insert_result = session.execute(
        sqlite_insert(NotificationDispatch)
        .values(dispatch_values)
        .on_conflict_do_nothing()
        .returning(NotificationDispatch.dispatch_id)
    )
    created_dispatch_ids = tuple(sorted(insert_result.scalars().all()))
    dispatch_keys = [
        (
            user_id,
            plan_id,
            due_at,
            SCHEDULED_NOTIFICATION_TYPE,
        )
        for user_id, plan_id, due_at in final_group_occurrence_ids
    ]
    dispatches = session.scalars(
        select(NotificationDispatch)
        .where(
            tuple_(
                NotificationDispatch.user_id,
                NotificationDispatch.plan_id,
                NotificationDispatch.due_at,
                NotificationDispatch.notification_type,
            ).in_(dispatch_keys)
        )
        .execution_options(populate_existing=True)
    ).all()
    dispatches_by_group = {
        (
            dispatch.user_id,
            dispatch.plan_id,
            _stored_datetime_as_utc(dispatch.due_at),
        ): dispatch
        for dispatch in dispatches
    }
    reused_dispatch_ids = []
    skipped_non_pending_count = 0
    member_values = set()
    created_dispatch_id_set = set(created_dispatch_ids)

    for group_key, occurrence_ids in final_group_occurrence_ids.items():
        dispatch = dispatches_by_group.get(group_key)

        if dispatch is None:
            raise NotificationServiceError(
                "The scheduled notification dispatch could not be loaded."
            )

        if dispatch.dispatch_id not in created_dispatch_id_set:
            reused_dispatch_ids.append(dispatch.dispatch_id)

        if dispatch.status != PENDING_DISPATCH_STATUS:
            skipped_non_pending_count += 1
            continue

        member_values.update(
            (dispatch.dispatch_id, occurrence_id)
            for occurrence_id in occurrence_ids
        )

    created_member_count = 0

    if member_values:
        member_result = session.execute(
            sqlite_insert(NotificationDispatchMember)
            .values(
                [
                    {
                        "dispatch_id": dispatch_id,
                        "occurrence_id": occurrence_id,
                    }
                    for dispatch_id, occurrence_id in sorted(member_values)
                ]
            )
            .on_conflict_do_nothing(
                index_elements=("dispatch_id", "occurrence_id"),
            )
            .returning(NotificationDispatchMember.occurrence_id)
        )
        created_member_count = len(member_result.scalars().all())

    return ScheduledDispatchCreationResult(
        created_dispatch_ids=created_dispatch_ids,
        reused_dispatch_ids=tuple(sorted(set(reused_dispatch_ids))),
        created_member_count=created_member_count,
        materialized_occurrence_count=materialized_occurrence_count,
        skipped_responded_count=skipped_responded_count,
        skipped_invalid_count=skipped_invalid_count,
        skipped_non_pending_count=skipped_non_pending_count,
    )


def _scheduled_candidate_window(
    *,
    action_time: datetime,
    rollout_at: datetime,
    scheduled_grace: timedelta,
) -> tuple[datetime, datetime]:
    action_time_utc = _normalize_service_instant(
        action_time,
        name="action_time",
    )
    rollout_at_utc = _normalize_service_instant(
        rollout_at,
        name="rollout_at",
    )

    if not isinstance(scheduled_grace, timedelta):
        raise NotificationServiceError(
            "scheduled_grace must be a datetime.timedelta value."
        )

    if scheduled_grace < timedelta(0):
        raise NotificationServiceError(
            "scheduled_grace must not be negative."
        )

    try:
        grace_start = action_time_utc - scheduled_grace
        inclusive_end = action_time_utc + timedelta(microseconds=1)
    except OverflowError as error:
        raise NotificationServiceError(
            "The scheduled notification window is outside the supported "
            "datetime range."
        ) from error

    return max(rollout_at_utc, grace_start), inclusive_end


def _normalize_service_instant(value: datetime, *, name: str) -> datetime:
    try:
        return normalize_occurrence_instant(value, name=name)
    except MedicationOccurrenceError as error:
        raise NotificationServiceError(str(error)) from error


def _normalize_candidates(
    candidates: Iterable[ScheduledNotificationCandidate],
) -> tuple[ScheduledNotificationCandidate, ...]:
    try:
        candidate_values = tuple(candidates)
    except TypeError as error:
        raise NotificationServiceError(
            "candidates must be an iterable of scheduled groups."
        ) from error

    grouped_values = {}

    for candidate in candidate_values:
        if not isinstance(candidate, ScheduledNotificationCandidate):
            raise NotificationServiceError(
                "Every candidate must be a ScheduledNotificationCandidate."
            )

        if (
            isinstance(candidate.user_id, bool)
            or not isinstance(candidate.user_id, int)
            or candidate.user_id < 1
            or isinstance(candidate.plan_id, bool)
            or not isinstance(candidate.plan_id, int)
            or candidate.plan_id < 1
        ):
            raise NotificationServiceError(
                "Candidate user_id and plan_id must be positive integers."
            )

        if (
            not isinstance(candidate.timezone_name, str)
            or not candidate.timezone_name.strip()
        ):
            raise NotificationServiceError(
                "Candidate timezone_name must be a non-empty string."
            )

        due_at = _normalize_service_instant(
            candidate.due_at,
            name="candidate.due_at",
        )

        if not candidate.occurrences:
            raise NotificationServiceError(
                "A scheduled notification candidate must have occurrences."
            )

        group_key = (candidate.user_id, candidate.plan_id, due_at)
        group = grouped_values.setdefault(
            group_key,
            {
                "timezone_name": candidate.timezone_name,
                "occurrences": set(),
            },
        )

        if group["timezone_name"] != candidate.timezone_name:
            raise NotificationServiceError(
                "One user cannot have multiple timezones in one group."
            )

        for occurrence in candidate.occurrences:
            if not isinstance(occurrence, ScheduledOccurrenceCandidate):
                raise NotificationServiceError(
                    "Every occurrence must be a scheduled occurrence "
                    "candidate."
                )

            if (
                isinstance(occurrence.schedule_id, bool)
                or not isinstance(occurrence.schedule_id, int)
                or occurrence.schedule_id < 1
            ):
                raise NotificationServiceError(
                    "Candidate schedule_id must be a positive integer."
                )

            scheduled_for = _normalize_service_instant(
                occurrence.scheduled_for,
                name="candidate.scheduled_for",
            )

            if scheduled_for != due_at:
                raise NotificationServiceError(
                    "Every occurrence must match the group's exact due_at."
                )

            group["occurrences"].add(
                ScheduledOccurrenceCandidate(
                    schedule_id=occurrence.schedule_id,
                    scheduled_for=scheduled_for,
                )
            )

    normalized = [
        ScheduledNotificationCandidate(
            user_id=user_id,
            plan_id=plan_id,
            timezone_name=group["timezone_name"],
            due_at=due_at,
            occurrences=tuple(
                sorted(
                    group["occurrences"],
                    key=lambda value: value.schedule_id,
                )
            ),
        )
        for (user_id, plan_id, due_at), group in grouped_values.items()
    ]
    normalized.sort(
        key=lambda value: (
            value.due_at,
            value.user_id,
            value.plan_id,
        )
    )
    return tuple(normalized)


def _list_current_occurrences(
    session,
    occurrence_keys: set[tuple[int, datetime]],
) -> dict[tuple[int, datetime], MedicationOccurrence]:
    if not occurrence_keys:
        return {}

    occurrences = session.scalars(
        select(MedicationOccurrence)
        .where(
            tuple_(
                MedicationOccurrence.schedule_id,
                MedicationOccurrence.scheduled_for,
            ).in_(occurrence_keys)
        )
        .execution_options(populate_existing=True)
    ).all()
    return {
        (
            occurrence.schedule_id,
            _stored_datetime_as_utc(occurrence.scheduled_for),
        ): occurrence
        for occurrence in occurrences
    }


def _stored_datetime_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


def _empty_creation_result() -> ScheduledDispatchCreationResult:
    return ScheduledDispatchCreationResult(
        created_dispatch_ids=(),
        reused_dispatch_ids=(),
        created_member_count=0,
        materialized_occurrence_count=0,
        skipped_responded_count=0,
        skipped_invalid_count=0,
        skipped_non_pending_count=0,
    )
