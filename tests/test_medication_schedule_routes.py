import re
import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flask import g
from sqlalchemy import create_engine, event, inspect as sqlalchemy_inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import app as app_module
from models import (
    Medicine,
    MedicationPlan,
    MedicationPlanTime,
    MedicationSchedule,
    MedicationTime,
    User,
    UserMedicine,
    db,
    utc_now,
)
from services.medication_schedule_service import (
    generate_future_occurrences,
)


EDIT_TRACKING_TIME = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
FIRST_EDIT_TIME = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
SECOND_EDIT_TIME = datetime(2026, 10, 6, 23, 0, tzinfo=UTC)
REMOVE_TIME = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)


class MedicationScheduleRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = (
            Path(cls.temporary_directory.name) / "schedule-route-test.db"
        )
        cls.application = app_module.app
        cls.application.config.update(TESTING=True)
        with cls.application.app_context():
            db.session.remove()
            cls.original_engine = db.engines[None]
            cls.test_engine = create_engine(
                "sqlite:///" + cls.database_path.as_posix()
            )
            db.engines[None] = cls.test_engine

    @classmethod
    def tearDownClass(cls):
        with cls.application.app_context():
            db.session.remove()
            db.drop_all()
            db.session.remove()
            db.engines[None] = cls.original_engine
        cls.test_engine.dispose()
        cls.temporary_directory.cleanup()

    def setUp(self):
        self.application_context = self.application.app_context()
        self.application_context.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = self.application.test_client()

        self.user = User(
            username="schedule-user",
            email="schedule@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.other_user = User(
            username="other-user",
            email="other@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.medicine = Medicine(
            item_seq="MED-001",
            item_name="테스트 약",
            entp_name="테스트 제약",
        )
        other_medicine = Medicine(
            item_seq="MED-002",
            item_name="다른 테스트 약",
        )
        inactive_medicine = Medicine(
            item_seq="MED-003",
            item_name="비활성 테스트 약",
        )
        self.user_medicine = UserMedicine(
            user=self.user,
            medicine=self.medicine,
            registration_source="search",
            is_active=True,
        )
        self.other_user_medicine = UserMedicine(
            user=self.other_user,
            medicine=other_medicine,
            registration_source="search",
            is_active=True,
        )
        self.inactive_user_medicine = UserMedicine(
            user=self.user,
            medicine=inactive_medicine,
            registration_source="search",
            is_active=False,
        )
        db.session.add_all(
            [
                self.user,
                self.other_user,
                self.medicine,
                other_medicine,
                inactive_medicine,
                self.user_medicine,
                self.other_user_medicine,
                self.inactive_user_medicine,
            ]
        )
        db.session.commit()
        self.user_id = self.user.user_id
        self.user_medicine_id = self.user_medicine.user_medicine_id
        self.other_user_medicine_id = (
            self.other_user_medicine.user_medicine_id
        )
        self.inactive_user_medicine_id = (
            self.inactive_user_medicine.user_medicine_id
        )

    def tearDown(self):
        db.session.remove()
        self.application_context.pop()

    @property
    def schedule_url(self):
        return (
            f"/my-medicines/{self.user_medicine_id}/schedule/new"
        )

    def edit_url(self, schedule_id):
        return f"/medication-schedules/{schedule_id}/edit"

    def restart_url(self, schedule_id):
        return f"/medication-schedules/{schedule_id}/restart"

    def history_url(self, user_medicine_id=None):
        return (
            "/my-medicines/"
            f"{user_medicine_id or self.user_medicine_id}/history"
        )

    def deactivate_url(self, user_medicine_id=None):
        return (
            "/my-medicines/"
            f"{user_medicine_id or self.user_medicine_id}/deactivate"
        )

    def post_deactivate(self, user_medicine_id=None, *, token=None):
        if token is None:
            token = self.get_csrf_token("/my-medicines")

        return self.client.post(
            self.deactivate_url(user_medicine_id),
            data={"csrf_token": token},
        )

    def log_in(self):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True

        g.pop("_login_user", None)

    def get_csrf_token(self, url=None):
        response = self.client.get(url or self.schedule_url)
        match = re.search(
            rb'name="csrf_token"\s+value="([^"]+)"',
            response.data,
        )
        self.assertIsNotNone(match)
        return match.group(1).decode("utf-8")

    def valid_form_data(self, **overrides):
        form_data = {
            "dose_amount_text": "1",
            "dose_unit_text": "정",
            "intake_timing": "after_meal",
            "daily_frequency": "1",
            "medication_times": ["09:00"],
            "start_date": "2099-01-01",
            "course_days": "3",
            "reported_doses_taken_before_tracking": "0",
        }
        form_data.update(overrides)
        return form_data

    def post_schedule(self, form_data=None, *, token=None):
        if token is None:
            token = self.get_csrf_token()

        payload = form_data or self.valid_form_data()
        payload["csrf_token"] = token
        return self.client.post(self.schedule_url, data=payload)

    def add_schedule(
        self,
        *,
        user_medicine_id=None,
        start_date_value=date(2099, 1, 1),
        tracking_started_at=None,
        course_days=3,
        medication_times=(time(9, 0),),
        reported=0,
        accounted=0,
        active=True,
        dose_amount_text="1",
        dose_unit_text="정",
        intake_timing="after_meal",
        created_at=None,
    ):
        schedule = MedicationSchedule(
            user_medicine_id=(
                user_medicine_id or self.user_medicine_id
            ),
            intake_timing=intake_timing,
            dose_amount_text=dose_amount_text,
            dose_unit_text=dose_unit_text,
            instructions=None,
            start_date=start_date_value,
            end_date=None,
            course_days=course_days,
            reported_doses_taken_before_tracking=reported,
            reminder_tracking_started_at=(
                tracking_started_at or utc_now()
            ),
            accounted_occurrence_count=accounted,
            monday=True,
            tuesday=True,
            wednesday=True,
            thursday=True,
            friday=True,
            saturday=True,
            sunday=True,
            is_active=active,
        )
        schedule.times.extend(
            MedicationTime(time_of_day=value)
            for value in medication_times
        )
        if created_at is not None:
            schedule.created_at = created_at
            schedule.updated_at = created_at
        db.session.add(schedule)
        db.session.commit()
        return schedule

    def valid_edit_form_data(self, schedule, **overrides):
        form_data = {
            "dose_amount_text": schedule.dose_amount_text,
            "dose_unit_text": schedule.dose_unit_text,
            "intake_timing": schedule.intake_timing,
            "daily_frequency": str(len(schedule.times)),
            "medication_times": [
                value.time_of_day.strftime("%H:%M")
                for value in schedule.times
            ],
            "start_date": schedule.start_date.isoformat(),
            "course_days": str(schedule.course_days),
            "reported_doses_taken_before_tracking": str(
                schedule.reported_doses_taken_before_tracking
            ),
            "schedule_version": (
                app_module.serialize_schedule_version(
                    schedule.updated_at
                )
            ),
        }
        form_data.update(overrides)
        return form_data

    def post_edit(self, schedule, form_data=None, *, token=None):
        if token is None:
            token = self.get_csrf_token("/my-medicines")

        payload = form_data or self.valid_edit_form_data(schedule)
        payload["csrf_token"] = token
        return self.client.post(
            self.edit_url(schedule.schedule_id),
            data=payload,
        )

    def post_restart(self, schedule, form_data=None, *, token=None):
        restart_url = self.restart_url(schedule.schedule_id)

        if token is None:
            token = self.get_csrf_token(restart_url)

        payload = form_data or self.valid_form_data()
        payload["csrf_token"] = token
        return self.client.post(restart_url, data=payload)

    def test_login_is_required_for_get_and_post(self):
        get_response = self.client.get(self.schedule_url)
        self.assertEqual(get_response.status_code, 302)
        self.assertIn("/login", get_response.headers["Location"])

        login_page = self.client.get("/login")
        token_match = re.search(
            rb'name="csrf_token"\s+value="([^"]+)"',
            login_page.data,
        )
        self.assertIsNotNone(token_match)
        form_data = self.valid_form_data()
        form_data["csrf_token"] = token_match.group(1).decode("utf-8")
        post_response = self.client.post(
            self.schedule_url,
            data=form_data,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertIn("/login", post_response.headers["Location"])

    def test_ownership_and_inactive_user_medicine_are_hidden(self):
        self.log_in()
        token = self.get_csrf_token()

        for user_medicine_id in (
            self.other_user_medicine_id,
            self.inactive_user_medicine_id,
            999_999,
        ):
            with self.subTest(
                method="GET",
                user_medicine_id=user_medicine_id,
            ):
                response = self.client.get(
                    f"/my-medicines/{user_medicine_id}/schedule/new"
                )
                self.assertEqual(response.status_code, 404)

            with self.subTest(
                method="POST",
                user_medicine_id=user_medicine_id,
            ):
                form_data = self.valid_form_data()
                form_data["csrf_token"] = token
                response = self.client.post(
                    f"/my-medicines/{user_medicine_id}/schedule/new",
                    data=form_data,
                )
                self.assertEqual(response.status_code, 404)

    def test_post_without_csrf_is_rejected(self):
        self.log_in()
        response = self.client.post(
            self.schedule_url,
            data=self.valid_form_data(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            0,
        )

    def test_valid_schedule_and_one_time_are_created(self):
        self.log_in()
        before = utc_now()
        response = self.post_schedule()
        after = utc_now()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/my-medicines"))
        schedule = db.session.scalar(db.select(MedicationSchedule))
        self.assertIsNotNone(schedule)
        self.assertEqual(len(schedule.times), 1)
        self.assertEqual(schedule.times[0].time_of_day, time(9, 0))
        tracking_started_at = app_module.as_utc(
            schedule.reminder_tracking_started_at
        )
        self.assertLessEqual(before, tracking_started_at)
        self.assertLessEqual(tracking_started_at, after)
        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)

    def test_multiple_times_are_normalized_and_created(self):
        self.log_in()
        response = self.post_schedule(
            self.valid_form_data(
                daily_frequency="3",
                medication_times=["20:00", "08:00", "14:00"],
            )
        )
        self.assertEqual(response.status_code, 302)
        schedule = db.session.scalar(db.select(MedicationSchedule))
        self.assertEqual(
            [value.time_of_day for value in schedule.times],
            [time(8, 0), time(14, 0), time(20, 0)],
        )

    def test_time_count_mismatch_and_duplicate_times_are_rejected(self):
        self.log_in()

        cases = (
            self.valid_form_data(
                daily_frequency="2",
                medication_times=["09:00"],
            ),
            self.valid_form_data(
                daily_frequency="2",
                medication_times=["09:00", "09:00"],
            ),
            self.valid_form_data(
                medication_times=["9:00"],
            ),
        )

        for form_data in cases:
            with self.subTest(form_data=form_data):
                response = self.post_schedule(form_data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    0,
                )

    def test_required_text_and_intake_timing_are_validated(self):
        self.log_in()

        cases = (
            self.valid_form_data(dose_amount_text="   "),
            self.valid_form_data(dose_unit_text="   "),
            self.valid_form_data(dose_unit_text="mL"),
            self.valid_form_data(intake_timing="not-allowed"),
        )

        for form_data in cases:
            with self.subTest(form_data=form_data):
                response = self.post_schedule(form_data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    0,
                )

    def test_integer_ranges_and_plan_capacity_are_validated(self):
        self.log_in()

        cases = (
            self.valid_form_data(daily_frequency="0"),
            self.valid_form_data(course_days="0"),
            self.valid_form_data(
                reported_doses_taken_before_tracking="-1"
            ),
            self.valid_form_data(
                course_days="2",
                reported_doses_taken_before_tracking="3",
            ),
        )

        for form_data in cases:
            with self.subTest(form_data=form_data):
                response = self.post_schedule(form_data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    0,
                )

    def test_validation_failure_preserves_submitted_values(self):
        self.log_in()
        response = self.post_schedule(
            self.valid_form_data(
                dose_amount_text="2.25",
                course_days="0",
                start_date="2026-10-05",
            )
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'value="2.25"', response.data)
        self.assertIn(b'value="2026-10-05"', response.data)
        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)

    def test_positive_decimal_dose_amounts_are_stored_as_entered(self):
        self.log_in()
        token = self.get_csrf_token()

        for dose_amount in ("0.5", "1.5", "2.25"):
            with self.subTest(dose_amount=dose_amount):
                response = self.post_schedule(
                    self.valid_form_data(
                        dose_amount_text=dose_amount,
                        course_days="1",
                        reported_doses_taken_before_tracking="1",
                    ),
                    token=token,
                )
                self.assertEqual(response.status_code, 302)
                schedule = db.session.scalar(
                    db.select(MedicationSchedule)
                    .order_by(MedicationSchedule.schedule_id.desc())
                )
                self.assertEqual(
                    schedule.dose_amount_text,
                    dose_amount,
                )
                self.assertFalse(schedule.is_active)

    def test_non_positive_and_invalid_decimal_doses_are_rejected(self):
        self.log_in()
        token = self.get_csrf_token()
        invalid_values = (
            "0",
            "-1",
            "-0.5",
            "abc",
            "1..5",
            "NaN",
            "Infinity",
        )

        for dose_amount in invalid_values:
            with self.subTest(dose_amount=dose_amount):
                response = self.post_schedule(
                    self.valid_form_data(
                        dose_amount_text=dose_amount
                    ),
                    token=token,
                )
                self.assertEqual(response.status_code, 400)

        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            0,
        )

    def test_decimal_dose_does_not_change_occurrence_count(self):
        self.log_in()
        response = self.post_schedule(
            self.valid_form_data(
                dose_amount_text="0.5",
                dose_unit_text="정",
                daily_frequency="3",
                medication_times=["08:00", "14:00", "20:00"],
                course_days="3",
            )
        )
        self.assertEqual(response.status_code, 302)
        schedule = db.session.scalar(db.select(MedicationSchedule))
        tracking_started_at = app_module.as_utc(
            schedule.reminder_tracking_started_at
        )
        summary = app_module.calculate_stored_schedule_summary(
            schedule,
            reference_at=tracking_started_at,
            timezone_name="Asia/Seoul",
        )
        self.assertEqual(schedule.dose_amount_text, "0.5")
        self.assertEqual(summary.daily_frequency, 3)
        self.assertEqual(summary.planned_total, 9)
        self.assertEqual(summary.remaining, 9)

    def test_past_today_and_future_start_dates_are_accepted(self):
        self.log_in()
        today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        start_dates = ("2020-01-01", today, "2099-01-01")

        for start_date_value in start_dates:
            with self.subTest(start_date=start_date_value):
                response = self.post_schedule(
                    self.valid_form_data(start_date=start_date_value)
                )
                self.assertEqual(response.status_code, 302)
                schedule = db.session.scalar(
                    db.select(MedicationSchedule).where(
                        MedicationSchedule.is_active.is_(True)
                    )
                )
                self.assertEqual(
                    schedule.start_date.isoformat(),
                    start_date_value,
                )
                schedule.is_active = False
                db.session.commit()

    def test_created_schedule_uses_server_managed_values(self):
        self.log_in()
        response = self.post_schedule(
            self.valid_form_data(
                daily_frequency="3",
                medication_times=["08:00", "14:00", "20:00"],
                course_days="3",
                reported_doses_taken_before_tracking="2",
            )
        )
        self.assertEqual(response.status_code, 302)
        schedule = db.session.scalar(db.select(MedicationSchedule))
        self.assertIsNone(schedule.end_date)
        self.assertIsNone(schedule.instructions)
        self.assertEqual(schedule.accounted_occurrence_count, 0)
        self.assertTrue(schedule.is_active)
        self.assertTrue(
            all(
                (
                    schedule.monday,
                    schedule.tuesday,
                    schedule.wednesday,
                    schedule.thursday,
                    schedule.friday,
                    schedule.saturday,
                    schedule.sunday,
                )
            )
        )

    def test_reported_equal_to_planned_creates_inactive_record(self):
        self.log_in()
        response = self.post_schedule(
            self.valid_form_data(
                daily_frequency="3",
                medication_times=["08:00", "14:00", "20:00"],
                course_days="3",
                reported_doses_taken_before_tracking="9",
            )
        )
        self.assertEqual(response.status_code, 302)
        schedule = db.session.scalar(db.select(MedicationSchedule))
        self.assertFalse(schedule.is_active)
        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)

    def test_running_active_schedule_rejects_get_and_second_post(self):
        self.log_in()
        token = self.get_csrf_token()
        first_response = self.post_schedule(token=token)
        self.assertEqual(first_response.status_code, 302)

        get_response = self.client.get(self.schedule_url)
        self.assertEqual(get_response.status_code, 302)
        second_response = self.post_schedule(token=token)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )

    def test_expired_open_get_does_not_write(self):
        self.log_in()
        expired_schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        schedule_id = expired_schedule.schedule_id

        response = self.client.get(self.schedule_url)
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        self.assertTrue(
            db.session.get(MedicationSchedule, schedule_id).is_active
        )

    def test_expired_open_is_closed_only_with_new_schedule_transaction(self):
        self.log_in()
        replacement_time = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
        expired_schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        old_schedule_id = expired_schedule.schedule_id
        token = self.get_csrf_token()

        with patch.object(
            app_module,
            "utc_now",
            return_value=replacement_time,
        ):
            response = self.post_schedule(token=token)

        self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        old_schedule = db.session.get(
            MedicationSchedule,
            old_schedule_id,
        )
        active_schedules = db.session.scalars(
            db.select(MedicationSchedule).where(
                MedicationSchedule.is_active.is_(True)
            )
        ).all()
        self.assertFalse(old_schedule.is_active)
        self.assertEqual(
            app_module.as_utc(old_schedule.closed_at),
            replacement_time,
        )
        self.assertEqual(
            app_module.as_utc(old_schedule.updated_at),
            replacement_time,
        )
        self.assertEqual(len(active_schedules), 1)
        self.assertNotEqual(
            active_schedules[0].schedule_id,
            old_schedule_id,
        )

    def test_legacy_create_without_active_schedule_succeeds(self):
        self.log_in()

        response = self.post_schedule()

        self.assertEqual(response.status_code, 302)
        schedule = db.session.scalar(db.select(MedicationSchedule))
        self.assertIsNotNone(schedule)
        self.assertTrue(schedule.is_active)
        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)

    def test_running_active_schedule_blocks_legacy_create(self):
        self.log_in()
        old_schedule = self.add_schedule()
        token = self.get_csrf_token("/my-medicines")
        payload = self.valid_form_data()
        payload["csrf_token"] = token

        get_response = self.client.get(self.schedule_url)
        post_response = self.client.post(self.schedule_url, data=payload)

        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)
        db.session.refresh(old_schedule)
        self.assertTrue(old_schedule.is_active)

    def test_legacy_create_validation_does_not_write(self):
        self.log_in()
        old_updated_at = self.user_medicine.updated_at

        response = self.post_schedule(
            self.valid_form_data(course_days="0")
        )

        self.assertEqual(response.status_code, 400)
        db.session.expire_all()
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertTrue(stored_user_medicine.is_active)
        self.assertEqual(stored_user_medicine.updated_at, old_updated_at)

    def test_legacy_create_database_error_preserves_medicine(self):
        self.log_in()
        old_updated_at = self.user_medicine.updated_at
        token = self.get_csrf_token()

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced test failure"),
        ):
            response = self.post_schedule(token=token)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertTrue(stored_user_medicine.is_active)
        self.assertEqual(stored_user_medicine.updated_at, old_updated_at)

    def test_partial_unique_index_prevents_two_active_schedules(self):
        self.add_schedule()
        second_schedule = MedicationSchedule(
            user_medicine_id=self.user_medicine_id,
            intake_timing="before_meal",
            dose_amount_text="1",
            dose_unit_text="정",
            start_date=date(2099, 2, 1),
            end_date=None,
            course_days=1,
            reported_doses_taken_before_tracking=0,
            reminder_tracking_started_at=utc_now(),
            accounted_occurrence_count=0,
            monday=True,
            tuesday=True,
            wednesday=True,
            thursday=True,
            friday=True,
            saturday=True,
            sunday=True,
            is_active=True,
        )
        db.session.add(second_schedule)

        with self.assertRaises(IntegrityError):
            db.session.commit()

        db.session.rollback()
        active_count = db.session.scalar(
            db.select(db.func.count(MedicationSchedule.schedule_id)).where(
                MedicationSchedule.is_active.is_(True)
            )
        )
        self.assertEqual(active_count, 1)

    def test_database_error_rolls_back_schedule_and_times(self):
        self.log_in()
        token = self.get_csrf_token()

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced test failure"),
        ):
            response = self.post_schedule(token=token)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            0,
        )
        self.assertEqual(db.session.query(MedicationTime).count(), 0)
        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)

    def test_database_error_restores_expired_open_schedule(self):
        self.log_in()
        expired_schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        old_schedule_id = expired_schedule.schedule_id
        token = self.get_csrf_token()

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced test failure"),
        ):
            response = self.post_schedule(token=token)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        schedules = db.session.scalars(
            db.select(MedicationSchedule)
        ).all()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].schedule_id, old_schedule_id)
        self.assertTrue(schedules[0].is_active)
        self.assertIsNone(schedules[0].closed_at)

    def test_invalid_user_timezone_is_handled_without_server_error(self):
        self.log_in()
        token = self.get_csrf_token()
        self.user.timezone = "Invalid/Timezone"
        db.session.commit()

        get_response = self.client.get(self.schedule_url)
        self.assertEqual(get_response.status_code, 302)
        post_response = self.post_schedule(token=token)
        self.assertEqual(post_response.status_code, 400)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            0,
        )

    def test_restart_login_is_required_for_get_and_post(self):
        schedule = self.add_schedule(active=False)
        restart_url = self.restart_url(schedule.schedule_id)

        get_response = self.client.get(restart_url)
        self.assertEqual(get_response.status_code, 302)
        self.assertIn("/login", get_response.headers["Location"])

        login_page = self.client.get("/login")
        token_match = re.search(
            rb'name="csrf_token"\s+value="([^"]+)"',
            login_page.data,
        )
        self.assertIsNotNone(token_match)
        form_data = self.valid_form_data()
        form_data["csrf_token"] = token_match.group(1).decode("utf-8")
        post_response = self.client.post(restart_url, data=form_data)
        self.assertEqual(post_response.status_code, 302)
        self.assertIn("/login", post_response.headers["Location"])

    def test_restart_hides_missing_and_other_users_schedules(self):
        other_schedule = self.add_schedule(
            user_medicine_id=self.other_user_medicine_id,
            active=False,
        )
        self.log_in()
        token = self.get_csrf_token("/my-medicines")

        for schedule_id in (other_schedule.schedule_id, 999_999):
            with self.subTest(method="GET", schedule_id=schedule_id):
                response = self.client.get(self.restart_url(schedule_id))
                self.assertEqual(response.status_code, 404)

            with self.subTest(method="POST", schedule_id=schedule_id):
                form_data = self.valid_form_data()
                form_data["csrf_token"] = token
                response = self.client.post(
                    self.restart_url(schedule_id),
                    data=form_data,
                )
                self.assertEqual(response.status_code, 404)

    def test_restart_get_copies_only_reference_values_without_writing(self):
        source = self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
            start_date_value=date(2025, 1, 2),
            course_days=14,
            medication_times=(time(20, 0), time(8, 30)),
            reported=3,
            accounted=2,
            active=False,
            dose_amount_text="0.5",
            dose_unit_text="포",
            intake_timing="before_meal",
        )
        source_id = source.schedule_id
        source_snapshot = (
            source.is_active,
            source.start_date,
            source.course_days,
            source.reported_doses_taken_before_tracking,
            source.accounted_occurrence_count,
            source.reminder_tracking_started_at,
            source.updated_at,
            [
                (value.medication_time_id, value.time_of_day)
                for value in source.times
            ],
        )
        self.log_in()
        fixed_time = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)

        with patch.object(app_module, "utc_now", return_value=fixed_time):
            response = self.client.get(self.restart_url(source_id))

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        for expected in (
            "다시 복용하기",
            "이전 복용 설정을 참고해 새 복용 과정을 시작합니다.",
            'value="0.5"',
            'value="08:30"',
            'value="20:00"',
            'value="2026-10-07"',
            'value="0"',
            "새 복용 일정 시작",
        ):
            self.assertIn(expected, html)
        self.assertRegex(
            html,
            r'id="dose_unit_text"[\s\S]*value="포"\s+selected',
        )
        self.assertRegex(
            html,
            r'id="intake_timing"[\s\S]*value="before_meal"\s+selected',
        )
        self.assertRegex(
            html,
            r'id="course_days"[\s\S]*?value=""',
        )
        self.assertNotIn('value="14"', html)

        db.session.expire_all()
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.inactive_user_medicine_id,
        )
        stored_source = db.session.get(MedicationSchedule, source_id)
        self.assertFalse(stored_user_medicine.is_active)
        self.assertEqual(
            (
                stored_source.is_active,
                stored_source.start_date,
                stored_source.course_days,
                stored_source.reported_doses_taken_before_tracking,
                stored_source.accounted_occurrence_count,
                stored_source.reminder_tracking_started_at,
                stored_source.updated_at,
                [
                    (value.medication_time_id, value.time_of_day)
                    for value in stored_source.times
                ],
            ),
            source_snapshot,
        )

    def test_restart_creates_new_schedule_and_reactivates_medicine(self):
        source = self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
            medication_times=(time(8, 0), time(20, 0)),
            reported=2,
            accounted=1,
            active=False,
            dose_amount_text="0.5",
            dose_unit_text="캡슐",
            intake_timing="before_meal",
        )
        source_id = source.schedule_id
        source_time_rows = [
            (value.medication_time_id, value.time_of_day)
            for value in source.times
        ]
        self.log_in()
        token = self.get_csrf_token(self.restart_url(source_id))
        restart_time = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
        form_data = self.valid_form_data(
            dose_amount_text="1.5",
            dose_unit_text="포",
            intake_timing="regardless_of_meal",
            daily_frequency="2",
            medication_times=["21:00", "09:00"],
            start_date="2026-10-07",
            course_days="4",
            reported_doses_taken_before_tracking="1",
        )

        with patch.object(app_module, "utc_now", return_value=restart_time):
            response = self.post_restart(source, form_data, token=token)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.inactive_user_medicine_id,
        )
        schedules = db.session.scalars(
            db.select(MedicationSchedule)
            .where(
                MedicationSchedule.user_medicine_id
                == self.inactive_user_medicine_id
            )
            .order_by(MedicationSchedule.schedule_id)
        ).all()
        self.assertEqual(len(schedules), 2)
        stored_source, new_schedule = schedules
        self.assertEqual(stored_source.schedule_id, source_id)
        self.assertFalse(stored_source.is_active)
        self.assertEqual(
            [
                (value.medication_time_id, value.time_of_day)
                for value in stored_source.times
            ],
            source_time_rows,
        )
        self.assertEqual(stored_source.dose_amount_text, "0.5")
        self.assertEqual(stored_source.accounted_occurrence_count, 1)
        self.assertTrue(stored_user_medicine.is_active)
        self.assertTrue(new_schedule.is_active)
        self.assertEqual(new_schedule.dose_amount_text, "1.5")
        self.assertEqual(new_schedule.dose_unit_text, "포")
        self.assertEqual(
            new_schedule.intake_timing,
            "regardless_of_meal",
        )
        self.assertEqual(new_schedule.start_date, date(2026, 10, 7))
        self.assertEqual(new_schedule.course_days, 4)
        self.assertEqual(
            new_schedule.reported_doses_taken_before_tracking,
            1,
        )
        self.assertEqual(new_schedule.accounted_occurrence_count, 0)
        self.assertEqual(
            app_module.as_utc(new_schedule.reminder_tracking_started_at),
            restart_time,
        )
        self.assertEqual(
            [value.time_of_day for value in new_schedule.times],
            [time(9, 0), time(21, 0)],
        )
        self.assertTrue(
            all(
                (
                    new_schedule.monday,
                    new_schedule.tuesday,
                    new_schedule.wednesday,
                    new_schedule.thursday,
                    new_schedule.friday,
                    new_schedule.saturday,
                    new_schedule.sunday,
                )
            )
        )

        history_response = self.client.get(
            self.history_url(self.inactive_user_medicine_id)
        )
        history_html = history_response.data.decode("utf-8")
        self.assertLess(
            history_html.index(
                f'data-schedule-id="{new_schedule.schedule_id}"'
            ),
            history_html.index(f'data-schedule-id="{source_id}"'),
        )

    def test_restart_closes_expired_active_schedule(self):
        self.log_in()
        restart_time = datetime(2026, 10, 7, 5, 0, tzinfo=UTC)
        old_schedule = self.add_schedule(
            course_days=1,
            medication_times=(time(8, 0),),
            active=True,
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=restart_time,
        ):
            response = self.post_restart(old_schedule)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_old = db.session.get(
            MedicationSchedule,
            old_schedule.schedule_id,
        )
        self.assertFalse(stored_old.is_active)
        self.assertEqual(
            app_module.as_utc(stored_old.closed_at),
            restart_time,
        )
        self.assertEqual(
            app_module.as_utc(stored_old.updated_at),
            restart_time,
        )
        self.assertEqual(
            db.session.scalar(
                db.select(db.func.count(MedicationSchedule.schedule_id))
                .where(MedicationSchedule.is_active.is_(True))
            ),
            1,
        )

    def test_restart_rejects_when_current_valid_schedule_exists(self):
        current_schedule = self.add_schedule()
        self.log_in()
        token = self.get_csrf_token("/my-medicines")

        get_response = self.client.get(
            self.restart_url(current_schedule.schedule_id)
        )
        form_data = self.valid_form_data()
        form_data["csrf_token"] = token
        post_response = self.client.post(
            self.restart_url(current_schedule.schedule_id),
            data=form_data,
        )

        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )
        db.session.refresh(current_schedule)
        self.assertTrue(current_schedule.is_active)
        message_response = self.client.get("/my-medicines")
        self.assertIn(
            "현재 진행 중인 복용 일정이 있습니다.".encode(),
            message_response.data,
        )

    def test_restart_running_conflict_preserves_current_schedule(self):
        self.log_in()
        current_schedule = self.add_schedule()
        old_updated_at = current_schedule.updated_at
        token = self.get_csrf_token("/my-medicines")
        form_data = self.valid_form_data()
        form_data["csrf_token"] = token

        get_response = self.client.get(
            self.restart_url(current_schedule.schedule_id)
        )
        post_response = self.client.post(
            self.restart_url(current_schedule.schedule_id),
            data=form_data,
        )

        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)
        db.session.refresh(current_schedule)
        self.assertTrue(current_schedule.is_active)
        self.assertEqual(current_schedule.updated_at, old_updated_at)

    def test_restart_can_use_an_older_source_schedule(self):
        older = self.add_schedule(
            active=False,
            dose_amount_text="0.5",
            medication_times=(time(7, 0),),
            created_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
        expired_open = self.add_schedule(
            active=True,
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
            created_at=datetime(2026, 10, 5, tzinfo=UTC),
        )
        self.log_in()

        response = self.post_restart(
            older,
            self.valid_form_data(dose_amount_text="0.5"),
        )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertFalse(
            db.session.get(
                MedicationSchedule,
                expired_open.schedule_id,
            ).is_active
        )
        newest = db.session.scalar(
            db.select(MedicationSchedule)
            .order_by(MedicationSchedule.schedule_id.desc())
        )
        self.assertNotEqual(newest.schedule_id, older.schedule_id)
        self.assertEqual(newest.dose_amount_text, "0.5")

    def test_restart_requires_remaining_occurrences_without_writing(self):
        source = self.add_schedule(active=False)
        self.log_in()
        before_schedule_count = db.session.query(
            MedicationSchedule
        ).count()

        response = self.post_restart(
            source,
            self.valid_form_data(
                course_days="2",
                reported_doses_taken_before_tracking="2",
            ),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "새 복용 과정에는 남은 예정 복용 횟수가 있어야 합니다.".encode(),
            response.data,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            before_schedule_count,
        )
        db.session.refresh(source)
        db.session.refresh(self.user_medicine)
        self.assertFalse(source.is_active)
        self.assertTrue(self.user_medicine.is_active)

    def test_restart_database_errors_roll_back_all_changes(self):
        source = self.add_schedule(
            medication_times=(time(8, 0), time(20, 0)),
            active=False,
        )
        source_id = source.schedule_id
        old_time_rows = [
            (value.medication_time_id, value.time_of_day)
            for value in source.times
        ]
        self.log_in()
        token = self.get_csrf_token(self.restart_url(source_id))

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced restart failure"),
        ):
            response = self.post_restart(source, token=token)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_source = db.session.get(MedicationSchedule, source_id)
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )
        self.assertFalse(stored_source.is_active)
        self.assertEqual(
            [
                (value.medication_time_id, value.time_of_day)
                for value in stored_source.times
            ],
            old_time_rows,
        )
        self.assertTrue(stored_user_medicine.is_active)

    def test_restart_failure_restores_expired_open_schedule(self):
        expired_schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        expired_schedule_id = expired_schedule.schedule_id
        old_updated_at = expired_schedule.updated_at
        self.log_in()
        token = self.get_csrf_token(
            self.restart_url(expired_schedule_id)
        )

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced restart failure"),
        ):
            response = self.post_restart(
                expired_schedule,
                token=token,
            )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_schedule = db.session.get(
            MedicationSchedule,
            expired_schedule_id,
        )
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)
        self.assertTrue(stored_schedule.is_active)
        self.assertIsNone(stored_schedule.closed_at)
        self.assertEqual(stored_schedule.updated_at, old_updated_at)

    def test_restart_integrity_conflict_is_handled_and_rolled_back(self):
        source = self.add_schedule(active=False)
        self.log_in()
        token = self.get_csrf_token(self.restart_url(source.schedule_id))
        conflict = IntegrityError(
            "forced restart conflict",
            {},
            Exception("unique conflict"),
        )

        with patch.object(db.session, "commit", side_effect=conflict):
            response = self.post_restart(source, token=token)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )
        message_response = self.client.get("/my-medicines")
        self.assertIn(
            "복용 일정이 다른 요청에서 변경되었습니다. ".encode(),
            message_response.data,
        )
        self.assertIn("최신 내용을 확인해주세요.".encode(), message_response.data)

    def test_restart_links_remain_in_history_not_my_medicines(self):
        current_schedule = self.add_schedule()
        self.log_in()

        current_history = self.client.get(self.history_url())
        self.assertNotIn("다시 복용하기".encode(), current_history.data)

        current_schedule.is_active = False
        db.session.commit()
        past_history = self.client.get(self.history_url())
        self.assertIn(
            self.restart_url(current_schedule.schedule_id).encode(),
            past_history.data,
        )

        older = self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
            active=False,
            created_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
        latest = self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
            active=False,
            created_at=datetime(2026, 10, 5, tzinfo=UTC),
        )
        inactive_history = self.client.get(
            self.history_url(self.inactive_user_medicine_id)
        )
        self.assertEqual(inactive_history.status_code, 200)
        html = inactive_history.data.decode("utf-8")
        self.assertIn(self.restart_url(latest.schedule_id), html)
        self.assertIn(self.restart_url(older.schedule_id), html)

        list_html = self.client.get("/my-medicines").data.decode("utf-8")
        self.assertNotIn(self.restart_url(latest.schedule_id), list_html)
        self.assertNotIn(self.restart_url(older.schedule_id), list_html)

    def test_edit_login_is_required_for_get_and_post(self):
        schedule = self.add_schedule()
        get_response = self.client.get(
            self.edit_url(schedule.schedule_id)
        )
        self.assertEqual(get_response.status_code, 302)
        self.assertIn("/login", get_response.headers["Location"])

        login_page = self.client.get("/login")
        token_match = re.search(
            rb'name="csrf_token"\s+value="([^"]+)"',
            login_page.data,
        )
        self.assertIsNotNone(token_match)
        form_data = self.valid_edit_form_data(schedule)
        form_data["csrf_token"] = token_match.group(1).decode("utf-8")
        post_response = self.client.post(
            self.edit_url(schedule.schedule_id),
            data=form_data,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertIn("/login", post_response.headers["Location"])

    def test_edit_ownership_and_active_states_are_hidden(self):
        self.log_in()
        other_schedule = self.add_schedule(
            user_medicine_id=self.other_user_medicine_id,
        )
        inactive_schedule = self.add_schedule(active=False)
        inactive_user_medicine_schedule = self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
        )
        token = self.get_csrf_token("/my-medicines")

        for schedule in (
            other_schedule,
            inactive_schedule,
            inactive_user_medicine_schedule,
        ):
            with self.subTest(schedule_id=schedule.schedule_id):
                get_response = self.client.get(
                    self.edit_url(schedule.schedule_id)
                )
                self.assertEqual(get_response.status_code, 404)
                form_data = self.valid_edit_form_data(schedule)
                form_data["csrf_token"] = token
                post_response = self.client.post(
                    self.edit_url(schedule.schedule_id),
                    data=form_data,
                )
                self.assertEqual(post_response.status_code, 404)

    def test_edit_post_without_csrf_is_rejected(self):
        self.log_in()
        schedule = self.add_schedule()
        response = self.client.post(
            self.edit_url(schedule.schedule_id),
            data=self.valid_edit_form_data(schedule),
        )
        self.assertEqual(response.status_code, 400)

    def test_edit_get_populates_form_without_database_changes(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
            reported=1,
            accounted=2,
            dose_amount_text="0.5",
            dose_unit_text="캡슐",
            intake_timing="before_meal",
        )
        schedule_id = schedule.schedule_id
        old_updated_at = schedule.updated_at
        old_tracking_started_at = schedule.reminder_tracking_started_at
        old_time_ids = [value.medication_time_id for value in schedule.times]

        response = self.client.get(self.edit_url(schedule_id))

        self.assertEqual(response.status_code, 200)
        self.assertIn("복용 설정 수정".encode(), response.data)
        self.assertIn(b'value="0.5"', response.data)
        self.assertIn("캡슐".encode(), response.data)
        for value in (b'value="08:00"', b'value="14:00"', b'value="20:00"'):
            self.assertIn(value, response.data)
        self.assertIn(b'name="schedule_version"', response.data)

        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule_id)
        self.assertTrue(stored.is_active)
        self.assertEqual(stored.accounted_occurrence_count, 2)
        self.assertEqual(stored.updated_at, old_updated_at)
        self.assertEqual(
            stored.reminder_tracking_started_at,
            old_tracking_started_at,
        )
        self.assertEqual(
            [value.medication_time_id for value in stored.times],
            old_time_ids,
        )

    def test_expired_open_edit_get_redirects_without_writing(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        schedule_id = schedule.schedule_id
        old_updated_at = schedule.updated_at
        old_tracking_started_at = schedule.reminder_tracking_started_at
        old_time_id = schedule.times[0].medication_time_id

        response = self.client.get(self.edit_url(schedule_id))

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule_id)
        self.assertTrue(stored.is_active)
        self.assertEqual(stored.updated_at, old_updated_at)
        self.assertEqual(
            stored.reminder_tracking_started_at,
            old_tracking_started_at,
        )
        self.assertEqual(stored.times[0].medication_time_id, old_time_id)

    def test_expired_open_edit_post_deactivates_atomically(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        schedule_id = schedule.schedule_id
        form_data = self.valid_edit_form_data(schedule)
        token = self.get_csrf_token("/my-medicines")

        with patch.object(
            app_module,
            "utc_now",
            return_value=FIRST_EDIT_TIME,
        ) as mocked_utc_now:
            response = self.post_edit(
                schedule,
                form_data,
                token=token,
            )

        self.assertEqual(mocked_utc_now.call_count, 1)
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertFalse(
            db.session.get(MedicationSchedule, schedule_id).is_active
        )

    def test_edit_updates_all_editable_fields_and_replaces_times(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8, 0), time(20, 0)),
        )
        old_time_rows = list(schedule.times)
        form_data = self.valid_edit_form_data(
            schedule,
            dose_amount_text="2.25",
            dose_unit_text="포",
            intake_timing="before_meal",
            daily_frequency="2",
            medication_times=["09:00", "21:00"],
            start_date="2026-10-06",
            course_days="5",
            reported_doses_taken_before_tracking="1",
        )

        response = self.post_edit(schedule, form_data)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.dose_amount_text, "2.25")
        self.assertEqual(stored.dose_unit_text, "포")
        self.assertEqual(stored.intake_timing, "before_meal")
        self.assertEqual(stored.start_date, date(2026, 10, 6))
        self.assertEqual(stored.course_days, 5)
        self.assertEqual(
            stored.reported_doses_taken_before_tracking,
            1,
        )
        self.assertEqual(
            [value.time_of_day for value in stored.times],
            [time(9, 0), time(21, 0)],
        )
        self.assertTrue(
            all(
                sqlalchemy_inspect(value).was_deleted
                for value in old_time_rows
            )
        )
        self.assertTrue(
            all(value not in old_time_rows for value in stored.times)
        )

    def test_edit_validation_reuses_rules_and_preserves_input(self):
        self.log_in()
        schedule = self.add_schedule()
        token = self.get_csrf_token("/my-medicines")
        original_updated_at = schedule.updated_at
        invalid_overrides = (
            {"dose_amount_text": "0"},
            {"dose_amount_text": "NaN"},
            {"dose_unit_text": "mL"},
            {"intake_timing": "invalid"},
            {"daily_frequency": "2", "medication_times": ["09:00"]},
            {"course_days": "0"},
            {"reported_doses_taken_before_tracking": "-1"},
        )

        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                response = self.post_edit(
                    schedule,
                    self.valid_edit_form_data(schedule, **overrides),
                    token=token,
                )
                self.assertEqual(response.status_code, 400)

        preserved_response = self.post_edit(
            schedule,
            self.valid_edit_form_data(
                schedule,
                dose_amount_text="2.25",
                course_days="0",
            ),
            token=token,
        )
        self.assertEqual(preserved_response.status_code, 400)
        self.assertIn(b'value="2.25"', preserved_response.data)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.dose_amount_text, "1")
        self.assertEqual(stored.updated_at, original_updated_at)

    def test_edit_three_times_keeps_five_remaining_occurrences(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2026, 10, 5),
            tracking_started_at=EDIT_TRACKING_TIME,
            course_days=3,
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
        )
        form_data = self.valid_edit_form_data(
            schedule,
            daily_frequency="3",
            medication_times=["09:00", "15:00", "21:00"],
        )
        token = self.get_csrf_token("/my-medicines")

        with patch.object(
            app_module,
            "utc_now",
            return_value=FIRST_EDIT_TIME,
        ) as mocked_utc_now:
            response = self.post_edit(schedule, form_data, token=token)

        self.assertEqual(mocked_utc_now.call_count, 1)
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.accounted_occurrence_count, 4)
        self.assertEqual(
            app_module.as_utc(stored.reminder_tracking_started_at),
            FIRST_EDIT_TIME,
        )
        occurrences = generate_future_occurrences(
            start_date=stored.start_date,
            course_days=stored.course_days,
            times=(value.time_of_day for value in stored.times),
            reported_doses_taken_before_tracking=(
                stored.reported_doses_taken_before_tracking
            ),
            accounted_occurrence_count=(
                stored.accounted_occurrence_count
            ),
            reminder_tracking_started_at=app_module.as_utc(
                stored.reminder_tracking_started_at
            ),
            reference_at=FIRST_EDIT_TIME,
            timezone_name="Asia/Seoul",
        )
        expected = (
            datetime(2026, 10, 6, 15, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            datetime(2026, 10, 6, 21, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            datetime(2026, 10, 7, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            datetime(2026, 10, 7, 15, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            datetime(2026, 10, 7, 21, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        )
        self.assertEqual(occurrences, expected)

    def test_edit_two_times_keeps_two_remaining_occurrences(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2026, 10, 5),
            tracking_started_at=EDIT_TRACKING_TIME,
            course_days=3,
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
        )
        form_data = self.valid_edit_form_data(
            schedule,
            daily_frequency="2",
            medication_times=["09:00", "21:00"],
        )
        token = self.get_csrf_token("/my-medicines")

        with patch.object(
            app_module,
            "utc_now",
            return_value=FIRST_EDIT_TIME,
        ):
            response = self.post_edit(schedule, form_data, token=token)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.accounted_occurrence_count, 4)
        occurrences = generate_future_occurrences(
            start_date=stored.start_date,
            course_days=stored.course_days,
            times=(value.time_of_day for value in stored.times),
            reported_doses_taken_before_tracking=0,
            accounted_occurrence_count=4,
            reminder_tracking_started_at=app_module.as_utc(
                stored.reminder_tracking_started_at
            ),
            reference_at=FIRST_EDIT_TIME,
            timezone_name="Asia/Seoul",
        )
        self.assertEqual(
            occurrences,
            (
                datetime(
                    2026,
                    10,
                    6,
                    21,
                    0,
                    tzinfo=ZoneInfo("Asia/Seoul"),
                ),
                datetime(
                    2026,
                    10,
                    7,
                    9,
                    0,
                    tzinfo=ZoneInfo("Asia/Seoul"),
                ),
            ),
        )

    def test_repeated_edit_accumulates_only_the_new_segment(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2026, 10, 5),
            tracking_started_at=EDIT_TRACKING_TIME,
            course_days=3,
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
        )
        first_form = self.valid_edit_form_data(
            schedule,
            daily_frequency="3",
            medication_times=["09:00", "15:00", "21:00"],
        )
        token = self.get_csrf_token("/my-medicines")

        with patch.object(
            app_module,
            "utc_now",
            return_value=FIRST_EDIT_TIME,
        ):
            first_response = self.post_edit(
                schedule,
                first_form,
                token=token,
            )

        self.assertEqual(first_response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.accounted_occurrence_count, 4)
        second_form = self.valid_edit_form_data(stored)
        token = self.get_csrf_token("/my-medicines")

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_EDIT_TIME,
        ):
            second_response = self.post_edit(
                stored,
                second_form,
                token=token,
            )

        self.assertEqual(second_response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.accounted_occurrence_count, 6)
        self.assertEqual(
            app_module.as_utc(stored.reminder_tracking_started_at),
            SECOND_EDIT_TIME,
        )

    def test_edit_rejects_plan_smaller_than_already_accounted(self):
        self.log_in()
        schedule = self.add_schedule(
            course_days=3,
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
            accounted=4,
        )
        old_updated_at = schedule.updated_at
        old_times = [value.time_of_day for value in schedule.times]
        form_data = self.valid_edit_form_data(
            schedule,
            dose_amount_text="2.25",
            daily_frequency="1",
            medication_times=["09:00"],
            course_days="3",
        )

        response = self.post_edit(schedule, form_data)

        self.assertEqual(response.status_code, 400)
        self.assertIn(b'value="2.25"', response.data)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.updated_at, old_updated_at)
        self.assertEqual(stored.accounted_occurrence_count, 4)
        self.assertEqual(
            [value.time_of_day for value in stored.times],
            old_times,
        )

    def test_edit_equal_plan_completes_schedule(self):
        self.log_in()
        schedule = self.add_schedule(
            course_days=3,
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
            accounted=4,
        )
        form_data = self.valid_edit_form_data(
            schedule,
            daily_frequency="2",
            medication_times=["09:00", "21:00"],
            course_days="2",
        )

        response = self.post_edit(schedule, form_data)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertFalse(stored.is_active)
        self.assertEqual(stored.accounted_occurrence_count, 4)

    def test_edit_allows_reported_count_corrections(self):
        self.log_in()
        schedule = self.add_schedule(reported=1)
        increase_form = self.valid_edit_form_data(
            schedule,
            reported_doses_taken_before_tracking="2",
        )
        increase_response = self.post_edit(schedule, increase_form)
        self.assertEqual(increase_response.status_code, 302)

        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.reported_doses_taken_before_tracking, 2)
        decrease_form = self.valid_edit_form_data(
            stored,
            reported_doses_taken_before_tracking="0",
        )
        decrease_response = self.post_edit(stored, decrease_form)
        self.assertEqual(decrease_response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.reported_doses_taken_before_tracking, 0)

    def test_edit_accepts_past_today_and_future_start_dates(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2026, 10, 6),
            course_days=10,
            medication_times=(time(23, 59),),
        )
        start_dates = (
            date(2020, 1, 1),
            date(2026, 10, 6),
            date(2099, 1, 1),
        )

        for index, start_date_value in enumerate(start_dates):
            with self.subTest(start_date=start_date_value):
                db.session.expire_all()
                stored = db.session.get(
                    MedicationSchedule,
                    schedule.schedule_id,
                )
                form_data = self.valid_edit_form_data(
                    stored,
                    start_date=start_date_value.isoformat(),
                )
                token = self.get_csrf_token("/my-medicines")
                edit_time = FIRST_EDIT_TIME + timedelta(minutes=index)

                with patch.object(
                    app_module,
                    "utc_now",
                    return_value=edit_time,
                ):
                    response = self.post_edit(
                        stored,
                        form_data,
                        token=token,
                    )

                self.assertEqual(response.status_code, 302)
                db.session.expire_all()
                stored = db.session.get(
                    MedicationSchedule,
                    schedule.schedule_id,
                )
                self.assertEqual(stored.start_date, start_date_value)

    def test_atomic_claim_rejects_changed_database_version(self):
        schedule = self.add_schedule()
        old_updated_at = schedule.updated_at
        newer_updated_at = (
            app_module.as_utc(old_updated_at) + timedelta(seconds=1)
        )
        schedule.updated_at = newer_updated_at
        db.session.commit()

        claimed = app_module.claim_medication_schedule_edit(
            schedule,
            old_updated_at=old_updated_at,
            edit_time=FIRST_EDIT_TIME,
        )

        self.assertFalse(claimed)
        db.session.rollback()
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(
            app_module.as_utc(stored.updated_at),
            newer_updated_at,
        )

    def test_stale_edit_token_preserves_the_newer_change(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(9, 0), time(21, 0)),
        )
        stale_form = self.valid_edit_form_data(
            schedule,
            dose_amount_text="2.25",
        )
        schedule.dose_amount_text = "1.5"
        schedule.updated_at = app_module.as_utc(schedule.updated_at) + timedelta(
            seconds=1
        )
        db.session.commit()

        response = self.post_edit(schedule, stale_form)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.dose_amount_text, "1.5")
        self.assertEqual(
            [value.time_of_day for value in stored.times],
            [time(9, 0), time(21, 0)],
        )

    def test_atomic_claim_conflict_preserves_schedule(self):
        self.log_in()
        schedule = self.add_schedule()
        old_updated_at = schedule.updated_at
        form_data = self.valid_edit_form_data(
            schedule,
            dose_amount_text="2.25",
        )

        with patch.object(
            app_module,
            "claim_medication_schedule_edit",
            return_value=False,
        ):
            response = self.post_edit(schedule, form_data)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule.schedule_id)
        self.assertEqual(stored.dose_amount_text, "1")
        self.assertEqual(stored.updated_at, old_updated_at)

    def test_edit_time_replacement_failure_rolls_back_everything(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8, 0), time(20, 0)),
            accounted=1,
        )
        schedule_id = schedule.schedule_id
        old_updated_at = schedule.updated_at
        old_tracking_started_at = schedule.reminder_tracking_started_at
        old_time_rows = [
            (value.medication_time_id, value.time_of_day)
            for value in schedule.times
        ]
        form_data = self.valid_edit_form_data(
            schedule,
            daily_frequency="2",
            medication_times=["09:00", "21:00"],
        )

        with patch.object(
            db.session,
            "flush",
            side_effect=SQLAlchemyError("forced time replacement failure"),
        ):
            response = self.post_edit(schedule, form_data)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored = db.session.get(MedicationSchedule, schedule_id)
        self.assertEqual(stored.accounted_occurrence_count, 1)
        self.assertEqual(stored.updated_at, old_updated_at)
        self.assertEqual(
            stored.reminder_tracking_started_at,
            old_tracking_started_at,
        )
        self.assertEqual(
            [
                (value.medication_time_id, value.time_of_day)
                for value in stored.times
            ],
            old_time_rows,
        )

    def test_my_medicines_ignores_schedule_completion_and_calculation_state(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        completed_response = self.client.get("/my-medicines")
        self.assertEqual(completed_response.status_code, 200)
        self.assertIn(self.medicine.item_name.encode(), completed_response.data)
        self.assertIn(self.history_url().encode(), completed_response.data)
        self.assertNotIn("과거에 복용했던 약".encode(), completed_response.data)

        self.user.timezone = "Invalid/Timezone"
        schedule.accounted_occurrence_count = 999
        db.session.commit()
        error_response = self.client.get("/my-medicines")
        self.assertEqual(error_response.status_code, 200)
        self.assertIn(self.medicine.item_name.encode(), error_response.data)
        self.assertNotIn("복용 설정 확인 필요".encode(), error_response.data)

    def test_my_medicines_shows_management_links_without_schedule_controls(self):
        self.log_in()
        response = self.client.get("/my-medicines")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/medicine/MED-001", response.data)
        self.assertIn(b"/my-medication-plan", response.data)
        self.assertIn(
            f"/my-medicines/{self.user_medicine_id}/deactivate".encode(),
            response.data,
        )
        self.assertIn("복용 이력 없음".encode(), response.data)
        self.assertNotIn(self.schedule_url.encode(), response.data)

        schedule = self.add_schedule()
        response = self.client.get("/my-medicines")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.history_url().encode(), response.data)
        self.assertNotIn(
            self.edit_url(schedule.schedule_id).encode(),
            response.data,
        )
        self.assertNotIn(
            self.restart_url(schedule.schedule_id).encode(),
            response.data,
        )

        plan = MedicationPlan(user_id=self.user_id, is_active=True)
        db.session.add(plan)
        db.session.flush()
        schedule.plan_id = plan.plan_id
        db.session.commit()
        plan_bound_response = self.client.get("/my-medicines")
        self.assertIn(self.medicine.item_name.encode(), plan_bound_response.data)
        self.assertNotIn("현재 복용 중".encode(), plan_bound_response.data)
        self.assertNotIn(
            self.edit_url(schedule.schedule_id).encode(),
            plan_bound_response.data,
        )

    def test_my_medicines_lists_active_medicine_without_schedule(self):
        self.log_in()
        response = self.client.get("/my-medicines")

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.medicine.item_name.encode(), response.data)
        self.assertIn("복용 이력 없음".encode(), response.data)
        self.assertNotIn(self.history_url().encode(), response.data)
        self.assertEqual(
            db.session.query(MedicationSchedule)
            .filter_by(user_medicine_id=self.user_medicine_id)
            .count(),
            0,
        )

    def test_current_medicine_with_old_history_is_listed_only_once(self):
        self.log_in()
        medicine = Medicine(
            item_seq="MED-CURRENT-HISTORY",
            item_name="Current With History",
        )
        user_medicine = UserMedicine(
            user=self.user,
            medicine=medicine,
            registration_source="search",
            is_active=True,
        )
        db.session.add_all([medicine, user_medicine])
        db.session.commit()
        self.add_schedule(
            user_medicine_id=user_medicine.user_medicine_id,
            active=False,
        )
        self.add_schedule(
            user_medicine_id=user_medicine.user_medicine_id,
            active=True,
        )

        response = self.client.get("/my-medicines")

        self.assertEqual(response.status_code, 200)
        marker = (
            "data-user-medicine-id="
            f'"{user_medicine.user_medicine_id}"'
        )
        self.assertEqual(response.data.decode("utf-8").count(marker), 1)
        self.assertIn("Current With History".encode(), response.data)
        self.assertIn("복용 이력".encode(), response.data)

    def test_one_calculation_error_does_not_break_other_rows(self):
        self.log_in()
        valid_schedule = self.add_schedule(active=False)
        invalid_schedule = self.add_schedule(active=True)
        invalid_schedule.accounted_occurrence_count = 999
        db.session.commit()

        list_response = self.client.get("/my-medicines")
        history_response = self.client.get(self.history_url())

        self.assertEqual(list_response.status_code, 200)
        self.assertIn(self.medicine.item_name.encode(), list_response.data)
        self.assertNotIn("복용 설정 확인 필요".encode(), list_response.data)
        self.assertEqual(history_response.status_code, 200)
        self.assertIn(
            "복용 설정 확인 필요".encode(),
            history_response.data,
        )
        self.assertIn("과거 복용 설정".encode(), history_response.data)
        self.assertIn(
            f'data-schedule-id="{valid_schedule.schedule_id}"'.encode(),
            history_response.data,
        )

    def test_medication_history_requires_login_and_owner(self):
        response = self.client.get(self.history_url())
        self.assertEqual(response.status_code, 302)

        self.log_in()
        self.assertEqual(
            self.client.get(
                self.history_url(self.other_user_medicine_id)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(self.history_url(999999)).status_code,
            404,
        )

        self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
        )
        response = self.client.get(
            self.history_url(self.inactive_user_medicine_id)
        )
        self.assertEqual(response.status_code, 200)

    def test_medication_history_lists_all_fields_in_newest_first_order(self):
        self.log_in()
        older = self.add_schedule(
            start_date_value=date(2099, 1, 1),
            tracking_started_at=datetime(2026, 10, 5, 5, 0, tzinfo=UTC),
            course_days=3,
            medication_times=(time(20, 0), time(8, 0)),
            reported=1,
            active=False,
            dose_amount_text="0.5",
            dose_unit_text="정",
            intake_timing="before_meal",
            created_at=datetime(2026, 10, 5, 0, 0, tzinfo=UTC),
        )
        newer = self.add_schedule(
            start_date_value=date(2099, 2, 1),
            course_days=2,
            medication_times=(time(21, 0),),
            active=True,
            dose_amount_text="1.5",
            dose_unit_text="캡슐",
            intake_timing="regardless_of_meal",
            created_at=datetime(2026, 10, 6, 0, 0, tzinfo=UTC),
        )

        response = self.client.get(self.history_url())

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertLess(
            html.index(f'data-schedule-id="{newer.schedule_id}"'),
            html.index(f'data-schedule-id="{older.schedule_id}"'),
        )
        for expected in (
            "2099-01-01",
            "0.5",
            "정",
            "식전",
            "2회",
            "08:00",
            "20:00",
            "3일",
            "1회",
            "과거 복용 설정",
            "식사와 관계없이",
            "현재 복용 중",
            "마지막 예정 복용 시각",
        ):
            self.assertIn(expected, html)
        self.assertLess(html.index("08:00"), html.index("20:00"))

    def test_medication_history_uses_truthful_schedule_statuses(self):
        self.log_in()
        current_schedule = self.add_schedule()
        current_response = self.client.get(self.history_url())
        self.assertIn("현재 복용 중".encode(), current_response.data)

        current_schedule.is_active = False
        completed_schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        db.session.commit()
        completed_response = self.client.get(self.history_url())
        self.assertIn(
            "예정된 복용 계획 완료".encode(),
            completed_response.data,
        )
        self.assertNotIn("실제 복용 완료".encode(), completed_response.data)

        completed_schedule.is_active = False
        db.session.commit()
        removed_schedule = self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
        )
        removed_response = self.client.get(
            self.history_url(self.inactive_user_medicine_id)
        )
        self.assertIn(
            "내 복용약에서 제거되어 중단된 계획".encode(),
            removed_response.data,
        )

        removed_schedule.is_active = False
        db.session.commit()
        past_response = self.client.get(
            self.history_url(self.inactive_user_medicine_id)
        )
        self.assertIn("과거 복용 설정".encode(), past_response.data)

    def test_medication_history_uses_schedule_state_and_marks_errors(self):
        self.log_in()
        self.add_schedule()

        response = self.client.get(self.history_url())

        self.assertEqual(response.status_code, 200)
        self.assertIn("현재 복용 중".encode(), response.data)
        self.assertNotIn(
            "새 복용 설정 대기 중인 이전 계획".encode(),
            response.data,
        )

        self.user.timezone = "Invalid/Timezone"
        db.session.commit()
        error_response = self.client.get(self.history_url())
        self.assertEqual(error_response.status_code, 200)
        self.assertIn("복용 설정 확인 필요".encode(), error_response.data)
        self.assertIn("확인할 수 없음".encode(), error_response.data)

    def test_medication_history_last_occurrence_is_exact_or_unknown(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2026, 10, 5),
            tracking_started_at=datetime(2026, 10, 5, 5, 0, tzinfo=UTC),
            course_days=3,
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
        )

        response = self.client.get(self.history_url())
        self.assertIn("2026-10-08 08:00".encode(), response.data)

        schedule.reported_doses_taken_before_tracking = 5
        schedule.accounted_occurrence_count = 4
        db.session.commit()
        unknown_response = self.client.get(self.history_url())
        self.assertIn("확인할 수 없음".encode(), unknown_response.data)

    def test_my_medicines_lists_only_active_rows_regardless_of_schedule_state(self):
        self.log_in()
        self.add_schedule()

        past_medicine = Medicine(
            item_seq="MED-PAST",
            item_name="Past Medicine",
        )
        past_user_medicine = UserMedicine(
            user=self.user,
            medicine=past_medicine,
            registration_source="search",
            is_active=True,
        )
        db.session.add_all([past_medicine, past_user_medicine])
        db.session.commit()
        self.add_schedule(
            user_medicine_id=past_user_medicine.user_medicine_id,
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )

        response = self.client.get("/my-medicines")

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        pending_marker = (
            f'data-user-medicine-id="{self.user_medicine_id}"'
        )
        inactive_without_history_marker = (
            "data-user-medicine-id="
            f'"{self.inactive_user_medicine_id}"'
        )
        self.assertEqual(html.count(pending_marker), 1)
        self.assertIn("Past Medicine", html)
        self.assertNotIn(inactive_without_history_marker, html)
        self.assertNotIn("복용 설정 필요", html)
        self.assertNotIn("과거에 복용했던 약", html)

    def test_active_medicines_are_sorted_by_registration_not_schedule(self):
        self.log_in()
        entries = []

        for index, created_at in enumerate(
            (
                datetime(2026, 10, 4, tzinfo=UTC),
                datetime(2026, 10, 6, tzinfo=UTC),
                datetime(2026, 10, 6, tzinfo=UTC),
            ),
            start=1,
        ):
            medicine = Medicine(
                item_seq=f"MED-SORT-{index}",
                item_name=f"Sorted Medicine {index}",
            )
            user_medicine = UserMedicine(
                user=self.user,
                medicine=medicine,
                registration_source="search",
                is_active=True,
                registered_at=created_at,
            )
            db.session.add_all([medicine, user_medicine])
            db.session.commit()
            self.add_schedule(
                user_medicine_id=user_medicine.user_medicine_id,
                active=False,
                created_at=datetime(2026, 10, 7 - index, tzinfo=UTC),
            )
            entries.append(user_medicine)

        response = self.client.get("/my-medicines")
        html = response.data.decode("utf-8")
        self.assertLess(
            html.index("Sorted Medicine 3"),
            html.index("Sorted Medicine 2"),
        )
        self.assertLess(
            html.index("Sorted Medicine 2"),
            html.index("Sorted Medicine 1"),
        )

    def test_my_medicines_and_history_get_requests_do_not_write(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        before_user_medicine = self.user_medicine.is_active
        before_schedule = (
            schedule.is_active,
            schedule.accounted_occurrence_count,
            schedule.reminder_tracking_started_at,
            schedule.updated_at,
        )
        before_time_rows = [
            (value.medication_time_id, value.time_of_day)
            for value in schedule.times
        ]

        self.assertEqual(self.client.get("/my-medicines").status_code, 200)
        self.assertEqual(
            self.client.get(self.history_url()).status_code,
            200,
        )

        db.session.expire_all()
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        stored_schedule = db.session.get(
            MedicationSchedule,
            schedule.schedule_id,
        )
        self.assertEqual(
            stored_user_medicine.is_active,
            before_user_medicine,
        )
        self.assertEqual(
            (
                stored_schedule.is_active,
                stored_schedule.accounted_occurrence_count,
                stored_schedule.reminder_tracking_started_at,
                stored_schedule.updated_at,
            ),
            before_schedule,
        )
        self.assertEqual(
            [
                (value.medication_time_id, value.time_of_day)
                for value in stored_schedule.times
            ],
            before_time_rows,
        )

    def test_my_medicines_query_count_does_not_grow_with_rows(self):
        self.log_in()

        for index in range(8):
            medicine = Medicine(
                item_seq=f"MED-NQ-{index}",
                item_name=f"Query Medicine {index}",
            )
            user_medicine = UserMedicine(
                user=self.user,
                medicine=medicine,
                registration_source="search",
                is_active=True,
            )
            db.session.add_all([medicine, user_medicine])
            db.session.commit()
            self.add_schedule(
                user_medicine_id=user_medicine.user_medicine_id,
                active=False,
            )

        select_statements = []

        def record_select(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            if statement.lstrip().upper().startswith("SELECT"):
                select_statements.append(statement)

        event.listen(
            self.test_engine,
            "before_cursor_execute",
            record_select,
        )
        try:
            response = self.client.get("/my-medicines")
        finally:
            event.remove(
                self.test_engine,
                "before_cursor_execute",
                record_select,
            )

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(select_statements), 5)

    def test_register_new_user_medicine_without_schedule_or_plan(self):
        self.log_in()
        new_medicine = Medicine(
            item_seq="MED-004",
            item_name="새 테스트약",
        )
        db.session.add(new_medicine)
        db.session.commit()
        before_count = db.session.query(UserMedicine).count()
        token = self.get_csrf_token("/my-medicines")

        response = self.client.post(
            "/my-medicines/add/MED-004",
            data={"csrf_token": token},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/my-medicines"))
        self.assertEqual(
            db.session.query(UserMedicine).count(),
            before_count + 1,
        )
        registered = db.session.scalar(
            db.select(UserMedicine).where(
                UserMedicine.user_id == self.user_id,
                UserMedicine.medicine_item_seq == "MED-004",
            )
        )
        self.assertTrue(registered.is_active)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            0,
        )
        self.assertEqual(db.session.query(MedicationPlan).count(), 0)

        medicines_page = self.client.get("/my-medicines")
        self.assertIn(new_medicine.item_name.encode(), medicines_page.data)
        self.assertIn(
            (
                'data-user-medicine-id="'
                f"{registered.user_medicine_id}"
                '"'
            ).encode(),
            medicines_page.data,
        )

        plan = MedicationPlan(user_id=self.user_id, is_active=True)
        db.session.add(plan)
        db.session.flush()
        db.session.add(
            MedicationPlanTime(plan_id=plan.plan_id, time_of_day=time(9, 0))
        )
        db.session.commit()
        selection = self.client.get(
            f"/medication-plans/{plan.plan_id}/medicines/add"
        )
        self.assertIn(
            (
                f"/medication-plans/{plan.plan_id}/medicines/"
                f"{registered.user_medicine_id}/new"
            ).encode(),
            selection.data,
        )

    def test_reactivate_reuses_row_and_preserves_schedule_history(self):
        self.log_in()
        old_schedule = self.add_schedule(
            user_medicine_id=self.inactive_user_medicine_id,
            medication_times=(time(8, 0), time(20, 0)),
            active=False,
        )
        old_schedule_id = old_schedule.schedule_id
        old_registered_at = self.inactive_user_medicine.registered_at
        old_times = [
            (value.medication_time_id, value.time_of_day)
            for value in old_schedule.times
        ]
        before_user_medicine_count = db.session.query(
            UserMedicine
        ).count()
        token = self.get_csrf_token("/my-medicines")

        response = self.client.post(
            "/my-medicines/add/MED-003",
            data={"csrf_token": token},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/my-medicines"))
        self.assertEqual(
            db.session.query(UserMedicine).count(),
            before_user_medicine_count,
        )
        db.session.expire_all()
        reactivated = db.session.get(
            UserMedicine,
            self.inactive_user_medicine_id,
        )
        stored_schedule = db.session.get(
            MedicationSchedule,
            old_schedule_id,
        )
        self.assertTrue(reactivated.is_active)
        self.assertEqual(reactivated.registration_source, "search")
        self.assertEqual(reactivated.registered_at, old_registered_at)
        self.assertFalse(stored_schedule.is_active)
        self.assertEqual(
            [
                (value.medication_time_id, value.time_of_day)
                for value in stored_schedule.times
            ],
            old_times,
        )
        self.assertEqual(db.session.query(MedicationPlan).count(), 0)

        plan = MedicationPlan(user_id=self.user_id, is_active=True)
        db.session.add(plan)
        db.session.flush()
        db.session.add(
            MedicationPlanTime(plan_id=plan.plan_id, time_of_day=time(9, 0))
        )
        db.session.commit()
        selection = self.client.get(
            f"/medication-plans/{plan.plan_id}/medicines/add"
        )
        self.assertIn(
            (
                f"/medication-plans/{plan.plan_id}/medicines/"
                f"{reactivated.user_medicine_id}/new"
            ).encode(),
            selection.data,
        )

    def test_active_duplicate_registration_preserves_schedule_state(self):
        self.log_in()
        schedule = self.add_schedule()
        before_count = db.session.query(UserMedicine).count()
        before_schedule_count = db.session.query(MedicationSchedule).count()
        token = self.get_csrf_token("/my-medicines")

        response = self.client.post(
            "/my-medicines/add/MED-001",
            data={"csrf_token": token},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.query(UserMedicine).count(),
            before_count,
        )
        db.session.refresh(schedule)
        self.assertTrue(schedule.is_active)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            before_schedule_count,
        )
        message_response = self.client.get("/my-medicines")
        self.assertIn(
            "이미 내 복용약에 등록되어 있습니다.".encode(),
            message_response.data,
        )

    def test_deactivate_closes_legacy_schedule_and_preserves_history(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8, 0), time(20, 0)),
        )
        schedule_id = schedule.schedule_id
        immutable_values = {
            column.name: getattr(schedule, column.name)
            for column in MedicationSchedule.__table__.columns
            if column.name not in {"is_active", "closed_at", "updated_at"}
        }
        stored_times = [
            (value.medication_time_id, value.time_of_day)
            for value in schedule.times
        ]
        old_registered_at = self.user_medicine.registered_at
        schedule_count = db.session.query(MedicationSchedule).count()

        with patch.object(
            app_module,
            "utc_now",
            return_value=REMOVE_TIME,
        ):
            response = self.post_deactivate()

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        deactivated = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        stored_schedule = db.session.get(
            MedicationSchedule,
            schedule_id,
        )
        self.assertFalse(deactivated.is_active)
        self.assertEqual(deactivated.registered_at, old_registered_at)
        self.assertEqual(
            app_module.as_utc(deactivated.updated_at),
            REMOVE_TIME,
        )
        self.assertFalse(stored_schedule.is_active)
        self.assertEqual(
            app_module.as_utc(stored_schedule.closed_at),
            REMOVE_TIME,
        )
        self.assertEqual(
            app_module.as_utc(stored_schedule.updated_at),
            REMOVE_TIME,
        )
        for name, value in immutable_values.items():
            self.assertEqual(getattr(stored_schedule, name), value)
        self.assertEqual(
            [
                (value.medication_time_id, value.time_of_day)
                for value in stored_schedule.times
            ],
            stored_times,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            schedule_count,
        )
        history = self.client.get(self.history_url())
        self.assertEqual(history.status_code, 200)
        self.assertIn(f'data-schedule-id="{schedule_id}"'.encode(), history.data)
        history_entry = app_module.build_schedule_history_entry(
            deactivated,
            stored_schedule,
            reference_at=datetime(2100, 1, 1, tzinfo=UTC),
            timezone_name=self.user.timezone,
        )
        self.assertEqual(history_entry["status"], "past")

    def test_deactivate_requires_login_owner_and_active_user_medicine(self):
        login_token = self.get_csrf_token("/login")
        login_required_response = self.post_deactivate(token=login_token)
        self.assertEqual(login_required_response.status_code, 302)
        self.assertIn("/login", login_required_response.headers["Location"])

        self.log_in()
        token = self.get_csrf_token("/my-medicines")
        owner_response = self.post_deactivate(
            self.other_user_medicine_id,
            token=token,
        )
        self.assertEqual(owner_response.status_code, 404)

        inactive_response = self.post_deactivate(
            self.inactive_user_medicine_id,
            token=token,
        )
        self.assertEqual(inactive_response.status_code, 302)
        db.session.refresh(self.inactive_user_medicine)
        self.assertFalse(self.inactive_user_medicine.is_active)

    def test_repeated_deactivate_is_safe_no_op(self):
        self.log_in()
        schedule = self.add_schedule()
        token = self.get_csrf_token("/my-medicines")

        with patch.object(
            app_module,
            "utc_now",
            return_value=REMOVE_TIME,
        ) as first_clock:
            first_response = self.post_deactivate(token=token)

        first_clock.assert_called_once_with()
        self.assertEqual(first_response.status_code, 302)
        db.session.expire_all()
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        stored_schedule = db.session.get(
            MedicationSchedule,
            schedule.schedule_id,
        )
        first_user_updated_at = stored_user_medicine.updated_at
        first_schedule_updated_at = stored_schedule.updated_at
        first_closed_at = stored_schedule.closed_at

        with patch.object(
            app_module,
            "utc_now",
            return_value=REMOVE_TIME + timedelta(minutes=1),
        ) as second_clock:
            second_response = self.post_deactivate(token=token)

        second_clock.assert_not_called()
        self.assertEqual(second_response.status_code, 302)
        db.session.expire_all()
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        stored_schedule = db.session.get(
            MedicationSchedule,
            schedule.schedule_id,
        )
        self.assertEqual(
            stored_user_medicine.updated_at,
            first_user_updated_at,
        )
        self.assertEqual(stored_schedule.updated_at, first_schedule_updated_at)
        self.assertEqual(stored_schedule.closed_at, first_closed_at)

    def test_deactivate_without_schedule_changes_only_user_medicine(self):
        self.log_in()
        old_registered_at = self.user_medicine.registered_at
        schedule_count = db.session.query(MedicationSchedule).count()
        time_count = db.session.query(MedicationTime).count()

        with patch.object(
            app_module,
            "utc_now",
            return_value=REMOVE_TIME,
        ):
            response = self.post_deactivate()

        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.user_medicine)
        self.assertFalse(self.user_medicine.is_active)
        self.assertEqual(self.user_medicine.registered_at, old_registered_at)
        self.assertEqual(
            app_module.as_utc(self.user_medicine.updated_at),
            REMOVE_TIME,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            schedule_count,
        )
        self.assertEqual(db.session.query(MedicationTime).count(), time_count)

    def test_deactivate_closes_expired_open_schedule(self):
        self.log_in()
        schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=REMOVE_TIME,
        ):
            response = self.post_deactivate()

        self.assertEqual(response.status_code, 302)
        db.session.refresh(schedule)
        self.assertFalse(schedule.is_active)
        self.assertEqual(app_module.as_utc(schedule.closed_at), REMOVE_TIME)

    def test_deactivate_schedule_claim_conflict_rolls_back(self):
        self.log_in()
        schedule = self.add_schedule()
        old_schedule_updated_at = schedule.updated_at
        old_user_medicine_updated_at = self.user_medicine.updated_at

        with patch.object(
            app_module,
            "claim_medication_schedule_removal",
            return_value=False,
        ):
            response = self.post_deactivate()

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_schedule = db.session.get(
            MedicationSchedule,
            schedule.schedule_id,
        )
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertTrue(stored_schedule.is_active)
        self.assertIsNone(stored_schedule.closed_at)
        self.assertEqual(stored_schedule.updated_at, old_schedule_updated_at)
        self.assertTrue(stored_user_medicine.is_active)
        self.assertEqual(
            stored_user_medicine.updated_at,
            old_user_medicine_updated_at,
        )

    def test_deactivate_user_claim_conflict_and_commit_failure_roll_back(self):
        self.log_in()
        schedule = self.add_schedule()
        old_schedule_updated_at = schedule.updated_at
        old_user_medicine_updated_at = self.user_medicine.updated_at

        for failure_kind in ("claim", "commit"):
            with self.subTest(failure_kind=failure_kind):
                if failure_kind == "claim":
                    failure_patch = patch.object(
                        app_module,
                        "claim_user_medicine_deactivation",
                        return_value=False,
                    )
                else:
                    failure_patch = patch.object(
                        db.session,
                        "commit",
                        side_effect=SQLAlchemyError("forced commit failure"),
                    )

                with failure_patch:
                    response = self.post_deactivate()

                self.assertEqual(response.status_code, 302)
                db.session.expire_all()
                stored_schedule = db.session.get(
                    MedicationSchedule,
                    schedule.schedule_id,
                )
                stored_user_medicine = db.session.get(
                    UserMedicine,
                    self.user_medicine_id,
                )
                self.assertTrue(stored_schedule.is_active)
                self.assertIsNone(stored_schedule.closed_at)
                self.assertEqual(
                    stored_schedule.updated_at,
                    old_schedule_updated_at,
                )
                self.assertTrue(stored_user_medicine.is_active)
                self.assertEqual(
                    stored_user_medicine.updated_at,
                    old_user_medicine_updated_at,
                )

    def test_existing_routes_and_my_medicine_mutations_still_work(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/register").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(
            self.client.get("/verify-email/not-valid").status_code,
            302,
        )

        self.log_in()
        self.assertEqual(self.client.get("/my-medicines").status_code, 200)
        self.assertEqual(
            self.client.get("/my-medicines/add").status_code,
            200,
        )
        new_medicine = Medicine(
            item_seq="MED-004",
            item_name="새 테스트 약",
        )
        db.session.add(new_medicine)
        db.session.commit()
        token = self.get_csrf_token("/my-medicines")
        response = self.client.post(
            "/my-medicines/add/MED-004",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 302)
        registered = db.session.scalar(
            db.select(UserMedicine).where(
                UserMedicine.user_id == self.user_id,
                UserMedicine.medicine_item_seq == "MED-004",
            )
        )
        self.assertTrue(registered.is_active)
        self.assertEqual(
            db.session.query(MedicationSchedule)
            .filter_by(user_medicine_id=registered.user_medicine_id)
            .count(),
            0,
        )

        token = self.get_csrf_token("/my-medicines")
        response = self.client.post(
            f"/my-medicines/{registered.user_medicine_id}/deactivate",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(registered)
        self.assertFalse(registered.is_active)


if __name__ == "__main__":
    unittest.main()
