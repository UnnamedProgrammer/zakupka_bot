import calendar
import math
from datetime import date, datetime, time, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.keyboards import (
    executor_actions_keyboard,
    export_edit_keyboard,
    receive_tmc_keyboard,
)
from app.bot.states import (
    ExecutorComment,
    ExecutorDeliveryDate,
    ExecutorFile,
    ExportReportEdit,
)
from app.db.models import (
    Attachment,
    Cfo,
    Comment,
    Department,
    Request,
    RequestItem,
    RequestStatus,
    Role,
    User,
    user_roles,
)
from app.db.session import SessionLocal
from app.services.constants import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_DONE,
    REQUEST_STATUS_IN_WORK,
    REQUEST_STATUS_REJECTED,
    REQUEST_STATUS_RECEIVED,
)
from app.services.attachments import build_photo_groups_from, fetch_request_media
from app.services.excel import (
    ReportParseError,
    build_employee_stats_xlsx,
    build_request_template_xlsx,
    parse_requests_report_xlsx,
    REQUEST_TEMPLATE_PATH,
    upsert_request_excel,
)
from app.services.files import save_telegram_file
from app.services.formatters import format_request_summary
from app.services.notifications import send_to_user
from app.services.users import ensure_username_format, get_or_create_user, user_has_role
from app.services.datetime import to_naive_utc
from app.config import settings

router = Router()


async def _get_request_status_id(session, code: str) -> int:
    return await session.scalar(select(RequestStatus.id).where(RequestStatus.code == code))


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


DELIVERY_LIST_PAGE_SIZE = 6
DAILY_LIST_PAGE_SIZE = 8
MY_LIST_PAGE_SIZE = 6


def _normalize_delivery_filter(value: str | None) -> str:
    if value in {"all", "missing"}:
        return value
    return "missing"


def _truncate_text(text: str, max_len: int = 60) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _delivery_request_label(request: Request) -> str:
    name = _normalize_text(request.item_name) or _normalize_text(request.supplier_name)
    if not name:
        name = "без названия"
    status = request.status.name if request.status else ""
    label = f"№{request.id} · {name}"
    if status:
        label = f"{label} · {status}"
    return _truncate_text(label, 64)


def _daily_request_label(request: Request) -> str:
    name = _normalize_text(request.item_name) or _normalize_text(request.supplier_name)
    if not name:
        name = "без названия"
    status = request.status.name if request.status else ""
    label = f"№{request.id} · {name}"
    if status:
        label = f"{label} · {status}"
    return _truncate_text(label, 64)


def _my_request_label(request: Request) -> str:
    name = _normalize_text(request.item_name) or _normalize_text(request.supplier_name)
    if not name:
        name = "без названия"
    status = request.status.name if request.status else ""
    label = f"№{request.id} · {name}"
    if status:
        label = f"{label} · {status}"
    return _truncate_text(label, 64)


def _delivery_list_text(total: int, page: int, total_pages: int, filter_key: str) -> str:
    filter_label = "без срока" if filter_key == "missing" else "все"
    if total == 0:
        return f"Заявок ({filter_label}) не найдено."
    return (
        "Выберите заявку для установки срока поставки.\n"
        f"Фильтр: {filter_label}. Страница {page}/{total_pages}."
    )


def _delivery_list_keyboard(
    requests_page: list[Request],
    filter_key: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for req in requests_page:
        builder.button(
            text=_delivery_request_label(req),
            callback_data=f"delivery_pick:{req.id}:{filter_key}:{page}",
        )
    if requests_page:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"delivery_list:{filter_key}:{page - 1}",
                )
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"delivery_list:{filter_key}:{page + 1}",
                )
            )
    if nav_buttons:
        builder.row(*nav_buttons)

    toggle_text = "Показать все" if filter_key == "missing" else "Только без срока"
    toggle_filter = "all" if filter_key == "missing" else "missing"
    builder.row(
        InlineKeyboardButton(
            text=toggle_text,
            callback_data=f"delivery_list:{toggle_filter}:1",
        )
    )
    return builder


async def _fetch_delivery_requests(
    session,
    executor_id: int,
    filter_key: str,
) -> list[Request]:
    query = select(Request).where(Request.executor_id == executor_id)
    if filter_key == "missing":
        query = query.where(Request.expected_delivery_at.is_(None))
    query = query.options(selectinload(Request.status)).order_by(Request.created_at.desc())
    rows = await session.execute(query)
    return rows.scalars().all()


async def _show_delivery_list(
    message: Message,
    executor_id: int,
    filter_key: str,
    page: int,
    edit: bool = False,
) -> None:
    filter_key = _normalize_delivery_filter(filter_key)
    async with SessionLocal() as session:
        requests = await _fetch_delivery_requests(session, executor_id, filter_key)
    total = len(requests)
    total_pages = max(1, math.ceil(total / DELIVERY_LIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * DELIVERY_LIST_PAGE_SIZE
    requests_page = requests[start : start + DELIVERY_LIST_PAGE_SIZE]
    text = _delivery_list_text(total, page, total_pages, filter_key)
    markup = _delivery_list_keyboard(
        requests_page, filter_key, page, total_pages
    ).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _fetch_daily_requests(session) -> list[Request]:
    now_local = datetime.now().astimezone()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    rows = await session.execute(
        select(Request)
        .where(Request.created_at >= start, Request.created_at < end)
        .options(selectinload(Request.status))
        .order_by(Request.created_at.desc())
    )
    return rows.scalars().all()


def _daily_list_keyboard(
    requests_page: list[Request],
    page: int,
    total_pages: int,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for req in requests_page:
        builder.button(
            text=_daily_request_label(req),
            callback_data=f"daily_pick:{req.id}:{page}",
        )
    if requests_page:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"daily_list:{page - 1}",
                )
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"daily_list:{page + 1}",
                )
            )
    if nav_buttons:
        builder.row(*nav_buttons)
    return builder


async def _show_daily_list(message: Message, page: int, edit: bool = False) -> None:
    async with SessionLocal() as session:
        requests = await _fetch_daily_requests(session)
    total = len(requests)
    if total == 0:
        text = "Заявок не найдено."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    total_pages = max(1, math.ceil(total / DAILY_LIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * DAILY_LIST_PAGE_SIZE
    requests_page = requests[start : start + DAILY_LIST_PAGE_SIZE]
    text = (
        "Выберите заявку для формирования шаблона.\n"
        f"Страница {page}/{total_pages}."
    )
    markup = _daily_list_keyboard(requests_page, page, total_pages).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _fetch_my_requests(session, executor_id: int) -> list[Request]:
    rows = await session.execute(
        select(Request)
        .where(Request.executor_id == executor_id)
        .options(selectinload(Request.status))
        .order_by(Request.created_at.desc())
    )
    return rows.scalars().all()


def _my_list_keyboard(
    requests_page: list[Request],
    page: int,
    total_pages: int,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for req in requests_page:
        builder.button(
            text=_my_request_label(req),
            callback_data=f"my_pick:{req.id}:{page}",
        )
    if requests_page:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"my_list:{page - 1}",
                )
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"my_list:{page + 1}",
                )
            )
    if nav_buttons:
        builder.row(*nav_buttons)
    return builder


async def _show_my_list(
    message: Message,
    executor_id: int,
    page: int,
    edit: bool = False,
) -> None:
    async with SessionLocal() as session:
        requests = await _fetch_my_requests(session, executor_id)
    total = len(requests)
    if total == 0:
        text = "У вас нет назначенных заявок."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    total_pages = max(1, math.ceil(total / MY_LIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * MY_LIST_PAGE_SIZE
    requests_page = requests[start : start + MY_LIST_PAGE_SIZE]
    text = f"Выберите заявку. Страница {page}/{total_pages}."
    markup = _my_list_keyboard(requests_page, page, total_pages).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _is_executor(session, user_id: int) -> bool:
    return await user_has_role(session, user_id, "executor")


async def _get_user(session, tg_user) -> User:
    username = await ensure_username_format(tg_user.username)
    user = await get_or_create_user(session, tg_user.id, username, tg_user.full_name)
    await session.commit()
    return user


async def _is_override_user(tg_user) -> bool:
    if settings.approval_override_tg_id and tg_user.id == settings.approval_override_tg_id:
        return True
    if not settings.approval_override_username:
        return False
    override = await ensure_username_format(settings.approval_override_username)
    current = await ensure_username_format(tg_user.username)
    if not override or not current:
        return False
    return override.lower() == current.lower()


async def _resolve_override_executor(session) -> User | None:
    if not settings.approval_override_tg_id and not settings.approval_override_username:
        return None
    override_user = None
    if settings.approval_override_tg_id:
        override_user = await session.scalar(
            select(User).where(User.tg_id == settings.approval_override_tg_id)
        )
    if not override_user and settings.approval_override_username:
        username = await ensure_username_format(settings.approval_override_username)
        if username:
            override_user = await session.scalar(
                select(User).where(User.tg_username == username)
            )
    if not override_user and settings.approval_override_tg_id:
        username = await ensure_username_format(settings.approval_override_username)
        override_user = await get_or_create_user(
            session, settings.approval_override_tg_id, username, None
        )
    return override_user


async def _ensure_executor_for_request(session, tg_user, request: Request) -> bool:
    user = await _get_user(session, tg_user)
    return user.id == request.executor_id


def _executor_keyboard_for_request(request: Request):
    status_code = request.status.code if request.status else None
    include_extras = status_code != REQUEST_STATUS_APPROVED
    return executor_actions_keyboard(request.id, include_extras=include_extras)


def _executor_actions_with_back(
    request: Request,
    back_data: str,
    attachments_data: str | None = None,
):
    actions = _executor_keyboard_for_request(request)
    builder = InlineKeyboardBuilder()
    for row in actions.inline_keyboard:
        builder.row(*row)
    if attachments_data:
        builder.row(
            InlineKeyboardButton(
                text="📎 Вложения",
                callback_data=attachments_data,
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ К списку", callback_data=back_data))
    return builder.as_markup()


async def _send_request_attachments_to_chat(
    bot,
    chat_id: int,
    request: Request,
    items: list[RequestItem],
    attachments: list[Attachment],
) -> None:
    if not attachments:
        return
    photo_groups = build_photo_groups_from(request, items, attachments)
    if photo_groups:
        await bot.send_message(chat_id, "Вложения")
        for title, photos in photo_groups:
            await bot.send_message(chat_id, title)
            for att in photos:
                if att.file_path:
                    await bot.send_photo(chat_id, FSInputFile(att.file_path))
                elif att.file_id:
                    await bot.send_photo(chat_id, att.file_id)
    request_excel_name = f"request_{request.id}.xlsx"
    skip_request_excel = request.description_method == "excel"
    for att in attachments:
        if att.file_type != "document":
            continue
        if skip_request_excel and att.file_name == request_excel_name:
            continue
        if att.file_id:
            await bot.send_document(chat_id, att.file_id)
        elif att.file_path:
            await bot.send_document(
                chat_id,
                FSInputFile(att.file_path, filename=att.file_name or None),
            )


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    month += delta
    if month < 1:
        return year - 1, 12
    if month > 12:
        return year + 1, 1
    return year, month


def _delivery_calendar_keyboard(
    request_id: int, year: int, month: int, back_data: str | None = None
):
    builder = InlineKeyboardBuilder()
    month_names = [
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
    prev_year, prev_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)

    builder.row(
        InlineKeyboardButton(
            text="«",
            callback_data=f"delivery_nav:{request_id}:{prev_year}:{prev_month}",
        ),
        InlineKeyboardButton(
            text=f"{month_names[month - 1]} {year}",
            callback_data="delivery_ignore",
        ),
        InlineKeyboardButton(
            text="»",
            callback_data=f"delivery_nav:{request_id}:{next_year}:{next_month}",
        ),
    )
    builder.row(
        *[
            InlineKeyboardButton(text=label, callback_data="delivery_ignore")
            for label in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        ]
    )
    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(
                    InlineKeyboardButton(text=" ", callback_data="delivery_ignore")
                )
            else:
                row.append(
                    InlineKeyboardButton(
                        text=str(day),
                        callback_data=(
                            f"delivery_date:{request_id}:{year}-{month:02d}-{day:02d}"
                        ),
                    )
                )
        builder.row(*row)
    if back_data:
        builder.row(
            InlineKeyboardButton(
                text="⬅️ К списку",
                callback_data=back_data,
            )
        )
    return builder.as_markup()


@router.callback_query(F.data.startswith("assign:"))
async def assign_executor(callback: CallbackQuery) -> None:
    _, req_id, exec_id = callback.data.split(":")
    request_id = int(req_id)
    executor_id = int(exec_id)
    async with SessionLocal() as session:
        current_user = await _get_user(session, callback.from_user)
        if not current_user.is_default_approver and not await _is_override_user(
            callback.from_user
        ):
            await callback.answer("Нет доступа")
            return
        request = await session.get(
            Request,
            request_id,
            options=[
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.attachments),
                selectinload(Request.items),
                selectinload(Request.comments),
            ],
        )
        executor = await session.get(User, executor_id)
        if not request or not executor:
            await callback.answer("Заявка не найдена")
            return
        override_executor = await _resolve_override_executor(session)
        target_executor = override_executor or executor
        request.executor_id = target_executor.id
        await session.commit()

        await send_to_user(
            callback.bot,
            target_executor,
            format_request_summary(request),
            reply_markup=_executor_keyboard_for_request(request),
        )
        items, attachments = await fetch_request_media(session, request.id)
        skip_request_excel = request.description_method == "excel"
        request_excel_name = f"request_{request.id}.xlsx"
        photo_groups = build_photo_groups_from(request, items, attachments)
        if photo_groups:
            await callback.bot.send_message(target_executor.tg_id, "Заявка")
            for title, photos in photo_groups:
                await callback.bot.send_message(target_executor.tg_id, title)
                for att in photos:
                    if att.file_path:
                        await callback.bot.send_photo(
                            target_executor.tg_id, FSInputFile(att.file_path)
                        )
                    elif att.file_id:
                        await callback.bot.send_photo(target_executor.tg_id, att.file_id)
        for att in attachments:
            if att.file_type != "document":
                continue
            if skip_request_excel and att.file_name == request_excel_name:
                continue
            if att.file_id:
                await callback.bot.send_document(target_executor.tg_id, att.file_id)
            elif att.file_path:
                await callback.bot.send_document(
                    target_executor.tg_id,
                    FSInputFile(att.file_path, filename=att.file_name or None),
                )
        await send_to_user(
            callback.bot,
            request.initiator,
            (
                f"Ваша заявка №{request.id} направлена исполнителю "
                f"{target_executor.full_name or target_executor.tg_username}."
            ),
        )
    await callback.answer("Исполнитель назначен")


@router.callback_query(F.data.startswith("status:"))
async def update_status(callback: CallbackQuery, state: FSMContext) -> None:
    _, req_id, status_code = callback.data.split(":")
    request_id = int(req_id)
    if status_code in (REQUEST_STATUS_REJECTED, REQUEST_STATUS_DONE):
        await state.set_state(ExecutorComment.comment)
        await state.update_data(request_id=request_id, status_code=status_code)
        await callback.message.answer("Введите комментарий")
        await callback.answer()
        return

    async with SessionLocal() as session:
        request = await session.get(
            Request,
            request_id,
            options=[selectinload(Request.initiator), selectinload(Request.status)],
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if not await _ensure_executor_for_request(session, callback.from_user, request):
            await callback.answer("Нет доступа")
            return
        status_id = await _get_request_status_id(session, status_code)
        request.status_id = status_id
        await session.flush()
        request = await session.get(
            Request,
            request_id,
            options=[
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.items),
                selectinload(Request.attachments),
            ],
        )
        await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()

        await send_to_user(
            callback.bot,
            request.initiator,
            f"Изменен статус вашей заявки №{request.id}: {request.status.name}",
        )
    await callback.answer("Статус обновлен")


@router.message(ExecutorComment.comment)
async def executor_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    status_code = data.get("status_code")
    async with SessionLocal() as session:
        request = await session.get(
            Request, request_id, options=[selectinload(Request.initiator)]
        )
        if not request:
            await message.answer("Заявка не найдена")
            await state.clear()
            return
        if not await _ensure_executor_for_request(session, message.from_user, request):
            await message.answer("Нет доступа")
            await state.clear()
            return
        author = await _get_user(session, message.from_user)
        author_name = author.full_name or author.tg_username or f"ID {author.id}"
        comment_text = message.text.strip()
        formatted_comment = f"Комментарий исполнителя {author_name} - {comment_text}"
        if status_code:
            status_id = await _get_request_status_id(session, status_code)
            request.status_id = status_id
            if status_code == REQUEST_STATUS_DONE:
                request.done_at = to_naive_utc(message.date)
            await session.flush()
            request = await session.get(
                Request,
                request_id,
                options=[
                    selectinload(Request.initiator),
                    selectinload(Request.department),
                    selectinload(Request.cfo),
                    selectinload(Request.status),
                    selectinload(Request.items),
                    selectinload(Request.attachments),
                ],
            )
            await upsert_request_excel(session, request, settings.files_dir)
        session.add(
            Comment(
                request_id=request.id,
                author_id=author.id,
                text=formatted_comment,
            )
        )
        await session.commit()

        if status_code:
            updated = await session.get(
                Request,
                request.id,
                options=[selectinload(Request.initiator), selectinload(Request.status)],
            )
            status_name = updated.status.name if updated and updated.status else status_code
            await send_to_user(
                message.bot,
                updated.initiator if updated else request.initiator,
                (
                    f"Изменен статус вашей заявки №{request.id}: {status_name}. "
                    f"Комментарий: {formatted_comment}"
                ),
            )
        else:
            updated = await session.get(
                Request, request.id, options=[selectinload(Request.initiator)]
            )
            await send_to_user(
                message.bot,
                updated.initiator if updated else request.initiator,
                f"Комментарий к заявке №{request.id}: {formatted_comment}",
            )
        if status_code == REQUEST_STATUS_DONE:
            await send_to_user(
                message.bot,
                request.initiator,
                "Если ТМЦ получено, подтвердите:",
                reply_markup=receive_tmc_keyboard(request.id),
            )
    await state.clear()
    await message.answer("Комментарий сохранен.")


@router.callback_query(F.data.startswith("comment:"))
async def add_comment(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = int(callback.data.split(":")[1])
    await state.set_state(ExecutorComment.comment)
    await state.update_data(request_id=request_id, status_code=None)
    await callback.message.answer("Введите комментарий")
    await callback.answer()


@router.callback_query(F.data.startswith("file:"))
async def add_file(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = int(callback.data.split(":")[1])
    await state.set_state(ExecutorFile.file)
    await state.update_data(request_id=request_id)
    await callback.message.answer("Отправьте файл Excel или Word")
    await callback.answer()


@router.message(ExecutorFile.file)
async def save_file(message: Message, state: FSMContext) -> None:
    if not message.document:
        await message.answer("Нужен файл Excel или Word")
        return
    data = await state.get_data()
    request_id = data.get("request_id")
    async with SessionLocal() as session:
        request = await session.get(Request, request_id)
        if not request:
            await message.answer("Заявка не найдена")
            await state.clear()
            return
        if not await _ensure_executor_for_request(session, message.from_user, request):
            await message.answer("Нет доступа")
            await state.clear()
            return
        author = await _get_user(session, message.from_user)
        file_path = await save_telegram_file(
            message.bot,
            message.document.file_id,
            dest_dir=settings.files_dir,
            filename_hint=message.document.file_name,
        )
        session.add(
            Attachment(
                request_id=request.id,
                uploader_id=author.id,
                file_id=message.document.file_id,
                file_unique_id=message.document.file_unique_id,
                file_name=message.document.file_name,
                file_path=file_path,
                file_type="document",
            )
        )
        await session.commit()
    await state.clear()
    await message.answer("Файл сохранен.")


@router.callback_query(F.data.startswith("delivery:"))
async def set_delivery(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.set_state(ExecutorDeliveryDate.date)
    await state.update_data(request_id=request_id)
    today = datetime.now().date()
    await callback.message.answer(
        "Выберите дату поставки",
        reply_markup=_delivery_calendar_keyboard(request_id, today.year, today.month),
    )
    await callback.answer()


@router.callback_query(F.data == "delivery_ignore")
async def delivery_ignore(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("delivery_nav:"))
async def delivery_nav(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, year, month = callback.data.split(":")
    data = await state.get_data()
    filter_key = data.get("delivery_list_filter")
    page = data.get("delivery_list_page")
    back_data = None
    if filter_key and page:
        back_data = f"delivery_list:{filter_key}:{page}"
    await callback.message.edit_reply_markup(
        reply_markup=_delivery_calendar_keyboard(
            int(request_id),
            int(year),
            int(month),
            back_data=back_data,
        )
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delivery_date:"))
async def delivery_date_select(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, date_str = callback.data.split(":")
    try:
        delivery_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await callback.answer("Некорректная дата")
        return
    async with SessionLocal() as session:
        request = await session.get(
            Request, int(request_id), options=[selectinload(Request.executor)]
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if not await _ensure_executor_for_request(session, callback.from_user, request):
            await callback.answer("Нет доступа")
            return
        request.expected_delivery_at = delivery_date
        await session.commit()
    data = await state.get_data()
    filter_key = data.get("delivery_list_filter")
    page = data.get("delivery_list_page")
    back_data = None
    if filter_key and page:
        back_data = f"delivery_list:{filter_key}:{page}"
    await state.clear()
    if back_data:
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ К списку", callback_data=back_data)
        builder.adjust(1)
        await callback.message.edit_text(
            f"Срок поставки сохранен: {delivery_date.strftime('%d-%m-%Y')}",
            reply_markup=builder.as_markup(),
        )
    else:
        await callback.message.edit_text(
            f"Срок поставки сохранен: {delivery_date.strftime('%Y-%m-%d')}"
        )
    await callback.answer()


@router.message(ExecutorDeliveryDate.date)
async def save_delivery_date(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    request_id = data.get("request_id")
    try:
        delivery_date = datetime.strptime(message.text.strip(), "%Y-%m-%d").date()
    except ValueError:
        await message.answer("Некорректная дата. Формат: YYYY-MM-DD")
        return
    async with SessionLocal() as session:
        request = await session.get(Request, request_id, options=[selectinload(Request.executor)])
        if not request:
            await message.answer("Заявка не найдена")
            await state.clear()
            return
        if not await _ensure_executor_for_request(session, message.from_user, request):
            await message.answer("Нет доступа")
            await state.clear()
            return
        request.expected_delivery_at = delivery_date
        await session.commit()
        await message.answer("Срок поставки сохранен.")
    await state.clear()


@router.callback_query(F.data.startswith("received:"))
async def received_tmc(callback: CallbackQuery) -> None:
    request_id = int(callback.data.split(":")[1])
    async with SessionLocal() as session:
        request = await session.get(
            Request,
            request_id,
            options=[
                selectinload(Request.executor),
                selectinload(Request.initiator),
            ],
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        actor = await _get_user(session, callback.from_user)
        if actor.id != request.initiator_id:
            await callback.answer("Нет доступа")
            return
        status_id = await _get_request_status_id(session, REQUEST_STATUS_RECEIVED)
        if request.received_at or request.status_id == status_id:
            if callback.message.reply_markup:
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Уже подтверждено")
            return
        request.status_id = status_id
        request.received_at = to_naive_utc(callback.message.date)
        await session.flush()
        request = await session.get(
            Request,
            request_id,
            options=[
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.executor),
            ],
        )
        await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()
        executor = None
        if request.executor_id:
            executor = await session.get(User, request.executor_id)
        if executor:
            await send_to_user(
                callback.bot,
                executor,
                f"ТМЦ по заявке №{request.id} было получено",
            )
    if callback.message.reply_markup:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Принято")


@router.callback_query(F.data.startswith("delivery_list:"))
async def delivery_list(callback: CallbackQuery, state: FSMContext) -> None:
    _, filter_key, page_str = callback.data.split(":")
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        user = await _get_user(session, callback.from_user)
        if not await _is_executor(session, user.id):
            await callback.answer("Доступно только исполнителям.")
            return
    await state.clear()
    await _show_delivery_list(
        callback.message,
        user.id,
        filter_key=filter_key,
        page=page,
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delivery_pick:"))
async def delivery_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, filter_key, page_str = callback.data.split(":")
    request_id = int(request_id)
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        user = await _get_user(session, callback.from_user)
        if not await _is_executor(session, user.id):
            await callback.answer("Доступно только исполнителям.")
            return
        request = await session.get(
            Request,
            request_id,
            options=[selectinload(Request.executor)],
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if not await _ensure_executor_for_request(session, callback.from_user, request):
            await callback.answer("Нет доступа")
            return
    await state.clear()
    await state.set_state(ExecutorDeliveryDate.date)
    await state.update_data(
        request_id=request_id,
        delivery_list_filter=_normalize_delivery_filter(filter_key),
        delivery_list_page=page,
    )
    today = datetime.now().date()
    back_data = f"delivery_list:{_normalize_delivery_filter(filter_key)}:{page}"
    await callback.message.edit_text(
        "Выберите дату поставки",
        reply_markup=_delivery_calendar_keyboard(
            request_id,
            today.year,
            today.month,
            back_data=back_data,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("my_list:"))
async def my_list(callback: CallbackQuery, state: FSMContext) -> None:
    _, page_str = callback.data.split(":")
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        user = await _get_user(session, callback.from_user)
        if not await _is_executor(session, user.id):
            await callback.answer("Доступно только исполнителям.")
            return
    await state.clear()
    await _show_my_list(callback.message, user.id, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("my_pick:"))
async def my_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id, page_str = callback.data.split(":")
    request_id = int(request_id)
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        user = await _get_user(session, callback.from_user)
        if not await _is_executor(session, user.id):
            await callback.answer("Доступно только исполнителям.")
            return
        request = await session.get(
            Request,
            request_id,
            options=[
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.items),
                selectinload(Request.comments),
            ],
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if not await _ensure_executor_for_request(session, callback.from_user, request):
            await callback.answer("Нет доступа")
            return
    await state.clear()
    back_data = f"my_list:{page}"
    attachments_data = f"my_attach:{request_id}:{page}"
    await callback.message.edit_text(
        format_request_summary(request),
        reply_markup=_executor_actions_with_back(
            request, back_data, attachments_data=attachments_data
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("my_attach:"))
async def my_attach(callback: CallbackQuery) -> None:
    _, request_id, _page = callback.data.split(":")
    request_id = int(request_id)
    async with SessionLocal() as session:
        user = await _get_user(session, callback.from_user)
        if not await _is_executor(session, user.id):
            await callback.answer("Доступно только исполнителям.")
            return
        request = await session.get(Request, request_id)
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if not await _ensure_executor_for_request(session, callback.from_user, request):
            await callback.answer("Нет доступа")
            return
        items, attachments = await fetch_request_media(session, request_id)
    if not attachments:
        await callback.message.answer("Вложений нет.")
        await callback.answer()
        return
    await _send_request_attachments_to_chat(
        callback.bot,
        callback.message.chat.id,
        request,
        items,
        attachments,
    )
    await callback.answer()


@router.message(
    F.text.in_(
        {"📤 Ежедневные заявки", "📤 Выгрузить ежедневные заявки"}
    )
)
async def export_daily_requests(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        if not await _is_executor(session, user.id):
            await message.answer("Доступно только исполнителям.")
            return
    await state.clear()
    await _show_daily_list(message, page=1, edit=False)


@router.callback_query(F.data.startswith("daily_list:"))
async def daily_list(callback: CallbackQuery, state: FSMContext) -> None:
    _, page_str = callback.data.split(":")
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        user = await _get_user(session, callback.from_user)
        if not await _is_executor(session, user.id):
            await callback.answer("Доступно только исполнителям.")
            return
    await state.clear()
    await _show_daily_list(callback.message, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("daily_pick:"))
async def daily_pick(callback: CallbackQuery) -> None:
    _, request_id, _page = callback.data.split(":")
    request_id = int(request_id)
    async with SessionLocal() as session:
        user = await _get_user(session, callback.from_user)
        if not await _is_executor(session, user.id):
            await callback.answer("Доступно только исполнителям.")
            return
        request = await session.get(
            Request,
            request_id,
            options=[
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.items).selectinload(RequestItem.omts_responsible),
                selectinload(Request.items).selectinload(RequestItem.category),
                selectinload(Request.items).selectinload(RequestItem.dds_article),
            ],
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        items = list(request.items or [])
        try:
            content = build_request_template_xlsx(
                request, items, template_path=REQUEST_TEMPLATE_PATH
            )
        except FileNotFoundError:
            await callback.message.answer("Шаблон заявки не найден.")
            await callback.answer()
            return
    filename = f"request_template_{request.id}.xlsx"
    await callback.message.answer_document(
        document=BufferedInputFile(content, filename=filename),
        caption=f"Шаблон заявки №{request.id}",
    )
    await callback.answer()


@router.message(
    F.text.in_(
        {"📊 Статистика сотрудников", "📊 Выгрузить статистику сотрудников"}
    )
)
async def export_employee_stats(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Request)
            .options(
                selectinload(Request.initiator),
                selectinload(Request.executor),
                selectinload(Request.status),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.comments),
                selectinload(Request.items).selectinload(RequestItem.omts_responsible),
                selectinload(Request.items).selectinload(RequestItem.category),
                selectinload(Request.items).selectinload(RequestItem.dds_article),
            )
            .order_by(Request.created_at.desc())
        )
        content = build_employee_stats_xlsx(rows.scalars().all())
    await message.answer_document(
        document=BufferedInputFile(content, filename="employee_stats.xlsx"),
        caption="Статистика сотрудников",
    )
    await state.clear()
    await state.set_state(ExportReportEdit.confirm)
    await state.update_data(report_type="stats")
    await message.answer(
        "Хотите изменить файл?",
        reply_markup=export_edit_keyboard("stats"),
    )


@router.callback_query(ExportReportEdit.confirm, F.data.startswith("export_edit:"))
async def export_edit_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    _, report_type, decision = callback.data.split(":")
    if decision == "no":
        await state.clear()
        await callback.message.answer("Хорошо, оставляю файл без изменений.")
        await callback.answer()
        return
    await state.set_state(ExportReportEdit.file)
    await state.update_data(report_type=report_type)
    await callback.message.answer(
        "Скачайте файл, внесите изменения и отправьте обратно.\n"
        "Можно менять только столбцы: Подразделение, ЦФО, МОЛ, Статус, "
        "Исполнитель, Поставщик, Срок поставки.\n"
        "Заголовки и ID менять нельзя. Формат даты: DD-MM-YYYY.",
    )
    await callback.answer()


@router.message(ExportReportEdit.file)
async def export_edit_upload(message: Message, state: FSMContext) -> None:
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
        user = await _get_user(session, message.from_user)
        if not await _is_executor(session, user.id):
            await message.answer("Доступно только исполнителям.")
            await state.clear()
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
            req_rows = await session.execute(
                select(Request).where(Request.id.in_(ids))
            )
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

            cfo_name = _normalize_text(values.get("ЦФО"))
            if not cfo_name:
                errors.append(f"Строка {row_num}: не заполнено ЦФО.")
                continue
            cfo_id = cfo_map.get(cfo_name.casefold())
            if not cfo_id:
                errors.append(f"Строка {row_num}: ЦФО \"{cfo_name}\" не найдено.")
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
            await message.answer(
                "Не удалось обработать файл:\n" + preview + suffix
            )
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

    await state.clear()


@router.message(F.text == "📅 Срок поставки")
async def delivery_menu(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        if not await _is_executor(session, user.id):
            await message.answer("Доступно только исполнителям.")
            return
    await state.clear()
    await _show_delivery_list(message, user.id, filter_key="missing", page=1, edit=False)
