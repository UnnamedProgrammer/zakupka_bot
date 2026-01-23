from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.bot.keyboards import (
    roles_keyboard,
    settings_keyboard,
    settings_list_keyboard,
    users_menu_keyboard,
    users_list_keyboard,
    request_edit_keyboard,
    request_fields_keyboard,
    request_items_keyboard,
    request_item_fields_keyboard,
    request_status_keyboard,
    departments_keyboard,
    cfo_keyboard,
)
from app.bot.states import (
    AdminAddCfo,
    AdminAddDepartment,
    AdminAddUser,
    AdminEditRequest,
)
from app.db.models import (
    Cfo,
    Department,
    Role,
    User,
    Request,
    RequestItem,
    RequestStatus,
    OmtsResponsible,
    RequestCategory,
    DdsArticle,
    user_roles,
)
from app.db.session import SessionLocal
from app.config import settings
from app.services.users import ensure_username_format, get_or_create_user, user_has_role
from app.services.excel import upsert_request_excel
from app.services.formatters import format_request_summary

router = Router()


async def _is_admin(session, user_id: int) -> bool:
    return await user_has_role(session, user_id, "admin")


async def _require_admin(callback: CallbackQuery) -> bool:
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        if not await _is_admin(session, user.id):
            await callback.answer("Нет доступа")
            return False
    return True


def _clean_optional(value: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered in {"-", "—", "нет", "пропустить", "skip"}:
        return None
    return text


async def _get_or_create_reference(session, model, name: str | None) -> int | None:
    normalized = _clean_optional(name)
    if not normalized:
        return None
    key = " ".join(normalized.split()).casefold()
    obj = await session.scalar(select(model).where(func.lower(func.trim(model.name)) == key))
    if not obj:
        obj = model(name=normalized)
        session.add(obj)
        await session.flush()
    return obj.id


async def _sync_request_primary_item(session, request_id: int) -> None:
    request = await session.get(Request, request_id)
    if not request:
        return
    first_item = await session.scalar(
        select(RequestItem)
        .where(RequestItem.request_id == request_id)
        .order_by(RequestItem.id)
    )
    if not first_item:
        request.item_name = None
        request.item_specs = None
        request.item_brand = None
        request.item_qty = None
        request.item_unit = None
        request.item_link = None
        request.item_note = None
        return
    request.item_name = first_item.name
    request.item_specs = first_item.specs
    request.item_brand = first_item.brand
    request.item_qty = first_item.qty
    request.item_unit = first_item.unit
    request.item_link = first_item.link
    request.item_note = first_item.note


async def _send_users_list(message: Message, role_key: str) -> None:
    role_map = {
        "employee": ["employee"],
        "executor": ["executor"],
        "leaders": ["approver", "chief_approver"],
    }
    role_codes = role_map.get(role_key, [role_key])
    async with SessionLocal() as session:
        rows = await session.execute(
            select(User.id, User.full_name, User.is_active)
            .join(user_roles, user_roles.c.user_id == User.id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(Role.code.in_(role_codes))
            .order_by(User.full_name)
        )
        users = {}
        for user_id, full_name, is_active in rows.all():
            if user_id not in users:
                users[user_id] = (user_id, full_name or f"ID {user_id}", is_active)
    title = {
        "employee": "Инициаторы",
        "executor": "Исполнители",
        "leaders": "Руководители",
    }.get(role_key, "Пользователи")
    if not users:
        await message.answer(f"{title}: список пуст.", reply_markup=users_menu_keyboard())
        return
    await message.answer(title, reply_markup=users_list_keyboard(role_key, list(users.values())))


async def _load_request_full(session, request_id: int) -> Request | None:
    result = await session.execute(
        select(Request)
        .where(Request.id == request_id)
        .options(
            selectinload(Request.initiator),
            selectinload(Request.department),
            selectinload(Request.cfo),
            selectinload(Request.status),
            selectinload(Request.items),
            selectinload(Request.comments),
        )
    )
    return result.scalar_one_or_none()


async def _send_request_menu(message: Message, request_id: int) -> None:
    async with SessionLocal() as session:
        request = await _load_request_full(session, request_id)
        if not request:
            await message.answer("Заявка не найдена.")
            return
        await message.answer(
            format_request_summary(request),
            reply_markup=request_edit_keyboard(request_id),
        )


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        if not await _is_admin(session, user.id):
            await message.answer("Нет доступа.")
            return
    await message.answer("Настройки", reply_markup=settings_keyboard())


@router.callback_query(F.data == "settings:departments")
async def settings_departments(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    async with SessionLocal() as session:
        rows = await session.execute(select(Department.id, Department.name).order_by(Department.name))
        deps = rows.all()
    await callback.message.answer(
        "Подразделения", reply_markup=settings_list_keyboard("departments", deps)
    )
    await callback.answer()


@router.callback_query(F.data == "departments:add")
async def department_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddDepartment.name)
    await callback.message.answer("Введите название подразделения")
    await callback.answer()


@router.message(AdminAddDepartment.name)
async def department_add_finish(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        session.add(Department(name=message.text.strip()))
        await session.commit()
    await state.clear()
    await message.answer("Подразделение добавлено.")


@router.callback_query(F.data.startswith("departments:del:"))
async def department_delete(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    dep_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        dep = await session.get(Department, dep_id)
        if dep:
            await session.delete(dep)
            await session.commit()
    await callback.answer("Удалено")


@router.callback_query(F.data == "settings:cfos")
async def settings_cfos(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    async with SessionLocal() as session:
        rows = await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
        items = rows.all()
    await callback.message.answer("ЦФО", reply_markup=settings_list_keyboard("cfos", items))
    await callback.answer()


@router.callback_query(F.data == "cfos:add")
async def cfo_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddCfo.name)
    await callback.message.answer("Введите название ЦФО")
    await callback.answer()


@router.message(AdminAddCfo.name)
async def cfo_add_finish(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        session.add(Cfo(name=message.text.strip()))
        await session.commit()
    await state.clear()
    await message.answer("ЦФО добавлено.")


@router.callback_query(F.data.startswith("cfos:del:"))
async def cfo_delete(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    cfo_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        cfo = await session.get(Cfo, cfo_id)
        if cfo:
            await session.delete(cfo)
            await session.commit()
    await callback.answer("Удалено")


@router.callback_query(F.data == "settings:users")
async def settings_users(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.clear()
    await callback.message.answer("Пользователи", reply_markup=users_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "users:add")
async def user_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddUser.full_name)
    await callback.message.answer("Введите ФИО пользователя")
    await callback.answer()


@router.message(AdminAddUser.full_name)
async def user_add_full_name(message: Message, state: FSMContext) -> None:
    full_name = message.text.strip()
    if not full_name:
        await message.answer("ФИО не может быть пустым. Введите ФИО пользователя.")
        return
    await state.update_data(full_name=full_name)
    async with SessionLocal() as session:
        roles = (await session.execute(select(Role.id, Role.name).order_by(Role.name))).all()
    await state.set_state(AdminAddUser.role)
    await message.answer("Выберите роль", reply_markup=roles_keyboard(roles))


@router.callback_query(AdminAddUser.role, F.data.startswith("role:"))
async def user_add_role(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    role_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    full_name = data.get("full_name")
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.full_name == full_name))
        if not user:
            user = User(full_name=full_name, is_active=True)
            session.add(user)
            await session.flush()
        else:
            user.is_active = True
        exists = await session.scalar(
            select(user_roles.c.user_id).where(
                user_roles.c.user_id == user.id, user_roles.c.role_id == role_id
            )
        )
        if not exists:
            await session.execute(
                user_roles.insert().values(user_id=user.id, role_id=role_id)
            )
        await session.commit()
    await state.clear()
    await callback.message.answer("Пользователь сохранен.", reply_markup=users_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("users:list:"))
async def users_list(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    role_key = callback.data.split(":")[2]
    await _send_users_list(callback.message, role_key)
    await callback.answer()


@router.callback_query(F.data.startswith("users:toggle:"))
async def users_toggle(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    _, _, role_key, user_id = callback.data.split(":")
    async with SessionLocal() as session:
        user = await session.get(User, int(user_id))
        if user:
            user.is_active = not user.is_active
            await session.commit()
    await _send_users_list(callback.message, role_key)
    await callback.answer()


@router.callback_query(F.data == "settings:requests")
async def settings_requests(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminEditRequest.request_id)
    await callback.message.answer("Введите ID заявки для редактирования")
    await callback.answer()


@router.message(AdminEditRequest.request_id)
async def request_edit_start(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("ID заявки должен быть числом.")
        return
    request_id = int(raw)
    await state.clear()
    await _send_request_menu(message, request_id)


@router.callback_query(F.data.startswith("req_edit:menu:"))
async def request_edit_menu(callback: CallbackQuery) -> None:
    request_id = int(callback.data.split(":")[2])
    await _send_request_menu(callback.message, request_id)
    await callback.answer()


@router.callback_query(F.data.startswith("req_edit:fields:"))
async def request_edit_fields(callback: CallbackQuery) -> None:
    request_id = int(callback.data.split(":")[2])
    await callback.message.answer(
        "Выберите поле для редактирования",
        reply_markup=request_fields_keyboard(request_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_edit:items:"))
async def request_edit_items(callback: CallbackQuery) -> None:
    request_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        items = (
            await session.execute(
                select(RequestItem.id, RequestItem.name)
                .where(RequestItem.request_id == request_id)
                .order_by(RequestItem.id)
            )
        ).all()
    if not items:
        await callback.message.answer(
            "У заявки пока нет товаров.",
            reply_markup=request_edit_keyboard(request_id),
        )
        await callback.answer()
        return
    await callback.message.answer(
        "Выберите товар для редактирования",
        reply_markup=request_items_keyboard(request_id, items),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_edit:item_add:"))
async def request_item_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = int(callback.data.split(":")[2])
    await state.set_state(AdminEditRequest.item_add_name)
    await state.update_data(request_id=request_id)
    await callback.message.answer("Введите наименование товара")
    await callback.answer()


@router.callback_query(F.data.startswith("req_edit_field:"))
async def request_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, field = callback.data.split(":")
    request_id = int(request_id)
    await state.update_data(request_id=request_id, edit_field=field)
    if field == "department":
        async with SessionLocal() as session:
            deps = (
                await session.execute(
                    select(Department.id, Department.name).order_by(Department.name)
                )
            ).all()
        await state.set_state(AdminEditRequest.field_value)
        await callback.message.answer(
            "Выберите подразделение", reply_markup=departments_keyboard(deps)
        )
    elif field == "cfo":
        async with SessionLocal() as session:
            cfos = (
                await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
            ).all()
        await state.set_state(AdminEditRequest.field_value)
        await callback.message.answer("Выберите ЦФО", reply_markup=cfo_keyboard(cfos))
    elif field == "status":
        async with SessionLocal() as session:
            statuses = (
                await session.execute(
                    select(RequestStatus.id, RequestStatus.name).order_by(RequestStatus.name)
                )
            ).all()
        await state.set_state(AdminEditRequest.field_value)
        await callback.message.answer(
            "Выберите статус",
            reply_markup=request_status_keyboard(statuses, request_id),
        )
    elif field == "executor":
        async with SessionLocal() as session:
            rows = await session.execute(
                select(User.id, User.full_name)
                .join(user_roles, user_roles.c.user_id == User.id)
                .join(Role, Role.id == user_roles.c.role_id)
                .where(Role.code == "executor")
                .order_by(User.full_name)
            )
            executors = rows.all()
        builder = InlineKeyboardBuilder()
        for user_id, name in executors:
            builder.button(
                text=name or f"ID {user_id}",
                callback_data=f"req_executor:{request_id}:{user_id}",
            )
        builder.button(text="⬅️ Назад", callback_data=f"req_edit:fields:{request_id}")
        builder.adjust(1)
        await state.set_state(AdminEditRequest.field_value)
        await callback.message.answer(
            "Выберите исполнителя", reply_markup=builder.as_markup()
        )
    else:
        await state.set_state(AdminEditRequest.field_value)
        prompt = {
            "initiator": "Введите ФИО инициатора",
            "mol": "Введите ФИО МОЛ",
            "supplier": "Введите поставщика",
            "delivery": "Введите дату поставки (DD-MM-YYYY)",
        }.get(field, "Введите значение")
        await callback.message.answer(prompt)
    await callback.answer()


@router.callback_query(AdminEditRequest.field_value, F.data.startswith("dept:"))
async def request_edit_department(callback: CallbackQuery, state: FSMContext) -> None:
    dep_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    request_id = data.get("request_id")
    if not request_id:
        await callback.answer("Нет заявки")
        return
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if request:
            request.department_id = dep_id
            await upsert_request_excel(session, request, settings.files_dir)
            await session.commit()
    await state.clear()
    await _send_request_menu(callback.message, request_id)
    await callback.answer("Подразделение обновлено")


@router.callback_query(AdminEditRequest.field_value, F.data.startswith("cfo:"))
async def request_edit_cfo(callback: CallbackQuery, state: FSMContext) -> None:
    cfo_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    request_id = data.get("request_id")
    if not request_id:
        await callback.answer("Нет заявки")
        return
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if request:
            request.cfo_id = cfo_id
            await upsert_request_excel(session, request, settings.files_dir)
            await session.commit()
    await state.clear()
    await _send_request_menu(callback.message, request_id)
    await callback.answer("ЦФО обновлено")


@router.callback_query(AdminEditRequest.field_value, F.data.startswith("req_status:"))
async def request_edit_status(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, status_id = callback.data.split(":")
    request_id = int(request_id)
    status_id = int(status_id)
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if request:
            request.status_id = status_id
            await upsert_request_excel(session, request, settings.files_dir)
            await session.commit()
    await state.clear()
    await _send_request_menu(callback.message, request_id)
    await callback.answer("Статус обновлен")


@router.callback_query(AdminEditRequest.field_value, F.data.startswith("req_executor:"))
async def request_edit_executor(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, executor_id = callback.data.split(":")
    request_id = int(request_id)
    executor_id = int(executor_id)
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if request:
            request.executor_id = executor_id
            await upsert_request_excel(session, request, settings.files_dir)
            await session.commit()
    await state.clear()
    await _send_request_menu(callback.message, request_id)
    await callback.answer("Исполнитель обновлен")


@router.message(AdminEditRequest.field_value)
async def request_edit_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    field = data.get("edit_field")
    if not request_id or not field:
        await message.answer("Нет данных для редактирования.")
        await state.clear()
        return
    value = message.text.strip()
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if not request:
            await message.answer("Заявка не найдена.")
            await state.clear()
            return
        if field == "initiator":
            user = await session.scalar(select(User).where(User.full_name == value))
            if not user:
                await message.answer("Инициатор не найден. Проверьте ФИО.")
                return
            request.initiator_id = user.id
        elif field == "mol":
            request.mol_full_name = _clean_optional(value)
        elif field == "supplier":
            request.supplier_name = _clean_optional(value)
        elif field == "delivery":
            try:
                request.expected_delivery_at = (
                    datetime.strptime(value, "%d-%m-%Y").date()
                )
            except ValueError:
                await message.answer("Некорректная дата. Формат: DD-MM-YYYY")
                return
        await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()
    await state.clear()
    await _send_request_menu(message, request_id)


@router.callback_query(F.data.startswith("req_item:"))
async def request_item_edit_start(callback: CallbackQuery) -> None:
    _, request_id, item_id = callback.data.split(":")
    request_id = int(request_id)
    item_id = int(item_id)
    async with SessionLocal() as session:
        item = await session.get(RequestItem, item_id)
    if not item:
        await callback.answer("Товар не найден")
        return
    await callback.message.answer(
        f"Товар: {item.name or '-'}",
        reply_markup=request_item_fields_keyboard(request_id, item_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_item_field:"))
async def request_item_field_select(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, item_id, field = callback.data.split(":")
    request_id = int(request_id)
    item_id = int(item_id)
    if field == "delete":
        async with SessionLocal() as session:
            item = await session.get(RequestItem, item_id)
            if item:
                await session.delete(item)
                await _sync_request_primary_item(session, request_id)
                request = await session.get(Request, request_id)
                if request:
                    await upsert_request_excel(session, request, settings.files_dir)
                await session.commit()
        await callback.message.answer("Товар удален.")
        await _send_request_menu(callback.message, request_id)
        await callback.answer()
        return
    await state.set_state(AdminEditRequest.item_value)
    await state.update_data(request_id=request_id, item_id=item_id, item_field=field)
    prompts = {
        "name": "Введите наименование",
        "specs": "Введите характеристики",
        "brand": "Введите марку/аналог",
        "qty": "Введите количество",
        "unit": "Введите ед. измерения",
        "link": "Введите ссылку",
        "note": "Введите примечание",
        "max_price": "Введите макс. цену",
        "omts": "Введите ответственного ОМТС",
        "category": "Введите категорию",
        "dds": "Введите статью ДДС",
    }
    await callback.message.answer(prompts.get(field, "Введите значение"))
    await callback.answer()


@router.message(AdminEditRequest.item_value)
async def request_item_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    item_id = data.get("item_id")
    field = data.get("item_field")
    if not request_id or not item_id or not field:
        await message.answer("Нет данных для редактирования.")
        await state.clear()
        return
    value = message.text.strip()
    async with SessionLocal() as session:
        item = await session.get(RequestItem, item_id)
        if not item:
            await message.answer("Товар не найден.")
            await state.clear()
            return
        if field == "name":
            item.name = _clean_optional(value)
        elif field == "specs":
            item.specs = _clean_optional(value)
        elif field == "brand":
            item.brand = _clean_optional(value)
        elif field == "qty":
            item.qty = _clean_optional(value)
        elif field == "unit":
            item.unit = _clean_optional(value)
        elif field == "link":
            item.link = _clean_optional(value)
        elif field == "note":
            item.note = _clean_optional(value)
        elif field == "max_price":
            item.max_price = _clean_optional(value)
        elif field == "omts":
            item.omts_responsible_id = await _get_or_create_reference(
                session, OmtsResponsible, value
            )
        elif field == "category":
            item.category_id = await _get_or_create_reference(
                session, RequestCategory, value
            )
        elif field == "dds":
            item.dds_article_id = await _get_or_create_reference(
                session, DdsArticle, value
            )
        await _sync_request_primary_item(session, request_id)
        request = await session.get(Request, request_id)
        if request:
            await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()
    await state.clear()
    await message.answer(
        "Товар обновлен.",
        reply_markup=request_item_fields_keyboard(request_id, item_id),
    )


@router.message(AdminEditRequest.item_add_name)
async def request_item_add_name(message: Message, state: FSMContext) -> None:
    name = _clean_optional(message.text)
    if not name:
        await message.answer("Наименование обязательно. Введите наименование товара.")
        return
    await state.update_data(item_add_name=name)
    await state.set_state(AdminEditRequest.item_add_specs)
    await message.answer("Введите характеристики (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_specs)
async def request_item_add_specs(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_specs=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_brand)
    await message.answer("Введите марку/аналог (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_brand)
async def request_item_add_brand(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_brand=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_qty)
    await message.answer("Введите количество")


@router.message(AdminEditRequest.item_add_qty)
async def request_item_add_qty(message: Message, state: FSMContext) -> None:
    qty = _clean_optional(message.text)
    if not qty:
        await message.answer("Количество обязательно. Введите количество.")
        return
    await state.update_data(item_add_qty=qty)
    await state.set_state(AdminEditRequest.item_add_unit)
    await message.answer("Введите единицу измерения")


@router.message(AdminEditRequest.item_add_unit)
async def request_item_add_unit(message: Message, state: FSMContext) -> None:
    unit = _clean_optional(message.text)
    if not unit:
        await message.answer("Ед. измерения обязательно. Введите единицу измерения.")
        return
    await state.update_data(item_add_unit=unit)
    await state.set_state(AdminEditRequest.item_add_link)
    await message.answer("Введите ссылку (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_link)
async def request_item_add_link(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_link=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_note)
    await message.answer("Введите примечание (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_note)
async def request_item_add_note(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_note=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_max_price)
    await message.answer("Введите макс. цену (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_max_price)
async def request_item_add_max_price(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_max_price=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_omts)
    await message.answer("Введите ответственного ОМТС (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_omts)
async def request_item_add_omts(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_omts=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_category)
    await message.answer("Введите категорию (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_category)
async def request_item_add_category(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_category=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_dds)
    await message.answer("Введите статью ДДС (или '-' для пропуска)")


@router.message(AdminEditRequest.item_add_dds)
async def request_item_add_dds(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    if not request_id:
        await message.answer("Не найдена заявка.")
        await state.clear()
        return
    dds_value = _clean_optional(message.text)
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if not request:
            await message.answer("Заявка не найдена.")
            await state.clear()
            return
        item = RequestItem(
            request_id=request_id,
            name=data.get("item_add_name"),
            specs=data.get("item_add_specs"),
            brand=data.get("item_add_brand"),
            qty=data.get("item_add_qty"),
            unit=data.get("item_add_unit"),
            link=data.get("item_add_link"),
            note=data.get("item_add_note"),
            max_price=data.get("item_add_max_price"),
            omts_responsible_id=await _get_or_create_reference(
                session, OmtsResponsible, data.get("item_add_omts")
            ),
            category_id=await _get_or_create_reference(
                session, RequestCategory, data.get("item_add_category")
            ),
            dds_article_id=await _get_or_create_reference(
                session, DdsArticle, dds_value
            ),
        )
        session.add(item)
        await session.flush()
        await _sync_request_primary_item(session, request_id)
        await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()
    await state.clear()
    await _send_request_menu(message, request_id)
