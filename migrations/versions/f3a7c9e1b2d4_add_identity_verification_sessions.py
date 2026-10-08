"""add identity verification sessions

Revision ID: f3a7c9e1b2d4
Revises: d9e7b4c2a1f6
Create Date: 2026-10-08

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f3a7c9e1b2d4"
down_revision = "d9e7b4c2a1f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "identity_verification_sessions",
        sa.Column(
            "identity_verification_session_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "verification_session_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "provider_transaction_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "completion_claim_token",
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column(
            "completion_claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "identity_subject_digest",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "age_eligibility",
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column(
            "failure_code",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "consumed_at",
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
            "purpose IN ('android_registration')",
            name="ck_identity_verification_session_purpose",
        ),
        sa.CheckConstraint(
            "provider IN ('toss')",
            name="ck_identity_verification_session_provider",
        ),
        sa.CheckConstraint(
            "status IN "
            "('pending', 'verified', 'failed', 'expired', 'consumed')",
            name="ck_identity_verification_session_status",
        ),
        sa.CheckConstraint(
            "age_eligibility IS NULL OR age_eligibility IN "
            "('AGE_14_OR_OVER', 'UNDER_14')",
            name="ck_identity_verification_session_age_eligibility",
        ),
        sa.CheckConstraint(
            "identity_subject_digest IS NULL "
            "OR length(identity_subject_digest) = 64",
            name="ck_identity_verification_session_identity_digest_length",
        ),
        sa.CheckConstraint(
            "(completion_claim_token IS NULL "
            "AND completion_claimed_at IS NULL) "
            "OR (completion_claim_token IS NOT NULL "
            "AND completion_claimed_at IS NOT NULL)",
            name="ck_identity_verification_session_completion_claim_pair",
        ),
        sa.CheckConstraint(
            "completion_claim_token IS NULL OR status = 'pending'",
            name="ck_identity_verification_session_completion_claim_state",
        ),
        sa.CheckConstraint(
            "completion_claim_token IS NULL "
            "OR provider_transaction_id IS NOT NULL",
            name=(
                "ck_identity_verification_session_completion_claim_transaction"
            ),
        ),
        sa.CheckConstraint(
            "completion_claim_token IS NULL "
            "OR (identity_subject_digest IS NULL "
            "AND age_eligibility IS NULL "
            "AND verified_at IS NULL)",
            name="ck_identity_verification_session_completion_claim_result",
        ),
        sa.CheckConstraint(
            "(status IN ('verified', 'consumed') "
            "AND identity_subject_digest IS NOT NULL) "
            "OR (status IN ('pending', 'failed', 'expired') "
            "AND identity_subject_digest IS NULL)",
            name="ck_identity_verification_session_identity_digest_state",
        ),
        sa.CheckConstraint(
            "status NOT IN ('verified', 'consumed') "
            "OR (verified_at IS NOT NULL AND age_eligibility IS NOT NULL)",
            name="ck_identity_verification_session_verified_state",
        ),
        sa.CheckConstraint(
            "(status = 'consumed' AND consumed_at IS NOT NULL) "
            "OR (status <> 'consumed' AND consumed_at IS NULL)",
            name="ck_identity_verification_session_consumed_state",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_identity_verification_session_expiry",
        ),
        sa.PrimaryKeyConstraint("identity_verification_session_id"),
        sa.UniqueConstraint(
            "verification_session_id",
            name="uq_identity_verification_session_public_id",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_transaction_id",
            name="uq_identity_verification_session_provider_transaction",
        ),
    )
    op.create_index(
        "ix_identity_verification_session_status_expires_at",
        "identity_verification_sessions",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_identity_verification_session_expires_at",
        "identity_verification_sessions",
        ["expires_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_identity_verification_session_expires_at",
        table_name="identity_verification_sessions",
    )
    op.drop_index(
        "ix_identity_verification_session_status_expires_at",
        table_name="identity_verification_sessions",
    )
    op.drop_table("identity_verification_sessions")
