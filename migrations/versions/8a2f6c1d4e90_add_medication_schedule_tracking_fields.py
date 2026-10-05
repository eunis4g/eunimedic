"""add medication schedule tracking fields

Revision ID: 8a2f6c1d4e90
Revises: 3f0d3babd6b4
Create Date: 2026-10-05

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8a2f6c1d4e90"
down_revision = "3f0d3babd6b4"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    schedule_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM medication_schedules")
    ).scalar_one()

    if schedule_count:
        raise RuntimeError(
            "MedicationSchedule rows exist; review an explicit backfill "
            "strategy before applying this migration."
        )

    with op.batch_alter_table(
        "medication_schedules",
        schema=None,
        recreate="always",
    ) as batch_op:
        batch_op.add_column(
            sa.Column("course_days", sa.Integer(), nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "reported_doses_taken_before_tracking",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "reminder_tracking_started_at",
                sa.DateTime(timezone=True),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "accounted_occurrence_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_medication_schedule_intake_timing",
            "intake_timing IN "
            "('before_meal', 'after_meal', 'regardless_of_meal')",
        )
        batch_op.create_check_constraint(
            "ck_medication_schedule_course_days",
            "course_days >= 1",
        )
        batch_op.create_check_constraint(
            "ck_medication_schedule_reported_doses",
            "reported_doses_taken_before_tracking >= 0",
        )
        batch_op.create_check_constraint(
            "ck_medication_schedule_accounted_occurrences",
            "accounted_occurrence_count >= 0",
        )

    op.create_index(
        "uq_active_medication_schedule_per_user_medicine",
        "medication_schedules",
        ["user_medicine_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade():
    op.drop_index(
        "uq_active_medication_schedule_per_user_medicine",
        table_name="medication_schedules",
    )

    with op.batch_alter_table(
        "medication_schedules",
        schema=None,
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_medication_schedule_accounted_occurrences",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_medication_schedule_reported_doses",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_medication_schedule_course_days",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_medication_schedule_intake_timing",
            type_="check",
        )
        batch_op.drop_column("accounted_occurrence_count")
        batch_op.drop_column("reminder_tracking_started_at")
        batch_op.drop_column("reported_doses_taken_before_tracking")
        batch_op.drop_column("course_days")
