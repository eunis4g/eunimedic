"""remove schedule setup pending from user medicines

Revision ID: 7f2c9d1a4b6e
Revises: 4e2b7c91a6d5
Create Date: 2026-10-07

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7f2c9d1a4b6e"
down_revision = "4e2b7c91a6d5"
branch_labels = None
depends_on = None


def _sqlite_version(connection):
    version_text = connection.execute(
        sa.text("SELECT sqlite_version()")
    ).scalar_one()
    return tuple(int(value) for value in version_text.split(".")[:3])


def upgrade():
    connection = op.get_bind()

    if (
        connection.dialect.name == "sqlite"
        and _sqlite_version(connection) < (3, 35, 0)
    ):
        raise RuntimeError(
            "Removing user_medicines.schedule_setup_pending requires "
            "SQLite 3.35.0 or newer so the column can be dropped "
            "without recreating the table."
        )

    if connection.dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE user_medicines "
            "DROP COLUMN schedule_setup_pending"
        )
    else:
        op.drop_column("user_medicines", "schedule_setup_pending")


def downgrade():
    op.add_column(
        "user_medicines",
        sa.Column(
            "schedule_setup_pending",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE user_medicines AS user_medicine "
            "SET schedule_setup_pending = CASE "
            "WHEN user_medicine.is_active = 1 "
            "AND NOT EXISTS ("
            "SELECT 1 FROM medication_schedules AS schedule "
            "WHERE schedule.user_medicine_id = "
            "user_medicine.user_medicine_id"
            ") THEN 1 ELSE 0 END"
        )
    )
    null_count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM user_medicines "
            "WHERE schedule_setup_pending IS NULL"
        )
    ).scalar_one()

    if null_count:
        raise RuntimeError(
            "schedule_setup_pending downgrade backfill left NULL rows."
        )
