"""add toss identity verification evidence

Revision ID: d36d259b5fdf
Revises: f3a7c9e1b2d4
Create Date: 2026-10-09

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d36d259b5fdf"
down_revision = "f3a7c9e1b2d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "toss_identity_verification_evidence",
        sa.Column(
            "toss_identity_verification_evidence_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "identity_verification_session_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "provider_transaction_id",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(provider_transaction_id) > 0",
            name="ck_toss_identity_evidence_transaction_not_empty",
        ),
        sa.CheckConstraint(
            "length(signature) > 0",
            name="ck_toss_identity_evidence_signature_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["identity_verification_session_id"],
            [
                "identity_verification_sessions."
                "identity_verification_session_id"
            ],
            name="fk_toss_identity_evidence_session",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "toss_identity_verification_evidence_id"
        ),
        sa.UniqueConstraint(
            "identity_verification_session_id",
            name="uq_toss_identity_evidence_session",
        ),
        sa.UniqueConstraint(
            "provider_transaction_id",
            name="uq_toss_identity_evidence_transaction",
        ),
    )


def downgrade():
    op.drop_table("toss_identity_verification_evidence")
