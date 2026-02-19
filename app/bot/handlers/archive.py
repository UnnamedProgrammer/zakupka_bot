import calendar
import math
from datetime import date, datetime, time, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.bot.states import ArchiveEdit, ArchiveFilter
from app.config import settings
from app.db.models import (
    Cfo,
    Department,
    Request,
    RequestItem,
    RequestStatus,
    Role,
    User,
    user_roles,
)
from app.db.session import SessionLocal
from app.services.excel import (
    ReportParseError,
    build_archive_requests_xlsx,
    parse_requests_report_xlsx,
    upsert_request_excel,
)
from app.services.files import save_telegram_file
from app.services.users import ensure_username_format, get_or_create_user, get_user_role_codes
from app.bot.handlers.common import cleanup_main_menu

router = Router()

INITIATOR_PAGE_SIZE = 6
ITEM_PAGE_SIZE = 6

MONTH_NAMES = [
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]
WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _normalize_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return " ".join(text.split())


def _clean_optional_text(value) -> str | None:
    text = _normalize_text(value)
    if not text:
        return None
    lowered = text.casefold()
    if lowered in {"-", "—", "нет", "пропустить", "skip"}:
        return None
    return text


def _parse_report_date(value) -> tuple[date | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value.date(), None
    if isinstance(value, date):
        return value, None
    text = _normalize_text(value)
    if not text:
        return None, None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    return None, f"Некорректная дата: {text}. Формат: DD-MM-YYYY."


def _parse_request_id(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = _normalize_text(value)
    if text.isdigit():
        return int(text)
    return None


def _truncate_text(text: str, max_len: int = 40) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _display_value(value: str | None, default: str = "все") -> str:
    text = _normalize_text(value)
    if not text:
        return default
    return _truncate_text(text, 50)


def _parse_iso_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _display_date(value: str | None) -> str:
    parsed = _parse_iso_date(value)
    if not parsed:
        return "не задана"
    return parsed.strftime("%d-%m-%Y")


def _status_icon(code: str | None) -> str:
    return {
        "pending_approval": "🕒",
        "approved": "✅",
        "in_work": "🛠️",
        "done": "🎉",
        "rejected": "❌",
        "received": "📦",
    }.get(code or "", "📌")


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    month += delta
    if month < 1:
        return year - 1, 12
    if month > 12:
        return year + 1, 1
    return year, month


def _paginate(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    total = len(items)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start : start + page_size], page, total_pages


def _archive_menu_text(data: dict) -> str:
    initiator = data.get("arch_initiator_name") or data.get("arch_initiator_query")
    return (
        "📚 Архив\n\n"
        "Фильтры:\n"
        f"- Статус: {_display_value(data.get('arch_status_name'), default='все')}\n"
        f"- Сотрудник: {_display_value(initiator, default='все')}\n"
        f"- Товар: {_display_value(data.get('arch_item_name'), default='все')}\n"
        f"- Поставщик: {_display_value(data.get('arch_supplier_name'), default='все')}\n"
        f"- Дата от: {_display_date(data.get('arch_date_from'))}\n"
        f"- Дата до: {_display_date(data.get('arch_date_to'))}\n\n"
        "Выберите фильтр или сформируйте отчет."
    )


def _archive_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Статус", callback_data="arch_filter:status")
    builder.button(text="Сотрудник", callback_data="arch_filter:initiator")
    builder.button(text="Товар", callback_data="arch_filter:item")
    builder.button(text="Поставщик", callback_data="arch_filter:supplier")
    builder.button(text="Дата от", callback_data="arch_filter:date_from")
    builder.button(text="Дата до", callback_data="arch_filter:date_to")
    builder.button(text="📥 Сформировать Excel", callback_data="arch_action:export")
    builder.button(text="♻️ Сбросить фильтры", callback_data="arch_action:reset")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def _status_keyboard(statuses: list[tuple[str, str]]):
    builder = InlineKeyboardBuilder()
    for code, name in statuses:
        builder.button(text=f"{_status_icon(code)} {name}", callback_data=f"arch_status:{code}")
    builder.button(text="📋 Все", callback_data="arch_status:all")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="arch_menu"))
    return builder.as_markup()


def _initiator_list_keyboard(
    items: list[tuple[int, str]], page: int, total_pages: int
):
    builder = InlineKeyboardBuilder()
    for user_id, label in items:
        builder.button(text=label, callback_data=f"arch_initiator_pick:{user_id}:{page}")
    if items:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"arch_initiator_list:{page - 1}",
                )
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"arch_initiator_list:{page + 1}",
                )
            )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="✍️ Ввести ФИО", callback_data="arch_initiator_manual"))
    builder.row(InlineKeyboardButton(text="🧹 Очистить фильтр", callback_data="arch_initiator_clear"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="arch_menu"))
    return builder.as_markup()


def _item_list_keyboard(items: list[str], page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    for idx, label in enumerate(items):
        builder.button(text=label, callback_data=f"arch_item_pick:{idx}:{page}")
    if items:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"arch_item_list:{page - 1}",
                )
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"arch_item_list:{page + 1}",
                )
            )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="arch_item_manual"))
    builder.row(InlineKeyboardButton(text="🧹 Очистить фильтр", callback_data="arch_item_clear"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="arch_menu"))
    return builder.as_markup()


def _input_prompt_keyboard(clear_callback: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Очистить", callback_data=clear_callback)
    builder.button(text="⬅️ Назад", callback_data="arch_menu")
    builder.adjust(1)
    return builder.as_markup()


def _archive_calendar_keyboard(kind: str, year: int, month: int):
    builder = InlineKeyboardBuilder()
    prev_year, prev_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    builder.row(
        InlineKeyboardButton(
            text="«",
            callback_data=f"arch_cal_nav:{kind}:{prev_year}:{prev_month}",
        ),
        InlineKeyboardButton(
            text=f"{MONTH_NAMES[month - 1]} {year}",
            callback_data="arch_cal_ignore",
        ),
        InlineKeyboardButton(
            text="»",
            callback_data=f"arch_cal_nav:{kind}:{next_year}:{next_month}",
        ),
    )
    builder.row(
        *[
            InlineKeyboardButton(text=label, callback_data="arch_cal_ignore")
            for label in WEEKDAY_LABELS
        ]
    )
    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="arch_cal_ignore"))
            else:
                row.append(
                    InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"arch_cal_set:{kind}:{year}-{month:02d}-{day:02d}",
                    )
                )
        builder.row(*row)
    builder.row(
        InlineKeyboardButton(text="🧹 Очистить", callback_data=f"arch_cal_clear:{kind}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="arch_menu"),
    )
    return builder.as_markup()


def _archive_edit_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data="arch_edit:yes")
    builder.button(text="❌ Нет", callback_data="arch_edit:no")
    builder.adjust(2)
    return builder.as_markup()


async def _edit_archive_message(bot, state: FSMContext, text: str, reply_markup, fallback_message=None):
    data = await state.get_data()
    chat_id = data.get("arch_chat_id")
    message_id = data.get("arch_message_id")
    if chat_id and message_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    if fallback_message:
        new_message = await fallback_message.answer(text, reply_markup=reply_markup)
    elif chat_id:
        new_message = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    else:
        return
    await state.update_data(
        arch_message_id=new_message.message_id,
        arch_chat_id=new_message.chat.id,
    )


async def _show_archive_menu(bot, state: FSMContext, fallback_message=None) -> None:
    data = await state.get_data()
    await _edit_archive_message(
        bot,
        state,
        _archive_menu_text(data),
        _archive_menu_keyboard(),
        fallback_message=fallback_message,
    )


async def _fetch_initiators(session) -> list[tuple[int, str]]:
    rows = await session.execute(
        select(User.id, User.full_name, User.tg_username)
        .order_by(User.full_name, User.tg_username)
    )
    items = []
    for user_id, full_name, tg_username in rows.all():
        label = _normalize_text(full_name) or _normalize_text(tg_username) or f"ID {user_id}"
        items.append((user_id, _truncate_text(label, 60)))
    return items


async def _fetch_item_names(session) -> list[str]:
    names: set[str] = set()
    rows = await session.execute(select(RequestItem.name).where(RequestItem.name.is_not(None)))
    for (name,) in rows.all():
        text = _normalize_text(name)
        if text:
            names.add(text)
    rows = await session.execute(select(Request.item_name).where(Request.item_name.is_not(None)))
    for (name,) in rows.all():
        text = _normalize_text(name)
        if text:
            names.add(text)
    return sorted(names, key=lambda value: value.casefold())


async def _show_initiator_list(bot, state: FSMContext, page: int, fallback_message=None) -> None:
    async with SessionLocal() as session:
        initiators = await _fetch_initiators(session)
    items_page, page, total_pages = _paginate(initiators, page, INITIATOR_PAGE_SIZE)
    data = await state.get_data()
    current = data.get("arch_initiator_name") or data.get("arch_initiator_query")
    text = "Выберите сотрудника для фильтра."
    if current:
        text += f"\nТекущий: {_display_value(current, default='все')}"
    if initiators:
        text += f"\nСтраница {page}/{total_pages}"
    else:
        text += "\nСотрудники не найдены."
    await _edit_archive_message(
        bot,
        state,
        text,
        _initiator_list_keyboard(items_page, page, total_pages),
        fallback_message=fallback_message,
    )


async def _show_item_list(bot, state: FSMContext, page: int, fallback_message=None) -> None:
    async with SessionLocal() as session:
        names = await _fetch_item_names(session)
    items_page, page, total_pages = _paginate(names, page, ITEM_PAGE_SIZE)
    data = await state.get_data()
    current = data.get("arch_item_name")
    text = "Выберите товар для фильтра."
    if current:
        text += f"\nТекущий: {_display_value(current, default='все')}"
    if names:
        text += f"\nСтраница {page}/{total_pages}"
    else:
        text += "\nТовары не найдены."
    await _edit_archive_message(
        bot,
        state,
        text,
        _item_list_keyboard([_truncate_text(name, 60) for name in items_page], page, total_pages),
        fallback_message=fallback_message,
    )


async def _show_calendar(
    bot, state: FSMContext, kind: str, year: int | None = None, month: int | None = None,
    fallback_message=None,
) -> None:
    data = await state.get_data()
    current = data.get("arch_date_from") if kind == "from" else data.get("arch_date_to")
    parsed = _parse_iso_date(current)
    if parsed and (year is None or month is None):
        year, month = parsed.year, parsed.month
    if year is None or month is None:
        today = datetime.now().date()
        year, month = today.year, today.month
    label = "начала" if kind == "from" else "окончания"
    text = f"Выберите дату {label}."
    if parsed:
        text += f"\nТекущая: {parsed.strftime('%d-%m-%Y')}"
    await _edit_archive_message(
        bot,
        state,
        text,
        _archive_calendar_keyboard(kind, year, month),
        fallback_message=fallback_message,
    )


async def _load_archive_requests(session, data: dict) -> list[Request]:
    query = (
        select(Request)
        .options(
            selectinload(Request.initiator),
            selectinload(Request.department),
            selectinload(Request.cfo),
            selectinload(Request.status),
            selectinload(Request.executor),
            selectinload(Request.comments),
            selectinload(Request.items).selectinload(RequestItem.omts_responsible),
            selectinload(Request.items).selectinload(RequestItem.category),
            selectinload(Request.items).selectinload(RequestItem.dds_article),
        )
        .order_by(Request.created_at.desc())
    )

    if data.get("arch_status_code"):
        status_id = await session.scalar(
            select(RequestStatus.id).where(RequestStatus.code == data["arch_status_code"])
        )
        if status_id:
            query = query.where(Request.status_id == status_id)

    initiator_id = data.get("arch_initiator_id")
    initiator_query = _normalize_text(data.get("arch_initiator_query"))
    if initiator_id:
        query = query.where(Request.initiator_id == initiator_id)
    elif initiator_query:
        query = query.join(User, User.id == Request.initiator_id).where(
            or_(
                User.full_name.ilike(f"%{initiator_query}%"),
                User.tg_username.ilike(f"%{initiator_query}%"),
            )
        )

    item_name = _normalize_text(data.get("arch_item_name"))
    if item_name:
        query = query.outerjoin(RequestItem).where(
            or_(
                Request.item_name.ilike(f"%{item_name}%"),
                RequestItem.name.ilike(f"%{item_name}%"),
            )
        )
        query = query.distinct()

    supplier_name = _normalize_text(data.get("arch_supplier_name"))
    if supplier_name:
        query = query.where(Request.supplier_name.ilike(f"%{supplier_name}%"))

    date_from = _parse_iso_date(data.get("arch_date_from"))
    date_to = _parse_iso_date(data.get("arch_date_to"))
    if date_from:
        query = query.where(Request.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        date_to_end = datetime.combine(date_to, time.min) + timedelta(days=1)
        query = query.where(Request.created_at < date_to_end)

    rows = await session.execute(query)
    return rows.scalars().all()


@router.message(F.text == "📚 Архив")
async def archive_start(message: Message, state: FSMContext) -> None:
    await cleanup_main_menu(message, state)
    await state.clear()
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
        if "admin" not in role_codes:
            await message.answer("Доступно только администраторам.")
            return
    await state.set_state(ArchiveFilter.menu)
    sent = await message.answer(
        _archive_menu_text({}),
        reply_markup=_archive_menu_keyboard(),
    )
    await state.update_data(arch_message_id=sent.message_id, arch_chat_id=sent.chat.id)


@router.callback_query(F.data == "arch_menu")
async def archive_menu(callback: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
        if "admin" not in role_codes:
            await callback.answer("Нет доступа")
            return
    await state.set_state(ArchiveFilter.menu)
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data == "arch_filter:status")
async def archive_status_menu(callback: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as session:
        rows = await session.execute(select(RequestStatus.code, RequestStatus.name))
        statuses = rows.all()
    current = (await state.get_data()).get("arch_status_name")
    text = "Выберите статус для фильтра."
    if current:
        text += f"\nТекущий: {_display_value(current, default='все')}"
    await _edit_archive_message(
        callback.bot,
        state,
        text,
        _status_keyboard(statuses),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_status:"))
async def archive_status_pick(callback: CallbackQuery, state: FSMContext) -> None:
    status_code = callback.data.split(":")[1]
    if status_code == "all":
        await state.update_data(arch_status_code=None, arch_status_name=None)
    else:
        async with SessionLocal() as session:
            name = await session.scalar(
                select(RequestStatus.name).where(RequestStatus.code == status_code)
            )
        await state.update_data(
            arch_status_code=status_code,
            arch_status_name=name,
        )
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data == "arch_filter:initiator")
async def archive_initiator_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_initiator_list(callback.bot, state, page=1, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_initiator_list:"))
async def archive_initiator_list(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    await _show_initiator_list(callback.bot, state, page=page, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_initiator_pick:"))
async def archive_initiator_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, user_id, _page = callback.data.split(":")
    user_id = int(user_id)
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
    if not user:
        await callback.answer("Сотрудник не найден.")
        return
    name = _normalize_text(user.full_name) or _normalize_text(user.tg_username) or f"ID {user.id}"
    await state.update_data(
        arch_initiator_id=user.id,
        arch_initiator_name=name,
        arch_initiator_query=None,
    )
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(F.data == "arch_initiator_clear")
async def archive_initiator_clear(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        arch_initiator_id=None,
        arch_initiator_name=None,
        arch_initiator_query=None,
    )
    await state.set_state(ArchiveFilter.menu)
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data == "arch_initiator_manual")
async def archive_initiator_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ArchiveFilter.initiator_input)
    await _edit_archive_message(
        callback.bot,
        state,
        "Введите ФИО сотрудника для фильтра (можно часть).",
        _input_prompt_keyboard("arch_initiator_clear"),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.message(ArchiveFilter.initiator_input)
async def archive_initiator_input(message: Message, state: FSMContext) -> None:
    if not message.text:
        await _edit_archive_message(
            message.bot,
            state,
            "Нужно отправить текст. Введите ФИО сотрудника для фильтра.",
            _input_prompt_keyboard("arch_initiator_clear"),
            fallback_message=message,
        )
        return
    value = _clean_optional_text(message.text)
    await state.update_data(
        arch_initiator_id=None,
        arch_initiator_name=value,
        arch_initiator_query=value,
    )
    await state.set_state(ArchiveFilter.menu)
    await _show_archive_menu(message.bot, state, fallback_message=message)


@router.callback_query(ArchiveFilter.menu, F.data == "arch_filter:item")
async def archive_item_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_item_list(callback.bot, state, page=1, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_item_list:"))
async def archive_item_list(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    await _show_item_list(callback.bot, state, page=page, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_item_pick:"))
async def archive_item_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, index, page = callback.data.split(":")
    page = int(page)
    index = int(index)
    async with SessionLocal() as session:
        names = await _fetch_item_names(session)
    items_page, page, _ = _paginate(names, page, ITEM_PAGE_SIZE)
    if index < 0 or index >= len(items_page):
        await callback.answer("Товар не найден.")
        return
    await state.update_data(arch_item_name=items_page[index])
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(F.data == "arch_item_clear")
async def archive_item_clear(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(arch_item_name=None)
    await state.set_state(ArchiveFilter.menu)
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data == "arch_item_manual")
async def archive_item_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ArchiveFilter.item_input)
    await _edit_archive_message(
        callback.bot,
        state,
        "Введите наименование товара для фильтра (можно часть).",
        _input_prompt_keyboard("arch_item_clear"),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.message(ArchiveFilter.item_input)
async def archive_item_input(message: Message, state: FSMContext) -> None:
    if not message.text:
        await _edit_archive_message(
            message.bot,
            state,
            "Нужно отправить текст. Введите наименование товара.",
            _input_prompt_keyboard("arch_item_clear"),
            fallback_message=message,
        )
        return
    value = _clean_optional_text(message.text)
    await state.update_data(arch_item_name=value)
    await state.set_state(ArchiveFilter.menu)
    await _show_archive_menu(message.bot, state, fallback_message=message)


@router.callback_query(ArchiveFilter.menu, F.data == "arch_filter:supplier")
async def archive_supplier_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ArchiveFilter.supplier_input)
    await _edit_archive_message(
        callback.bot,
        state,
        "Введите поставщика для фильтра (можно часть).",
        _input_prompt_keyboard("arch_supplier_clear"),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data == "arch_supplier_clear")
async def archive_supplier_clear(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(arch_supplier_name=None)
    await state.set_state(ArchiveFilter.menu)
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.message(ArchiveFilter.supplier_input)
async def archive_supplier_input(message: Message, state: FSMContext) -> None:
    if not message.text:
        await _edit_archive_message(
            message.bot,
            state,
            "Нужно отправить текст. Введите поставщика.",
            _input_prompt_keyboard("arch_supplier_clear"),
            fallback_message=message,
        )
        return
    value = _clean_optional_text(message.text)
    await state.update_data(arch_supplier_name=value)
    await state.set_state(ArchiveFilter.menu)
    await _show_archive_menu(message.bot, state, fallback_message=message)


@router.callback_query(ArchiveFilter.menu, F.data == "arch_filter:date_from")
async def archive_date_from(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_calendar(callback.bot, state, kind="from", fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data == "arch_filter:date_to")
async def archive_date_to(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_calendar(callback.bot, state, kind="to", fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_cal_nav:"))
async def archive_calendar_nav(callback: CallbackQuery, state: FSMContext) -> None:
    _, kind, year, month = callback.data.split(":")
    await _show_calendar(
        callback.bot,
        state,
        kind=kind,
        year=int(year),
        month=int(month),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_cal_set:"))
async def archive_calendar_set(callback: CallbackQuery, state: FSMContext) -> None:
    _, kind, date_str = callback.data.split(":")
    if kind == "from":
        await state.update_data(arch_date_from=date_str)
    else:
        await state.update_data(arch_date_to=date_str)
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data.startswith("arch_cal_clear:"))
async def archive_calendar_clear(callback: CallbackQuery, state: FSMContext) -> None:
    _, kind = callback.data.split(":")
    if kind == "from":
        await state.update_data(arch_date_from=None)
    else:
        await state.update_data(arch_date_to=None)
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(F.data == "arch_cal_ignore")
async def archive_calendar_ignore(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data == "arch_action:reset")
async def archive_filters_reset(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        arch_status_code=None,
        arch_status_name=None,
        arch_initiator_id=None,
        arch_initiator_name=None,
        arch_initiator_query=None,
        arch_item_name=None,
        arch_supplier_name=None,
        arch_date_from=None,
        arch_date_to=None,
    )
    await _show_archive_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(ArchiveFilter.menu, F.data == "arch_action:export")
async def archive_export(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        requests = await _load_archive_requests(session, data)
    if not requests:
        await callback.message.answer("По вашему запросу заявок не найдено.")
        await callback.answer()
        return
    await callback.message.answer("Формирую Excel файл, пожалуйста подождите...")
    content = build_archive_requests_xlsx(requests)
    filename = f"archive_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    await callback.message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption="Архив заявок",
    )
    await state.set_state(ArchiveEdit.confirm)
    await callback.message.answer(
        "Хотите загрузить измененный файл?",
        reply_markup=_archive_edit_keyboard(),
    )
    await callback.answer()


@router.callback_query(ArchiveEdit.confirm, F.data.startswith("arch_edit:"))
async def archive_edit_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    _, decision = callback.data.split(":")
    if decision == "no":
        await state.set_state(ArchiveFilter.menu)
        await callback.message.answer("Хорошо, оставляю файл без изменений.")
        await callback.answer()
        return
    await state.set_state(ArchiveEdit.file)
    await callback.message.answer(
        "Скачайте файл, внесите изменения и отправьте обратно.\n"
        "Можно менять только столбцы: Подразделение, ЦФО (Бюджет), МОЛ, Статус, "
        "Исполнитель, Поставщик, Срок поставки.\n"
        "Заголовки и ID менять нельзя. Формат даты: DD-MM-YYYY.",
    )
    await callback.answer()


@router.message(ArchiveEdit.file)
async def archive_edit_upload(message: Message, state: FSMContext) -> None:
    if not message.document:
        await message.answer("Пришлите Excel файл (.xlsx).")
        return
    file_path = await save_telegram_file(
        message.bot,
        message.document.file_id,
        dest_dir=settings.files_dir,
        filename_hint=message.document.file_name,
    )
    try:
        rows = parse_requests_report_xlsx(file_path)
    except ReportParseError as exc:
        await message.answer(f"Ошибка файла: {exc}")
        return

    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        roles = await get_user_role_codes(session, user.id)
        if "admin" not in roles:
            await message.answer("Доступно только администраторам.")
            await state.set_state(ArchiveFilter.menu)
            return

        status_rows = await session.execute(
            select(RequestStatus.id, RequestStatus.name)
        )
        status_map = {name.casefold(): status_id for status_id, name in status_rows.all()}

        dep_rows = await session.execute(select(Department.id, Department.name))
        dep_map = {name.casefold(): dep_id for dep_id, name in dep_rows.all()}

        cfo_rows = await session.execute(select(Cfo.id, Cfo.name))
        cfo_map = {name.casefold(): cfo_id for cfo_id, name in cfo_rows.all()}

        exec_rows = await session.execute(
            select(User.id, User.full_name, User.tg_username)
            .join(user_roles, user_roles.c.user_id == User.id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(Role.code == "executor")
        )
        executor_map: dict[str, list[int]] = {}
        for user_id, full_name, tg_username in exec_rows.all():
            if full_name:
                key = _normalize_text(full_name).casefold()
                executor_map.setdefault(key, []).append(user_id)
            if tg_username:
                key = _normalize_text(tg_username).casefold()
                executor_map.setdefault(key, []).append(user_id)

        errors: list[str] = []
        ids: list[int] = []
        seen_ids: set[int] = set()
        for row in rows:
            row_num = row["row"]
            values = row["values"]
            req_id = _parse_request_id(values.get("ID"))
            if not req_id:
                errors.append(f"Строка {row_num}: некорректный ID.")
                continue
            if req_id in seen_ids:
                errors.append(f"Строка {row_num}: повторяющийся ID {req_id}.")
                continue
            seen_ids.add(req_id)
            ids.append(req_id)

        if ids:
            req_rows = await session.execute(select(Request).where(Request.id.in_(ids)))
            req_map = {req.id: req for req in req_rows.scalars().all()}
        else:
            req_map = {}

        missing = [req_id for req_id in ids if req_id not in req_map]
        if missing:
            missing_list = ", ".join(str(req_id) for req_id in missing[:10])
            errors.append(f"Не найдены заявки с ID: {missing_list}.")

        updates: list[dict] = []
        for row in rows:
            row_num = row["row"]
            values = row["values"]
            req_id = _parse_request_id(values.get("ID"))
            if not req_id or req_id not in req_map:
                continue

            dep_name = _normalize_text(values.get("Подразделение"))
            if not dep_name:
                errors.append(f"Строка {row_num}: не заполнено подразделение.")
                continue
            dep_id = dep_map.get(dep_name.casefold())
            if not dep_id:
                errors.append(
                    f"Строка {row_num}: подразделение \"{dep_name}\" не найдено."
                )
                continue

            cfo_name = _normalize_text(values.get("ЦФО (Бюджет)"))
            if not cfo_name:
                errors.append(f"Строка {row_num}: не заполнено ЦФО (Бюджет).")
                continue
            cfo_id = cfo_map.get(cfo_name.casefold())
            if not cfo_id:
                errors.append(f"Строка {row_num}: ЦФО (Бюджет) \"{cfo_name}\" не найдено.")
                continue

            status_name = _normalize_text(values.get("Статус"))
            if not status_name:
                errors.append(f"Строка {row_num}: не заполнен статус.")
                continue
            status_id = status_map.get(status_name.casefold())
            if not status_id:
                errors.append(
                    f"Строка {row_num}: статус \"{status_name}\" не найден."
                )
                continue

            executor_name = _normalize_text(values.get("Исполнитель"))
            executor_id = None
            if executor_name:
                ids_for_name = executor_map.get(executor_name.casefold())
                if not ids_for_name:
                    errors.append(
                        f"Строка {row_num}: исполнитель \"{executor_name}\" не найден."
                    )
                    continue
                if len(ids_for_name) > 1:
                    errors.append(
                        f"Строка {row_num}: исполнитель \"{executor_name}\" неоднозначен."
                    )
                    continue
                executor_id = ids_for_name[0]

            expected_delivery, date_error = _parse_report_date(
                values.get("Срок поставки")
            )
            if date_error:
                errors.append(f"Строка {row_num}: {date_error}")
                continue

            updates.append(
                {
                    "request": req_map[req_id],
                    "department_id": dep_id,
                    "cfo_id": cfo_id,
                    "status_id": status_id,
                    "executor_id": executor_id,
                    "mol_full_name": _clean_optional_text(values.get("МОЛ")),
                    "supplier_name": _clean_optional_text(values.get("Поставщик")),
                    "expected_delivery_at": expected_delivery,
                }
            )

        if errors:
            preview = "\n".join(f"- {err}" for err in errors[:20])
            suffix = ""
            if len(errors) > 20:
                suffix = f"\n...и еще {len(errors) - 20} ошибок."
            await message.answer("Не удалось обработать файл:\n" + preview + suffix)
            return

        updated_count = 0
        for payload in updates:
            request = payload["request"]
            changed = False
            if request.department_id != payload["department_id"]:
                request.department_id = payload["department_id"]
                changed = True
            if request.cfo_id != payload["cfo_id"]:
                request.cfo_id = payload["cfo_id"]
                changed = True
            if request.status_id != payload["status_id"]:
                request.status_id = payload["status_id"]
                changed = True
            if request.executor_id != payload["executor_id"]:
                request.executor_id = payload["executor_id"]
                changed = True
            if request.mol_full_name != payload["mol_full_name"]:
                request.mol_full_name = payload["mol_full_name"]
                changed = True
            if request.supplier_name != payload["supplier_name"]:
                request.supplier_name = payload["supplier_name"]
                changed = True
            if request.expected_delivery_at != payload["expected_delivery_at"]:
                request.expected_delivery_at = payload["expected_delivery_at"]
                changed = True
            if changed:
                await upsert_request_excel(session, request, settings.files_dir)
                updated_count += 1

        if updated_count:
            await session.commit()
            await message.answer(f"Готово. Обновлено заявок: {updated_count}.")
        else:
            await message.answer("Изменений не найдено.")

    await state.set_state(ArchiveFilter.menu)
