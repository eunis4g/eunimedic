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
            schedule_setup_pending=True,
        )
        self.other_user_medicine = UserMedicine(
            user=self.other_user,
            medicine=self.other_medicine,
            registration_source="search",
            is_active=True,
            schedule_setup_pending=True,
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
        pending=True,
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
            schedule_setup_pending=pending,
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
        pending=False,
    ):
        user_medicine.schedule_setup_pending = pending
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
            accounted_occurrence_count=0,
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
        original_pending = self.user_medicine.schedule_setup_pending
        plan = self.add_plan()
        self.post_add_time(plan, "08:00")

        db.session.refresh(self.user_medicine)
        self.assertEqual(
            self.user_medicine.schedule_setup_pending,
            original_pending,
        )
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
        ongoing = self.add_user_medicine(
            "ONGOING",
            pending=False,
        )
        self.add_existing_schedule(ongoing, pending=False)
        pending = self.add_user_medicine("PENDING", pending=True)
        self.add_existing_schedule(pending, pending=True)
        available = self.add_user_medicine("AVAILABLE")
        inactive = self.add_user_medicine("INACTIVE", active=False)

        response = self.client.get(self.select_medicine_url(plan))

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.medicine.item_name.encode(), response.data)
        self.assertIn("이미 일정에 추가됨".encode(), response.data)
        self.assertIn(ongoing.medicine.item_name.encode(), response.data)
        self.assertIn("현재 복용 설정 사용 중".encode(), response.data)
        self.assertIn(pending.medicine.item_name.encode(), response.data)
        self.assertIn(available.medicine.item_name.encode(), response.data)
        self.assertNotIn(inactive.medicine.item_name.encode(), response.data)
        self.assertEqual(response.data.count("일정에 추가</a>".encode()), 2)

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
            self.user_medicine.schedule_setup_pending,
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
            stored_user_medicine.schedule_setup_pending,
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
        self.assertFalse(self.user_medicine.schedule_setup_pending)
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
            pending=False,
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
            pending=False,
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

    def test_pending_legacy_schedule_is_closed_even_when_future(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
            pending=True,
        )

        response = self.post_plan_medicine(plan, (plan_time,))

        self.assertEqual(response.status_code, 302)
        db.session.refresh(old_schedule)
        self.assertFalse(old_schedule.is_active)
        self.assertIsNotNone(old_schedule.closed_at)
        self.assertEqual(db.session.query(MedicationSchedule).count(), 2)

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
            pending=True,
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
                self.assertTrue(
                    self.user_medicine.schedule_setup_pending
                )

    def test_commit_failure_restores_old_schedule_pending_and_plan(self):
        self.log_in()
        plan = self.add_plan()
        plan_time = self.add_plan_time(plan, time(20, 0))
        old_schedule = self.add_existing_schedule(
            self.user_medicine,
            pending=True,
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
        self.assertTrue(stored_user_medicine.schedule_setup_pending)
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

    def test_plan_bound_legacy_routes_and_links_are_protected(self):
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

        for method, url in (
            ("get", edit_url),
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


if __name__ == "__main__":
    unittest.main()
