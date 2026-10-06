"""add medication plan schema

Revision ID: 4e2b7c91a6d5
Revises: dbc37b91f70b
Create Date: 2026-10-06

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4e2b7c91a6d5"
down_revision = "dbc37b91f70b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "medication_plans",
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_medication_plan_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_id"),
    )
    op.create_index(
        "ix_medication_plans_user_id",
        "medication_plans",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "uq_active_medication_plan_per_user",
        "medication_plans",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "medication_plan_times",
        sa.Column("plan_time_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("time_of_day", sa.Time(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["medication_plans.plan_id"],
            name="fk_medication_plan_time_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_time_id"),
        sa.UniqueConstraint(
            "plan_id",
            "plan_time_id",
            name="uq_medication_plan_time_parent_key",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "time_of_day",
            name="uq_medication_plan_time",
        ),
    )
    op.create_index(
        "ix_medication_plan_times_plan_id",
        "medication_plan_times",
        ["plan_id"],
        unique=False,
    )

    # SQLite can add nullable columns with inline foreign keys without
    # rebuilding medication_schedules. This keeps all existing parent and
    # child rows untouched during the additive upgrade.
    op.execute(
        "ALTER TABLE medication_schedules "
        "ADD COLUMN plan_id INTEGER "
        "REFERENCES medication_plans(plan_id)"
    )
    op.add_column(
        "medication_schedules",
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        "ALTER TABLE medication_schedules "
        "ADD COLUMN supersedes_schedule_id INTEGER "
        "REFERENCES medication_schedules(schedule_id)"
    )
    op.create_index(
        "ix_medication_schedules_plan_id",
        "medication_schedules",
        ["plan_id"],
        unique=False,
    )
    op.create_index(
        "uq_medication_schedule_plan_parent_key",
        "medication_schedules",
        ["plan_id", "schedule_id"],
        unique=True,
    )
    op.create_index(
        "uq_medication_schedule_supersedes",
        "medication_schedules",
        ["supersedes_schedule_id"],
        unique=True,
    )

    op.create_table(
        "medication_schedule_plan_times",
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("plan_time_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "schedule_id"],
            [
                "medication_schedules.plan_id",
                "medication_schedules.schedule_id",
            ],
            name="fk_schedule_plan_time_schedule",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "plan_time_id"],
            [
                "medication_plan_times.plan_id",
                "medication_plan_times.plan_time_id",
            ],
            name="fk_schedule_plan_time_plan_time",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "schedule_id",
            "plan_time_id",
            name="pk_medication_schedule_plan_time",
        ),
    )


def downgrade():
    op.drop_table("medication_schedule_plan_times")

    op.drop_index(
        "uq_medication_schedule_supersedes",
        table_name="medication_schedules",
    )
    op.drop_index(
        "uq_medication_schedule_plan_parent_key",
        table_name="medication_schedules",
    )
    op.drop_index(
        "ix_medication_schedules_plan_id",
        table_name="medication_schedules",
    )
    op.drop_column(
        "medication_schedules",
        "supersedes_schedule_id",
    )
    op.drop_column("medication_schedules", "closed_at")
    op.drop_column("medication_schedules", "plan_id")

    op.drop_index(
        "ix_medication_plan_times_plan_id",
        table_name="medication_plan_times",
    )
    op.drop_table("medication_plan_times")

    op.drop_index(
        "uq_active_medication_plan_per_user",
        table_name="medication_plans",
    )
    op.drop_index(
        "ix_medication_plans_user_id",
        table_name="medication_plans",
    )
    op.drop_table("medication_plans")
