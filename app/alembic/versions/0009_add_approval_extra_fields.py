"""add extra approval fields

Revision ID: 0009_add_approval_extra_fields
Revises: 0008_add_request_contract_fields
Create Date: 2026-02-12 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_add_approval_extra_fields"
down_revision = "0008_add_request_contract_fields"
branch_labels = None
depends_on = None


_FK_NAME = "fk_approvals_requested_by_id_users"


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("requested_by_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(_FK_NAME, "users", ["requested_by_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.drop_column("requested_by_id")
        batch_op.drop_column("kind")

