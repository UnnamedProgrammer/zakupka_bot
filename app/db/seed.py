from sqlalchemy import select

from app.db.models import (
    ApprovalStatus,
    Cfo,
    Department,
    RequestStatus,
    Role,
    User,
)


ROLE_DATA = [
    {"code": "employee", "name": "Сотрудник"},
    {"code": "approver", "name": "Согласующий"},
    {"code": "chief_approver", "name": "Главный согласующий"},
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

DEPARTMENT_DATA = [
    "АКБ СААЖ",
    "ИЭБ СААЖ",
    "Проектный модуль СААЖ",
    "Организационный модуль СААЖ",
    "Блок питания СААЖ",
    "СПА-салон",
]

CFO_DATA = [
    "Коттеджный поселок «Три медведя»",
    "Пирамида",
    "Хостелы",
    "СААЖ Южный парк (ОЭЗ)",
    "СААЖ Южный парк 2",
    "СААЖ ИО",
    "СААЖ Европа",
    "СААЖ Каллисто",
    "СААЖ Ганимед",
    "СААЖ Клининговый блок",
    "СПА-салон",
    "Блок питания – Якитория",
    "Блок питания – Яковлев",
    "Отдел организации питания – Магазин",
    "Отдел организации питания – Шоколадница",
    "Служба администрирования арендного жилья",
]

DEFAULT_USERS = [
    {
        "full_name": "Гайнутдинов Руслан Фаргатович",
        "role_code": "approver",
        "is_default_approver": True,
    },
    {
        "full_name": "Тихонова Людмила Васильевна",
        "role_code": "approver",
        "is_default_approver": True,
    },
    {"full_name": "Ковалев Д.А.", "role_code": "approver"},
    {"full_name": "Голубцова Анастасия Александровна", "role_code": "chief_approver"},
    {"full_name": "Губайдуллин Рамиль Рашитович", "role_code": "executor"},
    {"full_name": "Азизова Амира Фаридовна", "role_code": "executor"},
    {"full_name": "Шаймарданова Алина Рашидовна", "role_code": "executor"},
    {"full_name": "Зарипов Инсаф Илфакович", "role_code": "executor"},
]


async def seed_reference_data(session) -> None:
    await _seed_roles(session)
    await _seed_statuses(session)
    await _seed_departments(session)
    await _seed_cfos(session)
    await _seed_default_users(session)
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


async def _seed_departments(session) -> None:
    existing = {row[0] for row in (await session.execute(select(Department.name))).all()}
    for name in DEPARTMENT_DATA:
        if name not in existing:
            session.add(Department(name=name))


async def _seed_cfos(session) -> None:
    existing = {row[0] for row in (await session.execute(select(Cfo.name))).all()}
    for name in CFO_DATA:
        if name not in existing:
            session.add(Cfo(name=name))


async def _seed_default_users(session) -> None:
    roles = {
        row[0]: row[1]
        for row in (
            await session.execute(select(Role.code, Role.id))
        ).all()
    }
    existing = {row[0] for row in (await session.execute(select(User.full_name))).all()}
    for item in DEFAULT_USERS:
        if item["full_name"] in existing:
            continue
        role_id = roles.get(item["role_code"])
        if role_id:
            session.add(
                User(
                    full_name=item["full_name"],
                    role_id=role_id,
                    is_default_approver=item.get("is_default_approver", False),
                )
            )
