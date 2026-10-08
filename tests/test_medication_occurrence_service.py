import tempfile
import unittest
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from flask_migrate import upgrade
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

import app as app_module
from models import (
    Medicine,
    MedicationOccurrence,
    MedicationPlan,
    MedicationPlanTime,
    MedicationSchedule,
    MedicationSchedulePlanTime,
    MedicationTime,
    User,
    UserMedicine,
    db,
)
from services.medication_occurrence_service import (
    MedicationOccurrenceError,
    get_or_create_medication_occurrence,
    list_existing_medication_occurrences,
    list_schedule_occurrences_in_window,
    normalize_occurrence_instant,
    validate_schedule_occurrence,
)


MIGRATION_HEAD = "c6f4a2d9e8b1"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
SEOUL = ZoneInfo("Asia/Seoul")


def utc_datetime(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def local_datetime(
    year,
    month,
    day,
    hour=0,
    minute=0,
    *,
    timezone=SEOUL,
):
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=timezone,
    )


def stored_as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


class MedicationOccurrenceServiceTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "occurrence-service.db"
        )
        self.application_context = app_module.app.app_context()
        self.application_context.push()
        db.session.remove()
        self.original_engine = db.engines[None]
        self.test_engine = create_engine(
            "sqlite:///" + self.database_path.as_posix()
        )
        db.engines[None] = self.test_engine

        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=MIGRATION_HEAD,
        )

        self.user = User(
            username="occurrence-user",
            email="occurrence@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.medicine = Medicine(
            item_seq="OCCURRENCE-MED",
            item_name="Occurrence Medicine",
        )
        self.user_medicine = UserMedicine(
            user=self.user,
            medicine=self.medicine,
            registration_source="search",
            is_active=True,
        )
        self.plan = MedicationPlan(
            user=self.user,
            is_active=True,
        )
        db.session.add_all(
            [
                self.user,
                self.medicine,
                self.user_medicine,
                self.plan,
            ]
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def add_schedule(
        self,
        *,
        medication_times=(time(8), time(20)),
        start_date_value=date(2026, 10, 8),
        tracking_at=utc_datetime(2026, 10, 7, 15),
        closed_at=None,
        course_days=3,
        reported=0,
        accounted=0,
        plan_bound=True,
        supersedes_schedule_id=None,
    ):
        schedule = MedicationSchedule(
            user_medicine_id=self.user_medicine.user_medicine_id,
            plan_id=self.plan.plan_id if plan_bound else None,
            supersedes_schedule_id=supersedes_schedule_id,
            intake_timing="after_meal",
            dose_amount_text="1",
            dose_unit_text="tablet",
            instructions=None,
            start_date=start_date_value,
            end_date=None,
            course_days=course_days,
            reported_doses_taken_before_tracking=reported,
            reminder_tracking_started_at=tracking_at,
            accounted_occurrence_count=accounted,
            monday=True,
            tuesday=True,
            wednesday=True,
            thursday=True,
            friday=True,
            saturday=True,
            sunday=True,
            is_active=False,
            closed_at=closed_at,
            created_at=tracking_at,
            updated_at=tracking_at,
        )
        schedule.times.extend(
            MedicationTime(time_of_day=value)
            for value in medication_times
        )
        db.session.add(schedule)
        db.session.commit()
        return schedule

    def list_window(
        self,
        schedule,
        *,
        start=utc_datetime(2026, 10, 7, 15),
        end=utc_datetime(2026, 10, 9, 15),
        timezone_name="Asia/Seoul",
    ):
        return list_schedule_occurrences_in_window(
            schedule,
            timezone_name=timezone_name,
            window_start=start,
            window_end=end,
        )

    def test_window_enumeration_is_sorted_and_half_open(self):
        schedule = self.add_schedule(
            medication_times=(time(20), time(8)),
        )

        occurrences = self.list_window(schedule)
        self.assertEqual(
            occurrences,
            (
                utc_datetime(2026, 10, 7, 23),
                utc_datetime(2026, 10, 8, 11),
                utc_datetime(2026, 10, 8, 23),
                utc_datetime(2026, 10, 9, 11),
            ),
        )
        self.assertEqual(tuple(sorted(occurrences)), occurrences)
        self.assertEqual(
            self.list_window(
                schedule,
                start=utc_datetime(2026, 10, 6, 15),
                end=utc_datetime(2026, 10, 7, 15),
            ),
            (),
        )
        self.assertEqual(
            self.list_window(
                schedule,
                start=utc_datetime(2026, 10, 7, 23),
                end=utc_datetime(2026, 10, 8, 11),
            ),
            (utc_datetime(2026, 10, 7, 23),),
        )

    def test_start_and_closed_boundaries_are_enforced(self):
        tracked_after_morning = self.add_schedule(
            tracking_at=utc_datetime(2026, 10, 8, 1),
        )
        self.assertEqual(
            self.list_window(
                tracked_after_morning,
                start=utc_datetime(2026, 10, 7, 15),
                end=utc_datetime(2026, 10, 8, 15),
            ),
            (utc_datetime(2026, 10, 8, 11),),
        )

        closed_at_evening = self.add_schedule(
            closed_at=utc_datetime(2026, 10, 8, 11),
        )
        self.assertEqual(
            self.list_window(
                closed_at_evening,
                start=utc_datetime(2026, 10, 7, 15),
                end=utc_datetime(2026, 10, 8, 15),
            ),
            (utc_datetime(2026, 10, 7, 23),),
        )
        self.assertEqual(
            self.list_window(
                closed_at_evening,
                start=utc_datetime(2026, 10, 8, 11),
                end=utc_datetime(2026, 10, 8, 12),
            ),
            (),
        )

    def test_capacity_and_middle_window_account_for_prior_slots(self):
        nearly_consumed = self.add_schedule(
            medication_times=(time(8), time(14), time(20)),
            course_days=3,
            reported=4,
            accounted=4,
        )
        self.assertEqual(
            self.list_window(
                nearly_consumed,
                start=utc_datetime(2026, 10, 7, 15),
                end=utc_datetime(2026, 10, 9, 15),
            ),
            (utc_datetime(2026, 10, 7, 23),),
        )

        middle_window = self.add_schedule(
            course_days=2,
            reported=1,
        )
        self.assertEqual(
            self.list_window(
                middle_window,
                start=utc_datetime(2026, 10, 8, 15),
                end=utc_datetime(2026, 10, 9, 15),
            ),
            (utc_datetime(2026, 10, 8, 23),),
        )

    def test_version_chain_uses_each_schedule_time_snapshot(self):
        old_schedule = self.add_schedule(
            closed_at=utc_datetime(2026, 10, 8, 3),
        )
        successor = self.add_schedule(
            medication_times=(time(9), time(20)),
            tracking_at=utc_datetime(2026, 10, 8, 3),
            accounted=1,
            supersedes_schedule_id=old_schedule.schedule_id,
        )
        window = {
            "start": utc_datetime(2026, 10, 7, 15),
            "end": utc_datetime(2026, 10, 8, 15),
        }

        self.assertEqual(
            self.list_window(old_schedule, **window),
            (utc_datetime(2026, 10, 7, 23),),
        )
        self.assertEqual(
            self.list_window(successor, **window),
            (utc_datetime(2026, 10, 8, 11),),
        )

    def test_time_removal_chain_uses_successor_snapshot(self):
        old_schedule = self.add_schedule(
            closed_at=utc_datetime(2026, 10, 8, 3),
        )
        successor = self.add_schedule(
            medication_times=(time(20),),
            tracking_at=utc_datetime(2026, 10, 8, 3),
            accounted=1,
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        self.assertEqual(
            self.list_window(
                old_schedule,
                end=utc_datetime(2026, 10, 8, 15),
            ),
            (utc_datetime(2026, 10, 7, 23),),
        )
        self.assertEqual(
            self.list_window(
                successor,
                end=utc_datetime(2026, 10, 8, 15),
            ),
            (utc_datetime(2026, 10, 8, 11),),
        )

    def test_plan_time_association_is_not_historical_truth(self):
        schedule = self.add_schedule(medication_times=(time(8),))
        plan_time = MedicationPlanTime(
            plan_id=self.plan.plan_id,
            time_of_day=time(9),
            created_at=utc_datetime(2026, 10, 7, 15),
        )
        db.session.add(plan_time)
        db.session.flush()
        db.session.add(
            MedicationSchedulePlanTime(
                plan_id=self.plan.plan_id,
                schedule_id=schedule.schedule_id,
                plan_time_id=plan_time.plan_time_id,
            )
        )
        db.session.commit()

        self.assertEqual(
            self.list_window(
                schedule,
                end=utc_datetime(2026, 10, 8, 15),
            ),
            (utc_datetime(2026, 10, 7, 23),),
        )

    def test_legacy_and_invalid_windows_are_rejected(self):
        legacy = self.add_schedule(plan_bound=False)
        with self.assertRaises(MedicationOccurrenceError):
            self.list_window(legacy)

        schedule = self.add_schedule()
        invalid_windows = (
            (
                datetime(2026, 10, 8),
                utc_datetime(2026, 10, 9),
            ),
            (
                utc_datetime(2026, 10, 9),
                utc_datetime(2026, 10, 9),
            ),
            (
                utc_datetime(2026, 10, 10),
                utc_datetime(2026, 10, 9),
            ),
        )
        for window_start, window_end in invalid_windows:
            with self.subTest(start=window_start, end=window_end):
                with self.assertRaises(MedicationOccurrenceError):
                    self.list_window(
                        schedule,
                        start=window_start,
                        end=window_end,
                    )

    def test_utc_normalization_and_equivalent_offsets(self):
        seoul_value = local_datetime(2026, 10, 8, 8)
        expected = utc_datetime(2026, 10, 7, 23)
        self.assertEqual(normalize_occurrence_instant(seoul_value), expected)
        self.assertEqual(normalize_occurrence_instant(expected), expected)

        with self.assertRaises(MedicationOccurrenceError):
            normalize_occurrence_instant(datetime(2026, 10, 8, 8))

        schedule = self.add_schedule(medication_times=(time(8),))
        self.assertEqual(
            validate_schedule_occurrence(
                schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=seoul_value,
            ),
            expected,
        )
        self.assertEqual(
            validate_schedule_occurrence(
                schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=expected,
            ),
            expected,
        )

    def test_dst_gap_and_overlap_follow_schedule_policy(self):
        spring_schedule = self.add_schedule(
            medication_times=(time(2, 30),),
            start_date_value=date(2026, 3, 8),
            tracking_at=utc_datetime(2026, 3, 8, 5),
            course_days=1,
        )
        self.assertEqual(
            self.list_window(
                spring_schedule,
                start=utc_datetime(2026, 3, 8, 5),
                end=utc_datetime(2026, 3, 9, 5),
                timezone_name="America/New_York",
            ),
            (utc_datetime(2026, 3, 8, 7, 30),),
        )

        fall_schedule = self.add_schedule(
            medication_times=(time(1, 30),),
            start_date_value=date(2026, 11, 1),
            tracking_at=utc_datetime(2026, 11, 1, 4),
            course_days=1,
        )
        self.assertEqual(
            self.list_window(
                fall_schedule,
                start=utc_datetime(2026, 11, 1, 4),
                end=utc_datetime(2026, 11, 2, 5),
                timezone_name="America/New_York",
            ),
            (utc_datetime(2026, 11, 1, 5, 30),),
        )

    def test_exact_occurrence_validation_checks_all_boundaries(self):
        schedule = self.add_schedule(course_days=1)
        self.assertEqual(
            validate_schedule_occurrence(
                schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=utc_datetime(2026, 10, 7, 23),
            ),
            utc_datetime(2026, 10, 7, 23),
        )
        with self.assertRaises(MedicationOccurrenceError):
            validate_schedule_occurrence(
                schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=utc_datetime(2026, 10, 8),
            )

        closed = self.add_schedule(
            closed_at=utc_datetime(2026, 10, 8, 3),
        )
        with self.assertRaises(MedicationOccurrenceError):
            validate_schedule_occurrence(
                closed,
                timezone_name="Asia/Seoul",
                scheduled_for=utc_datetime(2026, 10, 8, 11),
            )

        capacity_limited = self.add_schedule(course_days=1, reported=1)
        with self.assertRaises(MedicationOccurrenceError):
            validate_schedule_occurrence(
                capacity_limited,
                timezone_name="Asia/Seoul",
                scheduled_for=utc_datetime(2026, 10, 8, 11),
            )

    def test_materialization_is_validated_idempotent_and_unanswered(self):
        schedule = self.add_schedule()
        action_time = local_datetime(2026, 10, 8, 8, 5)
        occurrence = get_or_create_medication_occurrence(
            db.session,
            schedule,
            timezone_name="Asia/Seoul",
            scheduled_for=local_datetime(2026, 10, 8, 8),
            action_time=action_time,
        )

        self.assertIsNone(occurrence.response_status)
        self.assertIsNone(occurrence.responded_at)
        self.assertEqual(
            stored_as_utc(occurrence.scheduled_for),
            utc_datetime(2026, 10, 7, 23),
        )
        self.assertEqual(
            stored_as_utc(occurrence.created_at),
            utc_datetime(2026, 10, 7, 23, 5),
        )
        self.assertEqual(
            stored_as_utc(occurrence.updated_at),
            utc_datetime(2026, 10, 7, 23, 5),
        )

        same_occurrence = get_or_create_medication_occurrence(
            db.session,
            schedule,
            timezone_name="Asia/Seoul",
            scheduled_for=utc_datetime(2026, 10, 7, 23),
            action_time=utc_datetime(2026, 10, 8),
        )
        self.assertEqual(same_occurrence.occurrence_id, occurrence.occurrence_id)
        self.assertEqual(
            db.session.scalar(
                select(func.count()).select_from(MedicationOccurrence)
            ),
            1,
        )

    def test_invalid_and_legacy_slots_are_not_materialized(self):
        schedule = self.add_schedule()
        legacy = self.add_schedule(plan_bound=False)

        for target_schedule, scheduled_for in (
            (schedule, utc_datetime(2026, 10, 8)),
            (legacy, utc_datetime(2026, 10, 7, 23)),
        ):
            with self.subTest(schedule_id=target_schedule.schedule_id):
                with self.assertRaises(MedicationOccurrenceError):
                    get_or_create_medication_occurrence(
                        db.session,
                        target_schedule,
                        timezone_name="Asia/Seoul",
                        scheduled_for=scheduled_for,
                        action_time=utc_datetime(2026, 10, 8),
                    )

        with self.assertRaises(MedicationOccurrenceError):
            get_or_create_medication_occurrence(
                db.session,
                schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=utc_datetime(2026, 10, 7, 23),
                action_time=datetime(2026, 10, 8),
            )

        self.assertEqual(
            db.session.scalar(
                select(func.count()).select_from(MedicationOccurrence)
            ),
            0,
        )

    def test_get_or_create_does_not_change_an_existing_response(self):
        schedule = self.add_schedule()
        occurrence = get_or_create_medication_occurrence(
            db.session,
            schedule,
            timezone_name="Asia/Seoul",
            scheduled_for=utc_datetime(2026, 10, 7, 23),
            action_time=utc_datetime(2026, 10, 8),
        )
        response_time = utc_datetime(2026, 10, 8, 1)
        occurrence.response_status = "taken"
        occurrence.responded_at = response_time
        occurrence.updated_at = response_time
        db.session.commit()

        existing = get_or_create_medication_occurrence(
            db.session,
            schedule,
            timezone_name="Asia/Seoul",
            scheduled_for=local_datetime(2026, 10, 8, 8),
            action_time=utc_datetime(2026, 10, 8, 2),
        )

        self.assertEqual(existing.response_status, "taken")
        self.assertEqual(stored_as_utc(existing.responded_at), response_time)
        self.assertEqual(stored_as_utc(existing.updated_at), response_time)

    def test_materialization_does_not_commit(self):
        schedule = self.add_schedule()
        get_or_create_medication_occurrence(
            db.session,
            schedule,
            timezone_name="Asia/Seoul",
            scheduled_for=utc_datetime(2026, 10, 7, 23),
            action_time=utc_datetime(2026, 10, 8),
        )
        db.session.rollback()

        self.assertEqual(
            db.session.scalar(
                select(func.count()).select_from(MedicationOccurrence)
            ),
            0,
        )

    def test_unique_conflict_path_keeps_caller_transaction_usable(self):
        schedule = self.add_schedule()
        schedule_id = schedule.schedule_id
        db.session.remove()

        with Session(self.test_engine) as first_session:
            first_schedule = first_session.get(
                MedicationSchedule,
                schedule_id,
            )
            first = get_or_create_medication_occurrence(
                first_session,
                first_schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=utc_datetime(2026, 10, 7, 23),
                action_time=utc_datetime(2026, 10, 8),
            )
            first_session.commit()
            first_id = first.occurrence_id

        with Session(self.test_engine) as second_session:
            second_schedule = second_session.get(
                MedicationSchedule,
                schedule_id,
            )
            existing = get_or_create_medication_occurrence(
                second_session,
                second_schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=local_datetime(2026, 10, 8, 8),
                action_time=utc_datetime(2026, 10, 8, 1),
            )
            additional = get_or_create_medication_occurrence(
                second_session,
                second_schedule,
                timezone_name="Asia/Seoul",
                scheduled_for=utc_datetime(2026, 10, 8, 11),
                action_time=utc_datetime(2026, 10, 8, 1),
            )
            second_session.commit()

            self.assertEqual(existing.occurrence_id, first_id)
            self.assertNotEqual(additional.occurrence_id, first_id)
            self.assertEqual(
                second_session.scalar(
                    select(func.count()).select_from(MedicationOccurrence)
                ),
                2,
            )

    def test_existing_occurrences_are_loaded_with_one_select(self):
        schedule = self.add_schedule()
        first = get_or_create_medication_occurrence(
            db.session,
            schedule,
            timezone_name="Asia/Seoul",
            scheduled_for=utc_datetime(2026, 10, 7, 23),
            action_time=utc_datetime(2026, 10, 8),
        )
        second = get_or_create_medication_occurrence(
            db.session,
            schedule,
            timezone_name="Asia/Seoul",
            scheduled_for=utc_datetime(2026, 10, 8, 11),
            action_time=utc_datetime(2026, 10, 8),
        )
        db.session.commit()
        db.session.expire_all()
        select_count = 0

        def count_occurrence_selects(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            nonlocal select_count

            if (
                statement.lstrip().upper().startswith("SELECT")
                and "medication_occurrences" in statement
            ):
                select_count += 1

        event.listen(
            self.test_engine,
            "before_cursor_execute",
            count_occurrence_selects,
        )
        try:
            existing = list_existing_medication_occurrences(
                db.session,
                (
                    (
                        schedule.schedule_id,
                        local_datetime(2026, 10, 8, 8),
                    ),
                    (
                        schedule.schedule_id,
                        utc_datetime(2026, 10, 8, 11),
                    ),
                    (
                        schedule.schedule_id,
                        utc_datetime(2026, 10, 8, 23),
                    ),
                ),
            )
        finally:
            event.remove(
                self.test_engine,
                "before_cursor_execute",
                count_occurrence_selects,
            )

        self.assertEqual(select_count, 1)
        self.assertEqual(
            set(existing),
            {
                (
                    schedule.schedule_id,
                    utc_datetime(2026, 10, 7, 23),
                ),
                (
                    schedule.schedule_id,
                    utc_datetime(2026, 10, 8, 11),
                ),
            },
        )
        self.assertEqual(
            {value.occurrence_id for value in existing.values()},
            {first.occurrence_id, second.occurrence_id},
        )


if __name__ == "__main__":
    unittest.main()
