import re
import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import g
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import app as app_module
from models import (
    Medicine,
    MedicationPlan,
    MedicationPlanTime,
    MedicationSchedule,
    MedicationSchedulePlanTime,
    MedicationTime,
    User,
    UserMedicine,
    db,
)


FIRST_ACTION_TIME = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
SECOND_ACTION_TIME = datetime(2026, 10, 6, 7, 1, tzinfo=UTC)


class MedicationPlanRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = (
            Path(cls.temporary_directory.name) / "plan-route-test.db"
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
            username="plan-user",
            email="plan@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.other_user = User(
            username="other-plan-user",
            email="other-plan@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.medicine = Medicine(
            item_seq="PLAN-MED-001",
            item_name="테스트 약",
        )
        self.other_medicine = Medicine(
            item_seq="PLAN-MED-002",
            item_name="다른 테스트 약",
        )
        self.user_medicine = UserMedicine(
            user=self.user,
            medicine=self.medicine,
            registration_source="search",
            is_active=True,
        )
        self.other_user_medicine = UserMedicine(
            user=self.other_user,
            medicine=self.other_medicine,
            registration_source="search",
            is_active=True,
        )
        db.session.add_all(
            [
                self.user,
                self.other_user,
                self.medicine,
                self.other_medicine,
                self.user_medicine,
                self.other_user_medicine,
            ]
        )
        db.session.commit()
        self.user_id = self.user.user_id
        self.other_user_id = self.other_user.user_id
        self.user_medicine_id = self.user_medicine.user_medicine_id

    def tearDown(self):
        db.session.remove()
        self.application_context.pop()

    def log_in(self, user_id=None):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id or self.user_id)
            session["_fresh"] = True

        g.pop("_login_user", None)

    def get_csrf_token(self, url="/my-medication-plan"):
        response = self.client.get(url)
        match = re.search(
            rb'name="csrf_token"\s+value="([^"]+)"',
            response.data,
        )
        self.assertIsNotNone(match)
        return match.group(1).decode("utf-8")

    def add_plan(self, *, user_id=None, updated_at=FIRST_ACTION_TIME):
        plan = MedicationPlan(
            user_id=user_id or self.user_id,
            is_active=True,
            created_at=updated_at,
            updated_at=updated_at,
        )
        db.session.add(plan)
        db.session.commit()
        return plan

    def add_plan_time(self, plan, value):
        plan_time = MedicationPlanTime(
            plan_id=plan.plan_id,
            time_of_day=value,
            created_at=FIRST_ACTION_TIME,
        )
        db.session.add(plan_time)
        db.session.commit()
        return plan_time

    def plan_version(self, plan):
        db.session.refresh(plan)
        return app_module.serialize_medication_plan_version(
            plan.updated_at
        )

    def post_create_plan(self, *, token=None):
        if token is None:
            token = self.get_csrf_token()

        return self.client.post(
            "/medication-plans",
            data={"csrf_token": token},
        )

    def post_add_time(
        self,
        plan,
        value,
        *,
        version=None,
        token=None,
    ):
        if token is None:
            token = self.get_csrf_token()
        if version is None:
            version = self.plan_version(plan)

        return self.client.post(
            f"/medication-plans/{plan.plan_id}/times",
            data={
                "csrf_token": token,
                "plan_version": version,
                "time_of_day": value,
            },
        )

    def post_delete_time(
        self,
        plan,
        plan_time,
        *,
        version=None,
        token=None,
    ):
        if token is None:
            token = self.get_csrf_token()
        if version is None:
            version = self.plan_version(plan)

        return self.client.post(
            "/medication-plans/"
            f"{plan.plan_id}/times/{plan_time.plan_time_id}/delete",
            data={
                "csrf_token": token,
                "plan_version": version,
            },
        )

    def move_plan_time_url(self, plan, plan_time):
        return (
            f"/medication-plans/{plan.plan_id}/times/"
            f"{plan_time.plan_time_id}/move"
        )

    def post_move_plan_time(
        self,
        plan,
        source_plan_time,
        target_time,
        schedules,
        *,
        plan_version=None,
        schedule_versions=None,
        token=None,
    ):
        url = self.move_plan_time_url(plan, source_plan_time)

        if token is None:
            token = self.get_csrf_token(url)
        if plan_version is None:
            plan_version = self.plan_version(plan)
        if schedule_versions is None:
            schedule_versions = {
                schedule.schedule_id: (
                    app_module.serialize_schedule_version(
                        schedule.updated_at
                    )
                )
                for schedule in schedules
            }

        data = {
            "csrf_token": token,
            "plan_version": plan_version,
            "target_time": target_time,
            "schedule_id": [
                str(schedule.schedule_id)
                if isinstance(schedule, MedicationSchedule)
                else str(schedule)
                for schedule in schedules
            ],
        }
        data.update(
            {
                f"schedule_version_{schedule_id}": version
                for schedule_id, version in schedule_versions.items()
            }
        )
        return self.client.post(url, data=data)

    def add_linked_schedule(self, plan, plan_time, *, active):
        schedule = MedicationSchedule(
            user_medicine_id=self.user_medicine_id,
            plan_id=plan.plan_id,
            intake_timing="after_meal",
            dose_amount_text="1",
            dose_unit_text="정",
            instructions=None,
            start_date=date(2099, 1, 1),
            end_date=None,
            course_days=3,
            reported_doses_taken_before_tracking=0,
            reminder_tracking_started_at=FIRST_ACTION_TIME,
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
        schedule.times.append(
            MedicationTime(time_of_day=plan_time.time_of_day)
        )
        db.session.add(schedule)
        db.session.flush()
        db.session.add(
            MedicationSchedulePlanTime(
                plan_id=plan.plan_id,
                schedule_id=schedule.schedule_id,
                plan_time_id=plan_time.plan_time_id,
            )
        )
        db.session.commit()
        return schedule

    def add_user_medicine(
        self,
        suffix,
        *,
        user=None,
        active=True,
    ):
        owner = user or self.user
        medicine = Medicine(
            item_seq=f"PLAN-MED-{suffix}",
            item_name=f"테스트 약 {suffix}",
        )
        user_medicine = UserMedicine(
            user=owner,
            medicine=medicine,
            registration_source="search",
            is_active=active,
        )
        db.session.add_all([medicine, user_medicine])
        db.session.commit()
        return user_medicine

    def add_existing_schedule(
        self,
        user_medicine,
        *,
        plan=None,
        plan_times=(),
        active=True,
        start_date_value=date(2099, 1, 1),
        tracking_started_at=FIRST_ACTION_TIME,
        course_days=3,
        medication_times=(time(20, 0),),
        reported=0,
        accounted=0,
    ):
        schedule = MedicationSchedule(
            user_medicine_id=user_medicine.user_medicine_id,
            plan_id=plan.plan_id if plan is not None else None,
            intake_timing="after_meal",
            dose_amount_text="1",
            dose_unit_text="정",
            instructions=None,
            start_date=start_date_value,
            end_date=None,
            course_days=course_days,
            reported_doses_taken_before_tracking=reported,
            reminder_tracking_started_at=tracking_started_at,
            accounted_occurrence_count=accounted,
            monday=True,
            tuesday=True,
            wednesday=True,
            thursday=True,
            friday=True,
            saturday=True,
            sunday=True,
            is_active=active,
            created_at=tracking_started_at,
            updated_at=tracking_started_at,
        )
        schedule.times.extend(
            MedicationTime(time_of_day=value)
            for value in medication_times
        )
        db.session.add(schedule)
        db.session.flush()

        if plan is not None:
            db.session.add_all(
                MedicationSchedulePlanTime(
                    plan_id=plan.plan_id,
                    schedule_id=schedule.schedule_id,
                    plan_time_id=plan_time.plan_time_id,
                )
                for plan_time in plan_times
            )

        db.session.commit()
        return schedule

    def select_medicine_url(self, plan):
        return f"/medication-plans/{plan.plan_id}/medicines/add"

    def plan_medicine_form_url(self, plan, user_medicine=None):
        target = user_medicine or self.user_medicine
        return (
            f"/medication-plans/{plan.plan_id}/medicines/"
            f"{target.user_medicine_id}/new"
        )

    def valid_plan_medicine_form(self, plan, plan_times, **overrides):
        form_data = {
            "dose_amount_text": "1",
            "dose_unit_text": "정",
            "intake_timing": "after_meal",
            "start_date": "2099-01-01",
            "course_days": "3",
            "reported_doses_taken_before_tracking": "0",
            "selected_plan_time_ids": [
                str(plan_time.plan_time_id)
                for plan_time in plan_times
            ],
            "plan_version": self.plan_version(plan),
        }
        form_data.update(overrides)
        return form_data

    def post_plan_medicine(
        self,
        plan,
        plan_times,
        *,
        user_medicine=None,
        form_data=None,
        token=None,
    ):
        target = user_medicine or self.user_medicine
        url = self.plan_medicine_form_url(plan, target)

        if token is None:
            token = self.get_csrf_token(url)

        payload = form_data or self.valid_plan_medicine_form(
            plan,
            plan_times,
        )
        payload["csrf_token"] = token

        return self.client.post(url, data=payload)

    def plan_schedule_edit_url(self, schedule):
        return f"/medication-schedules/{schedule.schedule_id}/edit"

    def valid_plan_schedule_edit_form(
        self,
        schedule,
        plan,
        plan_times,
        **overrides,
    ):
        db.session.refresh(schedule)
        form_data = {
            "dose_amount_text": "2",
            "dose_unit_text": "정",
            "intake_timing": "before_meal",
            "start_date": "2099-02-01",
            "course_days": "4",
            "reported_doses_taken_before_tracking": "0",
            "selected_plan_time_ids": [
                str(plan_time.plan_time_id)
                for plan_time in plan_times
            ],
            "plan_version": self.plan_version(plan),
            "schedule_version": (
                app_module.serialize_schedule_version(
                    schedule.updated_at
                )
            ),
        }
        form_data.update(overrides)
        return form_data

    def post_plan_schedule_edit(
        self,
        schedule,
        plan,
        plan_times,
        *,
        form_data=None,
        token=None,
    ):
        url = self.plan_schedule_edit_url(schedule)

        if token is None:
            token = self.get_csrf_token("/my-medication-plan")

        payload = dict(
            form_data
            or self.valid_plan_schedule_edit_form(
                schedule,
                plan,
                plan_times,
            )
        )
        payload["csrf_token"] = token
        return self.client.post(url, data=payload)

    def plan_schedule_remove_url(self, schedule):
        return (
            f"/medication-schedules/{schedule.schedule_id}"
            "/remove-from-plan"
        )

    def valid_plan_schedule_remove_form(
        self,
        schedule,
        plan,
        **overrides,
    ):
        db.session.refresh(schedule)
        form_data = {
            "plan_version": self.plan_version(plan),
            "schedule_version": (
                app_module.serialize_schedule_version(
                    schedule.updated_at
                )
            ),
        }
        form_data.update(overrides)
        return form_data

    def post_plan_schedule_remove(
        self,
        schedule,
        plan,
        *,
        form_data=None,
        token=None,
    ):
        if token is None:
            token = self.get_csrf_token("/my-medication-plan")

        payload = dict(
            form_data
            or self.valid_plan_schedule_remove_form(schedule, plan)
        )
        payload["csrf_token"] = token
        return self.client.post(
            self.plan_schedule_remove_url(schedule),
            data=payload,
        )

    def deactivate_url(self, user_medicine=None):
        target = user_medicine or self.user_medicine
        return (
            f"/my-medicines/{target.user_medicine_id}/deactivate"
        )

    def post_deactivate(self, user_medicine=None, *, token=None):
        if token is None:
            token = self.get_csrf_token("/my-medicines")

        return self.client.post(
            self.deactivate_url(user_medicine),
            data={"csrf_token": token},
        )

    def plan_schedule_readd_url(self, schedule):
        return (
            f"/medication-schedules/{schedule.schedule_id}"
            "/readd-to-plan"
        )

    def valid_plan_schedule_readd_form(
        self,
        plan,
        plan_times,
        **overrides,
    ):
        return self.valid_plan_medicine_form(
            plan,
            plan_times,
            **overrides,
        )

    def post_plan_schedule_readd(
        self,
        source_schedule,
        plan,
        plan_times,
        *,
        form_data=None,
        token=None,
    ):
        url = self.plan_schedule_readd_url(source_schedule)

        if token is None:
            token = self.get_csrf_token(url)

        payload = dict(
            form_data
            or self.valid_plan_schedule_readd_form(
                plan,
                plan_times,
            )
        )
        payload["csrf_token"] = token
        return self.client.post(url, data=payload)

    def test_login_required_and_csrf_protection(self):
        response = self.client.get("/my-medication-plan")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        login_token = self.get_csrf_token("/login")
        response = self.client.post(
            "/medication-plans",
            data={"csrf_token": login_token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))

        self.assertEqual(
            self.client.post("/medication-plans").status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/medication-plans/{plan.plan_id}/times"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/medication-plans/"
                f"{plan.plan_id}/times/"
                f"{plan_time.plan_time_id}/delete"
            ).status_code,
            400,
        )

    def test_get_without_plan_is_read_only_and_shows_create_button(self):
        self.log_in()
        before_counts = (
            db.session.query(MedicationPlan).count(),
            db.session.query(MedicationPlanTime).count(),
        )

        response = self.client.get("/my-medication-plan")

        self.assertEqual(response.status_code, 200)
        self.assertIn("아직 만든 복용약 일정이 없습니다.".encode(), response.data)
        self.assertIn("새 복용 일정 만들기".encode(), response.data)
        self.assertEqual(
            before_counts,
            (
                db.session.query(MedicationPlan).count(),
                db.session.query(MedicationPlanTime).count(),
            ),
        )

    def test_plan_creation_and_same_user_duplicate_are_safe(self):
        self.log_in()

        response = self.post_create_plan()

        self.assertEqual(response.status_code, 302)
        plan = db.session.scalar(db.select(MedicationPlan))
        self.assertEqual(plan.user_id, self.user_id)
        self.assertTrue(plan.is_active)

        response = self.post_create_plan()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationPlan).count(), 1)
        page = self.client.get("/my-medication-plan")
        self.assertIn(
            "이미 사용 중인 복용약 일정이 있습니다.".encode(),
            page.data,
        )

    def test_plan_creation_integrity_conflict_is_hidden_and_rolled_back(self):
        self.log_in()
        token = self.get_csrf_token()
        conflict = IntegrityError(
            "internal SQL must stay hidden",
            {},
            Exception("unique conflict"),
        )
        competing_plan = MedicationPlan(
            plan_id=999,
            user_id=self.user_id,
            is_active=True,
        )

        with (
            patch.object(
                app_module,
                "get_current_active_medication_plan",
                side_effect=[None, competing_plan],
            ),
            patch.object(db.session, "commit", side_effect=conflict),
        ):
            response = self.post_create_plan(token=token)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationPlan).count(), 0)
        page = self.client.get("/my-medication-plan")
        self.assertIn(
            "이미 사용 중인 복용약 일정이 있습니다.".encode(),
            page.data,
        )
        self.assertNotIn(b"internal SQL", page.data)

    def test_different_users_can_create_their_own_active_plans(self):
        self.log_in()
        self.post_create_plan()
        self.log_in(self.other_user_id)
        self.post_create_plan()

        plans = db.session.scalars(
            db.select(MedicationPlan).order_by(MedicationPlan.user_id)
        ).all()
        self.assertEqual(len(plans), 2)
        self.assertEqual(
            {plan.user_id for plan in plans},
            {self.user_id, self.other_user_id},
        )

    def test_plan_time_form_uses_native_minute_picker(self):
        self.log_in()
        self.add_plan()

        response = self.client.get("/my-medication-plan")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            '<label for="time_of_day">복용 시간</label>'.encode(),
            response.data,
        )
        input_match = re.search(
            rb'<input\s+[^>]*\bid="time_of_day"[^>]*>',
            response.data,
        )
        self.assertIsNotNone(input_match)
        input_tag = input_match.group(0)
        self.assertIn(b'name="time_of_day"', input_tag)
        self.assertIn(b'type="time"', input_tag)
        self.assertIn(b'step="60"', input_tag)
        self.assertRegex(input_tag, rb'\brequired(?:\s|>)')
        self.assertNotIn(b'placeholder=', input_tag)
        self.assertNotIn(b'inputmode=', input_tag)
        self.assertNotIn(b'pattern=', input_tag)

    def test_valid_times_are_local_wall_clock_values_and_sorted(self):
        self.log_in()
        plan = self.add_plan()

        for value in ["23:59", "14:30", "00:00", "08:00"]:
            with patch.object(
                app_module,
                "utc_now",
                return_value=FIRST_ACTION_TIME,
            ) as mocked_utc_now:
                response = self.post_add_time(plan, value)

            self.assertEqual(response.status_code, 302)
            self.assertEqual(mocked_utc_now.call_count, 1)
            db.session.expire_all()
            plan = db.session.get(MedicationPlan, plan.plan_id)

        stored_values = db.session.scalars(
            db.select(MedicationPlanTime.time_of_day)
            .where(MedicationPlanTime.plan_id == plan.plan_id)
            .order_by(MedicationPlanTime.time_of_day)
        ).all()
        self.assertEqual(
            stored_values,
            [time(0, 0), time(8, 0), time(14, 30), time(23, 59)],
        )
        self.assertTrue(all(value.tzinfo is None for value in stored_values))

        page = self.client.get("/my-medication-plan")
        positions = [
            page.data.index(value.encode())
            for value in ["00:00", "08:00", "14:30", "23:59"]
        ]
        self.assertEqual(positions, sorted(positions))

    def test_invalid_time_formats_are_rejected_without_version_change(self):
        self.log_in()
        plan = self.add_plan()
        original_updated_at = plan.updated_at
        token = self.get_csrf_token()

        for value in ["8:00", "25:00", "14:60", "abc", "", "   "]:
            with self.subTest(value=value):
                response = self.post_add_time(
                    plan,
                    value,
                    token=token,
                )
                self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        stored = db.session.get(MedicationPlan, plan.plan_id)
        self.assertEqual(stored.updated_at, original_updated_at)
        self.assertEqual(db.session.query(MedicationPlanTime).count(), 0)

    def test_duplicate_is_rejected_per_plan_but_allowed_for_another_plan(self):
        self.log_in()
        plan = self.add_plan()
        self.post_add_time(plan, "08:00")
        db.session.expire_all()
        plan = db.session.get(MedicationPlan, plan.plan_id)

        response = self.post_add_time(plan, "08:00")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationPlanTime).count(), 1)
        page = self.client.get("/my-medication-plan")
        self.assertIn("이미 등록된 복용 시간입니다.".encode(), page.data)

        other_plan = self.add_plan(user_id=self.other_user_id)
        self.log_in(self.other_user_id)
        response = self.post_add_time(other_plan, "08:00")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationPlanTime).count(), 2)

    def test_plan_and_plan_time_ownership_mismatches_return_404(self):
        own_plan = self.add_plan()
        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(8, 0))
        self.log_in()
        token = self.get_csrf_token()

        add_response = self.post_add_time(
            other_plan,
            "09:00",
            version=self.plan_version(other_plan),
            token=token,
        )
        delete_response = self.post_delete_time(
            own_plan,
            other_time,
            version=self.plan_version(own_plan),
            token=token,
        )
        missing_plan_response = self.client.post(
            "/medication-plans/999999/times",
            data={
                "csrf_token": token,
                "plan_version": self.plan_version(own_plan),
                "time_of_day": "09:00",
            },
        )
        missing_time_response = self.client.post(
            "/medication-plans/"
            f"{own_plan.plan_id}/times/999999/delete",
            data={
                "csrf_token": token,
                "plan_version": self.plan_version(own_plan),
            },
        )

        self.assertEqual(add_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)
        self.assertEqual(missing_plan_response.status_code, 404)
        self.assertEqual(missing_time_response.status_code, 404)
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, other_time.plan_time_id)
        )

    def test_unused_plan_time_can_be_deleted(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))
        plan_time_id = plan_time.plan_time_id

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ) as mocked_utc_now:
            response = self.post_delete_time(plan, plan_time)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(mocked_utc_now.call_count, 1)
        self.assertIsNone(db.session.get(MedicationPlanTime, plan_time_id))

    def test_active_schedule_association_blocks_deletion(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))
        schedule = self.add_linked_schedule(plan, plan_time, active=True)
        old_updated_at = plan.updated_at

        response = self.post_delete_time(plan, plan_time)

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, plan_time.plan_time_id)
        )
        self.assertIsNotNone(
            db.session.get(
                MedicationSchedulePlanTime,
                (schedule.schedule_id, plan_time.plan_time_id),
            )
        )
        db.session.refresh(plan)
        self.assertEqual(plan.updated_at, old_updated_at)
        page = self.client.get("/my-medication-plan")
        self.assertIn("현재 복용 일정에서 사용 중".encode(), page.data)

    def test_inactive_association_deletion_preserves_time_snapshot(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))
        schedule = self.add_linked_schedule(plan, plan_time, active=False)
        snapshot_id = schedule.times[0].medication_time_id
        schedule_id = schedule.schedule_id
        plan_time_id = plan_time.plan_time_id

        response = self.post_delete_time(plan, plan_time)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(MedicationPlanTime, plan_time_id))
        self.assertIsNone(
            db.session.get(
                MedicationSchedulePlanTime,
                (schedule_id, plan_time_id),
            )
        )
        snapshot = db.session.get(MedicationTime, snapshot_id)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.time_of_day, time(8, 0))
        self.assertEqual(snapshot.schedule_id, schedule_id)
        history_response = self.client.get(
            f"/my-medicines/{self.user_medicine_id}/history"
        )
        self.assertEqual(history_response.status_code, 200)
        self.assertIn(b"08:00", history_response.data)

    def test_get_with_plan_and_times_does_not_change_database(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))
        before = (
            plan.updated_at,
            db.session.query(MedicationPlan).count(),
            db.session.query(MedicationPlanTime).count(),
            plan_time.plan_time_id,
        )

        response = self.client.get("/my-medication-plan")

        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        stored_time = db.session.get(
            MedicationPlanTime,
            plan_time.plan_time_id,
        )
        after = (
            stored_plan.updated_at,
            db.session.query(MedicationPlan).count(),
            db.session.query(MedicationPlanTime).count(),
            stored_time.plan_time_id,
        )
        self.assertEqual(before, after)

    def test_move_plan_time_requires_owned_active_plan_and_matching_time(self):
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(9, 0))

        anonymous_response = self.client.get(
            self.move_plan_time_url(plan, source)
        )
        self.assertEqual(anonymous_response.status_code, 302)

        self.log_in()
        csrf_response = self.client.post(
            self.move_plan_time_url(plan, source),
            data={
                "plan_version": self.plan_version(plan),
                "target_time": "09:00",
            },
        )
        self.assertEqual(csrf_response.status_code, 400)
        self.assertEqual(
            self.client.get(
                self.move_plan_time_url(other_plan, other_time)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                self.move_plan_time_url(plan, other_time)
            ).status_code,
            404,
        )

        plan.is_active = False
        db.session.commit()
        self.assertEqual(
            self.client.get(
                self.move_plan_time_url(plan, source)
            ).status_code,
            404,
        )

    def test_move_plan_time_get_filters_candidates_and_is_read_only(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        active = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        inactive_user_medicine = self.add_user_medicine("INACTIVE")
        inactive = self.add_existing_schedule(
            inactive_user_medicine,
            plan=plan,
            plan_times=(source,),
            active=False,
            medication_times=(time(8, 0),),
        )
        legacy_user_medicine = self.add_user_medicine("LEGACY")
        legacy = self.add_existing_schedule(
            legacy_user_medicine,
            medication_times=(time(8, 0),),
        )
        complete_user_medicine = self.add_user_medicine("COMPLETE")
        complete = self.add_existing_schedule(
            complete_user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
            course_days=1,
            reported=1,
        )
        before = (
            plan.updated_at,
            db.session.query(MedicationPlanTime).count(),
            db.session.query(MedicationSchedule).count(),
        )

        response = self.client.get(
            self.move_plan_time_url(plan, source)
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"08:00", response.data)
        self.assertIn(b'name="target_time"', response.data)
        self.assertIn(b'type="time"', response.data)
        self.assertIn(b'step="60"', response.data)
        self.assertIn(b'required', response.data)
        self.assertIn(
            f'value="{active.schedule_id}"'.encode(),
            response.data,
        )
        self.assertRegex(
            response.data,
            (
                rb'value="'
                + str(active.schedule_id).encode()
                + rb'"[\s\S]*?checked'
            ),
        )
        self.assertNotIn(
            f'value="{inactive.schedule_id}"'.encode(),
            response.data,
        )
        self.assertNotIn(
            f'value="{legacy.schedule_id}"'.encode(),
            response.data,
        )
        self.assertNotIn(
            f'value="{complete.schedule_id}"'.encode(),
            response.data,
        )
        self.assertIn(b'id="select_all_schedules"', response.data)
        self.assertIn(b'id="clear_all_schedules"', response.data)
        db.session.refresh(plan)
        after = (
            plan.updated_at,
            db.session.query(MedicationPlanTime).count(),
            db.session.query(MedicationSchedule).count(),
        )
        self.assertEqual(after, before)

    def test_move_plan_time_redirects_when_there_are_no_candidates(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        before = plan.updated_at

        response = self.client.get(
            self.move_plan_time_url(plan, source)
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/my-medication-plan")
        db.session.refresh(plan)
        self.assertEqual(plan.updated_at, before)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            0,
        )

    def test_move_plan_time_rejects_invalid_form_and_unknown_schedule(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        invalid_cases = (
            ("", [schedule]),
            ("25:00", [schedule]),
            ("08:00", [schedule]),
            ("09:00", []),
            ("09:00", [schedule, schedule]),
        )

        for target_time, schedules in invalid_cases:
            with self.subTest(
                target_time=target_time,
                schedule_count=len(schedules),
            ):
                response = self.post_move_plan_time(
                    plan,
                    source,
                    target_time,
                    schedules,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    db.session.query(MedicationPlanTime).count(),
                    1,
                )
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    1,
                )
                db.session.refresh(schedule)
                self.assertTrue(schedule.is_active)

        response = self.post_move_plan_time(
            plan,
            source,
            "09:00",
            [999999],
            schedule_versions={999999: "unknown"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            1,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )

    def test_move_plan_time_rejects_schedules_outside_candidate_scope(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        other_source = self.add_plan_time(plan, time(12, 0))
        valid = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        inactive_user_medicine = self.add_user_medicine("SCOPE-INACTIVE")
        inactive = self.add_existing_schedule(
            inactive_user_medicine,
            plan=plan,
            plan_times=(source,),
            active=False,
            medication_times=(time(8, 0),),
        )
        no_source_user_medicine = self.add_user_medicine("SCOPE-NO-SOURCE")
        no_source = self.add_existing_schedule(
            no_source_user_medicine,
            plan=plan,
            plan_times=(other_source,),
            medication_times=(time(12, 0),),
        )
        other_owner_user_medicine = self.add_user_medicine(
            "SCOPE-OTHER-OWNER",
            user=self.other_user,
        )
        other_owner = self.add_existing_schedule(
            other_owner_user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        other_plan = self.add_plan(user_id=self.other_user_id)
        other_plan_time = self.add_plan_time(other_plan, time(14, 0))
        other_plan_user_medicine = self.add_user_medicine(
            "SCOPE-OTHER-PLAN"
        )
        other_plan_schedule = self.add_existing_schedule(
            other_plan_user_medicine,
            plan=other_plan,
            plan_times=(other_plan_time,),
            medication_times=(time(14, 0),),
        )

        for invalid_schedule in (
            inactive,
            no_source,
            other_owner,
            other_plan_schedule,
        ):
            with self.subTest(schedule_id=invalid_schedule.schedule_id):
                response = self.post_move_plan_time(
                    plan,
                    source,
                    "09:00",
                    [invalid_schedule],
                )
                self.assertEqual(response.status_code, 404)

        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            3,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            5,
        )
        db.session.refresh(valid)
        self.assertTrue(valid.is_active)

    def test_move_plan_time_reuses_target_and_versions_schedule(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(15, 0))
        target = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            start_date_value=date(2026, 10, 6),
            tracking_started_at=datetime(2026, 10, 6, 0, 0, tzinfo=UTC),
            course_days=5,
            medication_times=(time(15, 0),),
            reported=1,
            accounted=2,
        )
        old_id = schedule.schedule_id
        old_plan_updated_at = plan.updated_at

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_move_plan_time(
                plan,
                source,
                "20:00",
                [schedule],
            )

        self.assertEqual(response.status_code, 302)
        old_schedule = db.session.get(MedicationSchedule, old_id)
        successor = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id == old_id
            )
        )
        self.assertIsNotNone(successor)
        self.assertFalse(old_schedule.is_active)
        self.assertEqual(
            app_module.as_utc(old_schedule.closed_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(
            app_module.as_utc(old_schedule.updated_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(old_schedule.accounted_occurrence_count, 2)
        self.assertEqual(
            [value.time_of_day for value in old_schedule.times],
            [time(15, 0)],
        )
        self.assertEqual(
            [link.plan_time_id for link in old_schedule.plan_time_links],
            [source.plan_time_id],
        )
        self.assertTrue(successor.is_active)
        self.assertIsNone(successor.closed_at)
        self.assertEqual(successor.supersedes_schedule_id, old_id)
        self.assertEqual(successor.accounted_occurrence_count, 3)
        self.assertEqual(
            app_module.as_utc(successor.reminder_tracking_started_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(
            [value.time_of_day for value in successor.times],
            [time(20, 0)],
        )
        self.assertEqual(
            [link.plan_time_id for link in successor.plan_time_links],
            [target.plan_time_id],
        )
        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            2,
        )
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, source.plan_time_id)
        )
        db.session.refresh(plan)
        self.assertNotEqual(plan.updated_at, old_plan_updated_at)
        self.assertEqual(
            app_module.as_utc(plan.updated_at),
            SECOND_ACTION_TIME,
        )

    def test_move_plan_time_creates_one_target_for_selected_schedules(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        second_user_medicine = self.add_user_medicine("MOVE-2")
        third_user_medicine = self.add_user_medicine("MOVE-3")
        first = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        second = self.add_existing_schedule(
            second_user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        unselected = self.add_existing_schedule(
            third_user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_move_plan_time(
                plan,
                source,
                "09:30",
                [first, second],
            )

        self.assertEqual(response.status_code, 302)
        targets = db.session.scalars(
            db.select(MedicationPlanTime).where(
                MedicationPlanTime.plan_id == plan.plan_id,
                MedicationPlanTime.time_of_day == time(9, 30),
            )
        ).all()
        self.assertEqual(len(targets), 1)
        target = targets[0]

        for old_schedule in (first, second):
            stored_old = db.session.get(
                MedicationSchedule,
                old_schedule.schedule_id,
            )
            successor = db.session.scalar(
                db.select(MedicationSchedule).where(
                    MedicationSchedule.supersedes_schedule_id
                    == old_schedule.schedule_id
                )
            )
            self.assertFalse(stored_old.is_active)
            self.assertEqual(
                [
                    link.plan_time_id
                    for link in stored_old.plan_time_links
                ],
                [source.plan_time_id],
            )
            self.assertIsNotNone(successor)
            self.assertTrue(successor.is_active)
            self.assertEqual(
                [
                    link.plan_time_id
                    for link in successor.plan_time_links
                ],
                [target.plan_time_id],
            )
            self.assertEqual(
                [value.time_of_day for value in successor.times],
                [time(9, 30)],
            )

        db.session.refresh(unselected)
        self.assertTrue(unselected.is_active)
        self.assertIsNone(
            db.session.scalar(
                db.select(MedicationSchedule).where(
                    MedicationSchedule.supersedes_schedule_id
                    == unselected.schedule_id
                )
            )
        )
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, source.plan_time_id)
        )

    def test_move_plan_time_rejects_target_already_used_by_selection(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        target = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source, target),
            medication_times=(time(8, 0), time(20, 0)),
        )
        before = (plan.updated_at, schedule.updated_at)

        response = self.post_move_plan_time(
            plan,
            source,
            "20:00",
            [schedule],
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )
        db.session.refresh(plan)
        db.session.refresh(schedule)
        self.assertEqual((plan.updated_at, schedule.updated_at), before)
        self.assertTrue(schedule.is_active)
        self.assertEqual(
            sorted(value.time_of_day for value in schedule.times),
            [time(8, 0), time(20, 0)],
        )

    def test_move_plan_time_rejects_effectively_complete_race(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        movable = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        complete_user_medicine = self.add_user_medicine("RACE-COMPLETE")
        complete = self.add_existing_schedule(
            complete_user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
            course_days=1,
            reported=1,
        )

        page = self.client.get(self.move_plan_time_url(plan, source))
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            f'value="{movable.schedule_id}"'.encode(),
            page.data,
        )
        self.assertNotIn(
            f'value="{complete.schedule_id}"'.encode(),
            page.data,
        )

        response = self.post_move_plan_time(
            plan,
            source,
            "09:00",
            [complete],
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            1,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            2,
        )
        db.session.refresh(movable)
        db.session.refresh(complete)
        self.assertTrue(movable.is_active)
        self.assertTrue(complete.is_active)

    def test_move_plan_time_rolls_back_when_second_schedule_claim_fails(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        second_user_medicine = self.add_user_medicine("CLAIM-2")
        first = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        second = self.add_existing_schedule(
            second_user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        old_plan_updated_at = plan.updated_at
        original_claim = app_module.claim_medication_schedule_edit
        claim_count = 0

        def fail_second_claim(*args, **kwargs):
            nonlocal claim_count
            claim_count += 1
            if claim_count == 2:
                return False
            return original_claim(*args, **kwargs)

        with patch.object(
            app_module,
            "claim_medication_schedule_edit",
            side_effect=fail_second_claim,
        ), patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_move_plan_time(
                plan,
                source,
                "09:00",
                [first, second],
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(claim_count, 2)
        db.session.expire_all()
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        self.assertEqual(stored_plan.updated_at, old_plan_updated_at)
        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            1,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            2,
        )
        self.assertTrue(
            db.session.get(
                MedicationSchedule,
                first.schedule_id,
            ).is_active
        )
        self.assertTrue(
            db.session.get(
                MedicationSchedule,
                second.schedule_id,
            ).is_active
        )

    def test_move_plan_time_extends_immediate_version_chain(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        first = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=FIRST_ACTION_TIME + timedelta(seconds=1),
        ):
            first_response = self.post_move_plan_time(
                plan,
                source,
                "09:00",
                [first],
            )
        self.assertEqual(first_response.status_code, 302)
        second = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id
                == first.schedule_id
            )
        )
        middle_time = db.session.scalar(
            db.select(MedicationPlanTime).where(
                MedicationPlanTime.plan_id == plan.plan_id,
                MedicationPlanTime.time_of_day == time(9, 0),
            )
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            second_response = self.post_move_plan_time(
                plan,
                middle_time,
                "10:00",
                [second],
            )

        self.assertEqual(second_response.status_code, 302)
        third = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id
                == second.schedule_id
            )
        )
        self.assertIsNotNone(third)
        self.assertEqual(
            second.supersedes_schedule_id,
            first.schedule_id,
        )
        self.assertEqual(
            third.supersedes_schedule_id,
            second.schedule_id,
        )
        self.assertNotEqual(
            third.supersedes_schedule_id,
            first.schedule_id,
        )
        self.assertEqual(
            [value.time_of_day for value in third.times],
            [time(10, 0)],
        )

    def test_move_plan_time_rejects_stale_plan_and_schedule_versions(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )

        stale_plan_response = self.post_move_plan_time(
            plan,
            source,
            "09:00",
            [schedule],
            plan_version="stale",
        )
        stale_schedule_response = self.post_move_plan_time(
            plan,
            source,
            "09:00",
            [schedule],
            schedule_versions={schedule.schedule_id: "stale"},
        )

        self.assertEqual(stale_plan_response.status_code, 302)
        self.assertEqual(stale_schedule_response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            1,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )
        db.session.refresh(schedule)
        self.assertTrue(schedule.is_active)
        self.assertIsNone(schedule.closed_at)

    def test_move_plan_time_commit_failure_rolls_back_every_stage(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        old_plan_updated_at = plan.updated_at
        failure = SQLAlchemyError("commit failed")

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ), patch.object(db.session, "commit", side_effect=failure):
            response = self.post_move_plan_time(
                plan,
                source,
                "09:00",
                [schedule],
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            1,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            1,
        )
        db.session.refresh(plan)
        db.session.refresh(schedule)
        self.assertEqual(plan.updated_at, old_plan_updated_at)
        self.assertTrue(schedule.is_active)
        self.assertIsNone(schedule.closed_at)

    def test_move_history_survives_later_source_time_deletion(self):
        self.log_in()
        plan = self.add_plan()
        source = self.add_plan_time(plan, time(8, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(source,),
            medication_times=(time(8, 0),),
        )
        old_snapshot_id = schedule.times[0].medication_time_id

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            move_response = self.post_move_plan_time(
                plan,
                source,
                "09:00",
                [schedule],
            )
        self.assertEqual(move_response.status_code, 302)

        history_before_delete = self.client.get(
            f"/my-medicines/{self.user_medicine_id}/history"
        )
        self.assertEqual(history_before_delete.status_code, 200)
        self.assertIn(b"08:00", history_before_delete.data)
        self.assertIn(b"09:00", history_before_delete.data)

        delete_response = self.post_delete_time(plan, source)
        self.assertEqual(delete_response.status_code, 302)
        self.assertIsNone(
            db.session.get(MedicationPlanTime, source.plan_time_id)
        )
        old_snapshot = db.session.get(MedicationTime, old_snapshot_id)
        self.assertIsNotNone(old_snapshot)
        self.assertEqual(old_snapshot.time_of_day, time(8, 0))

        history_after_delete = self.client.get(
            f"/my-medicines/{self.user_medicine_id}/history"
        )
        self.assertEqual(history_after_delete.status_code, 200)
        self.assertIn(b"08:00", history_after_delete.data)
        self.assertIn(b"09:00", history_after_delete.data)

    def test_stale_plan_version_rejects_add_and_delete(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))
        stale_version = self.plan_version(plan)
        plan.updated_at = app_module.as_utc(plan.updated_at) + timedelta(
            seconds=1
        )
        db.session.commit()

        add_response = self.post_add_time(
            plan,
            "09:00",
            version=stale_version,
        )
        delete_response = self.post_delete_time(
            plan,
            plan_time,
            version=stale_version,
        )

        self.assertEqual(add_response.status_code, 302)
        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(db.session.query(MedicationPlanTime).count(), 1)
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, plan_time.plan_time_id)
        )

    def test_atomic_claim_rowcount_zero_rejects_add_and_delete(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))

        with patch.object(
            app_module,
            "claim_medication_plan_change",
            return_value=False,
        ) as claim:
            add_response = self.post_add_time(plan, "09:00")
            delete_response = self.post_delete_time(plan, plan_time)

        self.assertEqual(add_response.status_code, 302)
        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(claim.call_count, 2)
        self.assertEqual(db.session.query(MedicationPlanTime).count(), 1)

    def test_concurrent_duplicate_integrity_error_is_safe(self):
        self.log_in()
        plan = self.add_plan()
        existing = self.add_plan_time(plan, time(8, 0))
        version = self.plan_version(plan)
        token = self.get_csrf_token()

        with patch.object(
            db.session,
            "scalar",
            side_effect=[plan, None, existing.plan_time_id],
        ):
            response = self.post_add_time(
                plan,
                "08:00",
                version=version,
                token=token,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationPlanTime).count(), 1)
        page = self.client.get("/my-medication-plan")
        self.assertIn("이미 등록된 복용 시간입니다.".encode(), page.data)
        self.assertNotIn(b"UNIQUE constraint", page.data)

    def test_add_failure_rolls_back_plan_claim_and_insert(self):
        self.log_in()
        plan = self.add_plan()
        old_updated_at = plan.updated_at

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced insert failure"),
        ):
            response = self.post_add_time(plan, "08:00")

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        self.assertEqual(stored_plan.updated_at, old_updated_at)
        self.assertEqual(db.session.query(MedicationPlanTime).count(), 0)

    def test_delete_failure_rolls_back_plan_claim_and_delete(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))
        old_updated_at = plan.updated_at

        with patch.object(
            db.session,
            "delete",
            side_effect=SQLAlchemyError("forced delete failure"),
        ):
            response = self.post_delete_time(plan, plan_time)

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        self.assertEqual(stored_plan.updated_at, old_updated_at)
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, plan_time.plan_time_id)
        )

    def test_plan_ui_navigation_and_scope_are_minimal(self):
        self.log_in()
        plan = self.add_plan()
        self.add_plan_time(plan, time(8, 0))

        plan_page = self.client.get("/my-medication-plan")
        medicines_page = self.client.get("/my-medicines")

        self.assertEqual(plan_page.status_code, 200)
        self.assertIn("내 복용약 일정".encode(), plan_page.data)
        self.assertIn("복용 시간".encode(), plan_page.data)
        self.assertIn("+ 복용 시간 추가".encode(), plan_page.data)
        self.assertNotIn(self.medicine.item_name.encode(), plan_page.data)
        self.assertIn(b'href="/my-medication-plan"', medicines_page.data)

    def test_plan_actions_do_not_change_legacy_schedule_state(self):
        self.log_in()
        plan = self.add_plan()
        self.post_add_time(plan, "08:00")

        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)
        self.assertEqual(
            db.session.query(MedicationSchedulePlanTime).count(),
            0,
        )

    def test_edit_and_plan_deletion_routes_do_not_exist(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(8, 0))

        self.assertEqual(
            self.client.post(
                f"/medication-plans/{plan.plan_id}/delete"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/medication-plans/"
                f"{plan.plan_id}/times/{plan_time.plan_time_id}/edit"
            ).status_code,
            404,
        )

    def test_medicine_selection_requires_login_owner_and_plan_time(self):
        plan = self.add_plan()
        other_plan = self.add_plan(user_id=self.other_user_id)

        response = self.client.get(self.select_medicine_url(plan))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        self.log_in()
        self.assertEqual(
            self.client.get(self.select_medicine_url(other_plan)).status_code,
            404,
        )

        selection_response = self.client.get(
            self.select_medicine_url(plan)
        )
        form_response = self.client.get(
            self.plan_medicine_form_url(plan)
        )
        self.assertEqual(selection_response.status_code, 302)
        self.assertEqual(form_response.status_code, 302)
        page = self.client.get("/my-medication-plan")
        self.assertIn(
            "먼저 복용 시간을 하나 이상 추가해주세요.".encode(),
            page.data,
        )

    def test_plan_medicine_post_requires_login_and_csrf(self):
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        login_token = self.get_csrf_token("/login")
        form_data = self.valid_plan_medicine_form(plan, (plan_time,))
        form_data["csrf_token"] = login_token

        response = self.client.post(
            self.plan_medicine_form_url(plan),
            data=form_data,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        self.log_in()
        response = self.client.post(
            self.plan_medicine_form_url(plan),
            data=self.valid_plan_medicine_form(plan, (plan_time,)),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)

    def test_medicine_selection_classifies_current_states(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            medication_times=(time(20, 0),),
        )
        ongoing = self.add_user_medicine("ONGOING")
        self.add_existing_schedule(ongoing)
        another_ongoing = self.add_user_medicine("ANOTHER-ONGOING")
        self.add_existing_schedule(another_ongoing)
        available = self.add_user_medicine("AVAILABLE")
        inactive = self.add_user_medicine("INACTIVE", active=False)

        response = self.client.get(self.select_medicine_url(plan))

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.medicine.item_name.encode(), response.data)
        self.assertIn("이미 일정에 추가됨".encode(), response.data)
        self.assertIn(ongoing.medicine.item_name.encode(), response.data)
        self.assertIn("현재 복용 설정 사용 중".encode(), response.data)
        self.assertIn(
            another_ongoing.medicine.item_name.encode(),
            response.data,
        )
        self.assertIn(available.medicine.item_name.encode(), response.data)
        self.assertNotIn(inactive.medicine.item_name.encode(), response.data)
        self.assertEqual(response.data.count("일정에 추가</a>".encode()), 1)

        schedule_count = db.session.query(MedicationSchedule).count()
        direct_response = self.client.post(
            self.plan_medicine_form_url(plan),
            data={
                **self.valid_plan_medicine_form(plan, (plan_time,)),
                "csrf_token": self.get_csrf_token(),
            },
        )
        self.assertEqual(direct_response.status_code, 302)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            schedule_count,
        )

    def test_plan_selection_lists_active_medicine_without_schedule(self):
        self.log_in()
        plan = self.add_plan()
        self.add_plan_time(plan, time(20, 0))
        available = self.add_user_medicine("AVAILABLE-WITHOUT-SCHEDULE")

        response = self.client.get(self.select_medicine_url(plan))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            self.plan_medicine_form_url(plan, available).encode(),
            response.data,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule)
            .filter_by(user_medicine_id=available.user_medicine_id)
            .count(),
            0,
        )

    def test_plan_medicine_form_enforces_user_medicine_ownership(self):
        self.log_in()
        plan = self.add_plan()
        self.add_plan_time(plan, time(20, 0))
        inactive = self.add_user_medicine("INACTIVE", active=False)

        self.assertEqual(
            self.client.get(
                self.plan_medicine_form_url(
                    plan,
                    self.other_user_medicine,
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                self.plan_medicine_form_url(plan, inactive)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/medication-plans/999999/medicines/"
                f"{self.user_medicine_id}/new"
            ).status_code,
            404,
        )

    def test_plan_medicine_form_get_is_sorted_versioned_and_read_only(self):
        self.log_in()
        plan = self.add_plan()
        late = self.add_plan_time(plan, time(20, 0))
        early = self.add_plan_time(plan, time(8, 0))
        before = (
            plan.updated_at,
            db.session.query(MedicationSchedule).count(),
            db.session.query(MedicationTime).count(),
            db.session.query(MedicationSchedulePlanTime).count(),
            self.user_medicine.is_active,
        )

        response = self.client.get(self.plan_medicine_form_url(plan))

        self.assertEqual(response.status_code, 200)
        self.assertLess(response.data.index(b"08:00"), response.data.index(b"20:00"))
        self.assertIn(b'name="plan_version"', response.data)
        self.assertIn(b'name="dose_amount_text"', response.data)
        self.assertIn(b'value="regardless_of_meal"', response.data)
        self.assertIn(b'value="1"', response.data)
        self.assertIn(b'value="0"', response.data)
        db.session.expire_all()
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        after = (
            stored_plan.updated_at,
            db.session.query(MedicationSchedule).count(),
            db.session.query(MedicationTime).count(),
            db.session.query(MedicationSchedulePlanTime).count(),
            stored_user_medicine.is_active,
        )
        self.assertEqual(before, after)
        self.assertIsNotNone(early.plan_time_id)
        self.assertIsNotNone(late.plan_time_id)

    def test_plan_medicine_detail_validation_reuses_legacy_rules(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        token = self.get_csrf_token(
            self.plan_medicine_form_url(plan)
        )
        invalid_overrides = (
            {"dose_amount_text": "0"},
            {"dose_amount_text": "abc"},
            {"dose_unit_text": "mL"},
            {"intake_timing": "invalid"},
            {"start_date": "invalid"},
            {"course_days": "0"},
            {"reported_doses_taken_before_tracking": "-1"},
        )

        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                form_data = self.valid_plan_medicine_form(
                    plan,
                    (plan_time,),
                    **overrides,
                )
                response = self.post_plan_medicine(
                    plan,
                    (plan_time,),
                    form_data=form_data,
                    token=token,
                )
                self.assertEqual(response.status_code, 400)

        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)
        self.assertEqual(db.session.query(MedicationTime).count(), 0)
        self.assertEqual(
            db.session.query(MedicationSchedulePlanTime).count(),
            0,
        )

    def test_valid_decimal_units_timings_and_start_dates_are_stored(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        cases = (
            (self.user_medicine, "0.5", "정", "before_meal", "2026-10-05"),
            (
                self.add_user_medicine("VALID-2"),
                "1",
                "캡슐",
                "after_meal",
                "2026-10-06",
            ),
            (
                self.add_user_medicine("VALID-3"),
                "2.25",
                "포",
                "regardless_of_meal",
                "2026-10-07",
            ),
        )

        for index, (user_medicine, dose, unit, timing, start) in enumerate(cases):
            with self.subTest(unit=unit, timing=timing, start=start):
                db.session.expire_all()
                plan = db.session.get(MedicationPlan, plan.plan_id)
                form_data = self.valid_plan_medicine_form(
                    plan,
                    (plan_time,),
                    dose_amount_text=dose,
                    dose_unit_text=unit,
                    intake_timing=timing,
                    start_date=start,
                    course_days="3",
                )
                with patch.object(
                    app_module,
                    "utc_now",
                    return_value=FIRST_ACTION_TIME + timedelta(minutes=index),
                ):
                    response = self.post_plan_medicine(
                        plan,
                        (plan_time,),
                        user_medicine=user_medicine,
                        form_data=form_data,
                    )

                self.assertEqual(response.status_code, 302)

        schedules = db.session.scalars(
            db.select(MedicationSchedule).order_by(
                MedicationSchedule.schedule_id
            )
        ).all()
        self.assertEqual(len(schedules), 3)
        self.assertEqual(
            [schedule.dose_amount_text for schedule in schedules],
            ["0.5", "1", "2.25"],
        )
        self.assertEqual(
            [schedule.dose_unit_text for schedule in schedules],
            ["정", "캡슐", "포"],
        )
        self.assertEqual(
            [schedule.intake_timing for schedule in schedules],
            ["before_meal", "after_meal", "regardless_of_meal"],
        )
        planned_totals = [
            app_module.calculate_stored_schedule_summary(
                schedule,
                reference_at=FIRST_ACTION_TIME + timedelta(days=1),
                timezone_name=self.user.timezone,
            ).planned_total
            for schedule in schedules
        ]
        self.assertEqual(planned_totals, [3, 3, 3])

    def test_selected_plan_time_ids_are_strictly_validated(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(8, 0))
        token = self.get_csrf_token(
            self.plan_medicine_form_url(plan)
        )
        invalid_id_lists = (
            [],
            [str(other_time.plan_time_id)],
            ["999999"],
            [str(plan_time.plan_time_id), str(plan_time.plan_time_id)],
            ["not-an-id"],
        )

        for selected_ids in invalid_id_lists:
            with self.subTest(selected_ids=selected_ids):
                form_data = self.valid_plan_medicine_form(
                    plan,
                    (plan_time,),
                    selected_plan_time_ids=selected_ids,
                )
                response = self.post_plan_medicine(
                    plan,
                    (plan_time,),
                    form_data=form_data,
                    token=token,
                )
                self.assertEqual(response.status_code, 400)

        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)

    def test_stale_plan_version_blocks_plan_medicine_creation(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        stale_form = self.valid_plan_medicine_form(plan, (plan_time,))
        plan.updated_at = app_module.as_utc(plan.updated_at) + timedelta(
            seconds=1
        )
        self.add_plan_time(plan, time(8, 0))
        db.session.commit()

        response = self.post_plan_medicine(
            plan,
            (plan_time,),
            form_data=stale_form,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)

    def test_plan_medicine_creation_writes_schedule_snapshot_and_links(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        form_data = self.valid_plan_medicine_form(
            plan,
            (morning, evening),
            dose_amount_text="0.5",
            dose_unit_text="정",
            intake_timing="after_meal",
            start_date="2099-01-01",
            course_days="3",
            reported_doses_taken_before_tracking="1",
        )
        token = self.get_csrf_token(
            self.plan_medicine_form_url(plan)
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ) as mocked_utc_now:
            response = self.post_plan_medicine(
                plan,
                (morning, evening),
                form_data=form_data,
                token=token,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(mocked_utc_now.call_count, 1)
        schedule = db.session.scalar(db.select(MedicationSchedule))
        self.assertEqual(schedule.plan_id, plan.plan_id)
        self.assertEqual(schedule.user_medicine_id, self.user_medicine_id)
        self.assertEqual(schedule.dose_amount_text, "0.5")
        self.assertEqual(schedule.dose_unit_text, "정")
        self.assertEqual(schedule.intake_timing, "after_meal")
        self.assertEqual(schedule.start_date, date(2099, 1, 1))
        self.assertEqual(schedule.course_days, 3)
        self.assertEqual(schedule.reported_doses_taken_before_tracking, 1)
        self.assertEqual(schedule.accounted_occurrence_count, 0)
        self.assertEqual(
            app_module.as_utc(schedule.reminder_tracking_started_at),
            SECOND_ACTION_TIME,
        )
        self.assertIsNone(schedule.closed_at)
        self.assertIsNone(schedule.supersedes_schedule_id)
        self.assertIsNone(schedule.end_date)
        self.assertTrue(schedule.is_active)
        self.assertTrue(
            all(
                getattr(schedule, weekday)
                for weekday in (
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                )
            )
        )
        snapshot_times = sorted(
            medication_time.time_of_day
            for medication_time in schedule.times
        )
        linked_times = sorted(
            link.plan_time.time_of_day
            for link in schedule.plan_time_links
        )
        self.assertEqual(snapshot_times, [time(8, 0), time(20, 0)])
        self.assertEqual(linked_times, snapshot_times)
        self.assertEqual(len(schedule.plan_time_links), 2)
        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)
        db.session.refresh(plan)
        self.assertEqual(
            app_module.as_utc(plan.updated_at),
            SECOND_ACTION_TIME,
        )
        summary = app_module.calculate_stored_schedule_summary(
            schedule,
            reference_at=SECOND_ACTION_TIME,
            timezone_name=self.user.timezone,
        )
        self.assertEqual(summary.planned_total, 6)
        self.assertEqual(summary.remaining, 5)

    def test_remaining_zero_is_rejected_without_database_changes(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        form_data = self.valid_plan_medicine_form(
            plan,
            (plan_time,),
            course_days="1",
            reported_doses_taken_before_tracking="1",
        )
        old_updated_at = plan.updated_at

        response = self.post_plan_medicine(
            plan,
            (plan_time,),
            form_data=form_data,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("남은 예정 복용 횟수".encode(), response.data)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)
        db.session.refresh(plan)
        self.assertEqual(plan.updated_at, old_updated_at)

    def test_running_legacy_schedule_blocks_plan_creation(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
        )
        old_snapshot_ids = [
            medication_time.medication_time_id
            for medication_time in old_schedule.times
        ]
        token = self.get_csrf_token()
        form_data = self.valid_plan_medicine_form(plan, (plan_time,))

        response = self.post_plan_medicine(
            plan,
            (plan_time,),
            form_data=form_data,
            token=token,
        )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(old_schedule)
        self.assertTrue(old_schedule.is_active)
        self.assertIsNone(old_schedule.closed_at)
        self.assertEqual(
            [value.medication_time_id for value in old_schedule.times],
            old_snapshot_ids,
        )
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)

    def test_expired_legacy_schedule_is_closed_before_plan_creation(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        old_snapshot_id = old_schedule.times[0].medication_time_id

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_plan_medicine(
                plan,
                (plan_time,),
            )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_old = db.session.get(
            MedicationSchedule,
            old_schedule.schedule_id,
        )
        self.assertFalse(stored_old.is_active)
        self.assertEqual(
            app_module.as_utc(stored_old.closed_at),
            SECOND_ACTION_TIME,
        )
        self.assertIsNotNone(db.session.get(MedicationTime, old_snapshot_id))
        self.assertEqual(db.session.query(MedicationSchedule).count(), 2)

    def test_pending_future_legacy_schedule_blocks_plan_addition(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        token = self.get_csrf_token()
        form_data = self.valid_plan_medicine_form(plan, (plan_time,))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
        )

        response = self.post_plan_medicine(
            plan,
            (plan_time,),
            form_data=form_data,
            token=token,
        )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(old_schedule)
        self.assertTrue(old_schedule.is_active)
        self.assertIsNone(old_schedule.closed_at)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)

    def test_running_plan_bound_schedule_blocks_even_when_pending(self):
        self.log_in()
        current_plan = self.add_plan()
        current_time = self.add_plan_time(current_plan, time(20, 0))
        old_plan = MedicationPlan(
            user_id=self.user_id,
            is_active=False,
            created_at=FIRST_ACTION_TIME,
            updated_at=FIRST_ACTION_TIME,
        )
        db.session.add(old_plan)
        db.session.commit()
        old_time = self.add_plan_time(old_plan, time(8, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=old_plan,
            plan_times=(old_time,),
            medication_times=(time(8, 0),),
        )
        token = self.get_csrf_token()
        form_data = self.valid_plan_medicine_form(
            current_plan,
            (current_time,),
        )

        response = self.post_plan_medicine(
            current_plan,
            (current_time,),
            form_data=form_data,
            token=token,
        )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(old_schedule)
        self.assertTrue(old_schedule.is_active)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)

    def test_schedule_stage_failures_roll_back_plan_and_new_rows(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))

        for failure_at in (1, 2, 3):
            with self.subTest(failure_at=failure_at):
                db.session.expire_all()
                plan = db.session.get(MedicationPlan, plan.plan_id)
                old_updated_at = plan.updated_at
                original_flush = db.session.flush
                flush_count = 0

                def fail_selected_flush(*args, **kwargs):
                    nonlocal flush_count
                    flush_count += 1

                    if flush_count == failure_at:
                        raise SQLAlchemyError("forced staged failure")

                    return original_flush(*args, **kwargs)

                with patch.object(
                    db.session,
                    "flush",
                    side_effect=fail_selected_flush,
                ):
                    response = self.post_plan_medicine(
                        plan,
                        (plan_time,),
                    )

                self.assertEqual(response.status_code, 302)
                db.session.expire_all()
                plan = db.session.get(MedicationPlan, plan.plan_id)
                self.assertEqual(plan.updated_at, old_updated_at)
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    0,
                )
                self.assertEqual(db.session.query(MedicationTime).count(), 0)
                self.assertEqual(
                    db.session.query(MedicationSchedulePlanTime).count(),
                    0,
                )
                db.session.refresh(self.user_medicine)
                self.assertTrue(self.user_medicine.is_active)

    def test_commit_failure_restores_old_schedule_and_plan(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
            medication_times=(time(8, 0),),
        )
        old_updated_at = plan.updated_at
        old_time_id = old_schedule.times[0].medication_time_id

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced commit failure"),
        ):
            response = self.post_plan_medicine(plan, (plan_time,))

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_old = db.session.get(
            MedicationSchedule,
            old_schedule.schedule_id,
        )
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertTrue(stored_old.is_active)
        self.assertIsNone(stored_old.closed_at)
        self.assertIsNotNone(db.session.get(MedicationTime, old_time_id))
        self.assertTrue(stored_user_medicine.is_active)
        self.assertEqual(stored_plan.updated_at, old_updated_at)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)

    def test_plan_medicine_atomic_claim_and_integrity_conflicts_are_safe(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))

        with patch.object(
            app_module,
            "claim_medication_plan_change",
            return_value=False,
        ):
            claim_response = self.post_plan_medicine(plan, (plan_time,))

        self.assertEqual(claim_response.status_code, 302)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)

        conflict = IntegrityError(
            "internal unique SQL",
            {},
            Exception("unique conflict"),
        )

        with patch.object(db.session, "commit", side_effect=conflict):
            conflict_response = self.post_plan_medicine(
                plan,
                (plan_time,),
            )

        self.assertEqual(conflict_response.status_code, 302)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 0)
        page = self.client.get("/my-medication-plan")
        self.assertNotIn(b"internal unique SQL", page.data)

    def test_plan_page_groups_active_medicines_without_n_plus_one(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        noon = self.add_plan_time(plan, time(14, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        first = self.user_medicine
        second = self.add_user_medicine("GROUP-2")
        hidden = self.add_user_medicine("GROUP-HIDDEN", active=False)
        self.add_existing_schedule(
            first,
            plan=plan,
            plan_times=(morning, evening),
            medication_times=(time(8, 0), time(20, 0)),
        )
        self.add_existing_schedule(
            second,
            plan=plan,
            plan_times=(morning, noon, evening),
            medication_times=(time(8, 0), time(14, 0), time(20, 0)),
        )
        self.add_existing_schedule(
            hidden,
            plan=plan,
            plan_times=(noon,),
            medication_times=(time(14, 0),),
        )
        select_count = 0

        def count_selects(conn, cursor, statement, parameters, context, many):
            nonlocal select_count

            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(
            self.test_engine,
            "before_cursor_execute",
            count_selects,
        )

        try:
            response = self.client.get("/my-medication-plan")
        finally:
            event.remove(
                self.test_engine,
                "before_cursor_execute",
                count_selects,
            )

        self.assertEqual(response.status_code, 200)
        first_items = re.findall(
            rb'<li data-schedule-id="\d+">\s*'
            + re.escape(first.medicine.item_name.encode())
            + rb"\s*</li>",
            response.data,
        )
        second_items = re.findall(
            rb'<li data-schedule-id="\d+">\s*'
            + re.escape(second.medicine.item_name.encode())
            + rb"\s*</li>",
            response.data,
        )
        hidden_items = re.findall(
            rb'<li data-schedule-id="\d+">\s*'
            + re.escape(hidden.medicine.item_name.encode())
            + rb"\s*</li>",
            response.data,
        )
        self.assertEqual(len(first_items), 2)
        self.assertEqual(len(second_items), 3)
        self.assertEqual(hidden_items, [])
        self.assertLessEqual(select_count, 7)

    def test_created_active_association_keeps_plan_time_delete_blocked(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        self.post_plan_medicine(plan, (plan_time,))
        db.session.expire_all()
        plan = db.session.get(MedicationPlan, plan.plan_id)
        plan_time = db.session.get(
            MedicationPlanTime,
            plan_time.plan_time_id,
        )

        response = self.post_delete_time(plan, plan_time)

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, plan_time.plan_time_id)
        )

    def test_plan_schedule_edit_get_uses_links_and_is_read_only(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(morning,),
            medication_times=(time(20, 0),),
        )
        before = (
            db.session.query(MedicationSchedule).count(),
            db.session.query(MedicationTime).count(),
            db.session.query(MedicationSchedulePlanTime).count(),
            schedule.updated_at,
            plan.updated_at,
        )

        response = self.client.get(self.plan_schedule_edit_url(schedule))

        self.assertEqual(response.status_code, 200)
        self.assertIn("복용 일정 수정".encode(), response.data)
        self.assertIn(b'name="plan_version"', response.data)
        self.assertIn(b'name="schedule_version"', response.data)
        morning_pattern = (
            rb'value="' + str(morning.plan_time_id).encode()
            + rb'"\s+checked'
        )
        evening_pattern = (
            rb'value="' + str(evening.plan_time_id).encode()
            + rb'"\s+checked'
        )
        self.assertRegex(response.data, morning_pattern)
        self.assertNotRegex(response.data, evening_pattern)
        db.session.refresh(schedule)
        db.session.refresh(plan)
        after = (
            db.session.query(MedicationSchedule).count(),
            db.session.query(MedicationTime).count(),
            db.session.query(MedicationSchedulePlanTime).count(),
            schedule.updated_at,
            plan.updated_at,
        )
        self.assertEqual(after, before)

    def test_plan_schedule_edit_creates_an_immutable_successor(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(morning,),
            medication_times=(time(8, 0),),
            accounted=2,
        )
        old_id = schedule.schedule_id
        old_values = (
            schedule.dose_amount_text,
            schedule.intake_timing,
            schedule.start_date,
            schedule.course_days,
            schedule.reported_doses_taken_before_tracking,
            schedule.accounted_occurrence_count,
        )
        form_data = self.valid_plan_schedule_edit_form(
            schedule,
            plan,
            (evening,),
            reported_doses_taken_before_tracking="1",
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_plan_schedule_edit(
                schedule,
                plan,
                (evening,),
                form_data=form_data,
            )

        self.assertEqual(response.status_code, 302)
        old_schedule = db.session.get(MedicationSchedule, old_id)
        successor = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id == old_id
            )
        )
        self.assertIsNotNone(successor)
        self.assertFalse(old_schedule.is_active)
        self.assertEqual(
            app_module.as_utc(old_schedule.closed_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(
            (
                old_schedule.dose_amount_text,
                old_schedule.intake_timing,
                old_schedule.start_date,
                old_schedule.course_days,
                old_schedule.reported_doses_taken_before_tracking,
                old_schedule.accounted_occurrence_count,
            ),
            old_values,
        )
        self.assertEqual(
            [value.time_of_day for value in old_schedule.times],
            [time(8, 0)],
        )
        self.assertEqual(
            [link.plan_time_id for link in old_schedule.plan_time_links],
            [morning.plan_time_id],
        )
        self.assertEqual(successor.user_medicine_id, schedule.user_medicine_id)
        self.assertEqual(successor.plan_id, plan.plan_id)
        self.assertEqual(successor.supersedes_schedule_id, old_id)
        self.assertEqual(successor.dose_amount_text, "2")
        self.assertEqual(successor.reported_doses_taken_before_tracking, 1)
        self.assertEqual(successor.accounted_occurrence_count, 2)
        self.assertTrue(successor.is_active)
        self.assertEqual(
            [value.time_of_day for value in successor.times],
            [time(20, 0)],
        )
        self.assertEqual(
            [link.plan_time_id for link in successor.plan_time_links],
            [evening.plan_time_id],
        )
        db.session.refresh(self.user_medicine)
        self.assertTrue(self.user_medicine.is_active)

    def test_plan_schedule_edit_accumulates_only_elapsed_segment(self):
        self.log_in()
        plan = self.add_plan()
        afternoon = self.add_plan_time(plan, time(15, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(afternoon,),
            start_date_value=date(2026, 10, 6),
            tracking_started_at=datetime(2026, 10, 6, 0, 0, tzinfo=UTC),
            course_days=5,
            medication_times=(time(15, 0),),
            reported=1,
            accounted=2,
        )
        form_data = self.valid_plan_schedule_edit_form(
            schedule,
            plan,
            (afternoon,),
            start_date="2026-10-06",
            course_days="8",
            reported_doses_taken_before_tracking="1",
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            self.post_plan_schedule_edit(
                schedule,
                plan,
                (afternoon,),
                form_data=form_data,
            )

        successor = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id
                == schedule.schedule_id
            )
        )
        self.assertEqual(successor.accounted_occurrence_count, 3)
        self.assertEqual(successor.reported_doses_taken_before_tracking, 1)

    def test_plan_schedule_edit_capacity_equality_creates_inactive_version(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        form_data = self.valid_plan_schedule_edit_form(
            schedule,
            plan,
            (plan_time,),
            course_days="1",
            reported_doses_taken_before_tracking="1",
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_plan_schedule_edit(
                schedule,
                plan,
                (plan_time,),
                form_data=form_data,
            )

        self.assertEqual(response.status_code, 302)
        successor = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id
                == schedule.schedule_id
            )
        )
        self.assertIsNotNone(successor)
        self.assertFalse(successor.is_active)

    def test_effectively_complete_plan_schedule_get_and_post(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            course_days=3,
            reported=3,
        )
        schedule_id = schedule.schedule_id
        before_updated_at = schedule.updated_at

        get_response = self.client.get(
            self.plan_schedule_edit_url(schedule)
        )
        self.assertEqual(get_response.status_code, 302)
        db.session.refresh(schedule)
        self.assertTrue(schedule.is_active)
        self.assertEqual(schedule.updated_at, before_updated_at)

        form_data = self.valid_plan_schedule_edit_form(
            schedule,
            plan,
            (plan_time,),
        )
        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            post_response = self.post_plan_schedule_edit(
                schedule,
                plan,
                (plan_time,),
                form_data=form_data,
            )

        self.assertEqual(post_response.status_code, 302)
        stored = db.session.get(MedicationSchedule, schedule_id)
        self.assertFalse(stored.is_active)
        self.assertEqual(app_module.as_utc(stored.closed_at), SECOND_ACTION_TIME)
        self.assertIsNone(
            db.session.scalar(
                db.select(MedicationSchedule).where(
                    MedicationSchedule.supersedes_schedule_id == schedule_id
                )
            )
        )

    def test_plan_schedule_edit_rejects_invalid_inputs_without_writes(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(8, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        invalid_overrides = (
            {"selected_plan_time_ids": []},
            {"selected_plan_time_ids": ["abc"]},
            {"selected_plan_time_ids": [
                str(plan_time.plan_time_id),
                str(plan_time.plan_time_id),
            ]},
            {"selected_plan_time_ids": [str(other_time.plan_time_id)]},
            {"dose_amount_text": "0"},
            {
                "course_days": "1",
                "reported_doses_taken_before_tracking": "2",
            },
        )

        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                form_data = self.valid_plan_schedule_edit_form(
                    schedule,
                    plan,
                    (plan_time,),
                    **overrides,
                )
                response = self.post_plan_schedule_edit(
                    schedule,
                    plan,
                    (plan_time,),
                    form_data=form_data,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    1,
                )
                db.session.refresh(schedule)
                self.assertTrue(schedule.is_active)

    def test_plan_schedule_edit_rejects_stale_versions_and_claims(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        cases = (
            ({"plan_version": "stale"}, None),
            ({"schedule_version": "stale"}, None),
            ({}, "plan"),
            ({}, "schedule"),
        )

        for overrides, failed_claim in cases:
            with self.subTest(overrides=overrides, failed_claim=failed_claim):
                form_data = self.valid_plan_schedule_edit_form(
                    schedule,
                    plan,
                    (plan_time,),
                    **overrides,
                )
                if failed_claim == "plan":
                    mocked = patch.object(
                        app_module,
                        "claim_medication_plan_change",
                        return_value=False,
                    )
                elif failed_claim == "schedule":
                    mocked = patch.object(
                        app_module,
                        "claim_medication_schedule_edit",
                        return_value=False,
                    )
                else:
                    mocked = patch.object(
                        app_module,
                        "claim_medication_plan_change",
                        wraps=app_module.claim_medication_plan_change,
                    )

                with mocked:
                    response = self.post_plan_schedule_edit(
                        schedule,
                        plan,
                        (plan_time,),
                        form_data=form_data,
                    )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    1,
                )
                db.session.refresh(schedule)
                db.session.refresh(plan)
                self.assertTrue(schedule.is_active)

    def test_plan_schedule_edit_commit_failure_rolls_back_every_stage(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        old_plan_updated_at = plan.updated_at
        failure = SQLAlchemyError("commit failed")

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ), patch.object(db.session, "commit", side_effect=failure):
            response = self.post_plan_schedule_edit(
                schedule,
                plan,
                (plan_time,),
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)
        db.session.refresh(schedule)
        db.session.refresh(plan)
        self.assertTrue(schedule.is_active)
        self.assertIsNone(schedule.closed_at)
        self.assertEqual(plan.updated_at, old_plan_updated_at)

    def test_plan_schedule_link_corruption_is_safe(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        db.session.delete(schedule.plan_time_links[0])
        db.session.commit()

        response = self.client.get(self.plan_schedule_edit_url(schedule))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/my-medication-plan", response.headers["Location"])
        db.session.refresh(schedule)
        self.assertTrue(schedule.is_active)

    def test_closed_plan_version_history_stops_at_closed_at(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            start_date_value=date(2099, 1, 1),
        )
        form_data = self.valid_plan_schedule_edit_form(
            schedule,
            plan,
            (plan_time,),
        )
        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            self.post_plan_schedule_edit(
                schedule,
                plan,
                (plan_time,),
                form_data=form_data,
            )

        old_schedule = db.session.get(
            MedicationSchedule,
            schedule.schedule_id,
        )
        entry = app_module.build_schedule_history_entry(
            self.user_medicine,
            old_schedule,
            reference_at=datetime(2100, 1, 1, tzinfo=UTC),
            timezone_name=self.user.timezone,
        )
        self.assertEqual(entry["status"], "past")
        self.assertEqual(entry["medication_times"], (time(20, 0),))

    def test_inactive_predecessor_link_does_not_block_time_delete(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(morning,),
            medication_times=(time(8, 0),),
        )
        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            self.post_plan_schedule_edit(
                schedule,
                plan,
                (evening,),
            )

        db.session.refresh(plan)
        morning_response = self.post_delete_time(plan, morning)
        self.assertEqual(morning_response.status_code, 302)
        self.assertIsNone(
            db.session.get(MedicationPlanTime, morning.plan_time_id)
        )

        db.session.refresh(plan)
        evening_response = self.post_delete_time(plan, evening)
        self.assertEqual(evening_response.status_code, 302)
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, evening.plan_time_id)
        )

    def test_plan_schedule_edit_requires_login_csrf_and_strict_ownership(self):
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        edit_url = self.plan_schedule_edit_url(schedule)

        anonymous_response = self.client.get(edit_url)
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn("/login", anonymous_response.headers["Location"])

        self.log_in()
        self.assertEqual(self.client.post(edit_url).status_code, 400)

        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(8, 0))
        other_schedule = self.add_existing_schedule(
            self.other_user_medicine,
            plan=other_plan,
            plan_times=(other_time,),
            medication_times=(time(8, 0),),
        )
        self.assertEqual(
            self.client.get(
                self.plan_schedule_edit_url(other_schedule)
            ).status_code,
            404,
        )

        plan.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get(edit_url).status_code, 404)
        plan.is_active = True
        db.session.commit()

        self.user_medicine.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get(edit_url).status_code, 404)
        self.user_medicine.is_active = True
        db.session.commit()

        schedule.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get(edit_url).status_code, 404)

    def test_repeated_plan_edits_link_each_immediate_predecessor(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        first = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            self.post_plan_schedule_edit(first, plan, (plan_time,))

        second = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id
                == first.schedule_id
            )
        )
        db.session.refresh(plan)
        third_edit_time = SECOND_ACTION_TIME + timedelta(minutes=1)

        with patch.object(
            app_module,
            "utc_now",
            return_value=third_edit_time,
        ):
            self.post_plan_schedule_edit(second, plan, (plan_time,))

        third = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.supersedes_schedule_id
                == second.schedule_id
            )
        )
        self.assertEqual(second.supersedes_schedule_id, first.schedule_id)
        self.assertIsNotNone(third)
        self.assertEqual(third.supersedes_schedule_id, second.schedule_id)
        self.assertNotEqual(third.supersedes_schedule_id, first.schedule_id)

    def test_plan_bound_edit_is_enabled_but_restart_stays_blocked(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            medication_times=(time(20, 0),),
        )
        token = self.get_csrf_token()
        edit_url = f"/medication-schedules/{schedule.schedule_id}/edit"
        restart_url = f"/medication-schedules/{schedule.schedule_id}/restart"

        edit_response = self.client.get(edit_url)
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn(b'name="schedule_version"', edit_response.data)

        for method, url in (
            ("post", edit_url),
            ("get", restart_url),
            ("post", restart_url),
        ):
            with self.subTest(method=method, url=url):
                if method == "get":
                    response = self.client.get(url)
                else:
                    response = self.client.post(
                        url,
                        data={"csrf_token": token},
                    )

                self.assertEqual(response.status_code, 302)
                self.assertIn(
                    "/my-medication-plan",
                    response.headers["Location"],
                )

        medicines_page = self.client.get("/my-medicines")
        self.assertIn("내 복용약 일정에서 관리".encode(), medicines_page.data)
        self.assertNotIn(
            f'href="{edit_url}"'.encode(),
            medicines_page.data,
        )

        plan_page = self.client.get("/my-medication-plan")
        self.assertIn(f'href="{edit_url}"'.encode(), plan_page.data)
        self.assertEqual(plan_page.data.count(f'href="{edit_url}"'.encode()), 1)

        history_only_medicine = self.add_user_medicine("HISTORY")
        history_schedule = self.add_existing_schedule(
            history_only_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
            medication_times=(time(20, 0),),
        )
        history_response = self.client.get(
            "/my-medicines/"
            f"{history_only_medicine.user_medicine_id}/history"
        )
        self.assertEqual(history_response.status_code, 200)
        self.assertIn(b"20:00", history_response.data)
        self.assertNotIn(
            f"/medication-schedules/{history_schedule.schedule_id}/restart".encode(),
            history_response.data,
        )


    def test_plan_schedule_remove_requires_post_login_csrf_and_strict_state(self):
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        url = self.plan_schedule_remove_url(schedule)
        anonymous_token = self.get_csrf_token("/login")
        anonymous_response = self.client.post(
            url,
            data={
                **self.valid_plan_schedule_remove_form(schedule, plan),
                "csrf_token": anonymous_token,
            },
        )
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn("/login", anonymous_response.headers["Location"])

        self.log_in()
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(
            self.client.post(
                url,
                data=self.valid_plan_schedule_remove_form(schedule, plan),
            ).status_code,
            400,
        )
        token = self.get_csrf_token("/my-medication-plan")

        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(8, 0))
        other_schedule = self.add_existing_schedule(
            self.other_user_medicine,
            plan=other_plan,
            plan_times=(other_time,),
        )
        legacy_medicine = self.add_user_medicine("REMOVE-LEGACY")
        legacy_schedule = self.add_existing_schedule(legacy_medicine)
        inactive_medicine = self.add_user_medicine("REMOVE-INACTIVE")
        inactive_schedule = self.add_existing_schedule(
            inactive_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
        )
        removed_medicine = self.add_user_medicine(
            "REMOVE-MEDICINE",
            active=False,
        )
        removed_medicine_schedule = self.add_existing_schedule(
            removed_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        inactive_plan = MedicationPlan(
            user_id=self.user_id,
            is_active=False,
            created_at=FIRST_ACTION_TIME,
            updated_at=FIRST_ACTION_TIME,
        )
        db.session.add(inactive_plan)
        db.session.commit()
        inactive_plan_time = self.add_plan_time(
            inactive_plan,
            time(14, 0),
        )
        inactive_plan_medicine = self.add_user_medicine(
            "REMOVE-PLAN"
        )
        inactive_plan_schedule = self.add_existing_schedule(
            inactive_plan_medicine,
            plan=inactive_plan,
            plan_times=(inactive_plan_time,),
        )

        blocked_targets = (
            (other_schedule, other_plan),
            (legacy_schedule, plan),
            (inactive_schedule, plan),
            (removed_medicine_schedule, plan),
            (inactive_plan_schedule, inactive_plan),
        )
        for target, target_plan in blocked_targets:
            with self.subTest(schedule_id=target.schedule_id):
                response = self.client.post(
                    self.plan_schedule_remove_url(target),
                    data={
                        **self.valid_plan_schedule_remove_form(
                            target,
                            target_plan,
                        ),
                        "csrf_token": token,
                    },
                )
                self.assertEqual(response.status_code, 404)

        db.session.refresh(schedule)
        self.assertTrue(schedule.is_active)

    def test_plan_schedule_remove_closes_only_schedule_and_preserves_history(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(morning, evening),
            medication_times=(time(8, 0), time(20, 0)),
            reported=2,
            accounted=3,
        )
        schedule_id = schedule.schedule_id
        user_medicine_id = schedule.user_medicine_id
        immutable_values = {
            name: getattr(schedule, name)
            for name in (
                "user_medicine_id",
                "plan_id",
                "supersedes_schedule_id",
                "intake_timing",
                "dose_amount_text",
                "dose_unit_text",
                "instructions",
                "start_date",
                "end_date",
                "course_days",
                "reported_doses_taken_before_tracking",
                "reminder_tracking_started_at",
                "accounted_occurrence_count",
                "created_at",
            )
        }
        snapshot_ids = tuple(
            db.session.scalars(
                db.select(MedicationTime.medication_time_id)
                .where(MedicationTime.schedule_id == schedule_id)
                .order_by(MedicationTime.medication_time_id)
            )
        )
        link_count = db.session.query(
            MedicationSchedulePlanTime
        ).filter_by(schedule_id=schedule_id).count()
        schedule_count = db.session.query(MedicationSchedule).count()

        page = self.client.get("/my-medication-plan")
        action = self.plan_schedule_remove_url(schedule).encode()
        self.assertEqual(page.data.count(action), 1)
        self.assertEqual(
            page.data.count(
                b'<button type="submit">'
                + "일정에서 제거".encode()
                + b"</button>"
            ),
            1,
        )
        self.assertIn(b'name="plan_version"', page.data)
        self.assertIn(b'name="schedule_version"', page.data)

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_plan_schedule_remove(schedule, plan)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/my-medication-plan", response.headers["Location"])
        db.session.expire_all()
        stored_schedule = db.session.get(MedicationSchedule, schedule_id)
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        stored_user_medicine = db.session.get(
            UserMedicine,
            user_medicine_id,
        )
        self.assertFalse(stored_schedule.is_active)
        self.assertEqual(
            app_module.as_utc(stored_schedule.closed_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(
            app_module.as_utc(stored_schedule.updated_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(
            app_module.as_utc(stored_plan.updated_at),
            SECOND_ACTION_TIME,
        )
        for name, value in immutable_values.items():
            self.assertEqual(getattr(stored_schedule, name), value)
        self.assertTrue(stored_user_medicine.is_active)
        self.assertEqual(db.session.query(MedicationSchedule).count(), schedule_count)
        self.assertIsNone(
            db.session.scalar(
                db.select(MedicationSchedule).where(
                    MedicationSchedule.supersedes_schedule_id == schedule_id
                )
            )
        )
        self.assertEqual(
            tuple(
                db.session.scalars(
                    db.select(MedicationTime.medication_time_id)
                    .where(MedicationTime.schedule_id == schedule_id)
                    .order_by(MedicationTime.medication_time_id)
                )
            ),
            snapshot_ids,
        )
        self.assertEqual(
            db.session.query(MedicationSchedulePlanTime)
            .filter_by(schedule_id=schedule_id)
            .count(),
            link_count,
        )

        plan_page = self.client.get("/my-medication-plan")
        self.assertNotIn(
            f'data-current-schedule-id="{schedule_id}"'.encode(),
            plan_page.data,
        )
        self.assertNotIn(
            f'data-schedule-id="{schedule_id}"'.encode(),
            plan_page.data,
        )
        history = self.client.get(
            f"/my-medicines/{user_medicine_id}/history"
        )
        self.assertEqual(history.status_code, 200)
        self.assertIn(b"08:00", history.data)
        self.assertIn(b"20:00", history.data)
        self.assertNotIn(
            f"/medication-schedules/{schedule_id}/restart".encode(),
            history.data,
        )
        selection = self.client.get(self.select_medicine_url(stored_plan))
        self.assertIn(
            self.plan_medicine_form_url(
                stored_plan,
                stored_user_medicine,
            ).encode(),
            selection.data,
        )

    def test_deactivate_closes_plan_schedule_and_allows_plan_time_delete(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(morning,),
            medication_times=(time(8, 0),),
        )
        other_user_medicine = self.add_user_medicine("DEACTIVATE-OTHER")
        other_schedule = self.add_existing_schedule(
            other_user_medicine,
            plan=plan,
            plan_times=(evening,),
            medication_times=(time(20, 0),),
        )
        schedule_id = schedule.schedule_id
        snapshot_id = schedule.times[0].medication_time_id
        old_other_updated_at = other_schedule.updated_at
        immutable_values = {
            column.name: getattr(schedule, column.name)
            for column in MedicationSchedule.__table__.columns
            if column.name not in {"is_active", "closed_at", "updated_at"}
        }
        link_count = db.session.query(
            MedicationSchedulePlanTime
        ).filter_by(schedule_id=schedule_id).count()
        schedule_count = db.session.query(MedicationSchedule).count()

        blocked_response = self.post_delete_time(plan, morning)
        self.assertEqual(blocked_response.status_code, 302)
        self.assertIsNotNone(
            db.session.get(MedicationPlanTime, morning.plan_time_id)
        )

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ):
            response = self.post_deactivate()

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_schedule = db.session.get(MedicationSchedule, schedule_id)
        stored_other = db.session.get(
            MedicationSchedule,
            other_schedule.schedule_id,
        )
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        self.assertFalse(stored_user_medicine.is_active)
        self.assertEqual(
            app_module.as_utc(stored_user_medicine.updated_at),
            SECOND_ACTION_TIME,
        )
        self.assertFalse(stored_schedule.is_active)
        self.assertEqual(
            app_module.as_utc(stored_schedule.closed_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(
            app_module.as_utc(stored_schedule.updated_at),
            SECOND_ACTION_TIME,
        )
        self.assertEqual(
            app_module.as_utc(stored_plan.updated_at),
            SECOND_ACTION_TIME,
        )
        for name, value in immutable_values.items():
            self.assertEqual(getattr(stored_schedule, name), value)
        self.assertTrue(stored_other.is_active)
        self.assertEqual(stored_other.updated_at, old_other_updated_at)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            schedule_count,
        )
        self.assertIsNotNone(db.session.get(MedicationTime, snapshot_id))
        self.assertEqual(
            db.session.query(MedicationSchedulePlanTime)
            .filter_by(schedule_id=schedule_id)
            .count(),
            link_count,
        )

        plan_page = self.client.get("/my-medication-plan")
        self.assertNotIn(
            f'data-schedule-id="{schedule_id}"'.encode(),
            plan_page.data,
        )
        self.assertIn(
            f'data-schedule-id="{stored_other.schedule_id}"'.encode(),
            plan_page.data,
        )

        delete_response = self.post_delete_time(stored_plan, morning)
        self.assertEqual(delete_response.status_code, 302)
        self.assertIsNone(
            db.session.get(MedicationPlanTime, morning.plan_time_id)
        )
        self.assertIsNotNone(db.session.get(MedicationTime, snapshot_id))

    def test_deactivate_preserves_supersedes_chain(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        predecessor = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
        )
        successor = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        successor.supersedes_schedule_id = predecessor.schedule_id
        db.session.commit()
        predecessor_snapshot = {
            column.name: getattr(predecessor, column.name)
            for column in MedicationSchedule.__table__.columns
        }

        response = self.post_deactivate()

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        stored_predecessor = db.session.get(
            MedicationSchedule,
            predecessor.schedule_id,
        )
        stored_successor = db.session.get(
            MedicationSchedule,
            successor.schedule_id,
        )
        self.assertEqual(
            {
                column.name: getattr(stored_predecessor, column.name)
                for column in MedicationSchedule.__table__.columns
            },
            predecessor_snapshot,
        )
        self.assertFalse(stored_successor.is_active)
        self.assertEqual(
            stored_successor.supersedes_schedule_id,
            predecessor.schedule_id,
        )
        self.assertEqual(db.session.query(MedicationSchedule).count(), 2)

    def test_reactivate_after_deactivate_adds_independent_plan_schedule(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        old_schedule_id = old_schedule.schedule_id
        registration_token = self.get_csrf_token("/my-medicines")

        self.post_deactivate()
        register_response = self.client.post(
            f"/my-medicines/add/{self.medicine.item_seq}",
            data={"csrf_token": registration_token},
        )

        self.assertEqual(register_response.status_code, 302)
        db.session.expire_all()
        stored_old = db.session.get(MedicationSchedule, old_schedule_id)
        reactivated = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertTrue(reactivated.is_active)
        self.assertFalse(stored_old.is_active)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 1)
        selection = self.client.get(self.select_medicine_url(plan))
        self.assertIn(
            self.plan_medicine_form_url(plan, reactivated).encode(),
            selection.data,
        )

        add_response = self.post_plan_medicine(plan, (plan_time,))

        self.assertEqual(add_response.status_code, 302)
        schedules = db.session.scalars(
            db.select(MedicationSchedule)
            .where(
                MedicationSchedule.user_medicine_id
                == self.user_medicine_id
            )
            .order_by(MedicationSchedule.schedule_id)
        ).all()
        self.assertEqual(len(schedules), 2)
        self.assertEqual(schedules[0].schedule_id, old_schedule_id)
        self.assertFalse(schedules[0].is_active)
        self.assertNotEqual(schedules[1].schedule_id, old_schedule_id)
        self.assertTrue(schedules[1].is_active)
        self.assertIsNone(schedules[1].supersedes_schedule_id)

    def test_deactivate_plan_conflicts_and_commit_failure_roll_back(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        old_plan_updated_at = plan.updated_at
        old_schedule_updated_at = schedule.updated_at
        old_user_medicine_updated_at = self.user_medicine.updated_at
        snapshot_id = schedule.times[0].medication_time_id
        link_count = db.session.query(
            MedicationSchedulePlanTime
        ).filter_by(schedule_id=schedule.schedule_id).count()

        failures = (
            (
                "plan_claim",
                patch.object(
                    app_module,
                    "claim_medication_plan_change",
                    return_value=False,
                ),
            ),
            (
                "schedule_claim",
                patch.object(
                    app_module,
                    "claim_medication_schedule_removal",
                    return_value=False,
                ),
            ),
            (
                "commit",
                patch.object(
                    db.session,
                    "commit",
                    side_effect=SQLAlchemyError("forced commit failure"),
                ),
            ),
        )

        for failure_name, failure_patch in failures:
            with self.subTest(failure_name=failure_name), failure_patch:
                response = self.post_deactivate()

            self.assertEqual(response.status_code, 302)
            db.session.expire_all()
            stored_plan = db.session.get(MedicationPlan, plan.plan_id)
            stored_schedule = db.session.get(
                MedicationSchedule,
                schedule.schedule_id,
            )
            stored_user_medicine = db.session.get(
                UserMedicine,
                self.user_medicine_id,
            )
            self.assertEqual(stored_plan.updated_at, old_plan_updated_at)
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
            self.assertIsNotNone(db.session.get(MedicationTime, snapshot_id))
            self.assertEqual(
                db.session.query(MedicationSchedulePlanTime)
                .filter_by(schedule_id=schedule.schedule_id)
                .count(),
                link_count,
            )

    def test_removed_plan_schedule_can_be_added_as_a_new_course(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        old_schedule_id = old_schedule.schedule_id

        remove_response = self.post_plan_schedule_remove(
            old_schedule,
            plan,
        )
        self.assertEqual(remove_response.status_code, 302)
        db.session.expire_all()
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        stored_plan_time = db.session.get(
            MedicationPlanTime,
            plan_time.plan_time_id,
        )

        add_response = self.post_plan_medicine(
            stored_plan,
            (stored_plan_time,),
            user_medicine=stored_user_medicine,
        )

        self.assertEqual(add_response.status_code, 302)
        db.session.expire_all()
        old_stored = db.session.get(MedicationSchedule, old_schedule_id)
        new_schedule = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.user_medicine_id
                == self.user_medicine_id,
                MedicationSchedule.is_active.is_(True),
            )
        )
        self.assertIsNotNone(new_schedule)
        self.assertNotEqual(new_schedule.schedule_id, old_schedule_id)
        self.assertIsNone(new_schedule.supersedes_schedule_id)
        self.assertFalse(old_stored.is_active)
        self.assertIsNotNone(old_stored.closed_at)

    def test_plan_schedule_remove_rejects_stale_versions_and_failed_claims(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        old_plan_updated_at = plan.updated_at
        old_schedule_updated_at = schedule.updated_at
        cases = (
            ({"plan_version": "stale"}, None),
            ({"schedule_version": "stale"}, None),
            ({}, "plan"),
            ({}, "schedule"),
        )

        for overrides, failed_claim in cases:
            with self.subTest(
                overrides=overrides,
                failed_claim=failed_claim,
            ):
                form_data = self.valid_plan_schedule_remove_form(
                    schedule,
                    plan,
                    **overrides,
                )
                if failed_claim == "plan":
                    mocked = patch.object(
                        app_module,
                        "claim_medication_plan_change",
                        return_value=False,
                    )
                elif failed_claim == "schedule":
                    mocked = patch.object(
                        app_module,
                        "claim_medication_schedule_removal",
                        return_value=False,
                    )
                else:
                    mocked = patch.object(
                        app_module,
                        "claim_medication_plan_change",
                        wraps=app_module.claim_medication_plan_change,
                    )

                with mocked:
                    response = self.post_plan_schedule_remove(
                        schedule,
                        plan,
                        form_data=form_data,
                    )

                self.assertEqual(response.status_code, 302)
                db.session.refresh(schedule)
                db.session.refresh(plan)
                self.assertTrue(schedule.is_active)
                self.assertIsNone(schedule.closed_at)
                self.assertEqual(schedule.updated_at, old_schedule_updated_at)
                self.assertEqual(plan.updated_at, old_plan_updated_at)

    def test_plan_schedule_remove_commit_failure_rolls_back_all_changes(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        old_plan_updated_at = plan.updated_at
        old_schedule_updated_at = schedule.updated_at
        snapshot_id = schedule.times[0].medication_time_id
        failure = SQLAlchemyError("commit failed")

        with patch.object(
            app_module,
            "utc_now",
            return_value=SECOND_ACTION_TIME,
        ), patch.object(db.session, "commit", side_effect=failure):
            response = self.post_plan_schedule_remove(schedule, plan)

        self.assertEqual(response.status_code, 302)
        db.session.refresh(schedule)
        db.session.refresh(plan)
        db.session.refresh(self.user_medicine)
        self.assertTrue(schedule.is_active)
        self.assertIsNone(schedule.closed_at)
        self.assertEqual(schedule.updated_at, old_schedule_updated_at)
        self.assertEqual(plan.updated_at, old_plan_updated_at)
        self.assertTrue(self.user_medicine.is_active)
        self.assertIsNotNone(db.session.get(MedicationTime, snapshot_id))
        self.assertEqual(
            db.session.query(MedicationSchedulePlanTime)
            .filter_by(schedule_id=schedule.schedule_id)
            .count(),
            1,
        )

    def test_removed_schedule_does_not_block_plan_time_deletion(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        snapshot_id = schedule.times[0].medication_time_id
        other_user_medicine = self.add_user_medicine(
            "REMOVE-TIME-OTHER"
        )
        other_schedule = self.add_existing_schedule(
            other_user_medicine,
            plan=plan,
            plan_times=(plan_time,),
        )
        other_snapshot_id = (
            other_schedule.times[0].medication_time_id
        )

        self.post_plan_schedule_remove(schedule, plan)
        db.session.refresh(plan)
        blocked_response = self.post_delete_time(plan, plan_time)

        self.assertEqual(blocked_response.status_code, 302)
        self.assertIsNotNone(
            db.session.get(
                MedicationPlanTime,
                plan_time.plan_time_id,
            )
        )

        self.post_plan_schedule_remove(other_schedule, plan)
        db.session.refresh(plan)
        delete_response = self.post_delete_time(plan, plan_time)

        self.assertEqual(delete_response.status_code, 302)
        self.assertIsNone(
            db.session.get(
                MedicationPlanTime,
                plan_time.plan_time_id,
            )
        )
        self.assertIsNotNone(db.session.get(MedicationTime, snapshot_id))
        self.assertIsNotNone(
            db.session.get(MedicationTime, other_snapshot_id)
        )


    def test_plan_schedule_readd_get_prefills_snapshot_and_is_read_only(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(9, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        source = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(evening,),
            active=False,
            start_date_value=date(2025, 1, 2),
            course_days=14,
            medication_times=(time(8, 0), time(20, 0)),
            reported=3,
            accounted=2,
        )
        source.dose_amount_text = "0.5"
        source.dose_unit_text = "캡슐"
        source.intake_timing = "before_meal"
        db.session.commit()
        source_id = source.schedule_id
        source_snapshot = {
            column.name: getattr(source, column.name)
            for column in MedicationSchedule.__table__.columns
        }
        source_time_rows = tuple(
            (value.medication_time_id, value.time_of_day)
            for value in source.times
        )
        plan_updated_at = plan.updated_at
        user_medicine_updated_at = self.user_medicine.updated_at
        plan_time_count = db.session.query(MedicationPlanTime).count()
        schedule_count = db.session.query(MedicationSchedule).count()
        fixed_time = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)

        with patch.object(app_module, "utc_now", return_value=fixed_time):
            response = self.client.get(
                self.plan_schedule_readd_url(source)
            )

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        for expected in (
            "일정에 다시 추가",
            "과거에 입력한 복용 정보를 참고하여",
            'value="0.5"',
            'value="2026-10-07"',
            "과거 일정의 08:00",
            "시간은 현재 복용 일정표에 없습니다.",
        ):
            self.assertIn(expected, html)
        self.assertRegex(
            html,
            r'id="dose_unit_text"[\s\S]*value="캡슐"\s+selected',
        )
        self.assertRegex(
            html,
            r'id="intake_timing"[\s\S]*value="before_meal"\s+selected',
        )
        self.assertRegex(
            html,
            r'id="course_days"[\s\S]*?value=""',
        )
        self.assertRegex(
            html,
            r'id="reported_doses_taken_before_tracking"[\s\S]*?value="0"',
        )
        self.assertRegex(
            html,
            rf'value="{evening.plan_time_id}"\s+checked',
        )
        self.assertNotRegex(
            html,
            rf'value="{morning.plan_time_id}"\s+checked',
        )
        self.assertIn('name="plan_version"', html)
        self.assertNotIn('name="schedule_version"', html)

        db.session.expire_all()
        stored_source = db.session.get(MedicationSchedule, source_id)
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertEqual(
            {
                column.name: getattr(stored_source, column.name)
                for column in MedicationSchedule.__table__.columns
            },
            source_snapshot,
        )
        self.assertEqual(
            tuple(
                (value.medication_time_id, value.time_of_day)
                for value in stored_source.times
            ),
            source_time_rows,
        )
        self.assertEqual(stored_plan.updated_at, plan_updated_at)
        self.assertEqual(
            stored_user_medicine.updated_at,
            user_medicine_updated_at,
        )
        self.assertEqual(
            db.session.query(MedicationPlanTime).count(),
            plan_time_count,
        )
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            schedule_count,
        )

    def test_plan_schedule_readd_access_and_current_state_guards(self):
        plan = self.add_plan()
        source = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            active=False,
        )
        url = self.plan_schedule_readd_url(source)

        self.assertEqual(self.client.get(url).status_code, 302)
        anonymous_token = self.get_csrf_token("/login")
        anonymous_response = self.client.post(
            url,
            data={"csrf_token": anonymous_token},
        )
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn("/login", anonymous_response.headers["Location"])

        self.log_in()
        self.assertEqual(self.client.post(url, data={}).status_code, 400)
        no_time_response = self.client.get(url)
        self.assertEqual(no_time_response.status_code, 302)
        plan_time = self.add_plan_time(plan, time(20, 0))

        legacy_medicine = self.add_user_medicine("READD-LEGACY")
        legacy_source = self.add_existing_schedule(
            legacy_medicine,
            active=False,
        )
        active_medicine = self.add_user_medicine("READD-ACTIVE")
        active_source = self.add_existing_schedule(
            active_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=True,
        )
        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(8, 0))
        other_source = self.add_existing_schedule(
            self.other_user_medicine,
            plan=other_plan,
            plan_times=(other_time,),
            active=False,
        )

        for blocked_source in (
            legacy_source,
            active_source,
            other_source,
        ):
            with self.subTest(schedule_id=blocked_source.schedule_id):
                self.assertEqual(
                    self.client.get(
                        self.plan_schedule_readd_url(blocked_source)
                    ).status_code,
                    404,
                )

        inactive_medicine = self.add_user_medicine(
            "READD-INACTIVE-MEDICINE",
            active=False,
        )
        inactive_medicine_source = self.add_existing_schedule(
            inactive_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
        )
        inactive_response = self.client.get(
            self.plan_schedule_readd_url(inactive_medicine_source)
        )
        self.assertEqual(inactive_response.status_code, 302)
        db.session.refresh(inactive_medicine)
        self.assertFalse(inactive_medicine.is_active)

        inactive_plan = MedicationPlan(
            user_id=self.user_id,
            is_active=False,
            created_at=FIRST_ACTION_TIME,
            updated_at=FIRST_ACTION_TIME,
        )
        db.session.add(inactive_plan)
        db.session.commit()
        inactive_plan_time = self.add_plan_time(
            inactive_plan,
            time(14, 0),
        )
        inactive_plan_medicine = self.add_user_medicine(
            "READD-INACTIVE-PLAN"
        )
        inactive_plan_source = self.add_existing_schedule(
            inactive_plan_medicine,
            plan=inactive_plan,
            plan_times=(inactive_plan_time,),
            active=False,
        )
        inactive_plan_response = self.client.get(
            self.plan_schedule_readd_url(inactive_plan_source)
        )
        self.assertEqual(inactive_plan_response.status_code, 302)

        running_schedule = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=True,
            start_date_value=date(2099, 1, 1),
        )
        running_response = self.client.get(url)
        self.assertEqual(running_response.status_code, 302)
        db.session.refresh(source)
        db.session.refresh(running_schedule)
        self.assertFalse(source.is_active)
        self.assertTrue(running_schedule.is_active)

    def test_removed_plan_schedule_readd_creates_independent_schedule(self):
        self.log_in()
        plan = self.add_plan()
        morning = self.add_plan_time(plan, time(8, 0))
        evening = self.add_plan_time(plan, time(20, 0))
        source = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(morning, evening),
            active=True,
            medication_times=(time(8, 0), time(20, 0)),
            reported=2,
            accounted=3,
        )
        source_id = source.schedule_id
        self.post_plan_schedule_remove(source, plan)
        db.session.expire_all()
        source = db.session.get(MedicationSchedule, source_id)
        plan = db.session.get(MedicationPlan, plan.plan_id)
        morning = db.session.get(
            MedicationPlanTime,
            morning.plan_time_id,
        )
        evening = db.session.get(
            MedicationPlanTime,
            evening.plan_time_id,
        )
        source_snapshot = {
            column.name: getattr(source, column.name)
            for column in MedicationSchedule.__table__.columns
        }
        source_time_rows = tuple(
            (value.medication_time_id, value.time_of_day)
            for value in source.times
        )
        source_link_count = db.session.query(
            MedicationSchedulePlanTime
        ).filter_by(schedule_id=source_id).count()
        readd_time = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
        form_data = self.valid_plan_schedule_readd_form(
            plan,
            (morning, evening),
            dose_amount_text="1.5",
            dose_unit_text="캡슐",
            intake_timing="regardless_of_meal",
            start_date="2026-10-08",
            course_days="4",
            reported_doses_taken_before_tracking="1",
        )

        with patch.object(app_module, "utc_now", return_value=readd_time):
            response = self.post_plan_schedule_readd(
                source,
                plan,
                (morning, evening),
                form_data=form_data,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/my-medication-plan", response.headers["Location"])
        db.session.expire_all()
        stored_source = db.session.get(MedicationSchedule, source_id)
        schedules = db.session.scalars(
            db.select(MedicationSchedule)
            .where(
                MedicationSchedule.user_medicine_id
                == self.user_medicine_id
            )
            .order_by(MedicationSchedule.schedule_id)
        ).all()
        self.assertEqual(len(schedules), 2)
        new_schedule = schedules[-1]
        stored_plan = db.session.get(MedicationPlan, plan.plan_id)
        self.assertNotEqual(new_schedule.schedule_id, source_id)
        self.assertTrue(new_schedule.is_active)
        self.assertEqual(new_schedule.plan_id, plan.plan_id)
        self.assertIsNone(new_schedule.supersedes_schedule_id)
        self.assertIsNone(new_schedule.closed_at)
        self.assertEqual(new_schedule.dose_amount_text, "1.5")
        self.assertEqual(new_schedule.dose_unit_text, "캡슐")
        self.assertEqual(
            new_schedule.intake_timing,
            "regardless_of_meal",
        )
        self.assertEqual(new_schedule.start_date, date(2026, 10, 8))
        self.assertEqual(new_schedule.course_days, 4)
        self.assertEqual(
            new_schedule.reported_doses_taken_before_tracking,
            1,
        )
        self.assertEqual(new_schedule.accounted_occurrence_count, 0)
        self.assertEqual(
            app_module.as_utc(new_schedule.reminder_tracking_started_at),
            readd_time,
        )
        self.assertEqual(
            app_module.as_utc(new_schedule.created_at),
            readd_time,
        )
        self.assertEqual(
            app_module.as_utc(new_schedule.updated_at),
            readd_time,
        )
        self.assertEqual(
            app_module.as_utc(stored_plan.updated_at),
            readd_time,
        )
        self.assertEqual(
            [value.time_of_day for value in new_schedule.times],
            [time(8, 0), time(20, 0)],
        )
        linked_times = db.session.scalars(
            db.select(MedicationPlanTime.time_of_day)
            .join(MedicationSchedulePlanTime)
            .where(
                MedicationSchedulePlanTime.schedule_id
                == new_schedule.schedule_id
            )
            .order_by(MedicationPlanTime.time_of_day)
        ).all()
        self.assertEqual(linked_times, [time(8, 0), time(20, 0)])
        self.assertEqual(
            [value.time_of_day for value in new_schedule.times],
            linked_times,
        )
        new_links = db.session.scalars(
            db.select(MedicationSchedulePlanTime).where(
                MedicationSchedulePlanTime.schedule_id
                == new_schedule.schedule_id
            )
        ).all()
        self.assertTrue(new_links)
        self.assertTrue(
            all(
                link.plan_id == plan.plan_id
                and link.schedule_id == new_schedule.schedule_id
                for link in new_links
            )
        )
        self.assertEqual(
            {
                column.name: getattr(stored_source, column.name)
                for column in MedicationSchedule.__table__.columns
            },
            source_snapshot,
        )
        self.assertEqual(
            tuple(
                (value.medication_time_id, value.time_of_day)
                for value in stored_source.times
            ),
            source_time_rows,
        )
        self.assertEqual(
            db.session.query(MedicationSchedulePlanTime)
            .filter_by(schedule_id=source_id)
            .count(),
            source_link_count,
        )
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine_id,
        )
        self.assertTrue(stored_user_medicine.is_active)
        history = self.client.get(
            f"/my-medicines/{self.user_medicine_id}/history"
        )
        self.assertIn(
            f'data-schedule-id="{source_id}"'.encode(),
            history.data,
        )
        self.assertIn(
            f'data-schedule-id="{new_schedule.schedule_id}"'.encode(),
            history.data,
        )

    def test_plan_schedule_readd_validates_form_times_and_remaining(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        other_plan = self.add_plan(user_id=self.other_user_id)
        other_time = self.add_plan_time(other_plan, time(8, 0))
        source = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
        )
        schedule_count = db.session.query(MedicationSchedule).count()
        invalid_overrides = (
            {"selected_plan_time_ids": []},
            {"selected_plan_time_ids": ["missing"]},
            {
                "selected_plan_time_ids": [
                    str(plan_time.plan_time_id),
                    str(plan_time.plan_time_id),
                ]
            },
            {"selected_plan_time_ids": [str(other_time.plan_time_id)]},
            {"dose_amount_text": "0"},
            {"dose_unit_text": "invalid"},
            {"intake_timing": "invalid"},
            {"start_date": "invalid"},
            {"course_days": "0"},
            {"reported_doses_taken_before_tracking": "-1"},
            {
                "course_days": "3",
                "reported_doses_taken_before_tracking": "3",
            },
        )

        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                form_data = self.valid_plan_schedule_readd_form(
                    plan,
                    (plan_time,),
                    **overrides,
                )
                response = self.post_plan_schedule_readd(
                    source,
                    plan,
                    (plan_time,),
                    form_data=form_data,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    db.session.query(MedicationSchedule).count(),
                    schedule_count,
                )
                db.session.refresh(source)
                self.assertFalse(source.is_active)

    def test_plan_schedule_readd_concurrency_and_errors_roll_back(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        source = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
        )
        old_plan_updated_at = plan.updated_at
        old_source_updated_at = source.updated_at
        schedule_count = db.session.query(MedicationSchedule).count()
        stale_form = self.valid_plan_schedule_readd_form(
            plan,
            (plan_time,),
            plan_version="stale",
        )
        stale_response = self.post_plan_schedule_readd(
            source,
            plan,
            (plan_time,),
            form_data=stale_form,
        )
        self.assertEqual(stale_response.status_code, 302)

        valid_form = self.valid_plan_schedule_readd_form(
            plan,
            (plan_time,),
        )
        token = self.get_csrf_token(self.plan_schedule_readd_url(source))
        with patch.object(
            app_module,
            "claim_medication_plan_change",
            return_value=False,
        ):
            claim_response = self.post_plan_schedule_readd(
                source,
                plan,
                (plan_time,),
                form_data=valid_form,
                token=token,
            )
        self.assertEqual(claim_response.status_code, 302)

        failures = (
            IntegrityError(
                "forced unique conflict",
                {},
                Exception("unique conflict"),
            ),
            SQLAlchemyError("forced commit failure"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                form_data = self.valid_plan_schedule_readd_form(
                    plan,
                    (plan_time,),
                )
                token = self.get_csrf_token(
                    self.plan_schedule_readd_url(source)
                )
                with patch.object(
                    db.session,
                    "commit",
                    side_effect=failure,
                ):
                    response = self.post_plan_schedule_readd(
                        source,
                        plan,
                        (plan_time,),
                        form_data=form_data,
                        token=token,
                    )
                self.assertEqual(response.status_code, 302)

        db.session.refresh(source)
        db.session.refresh(plan)
        db.session.refresh(self.user_medicine)
        self.assertFalse(source.is_active)
        self.assertEqual(source.updated_at, old_source_updated_at)
        self.assertEqual(plan.updated_at, old_plan_updated_at)
        self.assertTrue(self.user_medicine.is_active)
        self.assertEqual(
            db.session.query(MedicationSchedule).count(),
            schedule_count,
        )

    def test_plan_schedule_readd_closes_expired_open_only_on_success(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        source = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
            tracking_started_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        expired_open = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=True,
            start_date_value=date(2020, 1, 1),
            tracking_started_at=datetime(2020, 1, 1, tzinfo=UTC),
            course_days=1,
        )
        old_expired_updated_at = expired_open.updated_at
        readd_time = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
        form_data = self.valid_plan_schedule_readd_form(
            plan,
            (plan_time,),
        )
        token = self.get_csrf_token(self.plan_schedule_readd_url(source))

        with patch.object(
            app_module,
            "utc_now",
            return_value=readd_time,
        ), patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("forced commit failure"),
        ):
            failed_response = self.post_plan_schedule_readd(
                source,
                plan,
                (plan_time,),
                form_data=form_data,
                token=token,
            )

        self.assertEqual(failed_response.status_code, 302)
        db.session.refresh(expired_open)
        self.assertTrue(expired_open.is_active)
        self.assertIsNone(expired_open.closed_at)
        self.assertEqual(expired_open.updated_at, old_expired_updated_at)

        form_data = self.valid_plan_schedule_readd_form(
            plan,
            (plan_time,),
        )
        with patch.object(
            app_module,
            "utc_now",
            return_value=readd_time,
        ):
            success_response = self.post_plan_schedule_readd(
                source,
                plan,
                (plan_time,),
                form_data=form_data,
            )

        self.assertEqual(success_response.status_code, 302)
        db.session.expire_all()
        stored_expired = db.session.get(
            MedicationSchedule,
            expired_open.schedule_id,
        )
        self.assertFalse(stored_expired.is_active)
        self.assertEqual(
            app_module.as_utc(stored_expired.closed_at),
            readd_time,
        )
        self.assertEqual(
            app_module.as_utc(stored_expired.updated_at),
            readd_time,
        )
        self.assertEqual(
            db.session.scalar(
                db.select(db.func.count(MedicationSchedule.schedule_id))
                .where(MedicationSchedule.is_active.is_(True))
            ),
            1,
        )

    def test_history_uses_latest_plan_readd_and_keeps_legacy_restart(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        older = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
            tracking_started_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
        latest = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=False,
            tracking_started_at=datetime(2026, 10, 5, tzinfo=UTC),
        )
        legacy = self.add_existing_schedule(
            self.user_medicine,
            active=False,
            tracking_started_at=datetime(2026, 10, 3, tzinfo=UTC),
        )
        history_url = (
            f"/my-medicines/{self.user_medicine_id}/history"
        )
        history = self.client.get(history_url)

        self.assertEqual(history.status_code, 200)
        self.assertIn(
            self.plan_schedule_readd_url(latest).encode(),
            history.data,
        )
        self.assertNotIn(
            self.plan_schedule_readd_url(older).encode(),
            history.data,
        )
        self.assertIn(
            f"/medication-schedules/{legacy.schedule_id}/restart".encode(),
            history.data,
        )
        self.assertEqual(
            history.data.count("일정에 다시 추가".encode()),
            1,
        )

        plan.is_active = False
        db.session.commit()
        inactive_plan_history = self.client.get(history_url)
        self.assertNotIn(
            self.plan_schedule_readd_url(latest).encode(),
            inactive_plan_history.data,
        )

        plan.is_active = True
        db.session.commit()
        db.session.delete(plan_time)
        db.session.commit()
        no_time_history = self.client.get(history_url)
        self.assertNotIn(
            self.plan_schedule_readd_url(latest).encode(),
            no_time_history.data,
        )
        plan_time = self.add_plan_time(plan, time(20, 0))

        current = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=True,
            start_date_value=date(2099, 1, 1),
        )
        blocked_history = self.client.get(history_url)
        self.assertNotIn(
            self.plan_schedule_readd_url(latest).encode(),
            blocked_history.data,
        )
        db.session.refresh(current)
        self.assertTrue(current.is_active)

    def test_edit_remove_readd_preserves_version_chain(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        first = self.add_existing_schedule(
            self.user_medicine,
            plan=plan,
            plan_times=(plan_time,),
            active=True,
        )
        first_id = first.schedule_id
        edit_response = self.post_plan_schedule_edit(
            first,
            plan,
            (plan_time,),
        )
        self.assertEqual(edit_response.status_code, 302)
        db.session.expire_all()
        second = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.is_active.is_(True),
                MedicationSchedule.user_medicine_id
                == self.user_medicine_id,
            )
        )
        second_id = second.schedule_id
        plan = db.session.get(MedicationPlan, plan.plan_id)
        self.post_plan_schedule_remove(second, plan)
        db.session.expire_all()
        second = db.session.get(MedicationSchedule, second_id)
        plan = db.session.get(MedicationPlan, plan.plan_id)
        plan_time = db.session.get(
            MedicationPlanTime,
            plan_time.plan_time_id,
        )
        readd_response = self.post_plan_schedule_readd(
            second,
            plan,
            (plan_time,),
        )

        self.assertEqual(readd_response.status_code, 302)
        db.session.expire_all()
        first = db.session.get(MedicationSchedule, first_id)
        second = db.session.get(MedicationSchedule, second_id)
        third = db.session.scalar(
            db.select(MedicationSchedule).where(
                MedicationSchedule.is_active.is_(True),
                MedicationSchedule.user_medicine_id
                == self.user_medicine_id,
            )
        )
        self.assertFalse(first.is_active)
        self.assertFalse(second.is_active)
        self.assertTrue(third.is_active)
        self.assertEqual(second.supersedes_schedule_id, first_id)
        self.assertIsNone(third.supersedes_schedule_id)


if __name__ == "__main__":
    unittest.main()
