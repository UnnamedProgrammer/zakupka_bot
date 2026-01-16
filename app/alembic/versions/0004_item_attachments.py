"""add item_id to attachments

Revision ID: 0004_item_attachments
Revises: 0003_request_items
Create Date: 2026-01-16 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_item_attachments"
down_revision = "0003_request_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("item_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "attachments_item_id_fkey",
        "attachments",
        "request_items",
        ["item_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("attachments_item_id_fkey", "attachments", type_="foreignkey")
    op.drop_column("attachments", "item_id")
