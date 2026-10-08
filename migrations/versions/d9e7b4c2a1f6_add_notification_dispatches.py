"""add notification dispatches

Revision ID: d9e7b4c2a1f6
Revises: c6f4a2d9e8b1
Create Date: 2026-10-08

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d9e7b4c2a1f6"
down_revision = "c6f4a2d9e8b1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_dispatches",
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column(
            "course_root_schedule_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "notification_type",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "delivery_channel",
            sa.String(length=16),
            server_default="email",
            nullable=False,
        ),
        sa.Column(
            "due_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "provider_message_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "last_error_code",
            sa.String(length=64),
            nullable=True,
        ),
        sa.CheckConstraint(
            "notification_type IN "
            "('scheduled_occurrence', 'unanswered_review')",
            name="ck_notification_dispatch_type",
        ),
        sa.CheckConstraint(
            "delivery_channel IN ('email')",
            name="ck_notification_dispatch_channel",
        ),
        sa.CheckConstraint(
            "status IN "
            "('pending', 'claimed', 'sent', 'failed', 'canceled')",
            name="ck_notification_dispatch_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_notification_dispatch_attempt_count",
        ),
        sa.CheckConstraint(
            "(notification_type = 'scheduled_occurrence' "
            "AND course_root_schedule_id IS NULL "
            "AND (plan_id IS NOT NULL "
            "OR status IN ('sent', 'failed', 'canceled'))) "
            "OR (notification_type = 'unanswered_review' "
            "AND course_root_schedule_id IS NOT NULL)",
            name="ck_notification_dispatch_identity",
        ),
        sa.CheckConstraint(
            "(status = 'claimed' "
            "AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL) "
            "OR (status <> 'claimed' "
            "AND claim_token IS NULL "
            "AND claimed_at IS NULL)",
            name="ck_notification_dispatch_claim_state",
        ),
        sa.CheckConstraint(
            "(status = 'sent' AND sent_at IS NOT NULL) "
            "OR (status <> 'sent' AND sent_at IS NULL)",
            name="ck_notification_dispatch_sent_state",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_notification_dispatch_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["medication_plans.plan_id"],
            name="fk_notification_dispatch_plan",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["course_root_schedule_id"],
            ["medication_schedules.schedule_id"],
            name="fk_notification_dispatch_course_root_schedule",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("dispatch_id"),
    )
    op.create_index(
        "uq_notification_dispatch_scheduled",
        "notification_dispatches",
        ["user_id", "plan_id", "due_at", "notification_type"],
        unique=True,
        sqlite_where=sa.text(
            "notification_type = 'scheduled_occurrence' "
            "AND plan_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_notification_dispatch_unanswered_review",
        "notification_dispatches",
        [
            "user_id",
            "course_root_schedule_id",
            "notification_type",
        ],
        unique=True,
        sqlite_where=sa.text(
            "notification_type = 'unanswered_review' "
            "AND course_root_schedule_id IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_notification_dispatch_worker_due",
        "notification_dispatches",
        ["status", "next_attempt_at", "due_at"],
        unique=False,
    )

    op.create_table(
        "notification_dispatch_members",
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["notification_dispatches.dispatch_id"],
            name="fk_notification_dispatch_member_dispatch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["medication_occurrences.occurrence_id"],
            name="fk_notification_dispatch_member_occurrence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "dispatch_id",
            "occurrence_id",
            name="pk_notification_dispatch_member",
        ),
    )
    op.create_index(
        "ix_notification_dispatch_members_occurrence_id",
        "notification_dispatch_members",
        ["occurrence_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_notification_dispatch_members_occurrence_id",
        table_name="notification_dispatch_members",
    )
    op.drop_table("notification_dispatch_members")

    op.drop_index(
        "ix_notification_dispatch_worker_due",
        table_name="notification_dispatches",
    )
    op.drop_index(
        "uq_notification_dispatch_unanswered_review",
        table_name="notification_dispatches",
    )
    op.drop_index(
        "uq_notification_dispatch_scheduled",
        table_name="notification_dispatches",
    )
    op.drop_table("notification_dispatches")
