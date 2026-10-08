"""add medication occurrences

Revision ID: c6f4a2d9e8b1
Revises: 7f2c9d1a4b6e
Create Date: 2026-10-08

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c6f4a2d9e8b1"
down_revision = "7f2c9d1a4b6e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "medication_occurrences",
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("response_status", sa.String(length=16), nullable=True),
        sa.Column(
            "responded_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
        sa.CheckConstraint(
            "response_status IS NULL OR "
            "response_status IN ('taken', 'not_taken')",
            name="ck_medication_occurrence_response_status",
        ),
        sa.CheckConstraint(
            "(response_status IS NULL AND responded_at IS NULL) OR "
            "(response_status IS NOT NULL AND responded_at IS NOT NULL)",
            name="ck_medication_occurrence_response_timestamp",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["medication_schedules.schedule_id"],
            name="fk_medication_occurrence_schedule",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("occurrence_id"),
        sa.UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_medication_occurrence_schedule_scheduled_for",
        ),
    )
    op.create_index(
        "ix_medication_occurrences_scheduled_for",
        "medication_occurrences",
        ["scheduled_for"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_medication_occurrences_scheduled_for",
        table_name="medication_occurrences",
    )
    op.drop_table("medication_occurrences")
