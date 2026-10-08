import tempfile
import unittest
from pathlib import Path

from flask_migrate import check, downgrade, upgrade
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import app as app_module
from models import (
    MedicationOccurrence,
    MedicationPlan,
    MedicationSchedule,
    NotificationDispatch,
    NotificationDispatchMember,
    User,
    db,
)


BASE_REVISION = "c6f4a2d9e8b1"
NOTIFICATION_REVISION = "d9e7b4c2a1f6"
PROJECT_HEAD_REVISION = "f3a7c9e1b2d4"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
TIMESTAMP = "2026-10-08 00:00:00.000000"
DUE_AT = "2026-10-08 08:00:00.000000"


class NotificationSchemaTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "notification-schema.db"
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
            revision=BASE_REVISION,
        )
        self._seed_representative_data()

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def _seed_representative_data(self):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "user_id, username, email, password_hash, timezone, "
                    "is_active, created_at, updated_at"
                    ") VALUES "
                    "(1, 'notification-user-1', 'notify1@example.com', "
                    "'hash', 'Asia/Seoul', 1, :timestamp, :timestamp), "
                    "(2, 'notification-user-2', 'notify2@example.com', "
                    "'hash', 'Asia/Seoul', 1, :timestamp, :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )
            connection.execute(
                text(
                    "INSERT INTO medicines ("
                    "item_seq, item_name, entp_name, created_at, "
                    "updated_at, cached_at"
                    ") VALUES "
                    "('NOTIFY-MED-1', 'Medicine 1', NULL, :timestamp, "
                    ":timestamp, NULL), "
                    "('NOTIFY-MED-2', 'Medicine 2', NULL, :timestamp, "
                    ":timestamp, NULL), "
                    "('NOTIFY-MED-3', 'Medicine 3', NULL, :timestamp, "
                    ":timestamp, NULL)"
                ),
                {"timestamp": TIMESTAMP},
            )
            connection.execute(
                text(
                    "INSERT INTO user_medicines ("
                    "user_medicine_id, user_id, medicine_item_seq, "
                    "registration_source, is_active, registered_at, "
                    "updated_at"
                    ") VALUES "
                    "(11, 1, 'NOTIFY-MED-1', 'search', 1, :timestamp, "
                    ":timestamp), "
                    "(12, 1, 'NOTIFY-MED-2', 'search', 1, :timestamp, "
                    ":timestamp), "
                    "(13, 2, 'NOTIFY-MED-3', 'search', 1, :timestamp, "
                    ":timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )
            connection.execute(
                text(
                    "INSERT INTO medication_plans ("
                    "plan_id, user_id, is_active, created_at, updated_at"
                    ") VALUES "
                    "(21, 1, 1, :timestamp, :timestamp), "
                    "(22, 1, 0, :timestamp, :timestamp), "
                    "(23, 2, 1, :timestamp, :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )
            connection.execute(
                text(
                    "INSERT INTO medication_plan_times ("
                    "plan_time_id, plan_id, time_of_day, created_at"
                    ") VALUES "
                    "(31, 21, '08:00:00.000000', :timestamp), "
                    "(32, 22, '09:00:00.000000', :timestamp), "
                    "(33, 23, '08:00:00.000000', :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )

            self._insert_schedule(connection, 41, 11, 21)
            self._insert_schedule(connection, 42, 12, 22)
            self._insert_schedule(connection, 43, 13, 23)
            self._insert_schedule(connection, 44, 11, 21)

            connection.execute(
                text(
                    "INSERT INTO medication_times ("
                    "medication_time_id, schedule_id, time_of_day"
                    ") VALUES "
                    "(51, 41, '08:00:00.000000'), "
                    "(52, 42, '09:00:00.000000'), "
                    "(53, 43, '08:00:00.000000')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO medication_schedule_plan_times ("
                    "plan_id, schedule_id, plan_time_id"
                    ") VALUES (21, 41, 31), (22, 42, 32), (23, 43, 33)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO medication_occurrences ("
                    "occurrence_id, schedule_id, scheduled_for, "
                    "response_status, responded_at, created_at, updated_at"
                    ") VALUES "
                    "(61, 41, '2026-10-08 08:00:00.000000', NULL, NULL, "
                    ":timestamp, :timestamp), "
                    "(62, 42, '2026-10-08 09:00:00.000000', NULL, NULL, "
                    ":timestamp, :timestamp), "
                    "(63, 43, '2026-10-08 08:00:00.000000', NULL, NULL, "
                    ":timestamp, :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )

    def _insert_schedule(
        self,
        connection,
        schedule_id,
        user_medicine_id,
        plan_id,
    ):
        connection.execute(
            text(
                "INSERT INTO medication_schedules ("
                "schedule_id, user_medicine_id, plan_id, "
                "supersedes_schedule_id, intake_timing, dose_amount_text, "
                "dose_unit_text, instructions, start_date, end_date, "
                "course_days, reported_doses_taken_before_tracking, "
                "reminder_tracking_started_at, accounted_occurrence_count, "
                "monday, tuesday, wednesday, thursday, friday, saturday, "
                "sunday, is_active, closed_at, created_at, updated_at"
                ") VALUES ("
                ":schedule_id, :user_medicine_id, :plan_id, NULL, "
                "'after_meal', '1', 'tablet', NULL, '2026-10-08', NULL, "
                "3, 0, :timestamp, 0, 1, 1, 1, 1, 1, 1, 1, 0, "
                ":timestamp, :timestamp, :timestamp)"
            ),
            {
                "schedule_id": schedule_id,
                "user_medicine_id": user_medicine_id,
                "plan_id": plan_id,
                "timestamp": TIMESTAMP,
            },
        )

    def _upgrade(self):
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=NOTIFICATION_REVISION,
        )

    def _insert_dispatch(
        self,
        dispatch_id,
        *,
        user_id=1,
        plan_id=21,
        course_root_schedule_id=None,
        notification_type="scheduled_occurrence",
        delivery_channel="email",
        due_at=DUE_AT,
        status="pending",
        attempt_count=0,
        next_attempt_at=None,
        claim_token=None,
        claimed_at=None,
        sent_at=None,
    ):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO notification_dispatches ("
                    "dispatch_id, user_id, plan_id, "
                    "course_root_schedule_id, notification_type, "
                    "delivery_channel, due_at, status, attempt_count, "
                    "next_attempt_at, claim_token, claimed_at, created_at, "
                    "sent_at, updated_at, provider_message_id, "
                    "last_error_code"
                    ") VALUES ("
                    ":dispatch_id, :user_id, :plan_id, "
                    ":course_root_schedule_id, :notification_type, "
                    ":delivery_channel, :due_at, :status, :attempt_count, "
                    ":next_attempt_at, :claim_token, :claimed_at, "
                    ":created_at, :sent_at, :updated_at, NULL, NULL)"
                ),
                {
                    "dispatch_id": dispatch_id,
                    "user_id": user_id,
                    "plan_id": plan_id,
                    "course_root_schedule_id": course_root_schedule_id,
                    "notification_type": notification_type,
                    "delivery_channel": delivery_channel,
                    "due_at": due_at,
                    "status": status,
                    "attempt_count": attempt_count,
                    "next_attempt_at": next_attempt_at,
                    "claim_token": claim_token,
                    "claimed_at": claimed_at,
                    "created_at": TIMESTAMP,
                    "sent_at": sent_at,
                    "updated_at": TIMESTAMP,
                },
            )

    def _assert_dispatch_rejected(self, dispatch_id, **values):
        with self.assertRaises(IntegrityError):
            self._insert_dispatch(dispatch_id, **values)

    def _insert_member(self, dispatch_id, occurrence_id):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO notification_dispatch_members ("
                    "dispatch_id, occurrence_id"
                    ") VALUES (:dispatch_id, :occurrence_id)"
                ),
                {
                    "dispatch_id": dispatch_id,
                    "occurrence_id": occurrence_id,
                },
            )

    def _read_existing_rows(self):
        queries = {
            "users": "SELECT * FROM users ORDER BY user_id",
            "medicines": "SELECT * FROM medicines ORDER BY item_seq",
            "user_medicines": (
                "SELECT * FROM user_medicines ORDER BY user_medicine_id"
            ),
            "medication_plans": (
                "SELECT * FROM medication_plans ORDER BY plan_id"
            ),
            "medication_plan_times": (
                "SELECT * FROM medication_plan_times ORDER BY plan_time_id"
            ),
            "medication_schedules": (
                "SELECT * FROM medication_schedules ORDER BY schedule_id"
            ),
            "medication_times": (
                "SELECT * FROM medication_times ORDER BY medication_time_id"
            ),
            "medication_occurrences": (
                "SELECT * FROM medication_occurrences ORDER BY occurrence_id"
            ),
        }

        with self.test_engine.connect() as connection:
            return {
                table_name: connection.execute(text(query)).all()
                for table_name, query in queries.items()
            }

    def _assert_database_is_healthy(self):
        with self.test_engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("PRAGMA foreign_key_check")).all(),
                [],
            )
            self.assertEqual(
                connection.execute(text("PRAGMA integrity_check")).scalar_one(),
                "ok",
            )

    def test_model_metadata_and_relationships(self):
        dispatch_table = NotificationDispatch.__table__
        member_table = NotificationDispatchMember.__table__

        self.assertEqual(dispatch_table.name, "notification_dispatches")
        self.assertEqual(
            tuple(dispatch_table.c.keys()),
            (
                "dispatch_id",
                "user_id",
                "plan_id",
                "course_root_schedule_id",
                "notification_type",
                "delivery_channel",
                "due_at",
                "status",
                "attempt_count",
                "next_attempt_at",
                "claim_token",
                "claimed_at",
                "created_at",
                "sent_at",
                "updated_at",
                "provider_message_id",
                "last_error_code",
            ),
        )
        for column_name in (
            "due_at",
            "next_attempt_at",
            "claimed_at",
            "created_at",
            "sent_at",
            "updated_at",
        ):
            self.assertTrue(dispatch_table.c[column_name].type.timezone)

        self.assertIsNotNone(dispatch_table.c.created_at.default)
        self.assertIsNotNone(dispatch_table.c.updated_at.default)
        self.assertIsNotNone(dispatch_table.c.updated_at.onupdate)
        self.assertEqual(
            tuple(member_table.primary_key.columns.keys()),
            ("dispatch_id", "occurrence_id"),
        )
        self.assertEqual(NotificationDispatch.members.property.lazy, "select")
        self.assertTrue(
            NotificationDispatch.members.property.cascade.delete_orphan
        )
        self.assertEqual(
            MedicationOccurrence.notification_dispatch_members.property.lazy,
            "select",
        )
        self.assertEqual(User.notification_dispatches.property.lazy, "select")
        self.assertEqual(
            MedicationPlan.notification_dispatches.property.lazy,
            "select",
        )
        self.assertEqual(
            MedicationSchedule.course_root_notification_dispatches.property.lazy,
            "select",
        )

    def test_migration_round_trip_preserves_existing_data(self):
        rows_before = self._read_existing_rows()
        self._assert_database_is_healthy()

        self._upgrade()
        self.assertEqual(self._read_existing_rows(), rows_before)
        with self.test_engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT COUNT(*) FROM notification_dispatches")
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM notification_dispatch_members"
                    )
                ).scalar_one(),
                0,
            )
        self._assert_database_is_healthy()
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=PROJECT_HEAD_REVISION,
        )
        check(directory=MIGRATIONS_DIRECTORY)

        downgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=BASE_REVISION,
        )
        inspector = inspect(self.test_engine)
        self.assertNotIn(
            "notification_dispatches",
            inspector.get_table_names(),
        )
        self.assertNotIn(
            "notification_dispatch_members",
            inspector.get_table_names(),
        )
        self.assertEqual(self._read_existing_rows(), rows_before)
        self._assert_database_is_healthy()

        self._upgrade()
        self.assertEqual(self._read_existing_rows(), rows_before)
        self._assert_database_is_healthy()

    def test_columns_constraints_foreign_keys_and_indexes(self):
        self._upgrade()
        inspector = inspect(self.test_engine)
        columns = {
            value["name"]: value
            for value in inspector.get_columns("notification_dispatches")
        }

        self.assertFalse(columns["user_id"]["nullable"])
        self.assertTrue(columns["plan_id"]["nullable"])
        self.assertTrue(columns["course_root_schedule_id"]["nullable"])
        self.assertFalse(columns["notification_type"]["nullable"])
        self.assertFalse(columns["delivery_channel"]["nullable"])
        self.assertFalse(columns["due_at"]["nullable"])
        self.assertFalse(columns["status"]["nullable"])
        self.assertFalse(columns["attempt_count"]["nullable"])
        self.assertTrue(columns["next_attempt_at"]["nullable"])
        self.assertTrue(columns["claim_token"]["nullable"])
        self.assertTrue(columns["claimed_at"]["nullable"])
        self.assertFalse(columns["created_at"]["nullable"])
        self.assertTrue(columns["sent_at"]["nullable"])
        self.assertFalse(columns["updated_at"]["nullable"])

        self.assertEqual(
            {
                value["name"]
                for value in inspector.get_check_constraints(
                    "notification_dispatches"
                )
            },
            {
                "ck_notification_dispatch_type",
                "ck_notification_dispatch_channel",
                "ck_notification_dispatch_status",
                "ck_notification_dispatch_attempt_count",
                "ck_notification_dispatch_identity",
                "ck_notification_dispatch_claim_state",
                "ck_notification_dispatch_sent_state",
            },
        )
        self.assertEqual(
            {
                (
                    value["name"],
                    tuple(value["constrained_columns"]),
                    value["referred_table"],
                    value.get("options", {}).get("ondelete"),
                )
                for value in inspector.get_foreign_keys(
                    "notification_dispatches"
                )
            },
            {
                (
                    "fk_notification_dispatch_user",
                    ("user_id",),
                    "users",
                    "CASCADE",
                ),
                (
                    "fk_notification_dispatch_plan",
                    ("plan_id",),
                    "medication_plans",
                    "SET NULL",
                ),
                (
                    "fk_notification_dispatch_course_root_schedule",
                    ("course_root_schedule_id",),
                    "medication_schedules",
                    "RESTRICT",
                ),
            },
        )

        indexes = {
            value["name"]: (
                tuple(value["column_names"]),
                bool(value["unique"]),
            )
            for value in inspector.get_indexes("notification_dispatches")
        }
        self.assertEqual(
            indexes,
            {
                "ix_notification_dispatch_worker_due": (
                    ("status", "next_attempt_at", "due_at"),
                    False,
                ),
                "uq_notification_dispatch_scheduled": (
                    ("user_id", "plan_id", "due_at", "notification_type"),
                    True,
                ),
                "uq_notification_dispatch_unanswered_review": (
                    (
                        "user_id",
                        "course_root_schedule_id",
                        "notification_type",
                    ),
                    True,
                ),
            },
        )
        member_indexes = {
            value["name"]: tuple(value["column_names"])
            for value in inspector.get_indexes(
                "notification_dispatch_members"
            )
        }
        self.assertEqual(
            member_indexes,
            {
                "ix_notification_dispatch_members_occurrence_id": (
                    "occurrence_id",
                )
            },
        )

        with self.test_engine.connect() as connection:
            index_sql = dict(
                connection.execute(
                    text(
                        "SELECT name, sql FROM sqlite_master "
                        "WHERE type = 'index' AND name LIKE "
                        "'uq_notification_dispatch_%'"
                    )
                ).all()
            )
        self.assertIn(
            "WHERE notification_type = 'scheduled_occurrence'",
            index_sql["uq_notification_dispatch_scheduled"],
        )
        self.assertIn(
            "WHERE notification_type = 'unanswered_review'",
            index_sql["uq_notification_dispatch_unanswered_review"],
        )

    def test_valid_rows_and_database_defaults(self):
        self._upgrade()
        self._insert_dispatch(1)
        self._insert_dispatch(
            2,
            plan_id=21,
            course_root_schedule_id=41,
            notification_type="unanswered_review",
            due_at="2026-10-08 10:00:00.000000",
        )
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO notification_dispatches ("
                    "dispatch_id, user_id, plan_id, notification_type, "
                    "due_at, created_at, updated_at"
                    ") VALUES (3, 1, 22, 'scheduled_occurrence', "
                    "'2026-10-08 11:00:00.000000', :timestamp, :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )
            stored = connection.execute(
                text(
                    "SELECT delivery_channel, status, attempt_count, "
                    "next_attempt_at FROM notification_dispatches "
                    "WHERE dispatch_id = 3"
                )
            ).one()

        self.assertEqual(stored, ("email", "pending", 0, None))

    def test_allowlist_and_attempt_constraints(self):
        self._upgrade()
        invalid_rows = (
            {"notification_type": "unknown"},
            {"status": "unknown"},
            {"delivery_channel": "web_push"},
            {"attempt_count": -1},
        )

        for dispatch_id, values in enumerate(invalid_rows, start=1):
            with self.subTest(values=values):
                self._assert_dispatch_rejected(dispatch_id, **values)

    def test_type_specific_identity_constraints(self):
        self._upgrade()
        self._assert_dispatch_rejected(1, plan_id=None)
        self._assert_dispatch_rejected(
            2,
            course_root_schedule_id=41,
        )
        self._assert_dispatch_rejected(
            3,
            plan_id=None,
            notification_type="unanswered_review",
        )

        self._insert_dispatch(
            4,
            plan_id=None,
            status="failed",
        )
        self._insert_dispatch(
            5,
            plan_id=None,
            course_root_schedule_id=41,
            notification_type="unanswered_review",
        )

    def test_claim_and_sent_state_constraints(self):
        self._upgrade()
        self._assert_dispatch_rejected(1, status="claimed")
        self._assert_dispatch_rejected(
            2,
            claim_token="claim-2",
            claimed_at=TIMESTAMP,
        )
        self._assert_dispatch_rejected(3, status="sent")
        self._assert_dispatch_rejected(4, sent_at=TIMESTAMP)

        self._insert_dispatch(
            5,
            status="claimed",
            claim_token="claim-5",
            claimed_at=TIMESTAMP,
        )
        self._insert_dispatch(
            6,
            plan_id=22,
            due_at="2026-10-08 09:00:00.000000",
            status="sent",
            sent_at=TIMESTAMP,
        )

    def test_scheduled_and_review_partial_unique_indexes(self):
        self._upgrade()
        self._insert_dispatch(1)
        self._assert_dispatch_rejected(2)
        self._insert_dispatch(3, plan_id=22)
        self._insert_dispatch(4, user_id=2, plan_id=23)

        self._insert_dispatch(
            5,
            plan_id=None,
            course_root_schedule_id=41,
            notification_type="unanswered_review",
        )
        self._assert_dispatch_rejected(
            6,
            plan_id=21,
            course_root_schedule_id=41,
            notification_type="unanswered_review",
        )
        self._insert_dispatch(
            7,
            plan_id=22,
            course_root_schedule_id=42,
            notification_type="unanswered_review",
        )

    def test_member_constraints_and_shared_occurrence(self):
        self._upgrade()
        self._insert_dispatch(1)
        self._insert_dispatch(
            2,
            plan_id=None,
            course_root_schedule_id=41,
            notification_type="unanswered_review",
        )
        self._insert_member(1, 61)
        self._insert_member(1, 62)
        self._insert_member(2, 61)

        with self.assertRaises(IntegrityError):
            self._insert_member(1, 61)
        with self.assertRaises(IntegrityError):
            self._insert_member(999_999, 61)
        with self.assertRaises(IntegrityError):
            self._insert_member(1, 999_999)

        with self.test_engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT dispatch_id, occurrence_id FROM "
                        "notification_dispatch_members "
                        "ORDER BY dispatch_id, occurrence_id"
                    )
                ).all(),
                [(1, 61), (1, 62), (2, 61)],
            )

    def test_member_delete_policies(self):
        self._upgrade()
        self._insert_dispatch(1)
        self._insert_member(1, 61)

        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM notification_dispatches "
                    "WHERE dispatch_id = 1"
                )
            )
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM notification_dispatch_members"
                    )
                ).scalar_one(),
                0,
            )

        self._insert_dispatch(2, plan_id=22)
        self._insert_member(2, 62)
        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM medication_occurrences "
                        "WHERE occurrence_id = 62"
                    )
                )

    def test_plan_set_null_and_pending_plan_protection(self):
        self._upgrade()
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO medication_plans ("
                    "plan_id, user_id, is_active, created_at, updated_at"
                    ") VALUES "
                    "(91, 1, 0, :timestamp, :timestamp), "
                    "(92, 1, 0, :timestamp, :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )

        self._insert_dispatch(
            1,
            plan_id=91,
            status="sent",
            sent_at=TIMESTAMP,
        )
        with self.test_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM medication_plans WHERE plan_id = 91")
            )
            self.assertIsNone(
                connection.execute(
                    text(
                        "SELECT plan_id FROM notification_dispatches "
                        "WHERE dispatch_id = 1"
                    )
                ).scalar_one()
            )

        self._insert_dispatch(2, plan_id=92)
        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM medication_plans WHERE plan_id = 92")
                )

    def test_course_root_and_user_delete_policies(self):
        self._upgrade()
        self._insert_dispatch(
            1,
            plan_id=21,
            course_root_schedule_id=44,
            notification_type="unanswered_review",
        )
        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM medication_schedules "
                        "WHERE schedule_id = 44"
                    )
                )

        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "user_id, username, email, password_hash, timezone, "
                    "is_active, created_at, updated_at"
                    ") VALUES (9, 'cascade-user', 'cascade@example.com', "
                    "'hash', 'Asia/Seoul', 1, :timestamp, :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )
            connection.execute(
                text(
                    "INSERT INTO medication_plans ("
                    "plan_id, user_id, is_active, created_at, updated_at"
                    ") VALUES (99, 9, 1, :timestamp, :timestamp)"
                ),
                {"timestamp": TIMESTAMP},
            )
        self._insert_dispatch(
            2,
            user_id=9,
            plan_id=99,
            status="sent",
            sent_at=TIMESTAMP,
        )

        with self.test_engine.begin() as connection:
            connection.execute(text("DELETE FROM users WHERE user_id = 9"))
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM notification_dispatches "
                        "WHERE dispatch_id = 2"
                    )
                ).scalar_one(),
                0,
            )

    def test_invalid_foreign_keys_are_rejected(self):
        self._upgrade()
        self._assert_dispatch_rejected(1, user_id=999_999)
        self._assert_dispatch_rejected(2, plan_id=999_999)
        self._assert_dispatch_rejected(
            3,
            plan_id=None,
            course_root_schedule_id=999_999,
            notification_type="unanswered_review",
        )
        self._assert_database_is_healthy()


if __name__ == "__main__":
    unittest.main()
