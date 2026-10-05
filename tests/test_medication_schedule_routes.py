import re
import tempfile
import unittest
from datetime import UTC, date, datetime, time
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flask import g
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import app as app_module
from models import (
    Medicine,
    MedicationSchedule,
    MedicationTime,
    User,
    UserMedicine,
    db,
    utc_now,
)


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
        active=True,
    ):
        schedule = MedicationSchedule(
            user_medicine_id=(
                user_medicine_id or self.user_medicine_id
            ),
            intake_timing="after_meal",
            dose_amount_text="1",
            dose_unit_text="정",
            instructions=None,
            start_date=start_date_value,
            end_date=None,
            course_days=course_days,
            reported_doses_taken_before_tracking=reported,
            reminder_tracking_started_at=(
                tracking_started_at or utc_now()
            ),
            accounted_occurrence_count=0,
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
        db.session.add(schedule)
        db.session.commit()
        return schedule

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
                dose_amount_text="",
                dose_unit_text="custom-unit",
                start_date="2026-10-05",
            )
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'value="custom-unit"', response.data)
        self.assertIn(b'value="2026-10-05"', response.data)

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
        expired_schedule = self.add_schedule(
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        old_schedule_id = expired_schedule.schedule_id
        token = self.get_csrf_token()
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
        self.assertEqual(len(active_schedules), 1)
        self.assertNotEqual(
            active_schedules[0].schedule_id,
            old_schedule_id,
        )

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

    def test_my_medicines_shows_create_link_or_configured_state(self):
        self.log_in()
        response = self.client.get("/my-medicines")
        self.assertEqual(response.status_code, 200)
        self.assertIn("복용 설정".encode(), response.data)

        self.add_schedule()
        response = self.client.get("/my-medicines")
        self.assertEqual(response.status_code, 200)
        self.assertIn("복용 설정됨".encode(), response.data)

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
