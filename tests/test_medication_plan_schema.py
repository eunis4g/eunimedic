import tempfile
import unittest
from pathlib import Path

from flask_migrate import check, downgrade, upgrade
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import app as app_module
from models import db


CURRENT_HEAD = "dbc37b91f70b"
NEW_HEAD = "4e2b7c91a6d5"
MIGRATIONS_DIRECTORY = str(
    Path(__file__).resolve().parents[1] / "migrations"
)
LEGACY_SCHEDULE_COLUMNS = (
    "schedule_id",
    "user_medicine_id",
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
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "is_active",
    "created_at",
    "updated_at",
)


class MedicationPlanSchemaTest(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "medication-plan.db"
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
            revision=CURRENT_HEAD,
        )
        self._seed_legacy_data()
        self.legacy_schedule_rows = self._read_legacy_schedule_rows()
        self.legacy_time_rows = self._read_medication_time_rows()
        inspector = inspect(self.test_engine)
        self.legacy_schedule_foreign_keys = {
            (
                tuple(value["constrained_columns"]),
                value["referred_table"],
                tuple(value["referred_columns"]),
            )
            for value in inspector.get_foreign_keys(
                "medication_schedules"
            )
        }
        self.legacy_time_foreign_keys = {
            (
                tuple(value["constrained_columns"]),
                value["referred_table"],
                tuple(value["referred_columns"]),
            )
            for value in inspector.get_foreign_keys("medication_times")
        }

    def tearDown(self):
        db.session.remove()
        db.engines[None] = self.original_engine
        self.test_engine.dispose()
        self.application_context.pop()
        self.temporary_directory.cleanup()

    def _seed_legacy_data(self):
        timestamp = "2026-10-06 00:00:00.000000"

        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "user_id, username, email, password_hash, timezone, "
                    "is_active, created_at, updated_at"
                    ") VALUES ("
                    "1, 'schema-user', 'schema@example.com', 'hash', "
                    "'Asia/Seoul', 1, :timestamp, :timestamp"
                    ")"
                ),
                {"timestamp": timestamp},
            )
            connection.execute(
                text(
                    "INSERT INTO medicines ("
                    "item_seq, item_name, entp_name, created_at, "
                    "updated_at, cached_at"
                    ") VALUES ("
                    "'SCHEMA-MED', 'Schema Medicine', NULL, :timestamp, "
                    ":timestamp, NULL"
                    ")"
                ),
                {"timestamp": timestamp},
            )
            connection.execute(
                text(
                    "INSERT INTO user_medicines ("
                    "user_medicine_id, user_id, medicine_item_seq, "
                    "registration_source, is_active, "
                    "schedule_setup_pending, registered_at, updated_at"
                    ") VALUES ("
                    "11, 1, 'SCHEMA-MED', 'search', 1, 0, "
                    ":timestamp, :timestamp"
                    ")"
                ),
                {"timestamp": timestamp},
            )
            self._insert_schedule(
                connection,
                schedule_id=101,
                is_active=True,
                schema_has_plan_columns=False,
            )
            connection.execute(
                text(
                    "INSERT INTO medication_times ("
                    "medication_time_id, schedule_id, time_of_day"
                    ") VALUES "
                    "(201, 101, '08:00:00.000000'), "
                    "(202, 101, '20:00:00.000000')"
                )
            )

    def _insert_schedule(
        self,
        connection,
        *,
        schedule_id,
        is_active,
        schema_has_plan_columns=True,
        plan_id=None,
        supersedes_schedule_id=None,
    ):
        columns = list(LEGACY_SCHEDULE_COLUMNS)
        values = {
            "schedule_id": schedule_id,
            "user_medicine_id": 11,
            "intake_timing": "after_meal",
            "dose_amount_text": "1",
            "dose_unit_text": "정",
            "instructions": None,
            "start_date": "2026-10-06",
            "end_date": None,
            "course_days": 3,
            "reported_doses_taken_before_tracking": 0,
            "reminder_tracking_started_at": (
                "2026-10-06 00:00:00.000000"
            ),
            "accounted_occurrence_count": 0,
            "monday": 1,
            "tuesday": 1,
            "wednesday": 1,
            "thursday": 1,
            "friday": 1,
            "saturday": 1,
            "sunday": 1,
            "is_active": int(is_active),
            "created_at": "2026-10-06 00:00:00.000000",
            "updated_at": "2026-10-06 00:00:00.000000",
        }

        if schema_has_plan_columns:
            columns.extend(
                ("plan_id", "closed_at", "supersedes_schedule_id")
            )
            values.update(
                plan_id=plan_id,
                closed_at=None,
                supersedes_schedule_id=supersedes_schedule_id,
            )

        connection.execute(
            text(
                "INSERT INTO medication_schedules ("
                + ", ".join(columns)
                + ") VALUES ("
                + ", ".join(f":{column}" for column in columns)
                + ")"
            ),
            values,
        )

    def _read_legacy_schedule_rows(self):
        with self.test_engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT "
                    + ", ".join(LEGACY_SCHEDULE_COLUMNS)
                    + " FROM medication_schedules ORDER BY schedule_id"
                )
            ).all()

    def _read_medication_time_rows(self):
        with self.test_engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT medication_time_id, schedule_id, time_of_day "
                    "FROM medication_times ORDER BY medication_time_id"
                )
            ).all()

    def _upgrade_to_new_head(self):
        upgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=NEW_HEAD,
        )

    def _insert_plan(self, plan_id, *, user_id=1, is_active=True):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO medication_plans ("
                    "plan_id, user_id, is_active, created_at, updated_at"
                    ") VALUES ("
                    ":plan_id, :user_id, :is_active, "
                    "'2026-10-06 00:00:00.000000', "
                    "'2026-10-06 00:00:00.000000'"
                    ")"
                ),
                {
                    "plan_id": plan_id,
                    "user_id": user_id,
                    "is_active": int(is_active),
                },
            )

    def _insert_plan_time(self, plan_time_id, plan_id, time_value):
        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO medication_plan_times ("
                    "plan_time_id, plan_id, time_of_day, created_at"
                    ") VALUES ("
                    ":plan_time_id, :plan_id, :time_value, "
                    "'2026-10-06 00:00:00.000000'"
                    ")"
                ),
                {
                    "plan_time_id": plan_time_id,
                    "plan_id": plan_id,
                    "time_value": time_value,
                },
            )

    def test_upgrade_is_additive_and_preserves_legacy_rows(self):
        self._upgrade_to_new_head()
        inspector = inspect(self.test_engine)

        self.assertTrue(
            {
                "medication_plans",
                "medication_plan_times",
                "medication_schedule_plan_times",
            }.issubset(inspector.get_table_names())
        )
        schedule_columns = {
            value["name"]
            for value in inspector.get_columns("medication_schedules")
        }
        self.assertTrue(
            {"plan_id", "closed_at", "supersedes_schedule_id"}.issubset(
                schedule_columns
            )
        )
        self.assertEqual(
            self._read_legacy_schedule_rows(),
            self.legacy_schedule_rows,
        )
        self.assertEqual(
            self._read_medication_time_rows(),
            self.legacy_time_rows,
        )

        with self.test_engine.connect() as connection:
            for table_name in (
                "medication_plans",
                "medication_plan_times",
                "medication_schedule_plan_times",
            ):
                self.assertEqual(
                    connection.execute(
                        text(f"SELECT COUNT(*) FROM {table_name}")
                    ).scalar_one(),
                    0,
                )

            nullable_values = connection.execute(
                text(
                    "SELECT plan_id, closed_at, supersedes_schedule_id "
                    "FROM medication_schedules WHERE schedule_id = 101"
                )
            ).one()
            self.assertEqual(tuple(nullable_values), (None, None, None))
            self.assertEqual(
                connection.execute(
                    text("PRAGMA foreign_key_check")
                ).all(),
                [],
            )

        new_schedule_foreign_keys = {
            (
                tuple(value["constrained_columns"]),
                value["referred_table"],
                tuple(value["referred_columns"]),
            )
            for value in inspector.get_foreign_keys(
                "medication_schedules"
            )
        }
        new_time_foreign_keys = {
            (
                tuple(value["constrained_columns"]),
                value["referred_table"],
                tuple(value["referred_columns"]),
            )
            for value in inspector.get_foreign_keys("medication_times")
        }
        self.assertTrue(
            self.legacy_schedule_foreign_keys.issubset(
                new_schedule_foreign_keys
            )
        )
        self.assertEqual(
            new_time_foreign_keys,
            self.legacy_time_foreign_keys,
        )
        schedule_indexes = {
            value["name"]
            for value in inspector.get_indexes("medication_schedules")
        }
        self.assertIn(
            "uq_active_medication_schedule_per_user_medicine",
            schedule_indexes,
        )

    def test_plan_and_plan_time_unique_constraints(self):
        self._upgrade_to_new_head()
        self._insert_plan(1, is_active=True)

        with self.assertRaises(IntegrityError):
            self._insert_plan(2, is_active=True)

        self._insert_plan(2, is_active=False)
        self._insert_plan(3, is_active=False)
        self._insert_plan_time(11, 1, "08:00:00.000000")

        with self.assertRaises(IntegrityError):
            self._insert_plan_time(12, 1, "08:00:00.000000")

        self._insert_plan_time(12, 2, "08:00:00.000000")

    def test_association_rejects_duplicates_and_cross_plan_links(self):
        self._upgrade_to_new_head()
        self._insert_plan(1, is_active=True)
        self._insert_plan(2, is_active=False)
        self._insert_plan_time(11, 1, "08:00:00.000000")
        self._insert_plan_time(12, 2, "08:00:00.000000")

        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE medication_schedules SET plan_id = 1 "
                    "WHERE schedule_id = 101"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO medication_schedule_plan_times ("
                    "plan_id, schedule_id, plan_time_id"
                    ") VALUES (1, 101, 11)"
                )
            )

        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO medication_schedule_plan_times ("
                        "plan_id, schedule_id, plan_time_id"
                        ") VALUES (1, 101, 11)"
                    )
                )

        for invalid_values in ((1, 101, 12), (2, 101, 12)):
            with self.subTest(values=invalid_values):
                with self.assertRaises(IntegrityError):
                    with self.test_engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO "
                                "medication_schedule_plan_times ("
                                "plan_id, schedule_id, plan_time_id"
                                ") VALUES (:plan_id, :schedule_id, "
                                ":plan_time_id)"
                            ),
                            {
                                "plan_id": invalid_values[0],
                                "schedule_id": invalid_values[1],
                                "plan_time_id": invalid_values[2],
                            },
                        )

    def test_supersedes_foreign_key_and_unique_constraint(self):
        self._upgrade_to_new_head()

        with self.test_engine.begin() as connection:
            self._insert_schedule(
                connection,
                schedule_id=102,
                is_active=False,
                supersedes_schedule_id=101,
            )

        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                self._insert_schedule(
                    connection,
                    schedule_id=103,
                    is_active=False,
                    supersedes_schedule_id=101,
                )

        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                self._insert_schedule(
                    connection,
                    schedule_id=104,
                    is_active=False,
                    supersedes_schedule_id=999_999,
                )

        with self.test_engine.begin() as connection:
            self._insert_schedule(
                connection,
                schedule_id=105,
                is_active=False,
            )
            self._insert_schedule(
                connection,
                schedule_id=106,
                is_active=False,
            )

    def test_delete_policies_preserve_schedule_time_snapshots(self):
        self._upgrade_to_new_head()
        self._insert_plan(1, is_active=True)
        self._insert_plan_time(11, 1, "08:00:00.000000")

        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE medication_schedules SET plan_id = 1 "
                    "WHERE schedule_id = 101"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO medication_schedule_plan_times ("
                    "plan_id, schedule_id, plan_time_id"
                    ") VALUES (1, 101, 11)"
                )
            )

        with self.assertRaises(IntegrityError):
            with self.test_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM medication_plans WHERE plan_id = 1")
                )

        with self.test_engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM medication_plan_times "
                    "WHERE plan_time_id = 11"
                )
            )
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM "
                        "medication_schedule_plan_times"
                    )
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM medication_times "
                        "WHERE schedule_id = 101"
                    )
                ).scalar_one(),
                2,
            )

    def test_migration_round_trip_and_schema_check(self):
        self._upgrade_to_new_head()

        check(directory=MIGRATIONS_DIRECTORY)

        downgrade(
            directory=MIGRATIONS_DIRECTORY,
            revision=CURRENT_HEAD,
        )
        downgraded_inspector = inspect(self.test_engine)
        self.assertNotIn(
            "medication_plans",
            downgraded_inspector.get_table_names(),
        )
        self.assertNotIn(
            "plan_id",
            {
                value["name"]
                for value in downgraded_inspector.get_columns(
                    "medication_schedules"
                )
            },
        )
        self.assertEqual(
            self._read_legacy_schedule_rows(),
            self.legacy_schedule_rows,
        )
        self.assertEqual(
            self._read_medication_time_rows(),
            self.legacy_time_rows,
        )

        self._upgrade_to_new_head()
        self.assertEqual(
            self._read_legacy_schedule_rows(),
            self.legacy_schedule_rows,
        )
        self.assertEqual(
            self._read_medication_time_rows(),
            self.legacy_time_rows,
        )

        with self.test_engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text(
                        "SELECT plan_id, closed_at, "
                        "supersedes_schedule_id "
                        "FROM medication_schedules "
                        "WHERE schedule_id = 101"
                    )
                ).one(),
                (None, None, None),
            )
            self.assertEqual(
                connection.execute(
                    text("PRAGMA foreign_key_check")
                ).all(),
                [],
            )


if __name__ == "__main__":
    unittest.main()
