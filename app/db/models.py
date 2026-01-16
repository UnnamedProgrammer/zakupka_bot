from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="role")


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="department")
    requests: Mapped[list["Request"]] = relationship(back_populates="department")


class Cfo(Base):
    __tablename__ = "cfos"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    requests: Mapped[list["Request"]] = relationship(back_populates="cfo")


class RequestStatus(Base):
    __tablename__ = "request_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    requests: Mapped[list["Request"]] = relationship(back_populates="status")


class ApprovalStatus(Base):
    __tablename__ = "approval_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    approvals: Mapped[list["Approval"]] = relationship(back_populates="status")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(100), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default_approver: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow
    )

    role: Mapped["Role"] = relationship(back_populates="users")
    department: Mapped["Department"] = relationship(back_populates="users")
    created_requests: Mapped[list["Request"]] = relationship(
        back_populates="initiator", foreign_keys="Request.initiator_id"
    )
    assigned_requests: Mapped[list["Request"]] = relationship(
        back_populates="executor", foreign_keys="Request.executor_id"
    )
    approvals: Mapped[list["Approval"]] = relationship(back_populates="approver")
    comments: Mapped[list["Comment"]] = relationship(back_populates="author")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="uploader")


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

    status: Mapped["RequestStatus"] = relationship(back_populates="requests")
    initiator: Mapped["User"] = relationship(
        back_populates="created_requests", foreign_keys=[initiator_id]
    )
    executor: Mapped["User"] = relationship(
        back_populates="assigned_requests", foreign_keys=[executor_id]
    )
    department: Mapped["Department"] = relationship(back_populates="requests")
    cfo: Mapped["Cfo"] = relationship(back_populates="requests")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="request")
    comments: Mapped[list["Comment"]] = relationship(back_populates="request")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="request")
    items: Mapped[list["RequestItem"]] = relationship(
        back_populates="request", order_by="RequestItem.id"
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
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    request: Mapped["Request"] = relationship(back_populates="items")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False)
    approver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status_id: Mapped[int] = mapped_column(ForeignKey("approval_statuses.id"), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    request: Mapped["Request"] = relationship(back_populates="approvals")
    approver: Mapped["User"] = relationship(back_populates="approvals")
    status: Mapped["ApprovalStatus"] = relationship(back_populates="approvals")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    request: Mapped["Request"] = relationship(back_populates="comments")
    author: Mapped["User"] = relationship(back_populates="comments")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), nullable=False)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(200))
    file_unique_id: Mapped[str | None] = mapped_column(String(200))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    request: Mapped["Request"] = relationship(back_populates="attachments")
    uploader: Mapped["User"] = relationship(back_populates="attachments")
