from sqlalchemy import select

from app.db.models import (
    ApprovalStatus,
    RequestStatus,
    Role,
)


ROLE_DATA = [
    {"code": "employee", "name": "Сотрудник"},
    {"code": "approver", "name": "Согласующий"},
    {"code": "executor", "name": "Исполнитель"},
    {"code": "admin", "name": "Администратор"},
]

REQUEST_STATUS_DATA = [
    {"code": "pending_approval", "name": "На согласовании"},
    {"code": "approved", "name": "Согласована"},
    {"code": "in_work", "name": "В работе"},
    {"code": "done", "name": "Выполнена"},
    {"code": "rejected", "name": "Отклонена"},
    {"code": "received", "name": "ТМЦ получено"},
]

APPROVAL_STATUS_DATA = [
    {"code": "pending", "name": "Ожидает"},
    {"code": "approved", "name": "Согласовано"},
    {"code": "rejected", "name": "Отклонено"},
]

async def seed_reference_data(session) -> None:
    await _seed_roles(session)
    await _seed_statuses(session)
    await session.commit()


async def _seed_roles(session) -> None:
    existing = {row[0] for row in (await session.execute(select(Role.code))).all()}
    for item in ROLE_DATA:
        if item["code"] not in existing:
            session.add(Role(**item))


async def _seed_statuses(session) -> None:
    req_existing = {row[0] for row in (await session.execute(select(RequestStatus.code))).all()}
    for item in REQUEST_STATUS_DATA:
        if item["code"] not in req_existing:
            session.add(RequestStatus(**item))

    app_existing = {row[0] for row in (await session.execute(select(ApprovalStatus.code))).all()}
    for item in APPROVAL_STATUS_DATA:
        if item["code"] not in app_existing:
            session.add(ApprovalStatus(**item))
