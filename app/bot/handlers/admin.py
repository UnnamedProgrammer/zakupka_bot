from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select, func
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
    requests_list_keyboard,
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
from app.bot.handlers.common import cleanup_main_menu

router = Router()

SETTINGS_PAGE_SIZE = 6


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


async def _try_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _store_settings_message(state: FSMContext | None, message: Message) -> None:
    if not state:
        return
    if not message.chat:
        return
    await state.update_data(
        admin_settings_message_id=message.message_id,
        admin_settings_chat_id=message.chat.id,
    )


async def _edit_settings_screen(
    bot,
    state: FSMContext | None,
    text: str,
    reply_markup=None,
    fallback_message: Message | None = None,
) -> None:
    message_id = None
    chat_id = None
    if state:
        data = await state.get_data()
        message_id = data.get("admin_settings_message_id")
        chat_id = data.get("admin_settings_chat_id")
    if message_id and chat_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass
    if (
        fallback_message
        and fallback_message.from_user
        and fallback_message.from_user.is_bot
    ):
        try:
            await fallback_message.edit_text(text, reply_markup=reply_markup)
            await _store_settings_message(state, fallback_message)
            return
        except Exception:
            pass
    if fallback_message:
        sent = await fallback_message.answer(text, reply_markup=reply_markup)
        await _store_settings_message(state, sent)


def _clamp_page(page: int, total_pages: int) -> int:
    if total_pages <= 1:
        return 0
    return max(0, min(page, total_pages - 1))


def _shorten_label(text: str, max_len: int = 48) -> str:
    if not text:
        return ""
    clean = " ".join(text.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


async def _load_role_menu_items(session) -> list[tuple[str, str]]:
    rows = await session.execute(select(Role.code, Role.name).order_by(Role.name))
    return rows.all()


async def _load_role_options(session) -> list[tuple[int, str]]:
    rows = await session.execute(select(Role.id, Role.name).order_by(Role.name))
    return rows.all()


async def _store_users_message(state: FSMContext | None, message: Message) -> None:
    if not state:
        return
    if not message.chat:
        return
    await state.update_data(
        admin_users_message_id=message.message_id,
        admin_users_chat_id=message.chat.id,
    )


async def _edit_users_screen(
    bot,
    state: FSMContext | None,
    text: str,
    reply_markup=None,
    fallback_message: Message | None = None,
) -> None:
    message_id = None
    chat_id = None
    if state:
        data = await state.get_data()
        message_id = data.get("admin_users_message_id")
        chat_id = data.get("admin_users_chat_id")
    if message_id and chat_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass
    if (
        fallback_message
        and fallback_message.from_user
        and fallback_message.from_user.is_bot
    ):
        try:
            await fallback_message.edit_text(text, reply_markup=reply_markup)
            await _store_users_message(state, fallback_message)
            return
        except Exception:
            pass
    if fallback_message:
        sent = await fallback_message.answer(text, reply_markup=reply_markup)
        await _store_users_message(state, sent)


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


async def _send_users_list(message: Message, role_key: str, state: FSMContext | None) -> None:
    async with SessionLocal() as session:
        role_items = await _load_role_menu_items(session)
        if role_key == "leaders":
            rows = await session.execute(
                select(User.id, User.full_name, User.is_active)
                .outerjoin(user_roles, user_roles.c.user_id == User.id)
                .outerjoin(Role, Role.id == user_roles.c.role_id)
                .where(or_(Role.code == "approver", User.is_default_approver.is_(True)))
                .distinct()
                .order_by(User.full_name, User.id)
            )
            title = "Руководители"
        else:
            role_name = next((name for code, name in role_items if code == role_key), None)
            if not role_name:
                await _edit_users_screen(
                    message.bot,
                    state,
                    "Роль не найдена.",
                    reply_markup=users_menu_keyboard(role_items),
                    fallback_message=message,
                )
                return
            rows = await session.execute(
                select(User.id, User.full_name, User.is_active)
                .join(user_roles, user_roles.c.user_id == User.id)
                .join(Role, Role.id == user_roles.c.role_id)
                .where(Role.code == role_key)
                .order_by(User.full_name)
            )
            title = role_name
        users = {}
        for user_id, full_name, is_active in rows.all():
            if user_id not in users:
                users[user_id] = (user_id, full_name or f"ID {user_id}", is_active)
    if not users:
        await _edit_users_screen(
            message.bot,
            state,
            f"{title}: список пуст.",
            reply_markup=users_menu_keyboard(role_items),
            fallback_message=message,
        )
        return
    await _edit_users_screen(
        message.bot,
        state,
        title,
        reply_markup=users_list_keyboard(role_key, list(users.values())),
        fallback_message=message,
    )


async def _show_departments_list(
    bot,
    state: FSMContext | None,
    page: int = 0,
    fallback_message: Message | None = None,
) -> None:
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(Department.id)))
        total = total or 0
        total_pages = max(1, (total + SETTINGS_PAGE_SIZE - 1) // SETTINGS_PAGE_SIZE)
        page = _clamp_page(page, total_pages)
        rows = await session.execute(
            select(Department.id, Department.name)
            .order_by(Department.name)
            .limit(SETTINGS_PAGE_SIZE)
            .offset(page * SETTINGS_PAGE_SIZE)
        )
        items = rows.all()
    if state:
        await state.update_data(admin_departments_page=page)
    if not items:
        text = "Подразделения: список пуст."
    else:
        text = "Подразделения"
        if total_pages > 1:
            text = f"{text} (стр. {page + 1}/{total_pages})"
    await _edit_settings_screen(
        bot,
        state,
        text,
        reply_markup=settings_list_keyboard("departments", items, page, total_pages),
        fallback_message=fallback_message,
    )


async def _show_cfos_list(
    bot,
    state: FSMContext | None,
    page: int = 0,
    fallback_message: Message | None = None,
) -> None:
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(Cfo.id)))
        total = total or 0
        total_pages = max(1, (total + SETTINGS_PAGE_SIZE - 1) // SETTINGS_PAGE_SIZE)
        page = _clamp_page(page, total_pages)
        rows = await session.execute(
            select(Cfo.id, Cfo.name)
            .order_by(Cfo.name)
            .limit(SETTINGS_PAGE_SIZE)
            .offset(page * SETTINGS_PAGE_SIZE)
        )
        items = rows.all()
    if state:
        await state.update_data(admin_cfos_page=page)
    if not items:
        text = "ЦФО (Бюджет): список пуст."
    else:
        text = "ЦФО (Бюджет)"
        if total_pages > 1:
            text = f"{text} (стр. {page + 1}/{total_pages})"
    await _edit_settings_screen(
        bot,
        state,
        text,
        reply_markup=settings_list_keyboard("cfos", items, page, total_pages),
        fallback_message=fallback_message,
    )


async def _show_requests_list(
    bot,
    state: FSMContext | None,
    page: int = 0,
    fallback_message: Message | None = None,
) -> None:
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(Request.id)))
        total = total or 0
        total_pages = max(1, (total + SETTINGS_PAGE_SIZE - 1) // SETTINGS_PAGE_SIZE)
        page = _clamp_page(page, total_pages)
        rows = await session.execute(
            select(Request.id, RequestStatus.name, User.full_name)
            .join(RequestStatus, RequestStatus.id == Request.status_id)
            .join(User, User.id == Request.initiator_id)
            .order_by(Request.created_at.desc(), Request.id.desc())
            .limit(SETTINGS_PAGE_SIZE)
            .offset(page * SETTINGS_PAGE_SIZE)
        )
        items = rows.all()
    if state:
        await state.update_data(admin_requests_page=page)
    if not items:
        text = "Заявки: список пуст."
    else:
        text = "Заявки"
        if total_pages > 1:
            text = f"{text} (стр. {page + 1}/{total_pages})"
    labels: list[tuple[int, str]] = []
    for request_id, status_name, initiator_name in items:
        label = f"№{request_id} · {status_name}"
        if initiator_name:
            label = f"{label} · {_shorten_label(initiator_name)}"
        labels.append((request_id, label))
    await _edit_settings_screen(
        bot,
        state,
        text,
        reply_markup=requests_list_keyboard(labels, page, total_pages),
        fallback_message=fallback_message,
    )


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


async def _send_request_menu(
    message: Message,
    request_id: int,
    state: FSMContext | None = None,
) -> None:
    async with SessionLocal() as session:
        request = await _load_request_full(session, request_id)
        if not request:
            if state:
                await _show_requests_list(
                    message.bot,
                    state,
                    page=(await state.get_data()).get("admin_requests_page", 0),
                    fallback_message=message,
                )
            else:
                await message.answer("Заявка не найдена.")
            return
        if state:
            await _edit_settings_screen(
                message.bot,
                state,
                format_request_summary(request),
                reply_markup=request_edit_keyboard(request_id),
                fallback_message=message,
            )
            return
        await message.answer(
            format_request_summary(request),
            reply_markup=request_edit_keyboard(request_id),
        )


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message, state: FSMContext) -> None:
    await cleanup_main_menu(message, state)
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        if not await _is_admin(session, user.id):
            await message.answer("Нет доступа.")
            return
    await _edit_settings_screen(
        message.bot,
        state,
        "Настройки",
        reply_markup=settings_keyboard(),
        fallback_message=message,
    )


@router.callback_query(F.data == "settings:menu")
async def settings_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(None)
    await _edit_settings_screen(
        callback.bot,
        state,
        "Настройки",
        reply_markup=settings_keyboard(),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data == "settings:departments")
async def settings_departments(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await _show_departments_list(
        callback.bot,
        state,
        page=0,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("departments:list:"))
async def departments_list_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    page = int(callback.data.split(":")[2])
    await _show_departments_list(
        callback.bot,
        state,
        page=page,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data == "departments:add")
async def department_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddDepartment.name)
    await _edit_settings_screen(
        callback.bot,
        state,
        "Введите название подразделения",
        reply_markup=None,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.message(AdminAddDepartment.name)
async def department_add_finish(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        session.add(Department(name=message.text.strip()))
        await session.commit()
    page = (await state.get_data()).get("admin_departments_page", 0)
    await state.set_state(None)
    await _show_departments_list(message.bot, state, page=page, fallback_message=message)
    await _try_delete_message(message)


@router.callback_query(F.data.startswith("departments:del:"))
async def department_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    parts = callback.data.split(":")
    dep_id = int(parts[2])
    if len(parts) > 3:
        page = int(parts[3])
    else:
        page = (await state.get_data()).get("admin_departments_page", 0)
    async with SessionLocal() as session:
        dep = await session.get(Department, dep_id)
        if dep:
            await session.delete(dep)
            await session.commit()
    await _show_departments_list(
        callback.bot,
        state,
        page=page,
        fallback_message=callback.message,
    )
    await callback.answer("Удалено")


@router.callback_query(F.data == "settings:cfos")
async def settings_cfos(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await _show_cfos_list(
        callback.bot,
        state,
        page=0,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cfos:list:"))
async def cfos_list_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    page = int(callback.data.split(":")[2])
    await _show_cfos_list(
        callback.bot,
        state,
        page=page,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data == "cfos:add")
async def cfo_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddCfo.name)
    await _edit_settings_screen(
        callback.bot,
        state,
        "Введите название ЦФО (Бюджет)",
        reply_markup=None,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.message(AdminAddCfo.name)
async def cfo_add_finish(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        session.add(Cfo(name=message.text.strip()))
        await session.commit()
    page = (await state.get_data()).get("admin_cfos_page", 0)
    await state.set_state(None)
    await _show_cfos_list(message.bot, state, page=page, fallback_message=message)
    await _try_delete_message(message)


@router.callback_query(F.data.startswith("cfos:del:"))
async def cfo_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    parts = callback.data.split(":")
    cfo_id = int(parts[2])
    if len(parts) > 3:
        page = int(parts[3])
    else:
        page = (await state.get_data()).get("admin_cfos_page", 0)
    async with SessionLocal() as session:
        cfo = await session.get(Cfo, cfo_id)
        if cfo:
            await session.delete(cfo)
            await session.commit()
    await _show_cfos_list(
        callback.bot,
        state,
        page=page,
        fallback_message=callback.message,
    )
    await callback.answer("Удалено")


@router.callback_query(F.data == "settings:users")
async def settings_users(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.clear()
    async with SessionLocal() as session:
        role_items = await _load_role_menu_items(session)
    await _edit_users_screen(
        callback.bot,
        state,
        "Пользователи",
        reply_markup=users_menu_keyboard(role_items),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data == "users:add")
async def user_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddUser.full_name)
    await _edit_users_screen(
        callback.bot,
        state,
        "Введите ФИО пользователя",
        reply_markup=None,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.message(AdminAddUser.full_name)
async def user_add_full_name(message: Message, state: FSMContext) -> None:
    full_name = message.text.strip()
    if not full_name:
        await message.answer("ФИО не может быть пустым. Введите ФИО пользователя.")
        return
    await state.update_data(full_name=full_name)
    await state.set_state(AdminAddUser.tg_username)
    await _edit_users_screen(
        message.bot,
        state,
        "Введите Telegram ник пользователя (начиная с @)",
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminAddUser.tg_username)
async def user_add_tg_username(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.startswith("@") or raw == "@":
        await message.answer("Ник должен начинаться с @. Пример: @username")
        return
    username = await ensure_username_format(raw)
    await state.update_data(tg_username=username)
    async with SessionLocal() as session:
        roles = await _load_role_options(session)
    await state.set_state(AdminAddUser.role)
    await _edit_users_screen(
        message.bot,
        state,
        "Выберите роль",
        reply_markup=roles_keyboard(roles),
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.callback_query(AdminAddUser.role, F.data.startswith("role:"))
async def user_add_role(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    role_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    full_name = data.get("full_name")
    tg_username = data.get("tg_username")
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.full_name == full_name))
        if not user and tg_username:
            user = await session.scalar(select(User).where(User.tg_username == tg_username))
        if not user:
            user = User(full_name=full_name, tg_username=tg_username, is_active=True)
            session.add(user)
            await session.flush()
        else:
            user.is_active = True
            if tg_username and not user.tg_username:
                user.tg_username = tg_username
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
        role_items = await _load_role_menu_items(session)
    await _edit_users_screen(
        callback.bot,
        state,
        "Пользователь сохранен.",
        reply_markup=users_menu_keyboard(role_items),
        fallback_message=callback.message,
    )
    await state.set_state(None)
    await state.update_data(full_name=None, tg_username=None)
    await callback.answer()


@router.callback_query(F.data.startswith("users:list:"))
async def users_list(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    role_key = callback.data.split(":")[2]
    await _send_users_list(callback.message, role_key, state)
    await callback.answer()


@router.callback_query(F.data.startswith("users:toggle:"))
async def users_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    _, _, role_key, user_id = callback.data.split(":")
    async with SessionLocal() as session:
        user = await session.get(User, int(user_id))
        if user:
            user.is_active = not user.is_active
            await session.commit()
    await _send_users_list(callback.message, role_key, state)
    await callback.answer()


@router.callback_query(F.data == "settings:requests")
async def settings_requests(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(None)
    await _show_requests_list(
        callback.bot,
        state,
        page=0,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("requests:list:"))
async def requests_list_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    page = int(callback.data.split(":")[2])
    await _show_requests_list(
        callback.bot,
        state,
        page=page,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data == "requests:enter_id")
async def requests_enter_id(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminEditRequest.request_id)
    await _edit_settings_screen(
        callback.bot,
        state,
        "Введите ID заявки для редактирования",
        reply_markup=None,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.message(AdminEditRequest.request_id)
async def request_edit_start(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.isdigit():
        await _edit_settings_screen(
            message.bot,
            state,
            "ID заявки должен быть числом.",
            reply_markup=None,
            fallback_message=message,
        )
        await _try_delete_message(message)
        return
    request_id = int(raw)
    await state.set_state(None)
    await _send_request_menu(message, request_id, state=state)
    await _try_delete_message(message)


@router.callback_query(F.data.startswith("req_edit:menu:"))
async def request_edit_menu(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = int(callback.data.split(":")[2])
    await _send_request_menu(callback.message, request_id, state=state)
    await callback.answer()


@router.callback_query(F.data.startswith("req_edit:fields:"))
async def request_edit_fields(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = int(callback.data.split(":")[2])
    await _edit_settings_screen(
        callback.bot,
        state,
        "Выберите поле для редактирования",
        reply_markup=request_fields_keyboard(request_id),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_edit:items:"))
async def request_edit_items(callback: CallbackQuery, state: FSMContext) -> None:
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
        await _edit_settings_screen(
            callback.bot,
            state,
            "У заявки пока нет товаров.",
            reply_markup=request_edit_keyboard(request_id),
            fallback_message=callback.message,
        )
        await callback.answer()
        return
    await _edit_settings_screen(
        callback.bot,
        state,
        "Выберите товар для редактирования",
        reply_markup=request_items_keyboard(request_id, items),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("req_edit:item_add:"))
async def request_item_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = int(callback.data.split(":")[2])
    await state.set_state(AdminEditRequest.item_add_name)
    await state.update_data(request_id=request_id)
    await _edit_settings_screen(
        callback.bot,
        state,
        "Введите наименование товара",
        reply_markup=None,
        fallback_message=callback.message,
    )
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
        await _edit_settings_screen(
            callback.bot,
            state,
            "Выберите подразделение",
            reply_markup=departments_keyboard(deps),
            fallback_message=callback.message,
        )
    elif field == "cfo":
        async with SessionLocal() as session:
            cfos = (
                await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
            ).all()
        await state.set_state(AdminEditRequest.field_value)
        await _edit_settings_screen(
            callback.bot,
            state,
            "Выберите ЦФО (Бюджет)",
            reply_markup=cfo_keyboard(cfos),
            fallback_message=callback.message,
        )
    elif field == "status":
        async with SessionLocal() as session:
            statuses = (
                await session.execute(
                    select(RequestStatus.id, RequestStatus.name).order_by(RequestStatus.name)
                )
            ).all()
        await state.set_state(AdminEditRequest.field_value)
        await _edit_settings_screen(
            callback.bot,
            state,
            "Выберите статус",
            reply_markup=request_status_keyboard(statuses, request_id),
            fallback_message=callback.message,
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
        await _edit_settings_screen(
            callback.bot,
            state,
            "Выберите исполнителя",
            reply_markup=builder.as_markup(),
            fallback_message=callback.message,
        )
    else:
        await state.set_state(AdminEditRequest.field_value)
        prompt = {
            "initiator": "Введите ФИО инициатора",
            "mol": "Введите ФИО МОЛ",
            "supplier": "Введите поставщика",
            "delivery": "Введите дату поставки (DD-MM-YYYY)",
        }.get(field, "Введите значение")
        await _edit_settings_screen(
            callback.bot,
            state,
            prompt,
            reply_markup=None,
            fallback_message=callback.message,
        )
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
    await state.set_state(None)
    await _send_request_menu(callback.message, request_id, state=state)
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
    await state.set_state(None)
    await _send_request_menu(callback.message, request_id, state=state)
    await callback.answer("ЦФО (Бюджет) обновлено")


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
    await state.set_state(None)
    await _send_request_menu(callback.message, request_id, state=state)
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
    await state.set_state(None)
    await _send_request_menu(callback.message, request_id, state=state)
    await callback.answer("Исполнитель обновлен")


@router.message(AdminEditRequest.field_value)
async def request_edit_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    field = data.get("edit_field")
    if not request_id or not field:
        await _edit_settings_screen(
            message.bot,
            state,
            "Нет данных для редактирования.",
            reply_markup=None,
            fallback_message=message,
        )
        await state.set_state(None)
        await _try_delete_message(message)
        return
    value = message.text.strip()
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if not request:
            await _edit_settings_screen(
                message.bot,
                state,
                "Заявка не найдена.",
                reply_markup=None,
                fallback_message=message,
            )
            await state.set_state(None)
            await _try_delete_message(message)
            return
        if field == "initiator":
            user = await session.scalar(select(User).where(User.full_name == value))
            if not user:
                await _edit_settings_screen(
                    message.bot,
                    state,
                    "Инициатор не найден. Проверьте ФИО.",
                    reply_markup=None,
                    fallback_message=message,
                )
                await _try_delete_message(message)
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
                await _edit_settings_screen(
                    message.bot,
                    state,
                    "Некорректная дата. Формат: DD-MM-YYYY",
                    reply_markup=None,
                    fallback_message=message,
                )
                await _try_delete_message(message)
                return
        await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()
    await state.set_state(None)
    await _send_request_menu(message, request_id, state=state)
    await _try_delete_message(message)


@router.callback_query(F.data.startswith("req_item:"))
async def request_item_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, item_id = callback.data.split(":")
    request_id = int(request_id)
    item_id = int(item_id)
    async with SessionLocal() as session:
        item = await session.get(RequestItem, item_id)
    if not item:
        await callback.answer("Товар не найден")
        return
    await _edit_settings_screen(
        callback.bot,
        state,
        f"Товар: {item.name or '-'}",
        reply_markup=request_item_fields_keyboard(request_id, item_id),
        fallback_message=callback.message,
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
        await state.set_state(None)
        await _send_request_menu(callback.message, request_id, state=state)
        await callback.answer("Товар удален.")
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
    await _edit_settings_screen(
        callback.bot,
        state,
        prompts.get(field, "Введите значение"),
        reply_markup=None,
        fallback_message=callback.message,
    )
    await callback.answer()


@router.message(AdminEditRequest.item_value)
async def request_item_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    item_id = data.get("item_id")
    field = data.get("item_field")
    if not request_id or not item_id or not field:
        await _edit_settings_screen(
            message.bot,
            state,
            "Нет данных для редактирования.",
            reply_markup=None,
            fallback_message=message,
        )
        await state.set_state(None)
        await _try_delete_message(message)
        return
    value = message.text.strip()
    async with SessionLocal() as session:
        item = await session.get(RequestItem, item_id)
        if not item:
            await _edit_settings_screen(
                message.bot,
                state,
                "Товар не найден.",
                reply_markup=None,
                fallback_message=message,
            )
            await state.set_state(None)
            await _try_delete_message(message)
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
    await state.set_state(None)
    await _edit_settings_screen(
        message.bot,
        state,
        "Товар обновлен.",
        reply_markup=request_item_fields_keyboard(request_id, item_id),
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_name)
async def request_item_add_name(message: Message, state: FSMContext) -> None:
    name = _clean_optional(message.text)
    if not name:
        await _edit_settings_screen(
            message.bot,
            state,
            "Наименование обязательно. Введите наименование товара.",
            reply_markup=None,
            fallback_message=message,
        )
        await _try_delete_message(message)
        return
    await state.update_data(item_add_name=name)
    await state.set_state(AdminEditRequest.item_add_specs)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите характеристики (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_specs)
async def request_item_add_specs(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_specs=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_brand)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите марку/аналог (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_brand)
async def request_item_add_brand(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_brand=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_qty)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите количество",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_qty)
async def request_item_add_qty(message: Message, state: FSMContext) -> None:
    qty = _clean_optional(message.text)
    if not qty:
        await _edit_settings_screen(
            message.bot,
            state,
            "Количество обязательно. Введите количество.",
            reply_markup=None,
            fallback_message=message,
        )
        await _try_delete_message(message)
        return
    await state.update_data(item_add_qty=qty)
    await state.set_state(AdminEditRequest.item_add_unit)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите единицу измерения",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_unit)
async def request_item_add_unit(message: Message, state: FSMContext) -> None:
    unit = _clean_optional(message.text)
    if not unit:
        await _edit_settings_screen(
            message.bot,
            state,
            "Ед. измерения обязательно. Введите единицу измерения.",
            reply_markup=None,
            fallback_message=message,
        )
        await _try_delete_message(message)
        return
    await state.update_data(item_add_unit=unit)
    await state.set_state(AdminEditRequest.item_add_link)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите ссылку (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_link)
async def request_item_add_link(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_link=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_note)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите примечание (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_note)
async def request_item_add_note(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_note=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_max_price)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите макс. цену (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_max_price)
async def request_item_add_max_price(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_max_price=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_omts)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите ответственного ОМТС (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_omts)
async def request_item_add_omts(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_omts=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_category)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите категорию (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_category)
async def request_item_add_category(message: Message, state: FSMContext) -> None:
    await state.update_data(item_add_category=_clean_optional(message.text))
    await state.set_state(AdminEditRequest.item_add_dds)
    await _edit_settings_screen(
        message.bot,
        state,
        "Введите статью ДДС (или '-' для пропуска)",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)


@router.message(AdminEditRequest.item_add_dds)
async def request_item_add_dds(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    if not request_id:
        await _edit_settings_screen(
            message.bot,
            state,
            "Не найдена заявка.",
            reply_markup=None,
            fallback_message=message,
        )
        await state.set_state(None)
        await _try_delete_message(message)
        return
    dds_value = _clean_optional(message.text)
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if not request:
            await _edit_settings_screen(
                message.bot,
                state,
                "Заявка не найдена.",
                reply_markup=None,
                fallback_message=message,
            )
            await state.set_state(None)
            await _try_delete_message(message)
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
    await state.set_state(None)
    await _send_request_menu(message, request_id, state=state)
    await _try_delete_message(message)
