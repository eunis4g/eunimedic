import smtplib
import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from flask_migrate import upgrade
from sqlalchemy import create_engine, select, text, update
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
from services.email_service import send_email, send_verification_email
from services.notification_worker_service import (
    MAX_DELIVERY_ATTEMPTS,
    NotificationWorkerError,
    _claim_dispatch,
    _finalize_sent,
    _prepare_claimed_dispatch,
    process_due_scheduled_notifications,
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


class NotificationWorkerServiceTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "notification-worker.db"
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

        self.user = User(
            username="worker-user",
            email="worker@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.medicine = Medicine(
            item_seq="WORKER-MEDICINE",
            item_name="Private Medicine Name",
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

    def add_schedule(self, *local_times):
        local_times = local_times or (time(10),)
        schedule = MedicationSchedule(
            user_medicine_id=self.user_medicine.user_medicine_id,
            plan_id=self.plan.plan_id,
            intake_timing="after_meal",
            dose_amount_text="1",
            dose_unit_text="tablet",
            instructions=None,
            start_date=date(2026, 10, 8),
            end_date=None,
            course_days=5,
            reported_doses_taken_before_tracking=0,
            reminder_tracking_started_at=utc_datetime(2026, 10, 7, 15),
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
            created_at=utc_datetime(2026, 10, 7, 15),
            updated_at=utc_datetime(2026, 10, 7, 15),
        )
        schedule.times.extend(
            MedicationTime(time_of_day=value) for value in local_times
        )
        db.session.add(schedule)
        db.session.commit()
        return schedule

    def add_notification(
        self,
        schedule,
        *,
        due_at=utc_datetime(2026, 10, 8, 1),
        response_status=None,
        next_attempt_at=None,
        attempt_count=0,
        notification_type="scheduled_occurrence",
    ):
        occurrence = MedicationOccurrence(
            schedule_id=schedule.schedule_id,
            scheduled_for=due_at,
            response_status=response_status,
            responded_at=(
                utc_datetime(2026, 10, 8, 1, 2)
                if response_status is not None
                else None
            ),
            created_at=utc_datetime(2026, 10, 8, 1),
            updated_at=utc_datetime(2026, 10, 8, 1),
        )
        dispatch = NotificationDispatch(
            user_id=self.user.user_id,
            plan_id=(
                self.plan.plan_id
                if notification_type == "scheduled_occurrence"
                else None
            ),
            course_root_schedule_id=(
                schedule.schedule_id
                if notification_type == "unanswered_review"
                else None
            ),
            notification_type=notification_type,
            delivery_channel="email",
            due_at=due_at,
            status="pending",
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            created_at=utc_datetime(2026, 10, 8, 1),
            updated_at=utc_datetime(2026, 10, 8, 1),
        )
        dispatch.members.append(
            NotificationDispatchMember(occurrence=occurrence)
        )
        db.session.add(dispatch)
        db.session.commit()
        return dispatch, occurrence

    def run_worker(self, sender, **overrides):
        arguments = {
            "action_time": utc_datetime(2026, 10, 8, 1, 5),
            "batch_size": 100,
            "sender": sender,
            "token_factory": lambda: "deterministic-claim-token",
            "clock": lambda: utc_datetime(2026, 10, 8, 1, 6),
        }
        arguments.update(overrides)
        return process_due_scheduled_notifications(db.session, **arguments)

    def test_due_dispatch_is_sent_outside_a_database_transaction(self):
        schedule = self.add_schedule()
        dispatch, occurrence = self.add_notification(schedule)
        calls = []

        def sender(**message):
            self.assertFalse(db.session().in_transaction())
            calls.append(message)
            return "provider-message-id"

        summary = self.run_worker(sender)
        db.session.refresh(dispatch)
        db.session.refresh(occurrence)

        self.assertEqual(summary.claimed_count, 1)
        self.assertEqual(summary.sent_count, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["to_email"], self.user.email)
        self.assertEqual(calls[0]["subject"], "복약 일정 알림")
        self.assertIn("10:00", calls[0]["body"])
        self.assertNotIn(self.user.username, calls[0]["body"])
        self.assertNotIn(self.medicine.item_name, calls[0]["body"])
        self.assertEqual(
            calls[0]["message_id"],
            f"<medicine-web-notification-{dispatch.dispatch_id}"
            "@medicine-web.local>",
        )
        self.assertEqual(dispatch.status, "sent")
        self.assertEqual(dispatch.attempt_count, 1)
        self.assertIsNone(dispatch.claim_token)
        self.assertIsNone(dispatch.claimed_at)
        self.assertEqual(dispatch.provider_message_id, "provider-message-id")
        self.assertEqual(
            stored_as_utc(dispatch.sent_at),
            utc_datetime(2026, 10, 8, 1, 6),
        )
        self.assertIsNone(occurrence.response_status)
        self.assertEqual(schedule.accounted_occurrence_count, 0)

    def test_future_due_and_future_retry_are_not_claimed(self):
        schedule = self.add_schedule(time(10), time(11))
        future_dispatch, _ = self.add_notification(
            schedule,
            due_at=utc_datetime(2026, 10, 8, 2),
        )
        retry_dispatch, _ = self.add_notification(
            schedule,
            next_attempt_at=utc_datetime(2026, 10, 8, 1, 10),
            attempt_count=1,
        )

        summary = self.run_worker(lambda **message: self.fail("unexpected"))

        self.assertEqual(summary.claimed_count, 0)
        self.assertEqual(future_dispatch.status, "pending")
        self.assertEqual(retry_dispatch.status, "pending")

    def test_unanswered_review_is_not_processed(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(
            schedule,
            notification_type="unanswered_review",
        )

        summary = self.run_worker(lambda **message: self.fail("unexpected"))

        self.assertEqual(summary.claimed_count, 0)
        self.assertEqual(dispatch.status, "pending")

    def test_non_email_dispatch_is_not_processed(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(schedule)
        db.session.execute(text("PRAGMA ignore_check_constraints=ON"))
        db.session.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.dispatch_id == dispatch.dispatch_id
            )
            .values(delivery_channel="web_push")
            .execution_options(synchronize_session=False)
        )
        db.session.commit()

        try:
            summary = self.run_worker(
                lambda **message: self.fail("unexpected")
            )
        finally:
            db.session.execute(text("PRAGMA ignore_check_constraints=OFF"))
            db.session.commit()

        db.session.refresh(dispatch)
        self.assertEqual(summary.claimed_count, 0)
        self.assertEqual(dispatch.status, "pending")

    def test_batch_is_bounded_and_ordered_by_due_time(self):
        schedule = self.add_schedule(time(9), time(10), time(11))
        dispatches = [
            self.add_notification(schedule, due_at=due_at)[0]
            for due_at in (
                utc_datetime(2026, 10, 8, 2),
                utc_datetime(2026, 10, 8, 0),
                utc_datetime(2026, 10, 8, 1),
            )
        ]
        sent_message_ids = []

        summary = self.run_worker(
            lambda **message: sent_message_ids.append(message["message_id"]),
            action_time=utc_datetime(2026, 10, 8, 3),
            batch_size=2,
        )

        self.assertEqual(summary.sent_count, 2)
        self.assertEqual(
            sent_message_ids,
            [
                f"<medicine-web-notification-{dispatches[1].dispatch_id}"
                "@medicine-web.local>",
                f"<medicine-web-notification-{dispatches[2].dispatch_id}"
                "@medicine-web.local>",
            ],
        )
        db.session.refresh(dispatches[0])
        self.assertEqual(dispatches[0].status, "pending")

    def test_only_one_session_can_claim_the_same_dispatch(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(schedule)
        claim_time = utc_datetime(2026, 10, 8, 1, 5)

        with Session(self.test_engine) as first_session:
            self.assertTrue(
                _claim_dispatch(
                    first_session,
                    dispatch_id=dispatch.dispatch_id,
                    claim_token="first-token",
                    claim_time=claim_time,
                )
            )

        with Session(self.test_engine) as second_session:
            self.assertFalse(
                _claim_dispatch(
                    second_session,
                    dispatch_id=dispatch.dispatch_id,
                    claim_token="second-token",
                    claim_time=claim_time,
                )
            )

        db.session.expire_all()
        claimed = db.session.get(NotificationDispatch, dispatch.dispatch_id)
        self.assertEqual(claimed.status, "claimed")
        self.assertEqual(claimed.claim_token, "first-token")
        self.assertEqual(stored_as_utc(claimed.claimed_at), claim_time)

    def test_responded_member_is_pruned_and_unanswered_member_is_sent(self):
        first_schedule = self.add_schedule()
        second_schedule = self.add_schedule()
        dispatch, answered = self.add_notification(
            first_schedule,
            response_status="taken",
        )
        unanswered = MedicationOccurrence(
            schedule_id=second_schedule.schedule_id,
            scheduled_for=utc_datetime(2026, 10, 8, 1),
            created_at=utc_datetime(2026, 10, 8, 1),
            updated_at=utc_datetime(2026, 10, 8, 1),
        )
        dispatch.members.append(
            NotificationDispatchMember(occurrence=unanswered)
        )
        db.session.commit()

        summary = self.run_worker(lambda **message: message["message_id"])
        member_ids = set(
            db.session.scalars(
                select(NotificationDispatchMember.occurrence_id).where(
                    NotificationDispatchMember.dispatch_id
                    == dispatch.dispatch_id
                )
            ).all()
        )

        self.assertEqual(summary.sent_count, 1)
        self.assertEqual(member_ids, {unanswered.occurrence_id})
        self.assertNotIn(answered.occurrence_id, member_ids)

    def test_not_taken_only_group_is_canceled_without_smtp(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(
            schedule,
            response_status="not_taken",
        )

        summary = self.run_worker(lambda **message: self.fail("unexpected"))
        db.session.refresh(dispatch)

        self.assertEqual(summary.canceled_count, 1)
        self.assertEqual(dispatch.status, "canceled")
        self.assertEqual(dispatch.attempt_count, 0)
        self.assertIsNone(dispatch.sent_at)
        self.assertIsNone(dispatch.claim_token)
        self.assertIsNone(dispatch.next_attempt_at)
        self.assertEqual(dispatch.members, [])

    def test_schedule_change_invalidates_member_and_cancels_dispatch(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(schedule)
        schedule.times[0].time_of_day = time(11)
        db.session.commit()

        summary = self.run_worker(lambda **message: self.fail("unexpected"))
        db.session.refresh(dispatch)

        self.assertEqual(summary.canceled_count, 1)
        self.assertEqual(dispatch.status, "canceled")
        self.assertEqual(dispatch.members, [])

    def test_temporary_failure_returns_dispatch_to_pending(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(schedule)

        def timeout_sender(**message):
            raise TimeoutError("contains private diagnostic data")

        summary = self.run_worker(timeout_sender)
        db.session.refresh(dispatch)

        self.assertEqual(summary.retry_count, 1)
        self.assertEqual(dispatch.status, "pending")
        self.assertEqual(dispatch.attempt_count, 1)
        self.assertEqual(dispatch.last_error_code, "smtp_timeout")
        self.assertNotIn("private", dispatch.last_error_code)
        self.assertEqual(
            stored_as_utc(dispatch.next_attempt_at),
            utc_datetime(2026, 10, 8, 1, 7),
        )
        self.assertIsNone(dispatch.claim_token)

    def test_permanent_recipient_failure_is_failed_immediately(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(schedule)

        def rejected_sender(**message):
            raise smtplib.SMTPRecipientsRefused(
                {message["to_email"]: (550, b"private server response")}
            )

        summary = self.run_worker(rejected_sender)
        db.session.refresh(dispatch)

        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(dispatch.status, "failed")
        self.assertEqual(dispatch.attempt_count, 1)
        self.assertEqual(
            dispatch.last_error_code,
            "smtp_recipient_rejected",
        )
        self.assertIsNone(dispatch.next_attempt_at)
        self.assertIsNone(dispatch.claim_token)

    def test_last_temporary_failure_reaches_max_attempts(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(
            schedule,
            attempt_count=MAX_DELIVERY_ATTEMPTS - 1,
        )

        summary = self.run_worker(
            lambda **message: (_ for _ in ()).throw(OSError("network"))
        )
        db.session.refresh(dispatch)

        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(dispatch.status, "failed")
        self.assertEqual(dispatch.attempt_count, MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(dispatch.last_error_code, "smtp_connection_error")

    def test_stale_finalize_cannot_overwrite_a_different_claim(self):
        schedule = self.add_schedule()
        dispatch, _ = self.add_notification(schedule)
        claim_time = utc_datetime(2026, 10, 8, 1, 5)
        self.assertTrue(
            _claim_dispatch(
                db.session,
                dispatch_id=dispatch.dispatch_id,
                claim_token="original-token",
                claim_time=claim_time,
            )
        )
        preparation = _prepare_claimed_dispatch(
            db.session,
            dispatch_id=dispatch.dispatch_id,
            claim_token="original-token",
            action_time=claim_time,
        )

        with Session(self.test_engine) as other_session:
            other_session.execute(
                update(NotificationDispatch)
                .where(
                    NotificationDispatch.dispatch_id == dispatch.dispatch_id
                )
                .values(claim_token="new-owner-token")
            )
            other_session.commit()

        finalized = _finalize_sent(
            db.session,
            delivery=preparation.delivery,
            finalize_time=utc_datetime(2026, 10, 8, 1, 6),
            provider_message_id="provider-id",
        )
        db.session.expire_all()
        current = db.session.get(NotificationDispatch, dispatch.dispatch_id)

        self.assertFalse(finalized)
        self.assertEqual(current.status, "claimed")
        self.assertEqual(current.claim_token, "new-owner-token")
        self.assertIsNone(current.sent_at)

    def test_naive_action_time_and_invalid_batch_are_rejected(self):
        with self.assertRaises(NotificationWorkerError):
            process_due_scheduled_notifications(
                db.session,
                action_time=datetime(2026, 10, 8, 1, 5),
            )

        with self.assertRaises(NotificationWorkerError):
            process_due_scheduled_notifications(
                db.session,
                action_time=utc_datetime(2026, 10, 8, 1, 5),
                batch_size=0,
            )


class EmailServiceGeneralizationTest(unittest.TestCase):

    def smtp_environment(self):
        return {
            "MAIL_SERVER": "smtp.example.com",
            "MAIL_PORT": "587",
            "MAIL_USERNAME": "mailer",
            "MAIL_PASSWORD": "secret",
            "MAIL_FROM": "no-reply@example.com",
            "MAIL_USE_TLS": "true",
            "MAIL_USE_SSL": "false",
        }

    def test_generic_sender_and_verification_use_the_same_smtp_path(self):
        messages = []

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                self.logged_in = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self, **kwargs):
                return None

            def login(self, username, password):
                self.logged_in = True

            def send_message(self, message):
                messages.append(message)

        with patch.dict("os.environ", self.smtp_environment(), clear=True):
            with patch("services.email_service.smtplib.SMTP", FakeSMTP):
                with patch(
                    "services.email_service.ssl.create_default_context",
                    return_value=object(),
                ):
                    provider_id = send_email(
                        "recipient@example.com",
                        "subject",
                        "body",
                        message_id="<stable@example.com>",
                    )
                    verification_result = send_verification_email(
                        "verify@example.com",
                        "123456",
                    )

        self.assertEqual(provider_id, "<stable@example.com>")
        self.assertIsNone(verification_result)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["Message-ID"], "<stable@example.com>")
        self.assertEqual(messages[1]["To"], "verify@example.com")
        self.assertEqual(
            messages[1]["Subject"],
            "Medicine Web 이메일 인증번호",
        )
        self.assertIn("인증번호: 123456", messages[1].get_content())
        self.assertIsNone(messages[1]["Message-ID"])


if __name__ == "__main__":
    unittest.main()
