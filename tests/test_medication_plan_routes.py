import re
import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import g
from sqlalchemy import create_engine
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


if __name__ == "__main__":
    unittest.main()
