"""drop relation between cfos and departments

Revision ID: 0011_drop_cfo_dep_relation
Revises: 0010_add_cfo_department_relation
Create Date: 2026-02-17 00:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0011_drop_cfo_dep_relation"
down_revision = "0010_add_cfo_department_relation"
branch_labels = None
depends_on = None


_FK_NAME = "fk_cfos_department_id_departments"
_UQ_DEP_NAME = "uq_cfos_department_id_name"


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_unique(columns: list[str]) -> bool:
    inspector = inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("cfos"):
        if constraint.get("column_names") == columns:
            return True
    for index in inspector.get_indexes("cfos"):
        if index.get("unique") and index.get("column_names") == columns:
            return True
    return False


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


def _drop_cfo_department_name_unique() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    for constraint in inspector.get_unique_constraints("cfos"):
        if constraint.get("column_names") == ["department_id", "name"]:
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, "cfos", type_="unique")
                return
    for index in inspector.get_indexes("cfos"):
        if index.get("unique") and index.get("column_names") == ["department_id", "name"]:
            op.drop_index(index["name"], table_name="cfos")
            return
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE cfos DROP CONSTRAINT IF EXISTS {_UQ_DEP_NAME}")


def _drop_department_fk() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    for fk in inspector.get_foreign_keys("cfos"):
        if fk.get("constrained_columns") == ["department_id"]:
            name = fk.get("name")
            if name:
                op.drop_constraint(name, "cfos", type_="foreignkey")
            elif bind.dialect.name == "postgresql":
                op.execute(f"ALTER TABLE cfos DROP CONSTRAINT IF EXISTS {_FK_NAME}")
            return
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE cfos DROP CONSTRAINT IF EXISTS {_FK_NAME}")


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column("cfos", "department_id"):
        if not _has_unique(["name"]):
            _add_cfo_name_unique()
        return

    duplicates = bind.execute(
        sa.text(
            """
            SELECT name, count(*) AS cnt
            FROM cfos
            GROUP BY name
            HAVING count(*) > 1
            ORDER BY cnt DESC, name
            LIMIT 10
            """
        )
    ).all()
    if duplicates:
        preview = ", ".join(f"{name} ({cnt})" for name, cnt in duplicates)
        raise RuntimeError(
            "Cannot remove cfos.department_id because duplicate CFO names exist: "
            f"{preview}. Deduplicate CFO names and retry migration."
        )

    _drop_cfo_department_name_unique()
    _drop_department_fk()

    with op.batch_alter_table("cfos") as batch_op:
        batch_op.drop_column("department_id")

    if not _has_unique(["name"]):
        _add_cfo_name_unique()


def downgrade() -> None:
    if _has_column("cfos", "department_id"):
        return

    with op.batch_alter_table("cfos") as batch_op:
        batch_op.add_column(sa.Column("department_id", sa.Integer(), nullable=True))

    bind = op.get_bind()

    op.execute(
        sa.text(
            """
            UPDATE cfos
            SET department_id = (
                SELECT d.id
                FROM departments AS d
                WHERE lower(trim(d.name)) = lower(trim(cfos.name))
                ORDER BY d.id
                LIMIT 1
            )
            WHERE department_id IS NULL
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
            "Downgrade failed: there are cfos rows without department_id. "
            "Create departments and set CFO mappings before retrying."
        )

    with op.batch_alter_table("cfos") as batch_op:
        batch_op.create_foreign_key(_FK_NAME, "departments", ["department_id"], ["id"])
        batch_op.alter_column(
            "department_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    _drop_cfo_name_unique()
    if not _has_unique(["department_id", "name"]):
        op.create_unique_constraint(_UQ_DEP_NAME, "cfos", ["department_id", "name"])
