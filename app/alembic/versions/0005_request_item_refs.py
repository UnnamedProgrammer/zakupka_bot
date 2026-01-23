"""add request item references

Revision ID: 0005_request_item_refs
Revises: 0004_item_attachments
Create Date: 2026-01-16 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_request_item_refs"
down_revision = "0004_item_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "omts_responsibles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False, unique=True),
    )
    op.create_table(
        "request_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False, unique=True),
    )
    op.create_table(
        "dds_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False, unique=True),
    )

    op.add_column("request_items", sa.Column("max_price", sa.String(length=50), nullable=True))
    op.add_column(
        "request_items",
        sa.Column("omts_responsible_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "request_items",
        sa.Column("category_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "request_items",
        sa.Column("dds_article_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "request_items_omts_responsible_id_fkey",
        "request_items",
        "omts_responsibles",
        ["omts_responsible_id"],
        ["id"],
    )
    op.create_foreign_key(
        "request_items_category_id_fkey",
        "request_items",
        "request_categories",
        ["category_id"],
        ["id"],
    )
    op.create_foreign_key(
        "request_items_dds_article_id_fkey",
        "request_items",
        "dds_articles",
        ["dds_article_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "request_items_dds_article_id_fkey", "request_items", type_="foreignkey"
    )
    op.drop_constraint(
        "request_items_category_id_fkey", "request_items", type_="foreignkey"
    )
    op.drop_constraint(
        "request_items_omts_responsible_id_fkey", "request_items", type_="foreignkey"
    )
    op.drop_column("request_items", "dds_article_id")
    op.drop_column("request_items", "category_id")
    op.drop_column("request_items", "omts_responsible_id")
    op.drop_column("request_items", "max_price")

    op.drop_table("dds_articles")
    op.drop_table("request_categories")
    op.drop_table("omts_responsibles")
