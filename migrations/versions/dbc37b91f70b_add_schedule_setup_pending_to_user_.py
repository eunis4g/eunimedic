"""add schedule setup pending to user medicines

Revision ID: dbc37b91f70b
Revises: 8a2f6c1d4e90
Create Date: 2026-10-06 10:43:20.917305

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "dbc37b91f70b"
down_revision = "8a2f6c1d4e90"
branch_labels = None
depends_on = None


def upgrade():
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
            "schedule_setup_pending backfill left NULL rows."
        )


def downgrade():
    op.drop_column("user_medicines", "schedule_setup_pending")
