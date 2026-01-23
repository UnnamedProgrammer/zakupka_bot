"""drop unique from users.tg_username

Revision ID: 0007_drop_tg_username_unique
Revises: 0006_user_roles
Create Date: 2026-01-17 00:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0007_drop_tg_username_unique"
down_revision = "0006_user_roles"
branch_labels = None
depends_on = None


def _drop_tg_username_unique() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table("users") as batch_op:
            batch_op.alter_column(
                "tg_username",
                existing_type=sa.String(length=100),
                unique=False,
            )
        return

    inspector = inspect(bind)
    for constraint in inspector.get_unique_constraints("users"):
        if constraint.get("column_names") == ["tg_username"]:
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, "users", type_="unique")
                return
    for index in inspector.get_indexes("users"):
        if index.get("unique") and index.get("column_names") == ["tg_username"]:
            op.drop_index(index["name"], table_name="users")
            return
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_tg_username_key"
        )


def _add_tg_username_unique() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table("users") as batch_op:
            batch_op.alter_column(
                "tg_username",
                existing_type=sa.String(length=100),
                unique=True,
            )
        return
    op.create_unique_constraint("users_tg_username_key", "users", ["tg_username"])


def upgrade() -> None:
    _drop_tg_username_unique()


def downgrade() -> None:
    _add_tg_username_unique()
