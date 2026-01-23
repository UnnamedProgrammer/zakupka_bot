"""add user_roles association

Revision ID: 0006_user_roles
Revises: 0005_request_item_refs
Create Date: 2026-01-16 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_user_roles"
down_revision = "0005_request_item_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )
    op.execute(
        "INSERT INTO user_roles (user_id, role_id) "
        "SELECT id, role_id FROM users WHERE role_id IS NOT NULL"
    )
    op.drop_constraint("users_role_id_fkey", "users", type_="foreignkey")
    op.drop_column("users", "role_id")


def downgrade() -> None:
    op.add_column("users", sa.Column("role_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE users SET role_id = ("
        "SELECT role_id FROM user_roles WHERE user_roles.user_id = users.id "
        "ORDER BY role_id LIMIT 1)"
    )
    op.create_foreign_key("users_role_id_fkey", "users", "roles", ["role_id"], ["id"])
    op.drop_table("user_roles")
