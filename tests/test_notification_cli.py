import os
import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from flask_migrate import upgrade
from sqlalchemy import create_engine, func, select, update

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
from notification_cli import (
    NotificationCliSettings,
    NotificationCliStageError,
    load_notification_cli_settings,
    run_scheduled_notification_pipeline,
)
from services.notification_service import ScheduledDispatchCreationResult
from services.notification_worker_service import (
    ScheduledNotificationDeliverySummary,
    StaleNotificationClaimRecoveryResult,
)


MIGRATION_HEAD = "d9e7b4c2a1f6"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
COMMAND_NAME = "process-medication-notifications"


def utc_datetime(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class NotificationCliTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "notification-cli.db"
        )
        self.application_context = app_module.app.app_context()
        self.application_context.push()
        db.session.remove()
        self.original_engine = db.engines[None]
        self.test_engine = create_engine(
            "sqlite:///" + self.database_path.as_posix()
        )
        db.engines[None] = self.test_engine
        upgrade(directory=MIGRATIONS_DIRECTORY, revision=MIGRATION_HEAD)
        self.runner = app_module.app.test_cli_runner()

        self.user = User(
            username="cli-user",
            email="cli-user@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.medicine = Medicine(
            item_seq="CLI-MEDICINE",
            item_name="Private CLI Medicine",
        )
        self.user_medicine = UserMedicine(
            user=self.user,
            medicine=self.medicine,
            registration_source="search",
            is_active=True,
        )
        self.plan = MedicationPlan(user=self.user, is_active=False)
        db.session.add_all(
            [self.user, self.medicine, self.user_medicine, self.plan]
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def environment(self, **overrides):
        values = {
            "NOTIFICATION_ROLLOUT_AT": "2026-10-08T00:00:00+00:00",
            "NOTIFICATION_SCHEDULED_GRACE_MINUTES": "15",
            "NOTIFICATION_CLAIM_TIMEOUT_MINUTES": "10",
            "NOTIFICATION_BATCH_SIZE": "50",
        }
        values.update(overrides)
        return values

    def invoke(self, *, now=None, environment=None, sender=None):
        now = now or utc_datetime(2026, 10, 8, 1, 5)
        environment = environment or self.environment()

        with patch.dict(os.environ, environment, clear=True):
            with patch("notification_cli.utc_now", return_value=now) as clock:
                with patch(
                    "services.notification_worker_service.send_email"
                ) as mocked_sender:
                    if sender is not None:
                        mocked_sender.side_effect = sender
                    else:
                        mocked_sender.return_value = "provider-message-id"

                    result = self.runner.invoke(args=[COMMAND_NAME])

        return result, clock, mocked_sender

    def add_schedule(
        self,
        *,
        start_date_value=date(2026, 10, 8),
        course_days=5,
        local_time=time(10),
        tracking_at=utc_datetime(2026, 10, 7, 15),
    ):
        schedule = MedicationSchedule(
            user_medicine_id=self.user_medicine.user_medicine_id,
            plan_id=self.plan.plan_id,
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
            closed_at=None,
            created_at=tracking_at,
            updated_at=tracking_at,
        )
        schedule.times.append(MedicationTime(time_of_day=local_time))
        db.session.add(schedule)
        db.session.commit()
        return schedule

    def add_dispatch(
        self,
        schedule,
        *,
        status="pending",
        response_status=None,
        claimed_at=None,
    ):
        due_at = utc_datetime(2026, 10, 8, 1)
        occurrence = MedicationOccurrence(
            schedule_id=schedule.schedule_id,
            scheduled_for=due_at,
            response_status=response_status,
            responded_at=(
                utc_datetime(2026, 10, 8, 1, 1)
                if response_status is not None
                else None
            ),
            created_at=due_at,
            updated_at=due_at,
        )
        dispatch = NotificationDispatch(
            user_id=self.user.user_id,
            plan_id=self.plan.plan_id,
            notification_type="scheduled_occurrence",
            delivery_channel="email",
            due_at=due_at,
            status=status,
            attempt_count=0,
            claim_token=("stale-token" if status == "claimed" else None),
            claimed_at=claimed_at if status == "claimed" else None,
            created_at=due_at,
            updated_at=due_at,
        )
        dispatch.members.append(
            NotificationDispatchMember(occurrence=occurrence)
        )
        db.session.add(dispatch)
        db.session.commit()
        return dispatch, occurrence

    def count(self, model):
        return db.session.scalar(select(func.count()).select_from(model))

    def test_command_is_registered_without_dry_run_option(self):
        result = self.runner.invoke(args=[COMMAND_NAME, "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Run the scheduled notification pipeline once", result.output)
        self.assertNotIn("--dry-run", result.output)

    def test_successful_empty_run_uses_defaults_and_prints_counts(self):
        environment = {
            "NOTIFICATION_ROLLOUT_AT": "2026-10-08T00:00:00Z",
        }

        result, clock, sender = self.invoke(environment=environment)

        self.assertEqual(result.exit_code, 0)
        clock.assert_called_once_with()
        sender.assert_not_called()
        self.assertIn("Recovered stale claims: 0", result.output)
        self.assertIn("Created dispatches: 0", result.output)
        self.assertIn("Sent: 0", result.output)

    def test_missing_rollout_fails_before_clock_or_database_work(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("notification_cli.utc_now") as clock:
                with patch(
                    "notification_cli.run_scheduled_notification_pipeline"
                ) as pipeline:
                    result = self.runner.invoke(args=[COMMAND_NAME])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("NOTIFICATION_ROLLOUT_AT is required", result.output)
        clock.assert_not_called()
        pipeline.assert_not_called()

    def test_invalid_rollout_values_are_rejected(self):
        for value in ("not-a-date", "2026-10-08T00:00:00"):
            with self.subTest(value=value):
                result, clock, sender = self.invoke(
                    environment=self.environment(
                        NOTIFICATION_ROLLOUT_AT=value
                    )
                )
                self.assertNotEqual(result.exit_code, 0)
                clock.assert_not_called()
                sender.assert_not_called()

    def test_invalid_integer_settings_are_rejected_before_db_work(self):
        invalid_settings = (
            ("NOTIFICATION_SCHEDULED_GRACE_MINUTES", "-1"),
            ("NOTIFICATION_SCHEDULED_GRACE_MINUTES", "invalid"),
            ("NOTIFICATION_SCHEDULED_GRACE_MINUTES", "999999999999999999"),
            ("NOTIFICATION_CLAIM_TIMEOUT_MINUTES", "0"),
            ("NOTIFICATION_CLAIM_TIMEOUT_MINUTES", "-1"),
            ("NOTIFICATION_BATCH_SIZE", "0"),
            ("NOTIFICATION_BATCH_SIZE", "invalid"),
        )

        for name, value in invalid_settings:
            with self.subTest(name=name, value=value):
                result, clock, sender = self.invoke(
                    environment=self.environment(**{name: value})
                )
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn(name, result.output)
                clock.assert_not_called()
                sender.assert_not_called()

    def test_settings_are_normalized_and_defaults_are_bounded(self):
        with patch.dict(
            os.environ,
            {"NOTIFICATION_ROLLOUT_AT": "2026-10-08T09:00:00+09:00"},
            clear=True,
        ):
            settings = load_notification_cli_settings()

        self.assertEqual(
            settings.rollout_at,
            utc_datetime(2026, 10, 8),
        )
        self.assertEqual(settings.scheduled_grace, timedelta(minutes=15))
        self.assertEqual(settings.claim_timeout, timedelta(minutes=10))
        self.assertEqual(settings.batch_size, 50)

    def test_pipeline_order_action_time_and_stage_commits(self):
        calls = []

        class FakeSession:
            def commit(self):
                calls.append("commit")

            def rollback(self):
                calls.append("rollback")

        action_time = utc_datetime(2026, 10, 8, 1, 5)
        settings = NotificationCliSettings(
            rollout_at=utc_datetime(2026, 10, 8),
            scheduled_grace=timedelta(minutes=15),
            claim_timeout=timedelta(minutes=10),
            batch_size=50,
        )
        recovery = StaleNotificationClaimRecoveryResult(recovered_count=2)
        creation = ScheduledDispatchCreationResult(
            created_dispatch_ids=(1, 2),
            reused_dispatch_ids=(3,),
            created_member_count=4,
            materialized_occurrence_count=4,
            skipped_responded_count=0,
            skipped_invalid_count=0,
            skipped_non_pending_count=0,
        )
        delivery = ScheduledNotificationDeliverySummary(
            claimed_count=3,
            sent_count=2,
            canceled_count=1,
            retry_count=0,
            failed_count=0,
            skipped_count=0,
        )

        def recover(session, **kwargs):
            calls.append(("recovery", kwargs))
            return recovery

        def list_candidates(session, **kwargs):
            calls.append(("candidates", kwargs))
            return ("candidate",)

        def create(session, candidates, **kwargs):
            calls.append(("creation", candidates, kwargs))
            return creation

        def deliver(session, **kwargs):
            calls.append(("worker", kwargs))
            return delivery

        with patch(
            "notification_cli.recover_stale_notification_claims",
            side_effect=recover,
        ):
            with patch(
                "notification_cli.list_scheduled_notification_candidates",
                side_effect=list_candidates,
            ):
                with patch(
                    "notification_cli.create_scheduled_notification_dispatches",
                    side_effect=create,
                ):
                    with patch(
                        "notification_cli.process_due_scheduled_notifications",
                        side_effect=deliver,
                    ):
                        result = run_scheduled_notification_pipeline(
                            FakeSession(),
                            action_time=action_time,
                            settings=settings,
                        )

        self.assertEqual(
            [value if isinstance(value, str) else value[0] for value in calls],
            [
                "recovery",
                "commit",
                "candidates",
                "creation",
                "commit",
                "worker",
            ],
        )
        self.assertEqual(result.recovery, recovery)
        self.assertEqual(result.creation, creation)
        self.assertEqual(result.delivery, delivery)
        self.assertEqual(result.candidate_count, 1)
        for value in calls:
            if isinstance(value, tuple) and value[0] != "creation":
                self.assertEqual(value[1]["action_time"], action_time)
        self.assertEqual(calls[3][2]["action_time"], action_time)

    def test_candidate_stage_failure_rolls_back_after_recovery_commit(self):
        calls = []

        class FakeSession:
            def commit(self):
                calls.append("commit")

            def rollback(self):
                calls.append("rollback")

        settings = NotificationCliSettings(
            rollout_at=utc_datetime(2026, 10, 8),
            scheduled_grace=timedelta(minutes=15),
            claim_timeout=timedelta(minutes=10),
            batch_size=50,
        )

        with patch(
            "notification_cli.recover_stale_notification_claims",
            return_value=StaleNotificationClaimRecoveryResult(1),
        ):
            with patch(
                "notification_cli.list_scheduled_notification_candidates",
                side_effect=RuntimeError("database unavailable"),
            ):
                with self.assertRaises(NotificationCliStageError):
                    run_scheduled_notification_pipeline(
                        FakeSession(),
                        action_time=utc_datetime(2026, 10, 8, 1),
                        settings=settings,
                    )

        self.assertEqual(calls, ["commit", "rollback"])

    def test_system_level_failure_returns_nonzero_without_details(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            with patch("notification_cli.utc_now", return_value=utc_datetime(2026, 10, 8, 1)):
                with patch(
                    "notification_cli.run_scheduled_notification_pipeline",
                    side_effect=NotificationCliStageError(
                        "Scheduled notification delivery failed."
                    ),
                ):
                    result = self.runner.invoke(args=[COMMAND_NAME])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Scheduled notification delivery failed", result.output)
        self.assertNotIn(self.user.email, result.output)

    def test_exact_action_time_is_sent_once_across_repeated_runs(self):
        self.add_schedule()

        first, first_clock, sender = self.invoke(
            now=utc_datetime(2026, 10, 8, 1)
        )
        second, second_clock, second_sender = self.invoke(
            now=utc_datetime(2026, 10, 8, 1)
        )

        self.assertEqual(first.exit_code, 0)
        self.assertEqual(second.exit_code, 0)
        first_clock.assert_called_once_with()
        second_clock.assert_called_once_with()
        sender.assert_called_once()
        second_sender.assert_not_called()
        self.assertIn("Scheduled candidates: 1", first.output)
        self.assertIn("Sent: 1", first.output)
        self.assertIn("Sent: 0", second.output)
        self.assertEqual(self.count(NotificationDispatch), 1)
        self.assertEqual(self.count(NotificationDispatchMember), 1)
        self.assertEqual(self.count(MedicationOccurrence), 1)

    def test_rollout_excludes_older_occurrences_even_with_large_grace(self):
        self.add_schedule(
            start_date_value=date(2026, 10, 7),
            course_days=1,
            tracking_at=utc_datetime(2026, 10, 6, 15),
        )
        environment = self.environment(
            NOTIFICATION_SCHEDULED_GRACE_MINUTES="2880"
        )

        result, _, sender = self.invoke(
            now=utc_datetime(2026, 10, 8, 1),
            environment=environment,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Scheduled candidates: 0", result.output)
        sender.assert_not_called()
        self.assertEqual(self.count(NotificationDispatch), 0)
        self.assertEqual(self.count(MedicationOccurrence), 0)

    def test_stale_claim_is_recovered_then_reclaimed_and_sent(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_dispatch(
            schedule,
            status="claimed",
            claimed_at=utc_datetime(2026, 10, 8),
        )

        result, _, sender = self.invoke()
        db.session.refresh(dispatch)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Recovered stale claims: 1", result.output)
        self.assertIn("Sent: 1", result.output)
        sender.assert_called_once()
        self.assertEqual(dispatch.status, "sent")
        self.assertIsNone(dispatch.claim_token)

    def test_pending_dispatch_with_responded_member_is_canceled(self):
        schedule = self.add_schedule()
        dispatch, occurrence = self.add_dispatch(
            schedule,
            response_status="taken",
        )

        result, _, sender = self.invoke()
        db.session.refresh(dispatch)
        db.session.refresh(occurrence)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Canceled: 1", result.output)
        sender.assert_not_called()
        self.assertEqual(dispatch.status, "canceled")
        self.assertEqual(occurrence.response_status, "taken")

    def test_individual_smtp_failure_is_recorded_with_zero_exit_code(self):
        self.add_schedule()

        def fail_sender(**message):
            raise TimeoutError("private transport detail")

        result, _, sender = self.invoke(sender=fail_sender)
        dispatch = db.session.scalar(select(NotificationDispatch))

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Retried: 1", result.output)
        sender.assert_called_once()
        self.assertEqual(dispatch.status, "pending")
        self.assertEqual(dispatch.last_error_code, "smtp_timeout")
        self.assertNotIn("private transport detail", result.output)

    def test_summary_contains_no_email_or_medicine_name(self):
        self.add_schedule()

        result, _, _ = self.invoke()

        self.assertEqual(result.exit_code, 0)
        self.assertNotIn(self.user.email, result.output)
        self.assertNotIn(self.medicine.item_name, result.output)


if __name__ == "__main__":
    unittest.main()
