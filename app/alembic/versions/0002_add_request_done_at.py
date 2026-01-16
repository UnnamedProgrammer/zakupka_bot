"""add done_at to requests

Revision ID: 0002_add_request_done_at
Revises: 0001_initial
Create Date: 2026-01-15 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_add_request_done_at"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requests", sa.Column("done_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("requests", "done_at")
