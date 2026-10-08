import re
import tempfile
import unittest
from datetime import UTC, date, datetime, time
from pathlib import Path
from unittest.mock import patch

from flask import g
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError

import app as app_module
import services.medication_occurrence_service as occurrence_service
from models import (
    Medicine,
    MedicationOccurrence,
    MedicationPlan,
    MedicationSchedule,
    MedicationTime,
    User,
    UserMedicine,
    db,
)
from services.medication_occurrence_service import (
    local_date_to_utc_window,
)


VIEW_TIME = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
DAY_START = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)


def utc_datetime(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class MedicationOccurrenceRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = (
            Path(cls.temporary_directory.name) / "occurrence-route-test.db"
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
            username="occurrence-route-user",
            email="occurrence-route@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.other_user = User(
            username="other-occurrence-route-user",
            email="other-occurrence-route@example.com",
            password_hash="test-password-hash",
            timezone="Asia/Seoul",
        )
        self.medicine = Medicine(
            item_seq="TODAY-MED-001",
            item_name="기본약",
        )
        self.user_medicine = UserMedicine(
            user=self.user,
            medicine=self.medicine,
            registration_source="search",
            is_active=True,
        )
        self.plan = MedicationPlan(user=self.user, is_active=True)
        self.other_plan = MedicationPlan(
            user=self.other_user,
            is_active=True,
        )
        db.session.add_all(
            [
                self.user,
                self.other_user,
                self.medicine,
                self.user_medicine,
                self.plan,
                self.other_plan,
            ]
        )
        db.session.commit()
        self.user_id = self.user.user_id

    def tearDown(self):
        db.session.remove()
        self.application_context.pop()

    def log_in(self, user_id=None):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id or self.user_id)
            session["_fresh"] = True

        g.pop("_login_user", None)

    def add_user_medicine(
        self,
        *,
        item_seq,
        item_name,
        user=None,
        is_active=True,
    ):
        medicine = Medicine(item_seq=item_seq, item_name=item_name)
        user_medicine = UserMedicine(
            user=user or self.user,
            medicine=medicine,
            registration_source="search",
            is_active=is_active,
        )
        db.session.add_all([medicine, user_medicine])
        db.session.commit()
        return user_medicine

    def add_schedule(
        self,
        *,
        user_medicine=None,
        plan=None,
        plan_bound=True,
        medication_times=(time(8),),
        start_date_value=date(2026, 10, 8),
        tracking_at=DAY_START,
        closed_at=None,
        course_days=3,
        reported=0,
        accounted=0,
        active=True,
        supersedes_schedule_id=None,
    ):
        selected_plan = plan or self.plan
        schedule = MedicationSchedule(
            user_medicine=user_medicine or self.user_medicine,
            plan_id=selected_plan.plan_id if plan_bound else None,
            supersedes_schedule_id=supersedes_schedule_id,
            intake_timing="after_meal",
            dose_amount_text="1",
            dose_unit_text="정",
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
            is_active=active,
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

    def add_occurrence(
        self,
        schedule,
        scheduled_for,
        *,
        response_status=None,
    ):
        responded_at = VIEW_TIME if response_status is not None else None
        occurrence = MedicationOccurrence(
            schedule_id=schedule.schedule_id,
            scheduled_for=scheduled_for,
            response_status=response_status,
            responded_at=responded_at,
            created_at=VIEW_TIME,
            updated_at=VIEW_TIME,
        )
        db.session.add(occurrence)
        db.session.commit()
        return occurrence

    def get_today(self, *, view_time=VIEW_TIME):
        with patch.object(
            app_module,
            "utc_now",
            return_value=view_time,
        ) as mocked_utc_now:
            response = self.client.get("/today-medications")

        self.assertEqual(mocked_utc_now.call_count, 1)
        return response

    def get_csrf_token(self, url="/my-medication-plan"):
        response = self.client.get(url)
        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def post_response(
        self,
        schedule,
        scheduled_for,
        response_status,
        *,
        occurrence_version=None,
        action_time=utc_datetime(2026, 10, 8, 6),
        csrf_token=None,
        expected_utc_calls=1,
    ):
        if occurrence_version is None:
            occurrence_version = app_module.VIRTUAL_OCCURRENCE_VERSION
        if csrf_token is None:
            csrf_token = self.get_csrf_token()

        with patch.object(
            app_module,
            "utc_now",
            return_value=action_time,
        ) as mocked_utc_now:
            response = self.client.post(
                (
                    f"/medication-schedules/{schedule.schedule_id}"
                    "/occurrences/response"
                ),
                data={
                    "csrf_token": csrf_token,
                    "scheduled_for": scheduled_for.isoformat(),
                    "response_status": response_status,
                    "occurrence_version": occurrence_version,
                },
            )

        self.assertEqual(mocked_utc_now.call_count, expected_utc_calls)
        return response

    def get_flash_messages(self):
        with self.client.session_transaction() as session:
            return tuple(session.get("_flashes", ()))

    def assert_entry_status(self, html, medicine_name, status_label):
        entry_start = html.index(medicine_name)
        entry_end = html.index("</li>", entry_start)
        self.assertIn(status_label, html[entry_start:entry_end])

    def test_login_is_required_and_route_is_get_only(self):
        response = self.client.get("/today-medications")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        self.log_in()
        response = self.client.post("/today-medications")
        self.assertEqual(response.status_code, 405)

    def test_empty_day_shows_empty_message_without_response_buttons(self):
        self.log_in()

        response = self.get_today()
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("오늘 복용할 약이 없습니다.", html)
        self.assertNotIn("<button", html)
        self.assertNotIn("<form", html)

    def test_past_exact_and_future_slots_use_one_view_time(self):
        self.log_in()
        self.add_schedule(
            medication_times=(time(8), time(12), time(20)),
        )

        html = self.get_today().get_data(as_text=True)

        self.assertIn("08:00", html)
        self.assertIn("12:00", html)
        self.assertIn("20:00", html)
        self.assertEqual(html.count('data-status="unanswered"'), 1)
        self.assertEqual(html.count('data-status="scheduled"'), 2)
        self.assertIn("2026-10-08", html)
        self.assertIn("Asia/Seoul", html)

    def test_existing_response_states_and_materialized_null_are_shown(self):
        self.log_in()
        taken_user_medicine = self.add_user_medicine(
            item_seq="TODAY-TAKEN",
            item_name="복용 완료약",
        )
        skipped_user_medicine = self.add_user_medicine(
            item_seq="TODAY-SKIPPED",
            item_name="미복용약",
        )
        null_user_medicine = self.add_user_medicine(
            item_seq="TODAY-NULL",
            item_name="응답 없는 약",
        )
        taken_schedule = self.add_schedule(
            user_medicine=taken_user_medicine,
        )
        skipped_schedule = self.add_schedule(
            user_medicine=skipped_user_medicine,
        )
        null_schedule = self.add_schedule(
            user_medicine=null_user_medicine,
        )
        scheduled_for = utc_datetime(2026, 10, 7, 23)
        self.add_occurrence(
            taken_schedule,
            scheduled_for,
            response_status="taken",
        )
        self.add_occurrence(
            skipped_schedule,
            scheduled_for,
            response_status="not_taken",
        )
        self.add_occurrence(null_schedule, scheduled_for)

        html = self.get_today().get_data(as_text=True)

        self.assert_entry_status(html, "복용 완료약", "복용했어요")
        self.assert_entry_status(html, "미복용약", "복용하지 않았어요")
        self.assert_entry_status(html, "응답 없는 약", "미응답")
        self.assertIn("복용하지 않았어요로 변경", html)
        self.assertIn("복용했어요로 변경", html)

    def test_entries_are_grouped_and_stably_sorted(self):
        self.log_in()
        late_user_medicine = self.add_user_medicine(
            item_seq="TODAY-LATE",
            item_name="늦은약",
        )
        z_user_medicine = self.add_user_medicine(
            item_seq="TODAY-Z",
            item_name="하늘약",
        )
        a_user_medicine = self.add_user_medicine(
            item_seq="TODAY-A",
            item_name="가나다약",
        )
        self.add_schedule(
            user_medicine=late_user_medicine,
            medication_times=(time(20),),
        )
        self.add_schedule(user_medicine=z_user_medicine)
        self.add_schedule(user_medicine=a_user_medicine)

        html = self.get_today().get_data(as_text=True)

        self.assertLess(html.index("08:00"), html.index("20:00"))
        self.assertLess(html.index("가나다약"), html.index("하늘약"))
        self.assertEqual(html.count('data-time-group="08:00"'), 1)

    def test_old_and_successor_versions_both_contribute_valid_slots(self):
        self.log_in()
        old_schedule = self.add_schedule(
            medication_times=(time(8), time(20)),
            closed_at=VIEW_TIME,
            active=False,
        )
        successor = self.add_schedule(
            medication_times=(time(9), time(20)),
            tracking_at=VIEW_TIME,
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertIn(
            f'data-schedule-id="{old_schedule.schedule_id}"',
            html,
        )
        self.assertIn(
            f'data-schedule-id="{successor.schedule_id}"',
            html,
        )
        self.assertIn("08:00", html)
        self.assertIn("20:00", html)
        self.assertNotIn("09:00", html)

    def test_closed_at_and_successor_tracking_boundaries_are_exclusive(self):
        self.log_in()
        old_schedule = self.add_schedule(
            medication_times=(time(8), time(12)),
            closed_at=VIEW_TIME,
            active=False,
        )
        successor = self.add_schedule(
            medication_times=(time(12), time(20)),
            tracking_at=VIEW_TIME,
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertEqual(html.count('data-time-group="12:00"'), 1)
        twelve_group_start = html.index('data-time-group="12:00"')
        twelve_group_end = html.index("</section>", twelve_group_start)
        twelve_group = html[twelve_group_start:twelve_group_end]
        self.assertIn(
            f'data-schedule-id="{successor.schedule_id}"',
            twelve_group,
        )
        self.assertNotIn(
            f'data-schedule-id="{old_schedule.schedule_id}"',
            twelve_group,
        )
        self.assertIn("20:00", html)
        self.assertIn("08:00", html)

    def test_time_move_does_not_recreate_a_past_successor_slot(self):
        self.log_in()
        old_schedule = self.add_schedule(
            medication_times=(time(8),),
            closed_at=VIEW_TIME,
            active=False,
        )
        self.add_schedule(
            medication_times=(time(9),),
            tracking_at=VIEW_TIME,
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertIn("08:00", html)
        self.assertNotIn("09:00", html)

    def test_time_remove_uses_each_version_snapshot(self):
        self.log_in()
        old_schedule = self.add_schedule(
            medication_times=(time(8), time(14), time(20)),
            closed_at=VIEW_TIME,
            active=False,
        )
        self.add_schedule(
            medication_times=(time(8), time(20)),
            tracking_at=VIEW_TIME,
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertIn("08:00", html)
        self.assertIn("20:00", html)
        self.assertNotIn("14:00", html)

    def test_whole_schedule_removal_keeps_only_earlier_slot(self):
        self.log_in()
        self.add_schedule(
            medication_times=(time(8), time(14), time(20)),
            closed_at=VIEW_TIME,
            active=False,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertIn("08:00", html)
        self.assertNotIn("14:00", html)
        self.assertNotIn("20:00", html)

    def test_inactive_user_medicine_keeps_earlier_valid_slot(self):
        self.log_in()
        self.user_medicine.is_active = False
        db.session.commit()
        self.add_schedule(
            medication_times=(time(8), time(14), time(20)),
            closed_at=VIEW_TIME,
            active=False,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertIn("기본약", html)
        self.assertIn("08:00", html)
        self.assertNotIn("14:00", html)
        self.assertNotIn("20:00", html)

    def test_response_remains_attached_to_the_old_schedule_version(self):
        self.log_in()
        old_schedule = self.add_schedule(
            medication_times=(time(8),),
            closed_at=VIEW_TIME,
            active=False,
        )
        self.add_occurrence(
            old_schedule,
            utc_datetime(2026, 10, 7, 23),
            response_status="taken",
        )
        self.add_schedule(
            medication_times=(time(20),),
            tracking_at=VIEW_TIME,
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        html = self.get_today().get_data(as_text=True)

        self.assert_entry_status(html, "기본약", "복용했어요")
        self.assertEqual(html.count("복용했어요"), 1)

    def test_natural_completion_does_not_hide_today_earlier_slot(self):
        self.log_in()
        self.add_schedule(
            medication_times=(time(8),),
            course_days=1,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertIn("08:00", html)
        self.assertIn("미응답", html)

    def test_other_user_and_legacy_schedules_are_omitted(self):
        self.log_in()
        legacy_user_medicine = self.add_user_medicine(
            item_seq="TODAY-LEGACY",
            item_name="레거시약",
        )
        other_user_medicine = self.add_user_medicine(
            item_seq="TODAY-OTHER",
            item_name="다른 사용자 약",
            user=self.other_user,
        )
        self.add_schedule(
            user_medicine=legacy_user_medicine,
            plan_bound=False,
        )
        self.add_schedule(
            user_medicine=other_user_medicine,
            plan=self.other_plan,
        )

        html = self.get_today().get_data(as_text=True)

        self.assertNotIn("레거시약", html)
        self.assertNotIn("다른 사용자 약", html)
        self.assertIn("오늘 복용할 약이 없습니다.", html)

    def test_invalid_schedule_is_skipped_without_hiding_valid_rows(self):
        self.log_in()
        invalid_user_medicine = self.add_user_medicine(
            item_seq="TODAY-INVALID",
            item_name="잘못된 일정 약",
        )
        valid_user_medicine = self.add_user_medicine(
            item_seq="TODAY-VALID",
            item_name="정상 일정 약",
        )
        self.add_schedule(
            user_medicine=invalid_user_medicine,
            medication_times=(),
        )
        self.add_schedule(user_medicine=valid_user_medicine)

        with self.assertLogs(app_module.app.logger, level="WARNING"):
            response = self.get_today()
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("일부 복약 정보를 불러오지 못했습니다.", html)
        self.assertIn("정상 일정 약", html)
        self.assertNotIn("잘못된 일정 약", html)

    def test_get_is_read_only_and_never_calls_materialization(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8), time(20)),
        )
        occurrence = self.add_occurrence(
            schedule,
            utc_datetime(2026, 10, 7, 23),
            response_status="taken",
        )
        before = {
            "occurrence_count": db.session.query(
                MedicationOccurrence
            ).count(),
            "occurrence": (
                occurrence.response_status,
                occurrence.responded_at,
                occurrence.updated_at,
            ),
            "schedule": (
                schedule.is_active,
                schedule.closed_at,
                schedule.accounted_occurrence_count,
                schedule.updated_at,
            ),
            "user_medicine": (
                self.user_medicine.is_active,
                self.user_medicine.updated_at,
            ),
            "plan": (self.plan.is_active, self.plan.updated_at),
            "times": tuple(
                (value.medication_time_id, value.time_of_day)
                for value in schedule.times
            ),
        }
        dml_statements = []

        def record_dml(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            operation = statement.lstrip().split(None, 1)[0].upper()
            if operation in {"INSERT", "UPDATE", "DELETE"}:
                dml_statements.append(statement)

        event.listen(
            self.test_engine,
            "before_cursor_execute",
            record_dml,
        )
        try:
            with patch.object(
                occurrence_service,
                "get_or_create_medication_occurrence",
            ) as materialize:
                response = self.get_today()
        finally:
            event.remove(
                self.test_engine,
                "before_cursor_execute",
                record_dml,
            )

        self.assertEqual(response.status_code, 200)
        materialize.assert_not_called()
        self.assertEqual(dml_statements, [])
        db.session.expire_all()
        stored_occurrence = db.session.get(
            MedicationOccurrence,
            occurrence.occurrence_id,
        )
        stored_schedule = db.session.get(
            MedicationSchedule,
            schedule.schedule_id,
        )
        stored_user_medicine = db.session.get(
            UserMedicine,
            self.user_medicine.user_medicine_id,
        )
        stored_plan = db.session.get(MedicationPlan, self.plan.plan_id)
        self.assertEqual(
            db.session.query(MedicationOccurrence).count(),
            before["occurrence_count"],
        )
        self.assertEqual(
            (
                stored_occurrence.response_status,
                stored_occurrence.responded_at,
                stored_occurrence.updated_at,
            ),
            before["occurrence"],
        )
        self.assertEqual(
            (
                stored_schedule.is_active,
                stored_schedule.closed_at,
                stored_schedule.accounted_occurrence_count,
                stored_schedule.updated_at,
            ),
            before["schedule"],
        )
        self.assertEqual(
            (
                stored_user_medicine.is_active,
                stored_user_medicine.updated_at,
            ),
            before["user_medicine"],
        )
        self.assertEqual(
            (stored_plan.is_active, stored_plan.updated_at),
            before["plan"],
        )
        self.assertEqual(
            tuple(
                (value.medication_time_id, value.time_of_day)
                for value in stored_schedule.times
            ),
            before["times"],
        )

    def test_select_count_does_not_grow_with_schedule_count(self):
        self.log_in()

        for index in range(8):
            user_medicine = self.add_user_medicine(
                item_seq=f"TODAY-NQ-{index}",
                item_name=f"Query Medicine {index}",
            )
            self.add_schedule(user_medicine=user_medicine)

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
            response = self.get_today()
        finally:
            event.remove(
                self.test_engine,
                "before_cursor_execute",
                record_select,
            )

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(select_statements), 5)

    def test_local_day_window_uses_dst_aware_midnights(self):
        spring_start, spring_end = local_date_to_utc_window(
            date(2026, 3, 8),
            timezone_name="America/New_York",
        )
        fall_start, fall_end = local_date_to_utc_window(
            date(2026, 11, 1),
            timezone_name="America/New_York",
        )

        self.assertEqual(spring_end - spring_start, app_module.timedelta(hours=23))
        self.assertEqual(fall_end - fall_start, app_module.timedelta(hours=25))

    def test_utc_occurrence_is_displayed_in_the_user_timezone(self):
        self.log_in()
        self.add_schedule(medication_times=(time(8),))

        html = self.get_today().get_data(as_text=True)

        self.assertIn("08:00", html)
        self.assertIn("2026-10-07T23:00:00+00:00", html)
        self.assertNotIn(">23:00<", html)

    def test_invalid_user_timezone_returns_guidance_instead_of_500(self):
        self.user.timezone = "Invalid/Timezone"
        db.session.commit()
        self.log_in()

        with self.assertLogs(app_module.app.logger, level="WARNING"):
            response = self.get_today()
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("오늘 복약 정보를 불러오지 못했습니다.", html)

    def test_medication_plan_contains_today_navigation_link(self):
        self.log_in()

        response = self.client.get("/my-medication-plan")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/today-medications"', html)
        self.assertIn("오늘 복약 확인", html)

    def test_past_entries_have_response_forms_but_future_entries_do_not(self):
        self.log_in()
        self.add_schedule(medication_times=(time(8), time(20)))

        html = self.get_today().get_data(as_text=True)
        past_start = html.index("2026-10-07T23:00:00+00:00")
        past_end = html.index("</li>", past_start)
        future_start = html.index("2026-10-08T11:00:00+00:00")
        future_end = html.index("</li>", future_start)
        past_entry = html[past_start:past_end]
        future_entry = html[future_start:future_end]

        self.assertEqual(past_entry.count("<form"), 2)
        self.assertIn('value="taken"', past_entry)
        self.assertIn('value="not_taken"', past_entry)
        self.assertIn('value="virtual"', past_entry)
        self.assertNotIn("<form", future_entry)
        self.assertNotIn("<button", future_entry)

    def test_virtual_occurrences_can_record_taken_and_not_taken(self):
        self.log_in()
        taken_user_medicine = self.add_user_medicine(
            item_seq="POST-TAKEN",
            item_name="복용 응답 약",
        )
        skipped_user_medicine = self.add_user_medicine(
            item_seq="POST-SKIPPED",
            item_name="미복용 응답 약",
        )
        taken_schedule = self.add_schedule(
            user_medicine=taken_user_medicine,
        )
        skipped_schedule = self.add_schedule(
            user_medicine=skipped_user_medicine,
        )
        scheduled_for = utc_datetime(2026, 10, 7, 23)
        action_time = utc_datetime(2026, 10, 8, 6)

        taken_response = self.post_response(
            taken_schedule,
            scheduled_for,
            "taken",
            action_time=action_time,
        )
        skipped_response = self.post_response(
            skipped_schedule,
            scheduled_for,
            "not_taken",
            action_time=action_time,
        )

        self.assertEqual(taken_response.status_code, 302)
        self.assertEqual(skipped_response.status_code, 302)
        self.assertTrue(
            taken_response.headers["Location"].endswith(
                "/today-medications"
            )
        )
        occurrences = db.session.query(MedicationOccurrence).order_by(
            MedicationOccurrence.schedule_id
        ).all()
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(
            {value.response_status for value in occurrences},
            {"taken", "not_taken"},
        )

        for occurrence in occurrences:
            self.assertEqual(occurrence.responded_at, action_time.replace(tzinfo=None))
            self.assertEqual(occurrence.created_at, action_time.replace(tzinfo=None))
            self.assertEqual(occurrence.updated_at, action_time.replace(tzinfo=None))

        self.assertIn(
            ("success", "복약 응답을 저장했습니다."),
            self.get_flash_messages(),
        )
        html = self.get_today(view_time=action_time).get_data(as_text=True)
        self.assert_entry_status(html, "복용 응답 약", "복용했어요")
        self.assert_entry_status(
            html,
            "미복용 응답 약",
            "복용하지 않았어요",
        )

    def test_response_does_not_change_schedule_accounting(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8), time(20)),
            reported=1,
            accounted=1,
            course_days=4,
        )
        before = (
            schedule.reported_doses_taken_before_tracking,
            schedule.accounted_occurrence_count,
            schedule.reminder_tracking_started_at,
            schedule.course_days,
            schedule.closed_at,
            schedule.is_active,
            schedule.updated_at,
        )

        self.post_response(
            schedule,
            utc_datetime(2026, 10, 8, 11),
            "taken",
            action_time=utc_datetime(2026, 10, 8, 12),
        )

        db.session.expire_all()
        stored_schedule = db.session.get(
            MedicationSchedule,
            schedule.schedule_id,
        )
        self.assertEqual(
            (
                stored_schedule.reported_doses_taken_before_tracking,
                stored_schedule.accounted_occurrence_count,
                stored_schedule.reminder_tracking_started_at,
                stored_schedule.course_days,
                stored_schedule.closed_at,
                stored_schedule.is_active,
                stored_schedule.updated_at,
            ),
            before,
        )

    def test_future_exact_now_and_tampered_occurrences_are_rejected(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8), time(12), time(20)),
        )

        exact_response = self.post_response(
            schedule,
            VIEW_TIME,
            "taken",
            action_time=VIEW_TIME,
        )
        future_response = self.post_response(
            schedule,
            utc_datetime(2026, 10, 8, 11),
            "taken",
            action_time=VIEW_TIME,
        )
        tampered_response = self.post_response(
            schedule,
            utc_datetime(2026, 10, 8, 0),
            "taken",
            action_time=VIEW_TIME,
        )

        self.assertEqual(exact_response.status_code, 302)
        self.assertEqual(future_response.status_code, 302)
        self.assertEqual(tampered_response.status_code, 302)
        self.assertEqual(db.session.query(MedicationOccurrence).count(), 0)

    def test_post_security_rejects_missing_csrf_and_invalid_status(self):
        self.log_in()
        schedule = self.add_schedule()
        scheduled_for = utc_datetime(2026, 10, 7, 23)
        url = (
            f"/medication-schedules/{schedule.schedule_id}"
            "/occurrences/response"
        )

        missing_csrf = self.client.post(
            url,
            data={
                "scheduled_for": scheduled_for.isoformat(),
                "response_status": "taken",
                "occurrence_version": "virtual",
            },
        )
        invalid_status = self.post_response(
            schedule,
            scheduled_for,
            "invalid",
        )

        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(invalid_status.status_code, 302)
        self.assertEqual(db.session.query(MedicationOccurrence).count(), 0)

    def test_unauthenticated_post_redirects_and_other_owner_returns_404(self):
        schedule = self.add_schedule()
        scheduled_for = utc_datetime(2026, 10, 7, 23)
        login_csrf = self.get_csrf_token("/login")
        url = (
            f"/medication-schedules/{schedule.schedule_id}"
            "/occurrences/response"
        )
        unauthenticated = self.client.post(
            url,
            data={
                "csrf_token": login_csrf,
                "scheduled_for": scheduled_for.isoformat(),
                "response_status": "taken",
                "occurrence_version": "virtual",
            },
        )
        self.assertEqual(unauthenticated.status_code, 302)
        self.assertIn("/login", unauthenticated.headers["Location"])

        other_user_medicine = self.add_user_medicine(
            item_seq="POST-OTHER",
            item_name="다른 사용자 응답 약",
            user=self.other_user,
        )
        other_schedule = self.add_schedule(
            user_medicine=other_user_medicine,
            plan=self.other_plan,
        )
        self.log_in()
        ownership_response = self.post_response(
            other_schedule,
            scheduled_for,
            "taken",
            expected_utc_calls=0,
        )

        self.assertEqual(ownership_response.status_code, 404)
        self.assertEqual(db.session.query(MedicationOccurrence).count(), 0)

    def test_legacy_schedule_response_is_rejected(self):
        self.log_in()
        legacy_schedule = self.add_schedule(plan_bound=False)

        response = self.post_response(
            legacy_schedule,
            utc_datetime(2026, 10, 7, 23),
            "taken",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationOccurrence).count(), 0)

    def test_existing_response_can_change_and_same_response_is_no_op(self):
        self.log_in()
        schedule = self.add_schedule()
        scheduled_for = utc_datetime(2026, 10, 7, 23)
        occurrence = self.add_occurrence(
            schedule,
            scheduled_for,
            response_status="taken",
        )
        original_responded_at = occurrence.responded_at
        original_updated_at = occurrence.updated_at
        original_version = (
            app_module.serialize_medication_occurrence_version(
                occurrence.updated_at
            )
        )

        same_response = self.post_response(
            schedule,
            scheduled_for,
            "taken",
            occurrence_version=original_version,
            action_time=utc_datetime(2026, 10, 8, 7),
        )
        db.session.expire_all()
        occurrence = db.session.get(
            MedicationOccurrence,
            occurrence.occurrence_id,
        )
        self.assertEqual(occurrence.responded_at, original_responded_at)
        self.assertEqual(occurrence.updated_at, original_updated_at)

        changed_at = utc_datetime(2026, 10, 8, 8)
        changed_response = self.post_response(
            schedule,
            scheduled_for,
            "not_taken",
            occurrence_version=original_version,
            action_time=changed_at,
        )
        db.session.expire_all()
        occurrence = db.session.get(
            MedicationOccurrence,
            occurrence.occurrence_id,
        )

        self.assertEqual(same_response.status_code, 302)
        self.assertEqual(changed_response.status_code, 302)
        self.assertEqual(occurrence.response_status, "not_taken")
        self.assertEqual(occurrence.responded_at, changed_at.replace(tzinfo=None))
        self.assertEqual(occurrence.updated_at, changed_at.replace(tzinfo=None))

    def test_virtual_first_response_conflict_preserves_the_winner(self):
        self.log_in()
        schedule = self.add_schedule()
        scheduled_for = utc_datetime(2026, 10, 7, 23)

        winner = self.post_response(
            schedule,
            scheduled_for,
            "taken",
            occurrence_version="virtual",
            action_time=utc_datetime(2026, 10, 8, 6),
        )
        loser = self.post_response(
            schedule,
            scheduled_for,
            "not_taken",
            occurrence_version="virtual",
            action_time=utc_datetime(2026, 10, 8, 7),
        )

        occurrences = db.session.query(MedicationOccurrence).all()
        self.assertEqual(winner.status_code, 302)
        self.assertEqual(loser.status_code, 302)
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].response_status, "taken")
        self.assertIn(
            (
                "error",
                "복약 응답이 변경되었습니다. 최신 상태를 확인해 주세요.",
            ),
            self.get_flash_messages(),
        )

    def test_virtual_same_response_retry_is_idempotent(self):
        self.log_in()
        schedule = self.add_schedule()
        scheduled_for = utc_datetime(2026, 10, 7, 23)
        self.post_response(
            schedule,
            scheduled_for,
            "taken",
            occurrence_version="virtual",
            action_time=utc_datetime(2026, 10, 8, 6),
        )
        occurrence = db.session.query(MedicationOccurrence).one()
        before = (occurrence.responded_at, occurrence.updated_at)

        retry = self.post_response(
            schedule,
            scheduled_for,
            "taken",
            occurrence_version="virtual",
            action_time=utc_datetime(2026, 10, 8, 7),
        )

        db.session.expire_all()
        occurrence = db.session.query(MedicationOccurrence).one()
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(
            (occurrence.responded_at, occurrence.updated_at),
            before,
        )
        self.assertIn(
            ("success", "이미 같은 복약 응답이 저장되어 있습니다."),
            self.get_flash_messages(),
        )

    def test_stale_existing_version_cannot_overwrite_a_newer_response(self):
        self.log_in()
        schedule = self.add_schedule()
        scheduled_for = utc_datetime(2026, 10, 7, 23)
        occurrence = self.add_occurrence(
            schedule,
            scheduled_for,
            response_status="taken",
        )
        stale_version = app_module.serialize_medication_occurrence_version(
            occurrence.updated_at
        )
        first_change_at = utc_datetime(2026, 10, 8, 7)
        self.post_response(
            schedule,
            scheduled_for,
            "not_taken",
            occurrence_version=stale_version,
            action_time=first_change_at,
        )

        stale_response = self.post_response(
            schedule,
            scheduled_for,
            "taken",
            occurrence_version=stale_version,
            action_time=utc_datetime(2026, 10, 8, 8),
        )

        db.session.expire_all()
        occurrence = db.session.get(
            MedicationOccurrence,
            occurrence.occurrence_id,
        )
        self.assertEqual(stale_response.status_code, 302)
        self.assertEqual(occurrence.response_status, "not_taken")
        self.assertEqual(
            occurrence.updated_at,
            first_change_at.replace(tzinfo=None),
        )

    def test_late_response_works_for_closed_completed_and_inactive_rows(self):
        self.log_in()
        closed_user_medicine = self.add_user_medicine(
            item_seq="POST-CLOSED",
            item_name="종료 일정 약",
        )
        completed_user_medicine = self.add_user_medicine(
            item_seq="POST-COMPLETE",
            item_name="완료 일정 약",
        )
        inactive_user_medicine = self.add_user_medicine(
            item_seq="POST-INACTIVE",
            item_name="삭제 약",
            is_active=False,
        )
        schedules = [
            self.add_schedule(
                user_medicine=closed_user_medicine,
                closed_at=VIEW_TIME,
                active=False,
            ),
            self.add_schedule(
                user_medicine=completed_user_medicine,
                course_days=1,
            ),
            self.add_schedule(
                user_medicine=inactive_user_medicine,
                closed_at=VIEW_TIME,
                active=False,
            ),
        ]
        scheduled_for = utc_datetime(2026, 10, 7, 23)

        for schedule in schedules:
            response = self.post_response(
                schedule,
                scheduled_for,
                "taken",
            )
            self.assertEqual(response.status_code, 302)

        self.assertEqual(db.session.query(MedicationOccurrence).count(), 3)

    def test_occurrence_at_or_after_closed_boundary_is_rejected(self):
        self.log_in()
        schedule = self.add_schedule(
            medication_times=(time(8), time(12), time(14)),
            closed_at=VIEW_TIME,
            active=False,
        )

        at_boundary = self.post_response(
            schedule,
            VIEW_TIME,
            "taken",
            action_time=utc_datetime(2026, 10, 8, 6),
        )
        after_boundary = self.post_response(
            schedule,
            utc_datetime(2026, 10, 8, 5),
            "taken",
            action_time=utc_datetime(2026, 10, 8, 6),
        )

        self.assertEqual(at_boundary.status_code, 302)
        self.assertEqual(after_boundary.status_code, 302)
        self.assertEqual(db.session.query(MedicationOccurrence).count(), 0)

    def test_response_updates_only_the_target_occurrence(self):
        self.log_in()
        other_user_medicine = self.add_user_medicine(
            item_seq="POST-ISOLATED",
            item_name="독립 응답 약",
        )
        first_schedule = self.add_schedule(
            medication_times=(time(8), time(20)),
        )
        second_schedule = self.add_schedule(
            user_medicine=other_user_medicine,
            medication_times=(time(8),),
        )
        first_morning = self.add_occurrence(
            first_schedule,
            utc_datetime(2026, 10, 7, 23),
        )
        first_evening = self.add_occurrence(
            first_schedule,
            utc_datetime(2026, 10, 8, 11),
        )
        second_morning = self.add_occurrence(
            second_schedule,
            utc_datetime(2026, 10, 7, 23),
        )
        version = app_module.serialize_medication_occurrence_version(
            first_morning.updated_at
        )

        self.post_response(
            first_schedule,
            utc_datetime(2026, 10, 7, 23),
            "taken",
            occurrence_version=version,
        )

        db.session.expire_all()
        self.assertEqual(
            db.session.get(
                MedicationOccurrence,
                first_morning.occurrence_id,
            ).response_status,
            "taken",
        )
        self.assertIsNone(
            db.session.get(
                MedicationOccurrence,
                first_evening.occurrence_id,
            ).response_status
        )
        self.assertIsNone(
            db.session.get(
                MedicationOccurrence,
                second_morning.occurrence_id,
            ).response_status
        )

    def test_old_version_response_does_not_touch_successor(self):
        self.log_in()
        old_schedule = self.add_schedule(
            medication_times=(time(8),),
            closed_at=VIEW_TIME,
            active=False,
        )
        successor = self.add_schedule(
            medication_times=(time(20),),
            tracking_at=VIEW_TIME,
            supersedes_schedule_id=old_schedule.schedule_id,
        )

        self.post_response(
            old_schedule,
            utc_datetime(2026, 10, 7, 23),
            "taken",
        )

        occurrence = db.session.query(MedicationOccurrence).one()
        self.assertEqual(occurrence.schedule_id, old_schedule.schedule_id)
        self.assertNotEqual(occurrence.schedule_id, successor.schedule_id)

    def test_commit_failure_rolls_back_materialization_and_response(self):
        self.log_in()
        schedule = self.add_schedule()
        csrf_token = self.get_csrf_token()

        with patch.object(
            db.session,
            "commit",
            side_effect=SQLAlchemyError("commit failed"),
        ):
            response = self.post_response(
                schedule,
                utc_datetime(2026, 10, 7, 23),
                "taken",
                csrf_token=csrf_token,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(MedicationOccurrence).count(), 0)


if __name__ == "__main__":
    unittest.main()
