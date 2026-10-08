import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from flask_migrate import upgrade
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

import app as app_module
from models import (
    Medicine,
    MedicationOccurrence,
    MedicationPlan,
    MedicationSchedule,
    MedicationTime,
    NotificationDispatch,
    NotificationDispatchMember,
    User,
    UserMedicine,
    db,
)
from services.notification_service import (
    NotificationServiceError,
    ScheduledNotificationCandidate,
    ScheduledOccurrenceCandidate,
    create_scheduled_notification_dispatches,
    list_scheduled_notification_candidates,
)


MIGRATION_HEAD = "d9e7b4c2a1f6"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)


def utc_datetime(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def stored_as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


class NotificationServiceTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "notification-service.db"
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

        (
            self.user,
            self.medicine,
            self.user_medicine,
            self.plan,
        ) = self.add_user_context("default")

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def add_user_context(self, suffix, *, timezone_name="Asia/Seoul"):
        user = User(
            username=f"notify-{suffix}",
            email=f"notify-{suffix}@example.com",
            password_hash="test-password-hash",
            timezone=timezone_name,
        )
        medicine = Medicine(
            item_seq=f"NOTIFY-{suffix}",
            item_name=f"Notification Medicine {suffix}",
        )
        user_medicine = UserMedicine(
            user=user,
            medicine=medicine,
            registration_source="search",
            is_active=True,
        )
        plan = MedicationPlan(user=user, is_active=False)
        db.session.add_all([user, medicine, user_medicine, plan])
        db.session.commit()
        return user, medicine, user_medicine, plan

    def add_schedule(
        self,
        *,
        user_medicine=None,
        plan=None,
        medication_times=(time(10),),
        start_date_value=date(2026, 10, 8),
        tracking_at=utc_datetime(2026, 10, 7, 15),
        closed_at=None,
        course_days=5,
        plan_bound=True,
        supersedes_schedule_id=None,
    ):
        user_medicine = user_medicine or self.user_medicine
        plan = plan or self.plan
        schedule = MedicationSchedule(
            user_medicine_id=user_medicine.user_medicine_id,
            plan_id=plan.plan_id if plan_bound else None,
            supersedes_schedule_id=supersedes_schedule_id,
            intake_timing="after_meal",
            dose_amount_text="1",
            dose_unit_text="tablet",
            instructions=None,
            start_date=start_date_value,
            end_date=None,
            course_days=course_days,
            reported_doses_taken_before_tracking=0,
            reminder_tracking_started_at=tracking_at,
            accounted_occurrence_count=0,
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

    def list_candidates(
        self,
        *,
        action_time=utc_datetime(2026, 10, 8, 1, 5),
        rollout_at=utc_datetime(2026, 10, 8),
        grace=timedelta(minutes=10),
        session=None,
    ):
        return list_scheduled_notification_candidates(
            session or db.session,
            action_time=action_time,
            rollout_at=rollout_at,
            scheduled_grace=grace,
        )

    def manual_candidate(self, *schedules, due_at=None):
        due_at = due_at or utc_datetime(2026, 10, 8, 1)
        first = schedules[0]
        return ScheduledNotificationCandidate(
            user_id=first.user_medicine.user_id,
            plan_id=first.plan_id,
            timezone_name=first.user_medicine.user.timezone,
            due_at=due_at,
            occurrences=tuple(
                ScheduledOccurrenceCandidate(
                    schedule_id=schedule.schedule_id,
                    scheduled_for=due_at,
                )
                for schedule in schedules
            ),
        )

    def add_occurrence(self, schedule, *, status=None, due_at=None):
        due_at = due_at or utc_datetime(2026, 10, 8, 1)
        responded_at = (
            utc_datetime(2026, 10, 8, 1, 2)
            if status is not None
            else None
        )
        occurrence = MedicationOccurrence(
            schedule_id=schedule.schedule_id,
            scheduled_for=due_at,
            response_status=status,
            responded_at=responded_at,
            created_at=utc_datetime(2026, 10, 8, 1, 1),
            updated_at=responded_at or utc_datetime(2026, 10, 8, 1, 1),
        )
        db.session.add(occurrence)
        db.session.commit()
        return occurrence

    def add_dispatch(self, *, status="pending", due_at=None):
        due_at = due_at or utc_datetime(2026, 10, 8, 1)
        dispatch = NotificationDispatch(
            user_id=self.user.user_id,
            plan_id=self.plan.plan_id,
            course_root_schedule_id=None,
            notification_type="scheduled_occurrence",
            delivery_channel="email",
            due_at=due_at,
            status=status,
            attempt_count=1 if status != "pending" else 0,
            next_attempt_at=None,
            claim_token="claim-token" if status == "claimed" else None,
            claimed_at=(
                utc_datetime(2026, 10, 8, 1, 3)
                if status == "claimed"
                else None
            ),
            sent_at=(
                utc_datetime(2026, 10, 8, 1, 4)
                if status == "sent"
                else None
            ),
            created_at=utc_datetime(2026, 10, 8, 1, 1),
            updated_at=utc_datetime(2026, 10, 8, 1, 4),
        )
        db.session.add(dispatch)
        db.session.commit()
        return dispatch

    def count(self, model):
        return db.session.scalar(select(func.count()).select_from(model))

    def test_01_valid_scheduled_candidate(self):
        schedule = self.add_schedule()

        candidates = self.list_candidates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].user_id, self.user.user_id)
        self.assertEqual(candidates[0].plan_id, self.plan.plan_id)
        self.assertEqual(candidates[0].due_at, utc_datetime(2026, 10, 8, 1))
        self.assertEqual(candidates[0].occurrences[0].schedule_id, schedule.schedule_id)

    def test_02_exact_action_time_is_included(self):
        self.add_schedule()

        candidates = self.list_candidates(
            action_time=utc_datetime(2026, 10, 8, 1),
            grace=timedelta(0),
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].due_at, utc_datetime(2026, 10, 8, 1))

    def test_03_future_occurrence_is_excluded(self):
        self.add_schedule(medication_times=(time(10, 1),))

        candidates = self.list_candidates(
            action_time=utc_datetime(2026, 10, 8, 1),
            grace=timedelta(minutes=10),
        )

        self.assertEqual(candidates, ())

    def test_04_occurrence_before_grace_is_excluded(self):
        self.add_schedule()

        candidates = self.list_candidates(
            action_time=utc_datetime(2026, 10, 8, 1, 16),
            grace=timedelta(minutes=15),
        )

        self.assertEqual(candidates, ())

    def test_05_occurrence_before_rollout_is_excluded(self):
        self.add_schedule()

        candidates = self.list_candidates(
            rollout_at=utc_datetime(2026, 10, 8, 1, 1),
        )

        self.assertEqual(candidates, ())

    def test_06_naive_action_time_is_rejected(self):
        with self.assertRaises(NotificationServiceError):
            self.list_candidates(
                action_time=datetime(2026, 10, 8, 1, 5),
            )

    def test_07_naive_rollout_at_is_rejected(self):
        with self.assertRaises(NotificationServiceError):
            self.list_candidates(
                rollout_at=datetime(2026, 10, 8),
            )

    def test_08_negative_grace_is_rejected(self):
        with self.assertRaises(NotificationServiceError):
            self.list_candidates(grace=timedelta(microseconds=-1))

    def test_09_legacy_schedule_is_excluded(self):
        self.add_schedule(plan_bound=False)

        self.assertEqual(self.list_candidates(), ())

    def test_09b_inactive_user_medicine_does_not_hide_valid_snapshot(self):
        self.add_schedule()
        self.user_medicine.is_active = False
        db.session.commit()

        candidates = self.list_candidates()

        self.assertEqual(len(candidates), 1)

    def test_10_closed_boundary_is_half_open(self):
        self.add_schedule(closed_at=utc_datetime(2026, 10, 8, 1))

        self.assertEqual(self.list_candidates(), ())

    def test_11_time_move_uses_each_schedule_snapshot(self):
        old_schedule = self.add_schedule(
            closed_at=utc_datetime(2026, 10, 8, 1, 30),
        )
        self.add_schedule(
            medication_times=(time(11),),
            tracking_at=utc_datetime(2026, 10, 8, 1, 30),
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        candidates = self.list_candidates(
            action_time=utc_datetime(2026, 10, 8, 2, 5),
            grace=timedelta(minutes=70),
        )

        self.assertEqual(
            tuple(candidate.due_at for candidate in candidates),
            (
                utc_datetime(2026, 10, 8, 1),
                utc_datetime(2026, 10, 8, 2),
            ),
        )

    def test_12_time_remove_does_not_restore_removed_future_slot(self):
        old_schedule = self.add_schedule(
            medication_times=(time(10), time(11)),
            closed_at=utc_datetime(2026, 10, 8, 1, 30),
        )
        self.add_schedule(
            medication_times=(time(11),),
            tracking_at=utc_datetime(2026, 10, 8, 1, 30),
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        candidates = self.list_candidates(
            action_time=utc_datetime(2026, 10, 8, 2, 5),
            grace=timedelta(minutes=70),
        )

        self.assertEqual(len(candidates), 2)
        self.assertEqual(sum(len(value.occurrences) for value in candidates), 2)

    def test_13_same_user_plan_and_instant_are_grouped(self):
        first = self.add_schedule()
        second = self.add_schedule()

        candidates = self.list_candidates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            {value.schedule_id for value in candidates[0].occurrences},
            {first.schedule_id, second.schedule_id},
        )

    def test_14_same_instant_with_different_plans_is_split(self):
        self.add_schedule()
        other_plan = MedicationPlan(user_id=self.user.user_id, is_active=False)
        db.session.add(other_plan)
        db.session.commit()
        self.add_schedule(plan=other_plan)

        candidates = self.list_candidates()

        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {value.plan_id for value in candidates},
            {self.plan.plan_id, other_plan.plan_id},
        )

    def test_15_same_instant_with_different_users_is_split(self):
        self.add_schedule()
        other_user, _, other_user_medicine, other_plan = self.add_user_context(
            "other"
        )
        self.add_schedule(
            user_medicine=other_user_medicine,
            plan=other_plan,
        )

        candidates = self.list_candidates()

        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {value.user_id for value in candidates},
            {self.user.user_id, other_user.user_id},
        )

    def test_16_candidate_helper_is_read_only(self):
        schedule = self.add_schedule()

        candidates = self.list_candidates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(self.count(MedicationOccurrence), 0)
        self.assertEqual(self.count(NotificationDispatch), 0)
        self.assertEqual(self.count(NotificationDispatchMember), 0)
        db.session.refresh(schedule)
        self.assertEqual(schedule.accounted_occurrence_count, 0)

    def test_17_virtual_occurrence_is_materialized(self):
        self.add_schedule()
        candidates = self.list_candidates()
        action_time = utc_datetime(2026, 10, 8, 1, 5)

        result = create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=action_time,
        )
        occurrence = db.session.scalar(select(MedicationOccurrence))

        self.assertEqual(result.materialized_occurrence_count, 1)
        self.assertIsNone(occurrence.response_status)
        self.assertIsNone(occurrence.responded_at)
        self.assertEqual(stored_as_utc(occurrence.created_at), action_time)
        self.assertEqual(stored_as_utc(occurrence.updated_at), action_time)

    def test_18_existing_unanswered_occurrence_is_reused(self):
        schedule = self.add_schedule()
        occurrence = self.add_occurrence(schedule)

        result = create_scheduled_notification_dispatches(
            db.session,
            self.list_candidates(),
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )

        self.assertEqual(result.materialized_occurrence_count, 0)
        self.assertEqual(self.count(MedicationOccurrence), 1)
        member = db.session.scalar(select(NotificationDispatchMember))
        self.assertEqual(member.occurrence_id, occurrence.occurrence_id)

    def test_19_taken_occurrence_is_excluded(self):
        schedule = self.add_schedule()
        self.add_occurrence(schedule, status="taken")

        self.assertEqual(self.list_candidates(), ())

    def test_20_not_taken_occurrence_is_excluded(self):
        schedule = self.add_schedule()
        self.add_occurrence(schedule, status="not_taken")

        self.assertEqual(self.list_candidates(), ())

    def test_21_mixed_group_links_only_unanswered_occurrence(self):
        answered_schedule = self.add_schedule()
        unanswered_schedule = self.add_schedule()
        self.add_occurrence(answered_schedule, status="taken")
        candidates = self.list_candidates()

        create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )

        member = db.session.scalar(select(NotificationDispatchMember))
        occurrence = db.session.get(MedicationOccurrence, member.occurrence_id)
        self.assertEqual(occurrence.schedule_id, unanswered_schedule.schedule_id)
        self.assertEqual(self.count(NotificationDispatchMember), 1)

    def test_22_all_responded_stale_group_creates_no_dispatch(self):
        first = self.add_schedule()
        second = self.add_schedule()
        candidate = self.manual_candidate(first, second)
        self.add_occurrence(first, status="taken")
        self.add_occurrence(second, status="not_taken")

        result = create_scheduled_notification_dispatches(
            db.session,
            (candidate,),
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )

        self.assertEqual(result.skipped_responded_count, 2)
        self.assertEqual(self.count(NotificationDispatch), 0)

    def test_23_one_group_creates_one_dispatch_and_many_members(self):
        self.add_schedule()
        self.add_schedule()

        result = create_scheduled_notification_dispatches(
            db.session,
            self.list_candidates(),
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        dispatch = db.session.scalar(select(NotificationDispatch))

        self.assertEqual(len(result.created_dispatch_ids), 1)
        self.assertEqual(result.created_member_count, 2)
        self.assertEqual(self.count(NotificationDispatch), 1)
        self.assertEqual(self.count(NotificationDispatchMember), 2)
        self.assertEqual(dispatch.notification_type, "scheduled_occurrence")
        self.assertEqual(dispatch.delivery_channel, "email")
        self.assertEqual(dispatch.status, "pending")
        self.assertEqual(dispatch.attempt_count, 0)
        self.assertIsNone(dispatch.next_attempt_at)
        self.assertIsNone(dispatch.course_root_schedule_id)

    def test_24_repeated_service_call_is_idempotent(self):
        self.add_schedule()
        candidates = self.list_candidates()

        first = create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        second = create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 6),
        )

        self.assertEqual(len(first.created_dispatch_ids), 1)
        self.assertEqual(second.created_dispatch_ids, ())
        self.assertEqual(second.created_member_count, 0)

    def test_25_dispatch_partial_unique_prevents_duplicates(self):
        self.add_schedule()
        candidates = self.list_candidates()

        create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 6),
        )

        self.assertEqual(self.count(NotificationDispatch), 1)

    def test_26_member_primary_key_prevents_duplicates(self):
        self.add_schedule()
        candidates = self.list_candidates()

        create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 6),
        )

        self.assertEqual(self.count(NotificationDispatchMember), 1)

    def test_27_independent_sessions_converge_on_one_dispatch(self):
        self.add_schedule()
        candidates = self.list_candidates()

        with Session(self.test_engine) as first_session:
            create_scheduled_notification_dispatches(
                first_session,
                candidates,
                action_time=utc_datetime(2026, 10, 8, 1, 5),
            )
            first_session.commit()
        with Session(self.test_engine) as second_session:
            result = create_scheduled_notification_dispatches(
                second_session,
                candidates,
                action_time=utc_datetime(2026, 10, 8, 1, 6),
            )
            second_session.commit()

        db.session.expire_all()
        self.assertEqual(self.count(NotificationDispatch), 1)
        self.assertEqual(len(result.reused_dispatch_ids), 1)

    def test_28_independent_sessions_converge_on_one_member_per_occurrence(self):
        self.add_schedule()
        self.add_schedule()
        candidates = self.list_candidates()

        for minute in (5, 6):
            with Session(self.test_engine) as independent_session:
                create_scheduled_notification_dispatches(
                    independent_session,
                    candidates,
                    action_time=utc_datetime(2026, 10, 8, 1, minute),
                )
                independent_session.commit()

        db.session.expire_all()
        self.assertEqual(self.count(NotificationDispatchMember), 2)

    def test_29_existing_pending_dispatch_is_enriched(self):
        first = self.add_schedule()
        first_occurrence = self.add_occurrence(first)
        dispatch = self.add_dispatch()
        db.session.add(
            NotificationDispatchMember(
                dispatch_id=dispatch.dispatch_id,
                occurrence_id=first_occurrence.occurrence_id,
            )
        )
        db.session.commit()
        second = self.add_schedule()

        result = create_scheduled_notification_dispatches(
            db.session,
            (self.manual_candidate(first, second),),
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )

        self.assertEqual(result.created_dispatch_ids, ())
        self.assertEqual(result.reused_dispatch_ids, (dispatch.dispatch_id,))
        self.assertEqual(result.created_member_count, 1)
        self.assertEqual(self.count(NotificationDispatchMember), 2)

    def assert_non_pending_dispatch_is_untouched(self, status):
        schedule = self.add_schedule()
        dispatch = self.add_dispatch(status=status)
        original = (
            dispatch.status,
            dispatch.attempt_count,
            dispatch.claim_token,
            dispatch.claimed_at,
            dispatch.sent_at,
            dispatch.updated_at,
        )

        result = create_scheduled_notification_dispatches(
            db.session,
            (self.manual_candidate(schedule),),
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        db.session.refresh(dispatch)

        self.assertEqual(result.skipped_non_pending_count, 1)
        self.assertEqual(self.count(NotificationDispatchMember), 0)
        self.assertEqual(
            (
                dispatch.status,
                dispatch.attempt_count,
                dispatch.claim_token,
                dispatch.claimed_at,
                dispatch.sent_at,
                dispatch.updated_at,
            ),
            original,
        )

    def test_30_existing_claimed_dispatch_is_untouched(self):
        self.assert_non_pending_dispatch_is_untouched("claimed")

    def test_31_existing_sent_dispatch_is_untouched(self):
        self.assert_non_pending_dispatch_is_untouched("sent")

    def test_32_existing_failed_dispatch_is_untouched(self):
        self.assert_non_pending_dispatch_is_untouched("failed")

    def test_33_existing_canceled_dispatch_is_untouched(self):
        self.assert_non_pending_dispatch_is_untouched("canceled")

    def test_34_schedule_accounting_is_not_changed(self):
        schedule = self.add_schedule()

        create_scheduled_notification_dispatches(
            db.session,
            self.list_candidates(),
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        db.session.refresh(schedule)

        self.assertEqual(schedule.accounted_occurrence_count, 0)
        self.assertEqual(schedule.reported_doses_taken_before_tracking, 0)

    def test_35_response_race_is_rechecked_without_overwriting_response(self):
        schedule = self.add_schedule()
        stale_candidates = self.list_candidates()
        occurrence = self.add_occurrence(schedule, status="taken")
        original_updated_at = occurrence.updated_at

        result = create_scheduled_notification_dispatches(
            db.session,
            stale_candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        db.session.refresh(occurrence)

        self.assertEqual(result.skipped_responded_count, 1)
        self.assertEqual(occurrence.response_status, "taken")
        self.assertEqual(occurrence.updated_at, original_updated_at)
        self.assertEqual(self.count(NotificationDispatch), 0)

    def test_36_commit_and_rollback_are_caller_responsibilities(self):
        self.add_schedule()
        candidates = self.list_candidates()

        create_scheduled_notification_dispatches(
            db.session,
            candidates,
            action_time=utc_datetime(2026, 10, 8, 1, 5),
        )
        self.assertTrue(db.session().in_transaction())
        db.session.rollback()

        self.assertEqual(self.count(MedicationOccurrence), 0)
        self.assertEqual(self.count(NotificationDispatch), 0)
        self.assertEqual(self.count(NotificationDispatchMember), 0)

    def test_37_transaction_failure_propagates_and_can_be_rolled_back(self):
        self.add_schedule()
        candidates = self.list_candidates()

        def fail_dispatch_insert(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            if statement.lstrip().startswith("INSERT INTO notification_dispatches"):
                raise RuntimeError("simulated dispatch insert failure")

        event.listen(
            self.test_engine,
            "before_cursor_execute",
            fail_dispatch_insert,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                create_scheduled_notification_dispatches(
                    db.session,
                    candidates,
                    action_time=utc_datetime(2026, 10, 8, 1, 5),
                )
        finally:
            event.remove(
                self.test_engine,
                "before_cursor_execute",
                fail_dispatch_insert,
            )

        self.assertTrue(db.session().in_transaction())
        db.session.rollback()
        self.assertEqual(self.count(MedicationOccurrence), 0)
        self.assertEqual(self.count(NotificationDispatch), 0)

    def test_38_candidate_and_creation_select_counts_are_bounded(self):
        schedules = [self.add_schedule() for _ in range(5)]
        select_count = 0

        def count_selects(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(self.test_engine, "before_cursor_execute", count_selects)
        try:
            candidates = self.list_candidates()
        finally:
            event.remove(self.test_engine, "before_cursor_execute", count_selects)

        self.assertLessEqual(select_count, 3)

        for schedule in schedules:
            self.add_occurrence(schedule)

        select_count = 0
        event.listen(self.test_engine, "before_cursor_execute", count_selects)
        try:
            create_scheduled_notification_dispatches(
                db.session,
                candidates,
                action_time=utc_datetime(2026, 10, 8, 1, 5),
            )
        finally:
            event.remove(self.test_engine, "before_cursor_execute", count_selects)

        self.assertLessEqual(select_count, 5)

    def test_39_dst_overlap_follows_existing_occurrence_policy(self):
        self.user.timezone = "America/New_York"
        db.session.commit()
        self.add_schedule(
            medication_times=(time(1, 30),),
            start_date_value=date(2026, 11, 1),
            tracking_at=utc_datetime(2026, 11, 1, 4),
            course_days=1,
        )

        candidates = self.list_candidates(
            action_time=utc_datetime(2026, 11, 1, 5, 35),
            rollout_at=utc_datetime(2026, 11, 1, 4),
            grace=timedelta(minutes=10),
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].due_at, utc_datetime(2026, 11, 1, 5, 30))


if __name__ == "__main__":
    unittest.main()
