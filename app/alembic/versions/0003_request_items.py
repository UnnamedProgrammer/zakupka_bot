"""add request items and nullable attachment ids

Revision ID: 0003_request_items
Revises: 0002_add_request_done_at
Create Date: 2026-01-16 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_request_items"
down_revision = "0002_add_request_done_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("name", sa.String(length=300)),
        sa.Column("specs", sa.Text()),
        sa.Column("brand", sa.Text()),
        sa.Column("qty", sa.String(length=50)),
        sa.Column("unit", sa.String(length=50)),
        sa.Column("link", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.alter_column("attachments", "file_id", existing_type=sa.String(length=200), nullable=True)
    op.alter_column(
        "attachments", "file_unique_id", existing_type=sa.String(length=200), nullable=True
    )


def downgrade() -> None:
    op.alter_column("attachments", "file_unique_id", existing_type=sa.String(length=200), nullable=False)
    op.alter_column("attachments", "file_id", existing_type=sa.String(length=200), nullable=False)
    op.drop_table("request_items")
