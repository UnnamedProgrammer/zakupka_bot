"""add contract fields to requests

Revision ID: 0008_add_request_contract_fields
Revises: 0007_drop_tg_username_unique
Create Date: 2026-02-12 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_add_request_contract_fields"
down_revision = "0007_drop_tg_username_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requests") as batch_op:
        batch_op.add_column(
            sa.Column("contract_max_price", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(sa.Column("bdds_article_category", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("requests") as batch_op:
        batch_op.drop_column("bdds_article_category")
        batch_op.drop_column("contract_max_price")

