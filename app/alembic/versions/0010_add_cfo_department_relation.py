"""add one-to-many relation between departments and cfos

Revision ID: 0010_add_cfo_department_relation
Revises: 0009_add_approval_extra_fields
Create Date: 2026-02-16 00:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0010_add_cfo_department_relation"
down_revision = "0009_add_approval_extra_fields"
branch_labels = None
depends_on = None


_FK_NAME = "fk_cfos_department_id_departments"
_UQ_NAME = "uq_cfos_department_id_name"


def _drop_cfo_name_unique() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("cfos") as batch_op:
            batch_op.alter_column(
                "name",
                existing_type=sa.String(length=200),
                unique=False,
            )
        return

    inspector = inspect(bind)
    for constraint in inspector.get_unique_constraints("cfos"):
        if constraint.get("column_names") == ["name"]:
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, "cfos", type_="unique")
                return
    for index in inspector.get_indexes("cfos"):
        if index.get("unique") and index.get("column_names") == ["name"]:
            op.drop_index(index["name"], table_name="cfos")
            return
    if dialect == "postgresql":
        op.execute("ALTER TABLE cfos DROP CONSTRAINT IF EXISTS cfos_name_key")


def _add_cfo_name_unique() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("cfos") as batch_op:
            batch_op.alter_column(
                "name",
                existing_type=sa.String(length=200),
                unique=True,
            )
        return

    op.create_unique_constraint("cfos_name_key", "cfos", ["name"])


def upgrade() -> None:
    with op.batch_alter_table("cfos") as batch_op:
        batch_op.add_column(sa.Column("department_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(_FK_NAME, "departments", ["department_id"], ["id"])

    bind = op.get_bind()

    op.execute(
        sa.text(
            """
            UPDATE cfos AS c
            SET department_id = d.id
            FROM departments AS d
            WHERE c.department_id IS NULL
              AND lower(trim(c.name)) = lower(trim(d.name))
            """
        )
    )

    first_department_id = bind.execute(
        sa.text("SELECT id FROM departments ORDER BY id LIMIT 1")
    ).scalar()
    if first_department_id is not None:
        op.execute(
            sa.text(
                "UPDATE cfos SET department_id = :department_id WHERE department_id IS NULL"
            ),
            {"department_id": first_department_id},
        )

    remaining = bind.execute(
        sa.text("SELECT count(*) FROM cfos WHERE department_id IS NULL")
    ).scalar() or 0
    if remaining:
        raise RuntimeError(
            "Migration failed: there are cfos rows without department_id. "
            "Create departments and set CFO mappings before retrying."
        )

    with op.batch_alter_table("cfos") as batch_op:
        batch_op.alter_column(
            "department_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    _drop_cfo_name_unique()
    op.create_unique_constraint(_UQ_NAME, "cfos", ["department_id", "name"])


def downgrade() -> None:
    op.drop_constraint(_UQ_NAME, "cfos", type_="unique")
    _add_cfo_name_unique()

    with op.batch_alter_table("cfos") as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.drop_column("department_id")
