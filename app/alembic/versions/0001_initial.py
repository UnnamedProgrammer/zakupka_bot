"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2024-01-15 10:10:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
    )
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False, unique=True),
    )
    op.create_table(
        "cfos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False, unique=True),
    )
    op.create_table(
        "request_statuses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
    )
    op.create_table(
        "approval_statuses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.BigInteger(), unique=True),
        sa.Column("tg_username", sa.String(length=100), unique=True),
        sa.Column("full_name", sa.String(length=200)),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_default_approver", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status_id", sa.Integer(), sa.ForeignKey("request_statuses.id"), nullable=False),
        sa.Column("initiator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("cfo_id", sa.Integer(), sa.ForeignKey("cfos.id"), nullable=False),
        sa.Column("description_method", sa.String(length=50), nullable=False),
        sa.Column("item_name", sa.String(length=300)),
        sa.Column("item_specs", sa.Text()),
        sa.Column("item_brand", sa.Text()),
        sa.Column("item_qty", sa.String(length=50)),
        sa.Column("item_unit", sa.String(length=50)),
        sa.Column("item_link", sa.Text()),
        sa.Column("item_note", sa.Text()),
        sa.Column("supplier_name", sa.String(length=200)),
        sa.Column("mol_full_name", sa.String(length=200)),
        sa.Column("executor_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("expected_delivery_at", sa.Date()),
        sa.Column("delivery_notified_at", sa.DateTime()),
        sa.Column("received_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime()),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("approver_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status_id", sa.Integer(), sa.ForeignKey("approval_statuses.id"), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("decided_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("uploader_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("file_id", sa.String(length=200), nullable=False),
        sa.Column("file_unique_id", sa.String(length=200), nullable=False),
        sa.Column("file_name", sa.String(length=255)),
        sa.Column("file_path", sa.String(length=500)),
        sa.Column("file_type", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("attachments")
    op.drop_table("comments")
    op.drop_table("approvals")
    op.drop_table("requests")
    op.drop_table("users")
    op.drop_table("approval_statuses")
    op.drop_table("request_statuses")
    op.drop_table("cfos")
    op.drop_table("departments")
    op.drop_table("roles")
