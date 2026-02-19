from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    Table,
    Column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("role_id", ForeignKey("roles.id"), primary_key=True),
)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship(
        secondary=user_roles, back_populates="roles", lazy="raise"
    )


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="department", lazy="raise")
    requests: Mapped[list["Request"]] = relationship(back_populates="department", lazy="raise")


class Cfo(Base):
    __tablename__ = "cfos"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    requests: Mapped[list["Request"]] = relationship(back_populates="cfo", lazy="raise")


class OmtsResponsible(Base):
    __tablename__ = "omts_responsibles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    items: Mapped[list["RequestItem"]] = relationship(
        back_populates="omts_responsible",
        lazy="raise",
    )


class RequestCategory(Base):
    __tablename__ = "request_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    items: Mapped[list["RequestItem"]] = relationship(
        back_populates="category",
        lazy="raise",
    )


class DdsArticle(Base):
    __tablename__ = "dds_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    items: Mapped[list["RequestItem"]] = relationship(
        back_populates="dds_article",
        lazy="raise",
    )


class RequestStatus(Base):
    __tablename__ = "request_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    requests: Mapped[list["Request"]] = relationship(back_populates="status", lazy="raise")


class ApprovalStatus(Base):
    __tablename__ = "approval_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    approvals: Mapped[list["Approval"]] = relationship(back_populates="status", lazy="raise")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(100))
    full_name: Mapped[str | None] = mapped_column(String(200))
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default_approver: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow
    )

    roles: Mapped[list["Role"]] = relationship(
        secondary=user_roles, back_populates="users", lazy="raise"
    )
    department: Mapped["Department"] = relationship(back_populates="users", lazy="raise")
    created_requests: Mapped[list["Request"]] = relationship(
        back_populates="initiator",
        foreign_keys="Request.initiator_id",
        lazy="raise",
    )
    assigned_requests: Mapped[list["Request"]] = relationship(
        back_populates="executor",
        foreign_keys="Request.executor_id",
        lazy="raise",
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="approver",
        foreign_keys="Approval.approver_id",
        lazy="raise",
    )
    comments: Mapped[list["Comment"]] = relationship(back_populates="author", lazy="raise")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="uploader", lazy="raise")


class Request(Base):
    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    status_id: Mapped[int] = mapped_column(ForeignKey("request_statuses.id"), nullable=False)
    initiator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    cfo_id: Mapped[int] = mapped_column(ForeignKey("cfos.id"), nullable=False)
    description_method: Mapped[str] = mapped_column(String(50), nullable=False)

    item_name: Mapped[str | None] = mapped_column(String(300))
    item_specs: Mapped[str | None] = mapped_column(Text)
    item_brand: Mapped[str | None] = mapped_column(Text)
    item_qty: Mapped[str | None] = mapped_column(String(50))
    item_unit: Mapped[str | None] = mapped_column(String(50))
    item_link: Mapped[str | None] = mapped_column(Text)
    item_note: Mapped[str | None] = mapped_column(Text)
    supplier_name: Mapped[str | None] = mapped_column(String(200))

    mol_full_name: Mapped[str | None] = mapped_column(String(200))
    contract_max_price: Mapped[str | None] = mapped_column(String(50))
    bdds_article_category: Mapped[str | None] = mapped_column(Text)

    executor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    expected_delivery_at: Mapped[dt.date | None] = mapped_column(Date)
    delivery_notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    received_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    status: Mapped["RequestStatus"] = relationship(back_populates="requests", lazy="raise")
    initiator: Mapped["User"] = relationship(
        back_populates="created_requests",
        foreign_keys=[initiator_id],
        lazy="raise",
    )
    executor: Mapped["User"] = relationship(
        back_populates="assigned_requests",
        foreign_keys=[executor_id],
        lazy="raise",
    )
    department: Mapped["Department"] = relationship(back_populates="requests", lazy="raise")
    cfo: Mapped["Cfo"] = relationship(back_populates="requests", lazy="raise")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="request", lazy="raise")
    comments: Mapped[list["Comment"]] = relationship(back_populates="request", lazy="raise")
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="request", order_by="Attachment.id", lazy="raise"
    )
    items: Mapped[list["RequestItem"]] = relationship(
        back_populates="request", order_by="RequestItem.id", lazy="raise"
    )


class RequestItem(Base):
    __tablename__ = "request_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(300))
    specs: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[str | None] = mapped_column(String(50))
    unit: Mapped[str | None] = mapped_column(String(50))
    link: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    max_price: Mapped[str | None] = mapped_column(String(50))
    omts_responsible_id: Mapped[int | None] = mapped_column(
        ForeignKey("omts_responsibles.id")
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("request_categories.id"))
    dds_article_id: Mapped[int | None] = mapped_column(ForeignKey("dds_articles.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    request: Mapped["Request"] = relationship(back_populates="items", lazy="raise")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="item", lazy="raise")
    omts_responsible: Mapped["OmtsResponsible"] = relationship(
        back_populates="items", lazy="raise"
    )
    category: Mapped["RequestCategory"] = relationship(back_populates="items", lazy="raise")
    dds_article: Mapped["DdsArticle"] = relationship(back_populates="items", lazy="raise")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(50))
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status_id: Mapped[int] = mapped_column(ForeignKey("approval_statuses.id"), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    request: Mapped["Request"] = relationship(back_populates="approvals", lazy="raise")
    approver: Mapped["User"] = relationship(
        back_populates="approvals",
        foreign_keys=[approver_id],
        lazy="raise",
    )
    status: Mapped["ApprovalStatus"] = relationship(back_populates="approvals", lazy="raise")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    request: Mapped["Request"] = relationship(back_populates="comments", lazy="raise")
    author: Mapped["User"] = relationship(back_populates="comments", lazy="raise")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("request_items.id"))
    file_id: Mapped[str | None] = mapped_column(String(200))
    file_unique_id: Mapped[str | None] = mapped_column(String(200))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    request: Mapped["Request"] = relationship(back_populates="attachments", lazy="raise")
    uploader: Mapped["User"] = relationship(back_populates="attachments", lazy="raise")
    item: Mapped["RequestItem"] = relationship(back_populates="attachments", lazy="raise")
