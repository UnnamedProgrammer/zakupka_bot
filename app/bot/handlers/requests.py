import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot.keyboards import approval_action_keyboard
from app.bot.states import RequestCreate, TemplateDownload
from app.config import settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    Attachment,
    Cfo,
    DdsArticle,
    Department,
    OmtsResponsible,
    Request,
    RequestCategory,
    RequestItem,
    RequestStatus,
    User,
)
from app.db.session import SessionLocal
from app.services.attachments import build_attachment_groups_from, fetch_request_media
from app.services.constants import APPROVAL_STATUS_PENDING, REQUEST_STATUS_PENDING
from app.services.excel import (
    TemplateParseError,
    build_request_template_prefilled_xlsx,
    build_request_xlsx,
    parse_request_template,
)
from app.services.files import save_bytes_file, save_telegram_file
from app.services.formatters import format_request_summary
from app.services.notifications import send_to_user
from app.services.users import ensure_username_format, get_or_create_user
from app.bot.handlers.common import cleanup_main_menu

router = Router()
logger = logging.getLogger(__name__)
_album_buffers: dict[tuple[int, str], dict] = {}
_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "request_template.xlsx"
TEMPLATE_PAGE_SIZE = 6


async def _try_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except (TelegramForbiddenError, TelegramBadRequest):
        return


def _is_image_document(message: Message) -> bool:
    if not message.document:
        return False
    mime_type = message.document.mime_type or ""
    return mime_type.startswith("image/")


async def _queue_album_attachment(message: Message, state: FSMContext, file_info: dict) -> None:
    if not message.media_group_id:
        return
    data = await state.get_data()
    item_index = data.get("current_item_index")
    if item_index is None:
        return
    key = (message.chat.id, message.media_group_id)
    buffer = _album_buffers.get(key)
    if not buffer:
        buffer = {
            "photos": [],
            "chat_id": message.chat.id,
            "bot": message.bot,
            "task": None,
            "item_index": item_index,
        }
        _album_buffers[key] = buffer
    buffer["photos"].append(file_info)
    if buffer["task"] is None:
        buffer["task"] = asyncio.create_task(_flush_album(key, state))


async def _flush_album(key: tuple[int, str], state: FSMContext) -> None:
    await asyncio.sleep(0.8)
    buffer = _album_buffers.pop(key, None)
    if not buffer:
        return
    data = await state.get_data()
    items = data.get("items") or []
    item_index = buffer.get("item_index")
    if item_index is None or item_index >= len(items):
        return
    item = items[item_index]
    attachments = item.get("attachments") or []
    current_photos = sum(1 for att in attachments if att.get("file_type") == "photo")
    allowed = max(0, 3 - current_photos)
    if allowed <= 0:
        await buffer["bot"].send_message(
            buffer["chat_id"], "Можно добавить не более 3 фото для товара."
        )
        return
    photos = buffer["photos"]
    to_add = photos[:allowed]
    attachments.extend(to_add)
    item["attachments"] = attachments
    items[item_index] = item
    await state.update_data(items=items)
    if len(photos) > allowed:
        await buffer["bot"].send_message(
            buffer["chat_id"],
            f"Добавлено {allowed} фото. Ограничение на количество фото макс. 3",
        )
    else:
        await buffer["bot"].send_message(
            buffer["chat_id"],
            f"Добавлено фото: {len(photos)}. Можно отправить еще или нажмите «Готово».",
        )


async def _get_status_id(session, model, code: str) -> int:
    return await session.scalar(select(model.id).where(model.code == code))


def _normalize_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _normalize_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return " ".join(text.split())


def _paginate(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start : start + page_size], page, total_pages


def _truncate_text(text: str, max_len: int = 48) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _display_value(value: str | None, default: str = "не выбрано") -> str:
    text = _normalize_text(value)
    if not text:
        return default
    return _truncate_text(text, 60)


def _item_label(item: dict, index: int) -> str:
    name = _normalize_text(item.get("name")) or "без названия"
    qty = _normalize_text(item.get("qty"))
    unit = _normalize_text(item.get("unit"))
    qty_unit = " ".join(part for part in [qty, unit] if part)
    suffix = f" ({qty_unit})" if qty_unit else ""
    label = f"{index + 1}. {name}{suffix}"
    return _truncate_text(label, 60)


def _items_overview(items: list[dict]) -> str:
    if not items:
        return "нет"
    lines = [f"- {_item_label(item, idx)}" for idx, item in enumerate(items)]
    return "\n".join(lines)


def _count_links(text: str | None) -> int:
    if not text:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def _is_item_empty(item: dict) -> bool:
    if _normalize_text(item.get("name")):
        return False
    if _normalize_text(item.get("specs")):
        return False
    if _normalize_text(item.get("brand")):
        return False
    if _normalize_text(item.get("qty")):
        return False
    if _normalize_text(item.get("unit")):
        return False
    if _normalize_text(item.get("link")):
        return False
    if _normalize_text(item.get("note")):
        return False
    attachments = item.get("attachments") or []
    return len(attachments) == 0


async def _get_or_create_reference(
    session, model, name: str | None, cache: dict[str, int]
) -> int | None:
    if not name:
        return None
    normalized_name = " ".join(name.split())
    key = _normalize_key(normalized_name)
    if not key:
        return None
    if key in cache:
        return cache[key]
    obj = await session.scalar(select(model).where(func.lower(func.trim(model.name)) == key))
    if not obj:
        obj = model(name=normalized_name)
        session.add(obj)
        await session.flush()
    cache[key] = obj.id
    return obj.id


async def _send_request_with_attachments(
    bot,
    request: Request,
    user: User,
    items: list[RequestItem],
    attachments: list[Attachment],
) -> None:
    await send_to_user(bot, user, format_request_summary(request))
    if not user.tg_id:
        return
    attachment_groups = build_attachment_groups_from(request, items, attachments)
    if attachment_groups:
        await bot.send_message(user.tg_id, "Вложения по товарам:")
    for title, item_attachments in attachment_groups:
        await bot.send_message(user.tg_id, title)
        for att in item_attachments:
            if att.file_type == "photo":
                if att.file_path:
                    await bot.send_photo(user.tg_id, FSInputFile(att.file_path))
                elif att.file_id:
                    await bot.send_photo(user.tg_id, att.file_id)
                continue
            if att.file_type != "document":
                continue
            if att.file_id:
                await bot.send_document(user.tg_id, att.file_id)
            elif att.file_path:
                await bot.send_document(
                    user.tg_id,
                    FSInputFile(att.file_path, filename=att.file_name or None),
                )


async def _send_request_with_attachments_to_chat(
    bot,
    request: Request,
    chat_id: int,
    items: list[RequestItem],
    attachments: list[Attachment],
) -> None:
    await bot.send_message(chat_id, format_request_summary(request))
    attachment_groups = build_attachment_groups_from(request, items, attachments)
    if attachment_groups:
        await bot.send_message(chat_id, "Вложения по товарам:")
    for title, item_attachments in attachment_groups:
        await bot.send_message(chat_id, title)
        for att in item_attachments:
            if att.file_type == "photo":
                if att.file_path:
                    await bot.send_photo(chat_id, FSInputFile(att.file_path))
                elif att.file_id:
                    await bot.send_photo(chat_id, att.file_id)
                continue
            if att.file_type != "document":
                continue
            if att.file_id:
                await bot.send_document(chat_id, att.file_id)
            elif att.file_path:
                await bot.send_document(
                    chat_id,
                    FSInputFile(att.file_path, filename=att.file_name or None),
                )


async def _send_approval_to_chat(
    bot,
    request: Request,
    approval_id: int,
    chat_id: int,
    items: list[RequestItem],
    attachments: list[Attachment],
) -> None:
    try:
        await _send_request_with_attachments_to_chat(
            bot, request, chat_id, items, attachments
        )
        await bot.send_message(
            chat_id,
            "Примите решение по заявке:",
            reply_markup=approval_action_keyboard(approval_id),
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning("Failed to send approval to chat_id=%s: %s", chat_id, exc)


async def _resolve_override_user(session) -> User | None:
    if not settings.approval_override_username:
        return None
    override = await ensure_username_format(settings.approval_override_username)
    if not override:
        return None
    user = await session.scalar(select(User).where(User.tg_username == override))
    if not user or not user.tg_id:
        logger.warning(
            "Override user %s not found or missing tg_id. Run /start with that account.",
            override,
        )
        return user
    return user


async def _get_next_pending_approval(session, request_id: int):
    pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
    rows = await session.execute(
        select(Approval, User)
        .join(User, User.id == Approval.approver_id)
        .where(Approval.request_id == request_id, Approval.status_id == pending_id)
        .order_by(Approval.id)
    )
    return rows.first()


def _request_menu_text(data: dict, error: str | None = None) -> str:
    method = data.get("request_method")
    method_label = {"manual": "вручную", "excel": "excel"}.get(method, "не выбран")
    initiator = data.get("initiator_name") or data.get("initiator_tg_name")
    lines = [
        "📝 Создание заявки",
        f"Способ: {method_label}",
        f"Инициатор: {_display_value(initiator, default='не указан')}",
    ]

    if method == "manual":
        lines.extend(
            [
                f"Подразделение: {_display_value(data.get('department_name'))}",
                f"ЦФО: {_display_value(data.get('cfo_name'))}",
                f"МОЛ: {_display_value(data.get('mol_full_name'), default='не указан')}",
                (
                    "Макс. цена договора (тыс.руб.): "
                    f"{_display_value(data.get('contract_max_price'))}"
                ),
                (
                    "БДДС (Статья - Категория): "
                    f"{_display_value(data.get('bdds_article_category'))}"
                ),
                "Товары:",
                _items_overview(data.get("items") or []),
                "Согласующий: автоматически (Default Approver)",
            ]
        )
        lines.append("")
        lines.append("Заполните поля и нажмите «Отправить».")
    elif method == "excel":
        file_name = data.get("excel_file_name")
        if file_name:
            lines.append(f"Файл: {file_name}")
        else:
            lines.append("Файл: не загружен")
    else:
        lines.append("Выберите способ создания заявки.")

    if error:
        lines.append("")
        lines.append(f"⚠️ {error}")
    return "\n".join(lines)


def _request_menu_keyboard(data: dict):
    method = data.get("request_method")
    builder = InlineKeyboardBuilder()

    if method == "excel":
        builder.button(text="❌ Отмена", callback_data="req_cancel")
        builder.adjust(1)
        return builder.as_markup()

    builder.button(text="✍️ Вручную", callback_data="req_method:manual")
    builder.button(text="📄 Excel", callback_data="req_method:excel")

    if method == "manual":
        builder.button(text="🏢 Подразделение", callback_data="req_field:department")
        builder.button(text="🏷️ ЦФО", callback_data="req_field:cfo")
        builder.button(text="👔 МОЛ", callback_data="req_field:mol")
        builder.button(text="💰 Макс. цена", callback_data="req_field:contract_max_price")
        builder.button(text="📑 БДДС", callback_data="req_field:bdds_article_category")
        builder.button(text="🧾 Товары", callback_data="req_items:menu")
        builder.button(text="🚀 Отправить", callback_data="req_submit")
        builder.button(text="❌ Отмена", callback_data="req_cancel")
        builder.adjust(2, 2, 2, 2, 2)
    else:
        builder.button(text="❌ Отмена", callback_data="req_cancel")
        builder.adjust(2, 1)
    return builder.as_markup()


def _departments_keyboard(items: list[tuple[int, str]]):
    builder = InlineKeyboardBuilder()
    for dep_id, name in items:
        builder.button(text=f"🏢 {name}", callback_data=f"req_department:{dep_id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="req_menu"))
    return builder.as_markup()


def _cfo_keyboard(items: list[tuple[int, str]]):
    builder = InlineKeyboardBuilder()
    for cfo_id, name in items:
        builder.button(text=f"🏷️ {name}", callback_data=f"req_cfo:{cfo_id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="req_menu"))
    return builder.as_markup()


def _manual_departments_keyboard(items: list[tuple[int, str]]):
    builder = InlineKeyboardBuilder()
    for dep_id, name in items:
        builder.button(text=f"🏢 {name}", callback_data=f"req_department:{dep_id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel"))
    return builder.as_markup()


def _manual_cfo_keyboard(items: list[tuple[int, str]]):
    builder = InlineKeyboardBuilder()
    for cfo_id, name in items:
        builder.button(text=f"🏷️ {name}", callback_data=f"req_cfo:{cfo_id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel"))
    return builder.as_markup()


def _manual_item_more_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить товар", callback_data="item_more:yes")
    builder.button(text="🚀 Отправить заявку", callback_data="item_more:no")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="req_cancel"))
    return builder.as_markup()


def _items_menu_keyboard(items: list[dict]):
    builder = InlineKeyboardBuilder()
    for idx, item in enumerate(items):
        builder.button(text=_item_label(item, idx), callback_data=f"req_item_edit:{idx}")
    builder.button(text="➕ Добавить товар", callback_data="req_item_add")
    builder.button(text="⬅️ Назад", callback_data="req_menu")
    builder.adjust(1)
    return builder.as_markup()


def _item_editor_keyboard(item_index: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="Наименование", callback_data=f"req_item_field:{item_index}:name")
    builder.button(text="Характеристики", callback_data=f"req_item_field:{item_index}:specs")
    builder.button(text="Марка/аналог", callback_data=f"req_item_field:{item_index}:brand")
    builder.button(text="Количество", callback_data=f"req_item_field:{item_index}:qty")
    builder.button(text="Ед. измерения", callback_data=f"req_item_field:{item_index}:unit")
    builder.button(text="Ссылка", callback_data=f"req_item_field:{item_index}:link")
    builder.button(text="Примечание", callback_data=f"req_item_field:{item_index}:note")
    builder.button(text="Вложения", callback_data=f"req_item_attachments:{item_index}")
    builder.button(text="🗑️ Удалить товар", callback_data=f"req_item_delete:{item_index}")
    builder.button(text="⬅️ К списку", callback_data="req_items:menu")
    builder.adjust(2, 2, 2, 2, 1, 1)
    return builder.as_markup()


def _input_prompt_keyboard(clear_callback: str, back_callback: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Очистить", callback_data=clear_callback)
    builder.button(text="⬅️ Назад", callback_data=back_callback)
    builder.adjust(2)
    return builder.as_markup()


def _attachments_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="req_item_attach_done")
    builder.button(text="⏭️ Пропустить", callback_data="req_item_attach_skip")
    builder.button(text="🧹 Очистить", callback_data="req_item_attach_clear")
    builder.adjust(2, 1)
    return builder.as_markup()


def _template_departments_keyboard(
    items: list[tuple[int, str]], page: int, total_pages: int
):
    builder = InlineKeyboardBuilder()
    for dep_id, name in items:
        builder.button(text=_truncate_text(name, 60), callback_data=f"tmpl_dep_pick:{dep_id}:{page}")
    if items:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"tmpl_dep_list:{page - 1}")
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(text="➡️ Далее", callback_data=f"tmpl_dep_list:{page + 1}")
            )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="tmpl_cancel"))
    return builder.as_markup()


def _template_cfo_keyboard(
    items: list[tuple[int, str]], page: int, total_pages: int
):
    builder = InlineKeyboardBuilder()
    for cfo_id, name in items:
        builder.button(text=_truncate_text(name, 60), callback_data=f"tmpl_cfo_pick:{cfo_id}:{page}")
    if items:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"tmpl_cfo_list:{page - 1}")
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(text="➡️ Далее", callback_data=f"tmpl_cfo_list:{page + 1}")
            )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="⬅️ К подразделениям", callback_data="tmpl_dep_back"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="tmpl_cancel"))
    return builder.as_markup()


def _items_menu_text(items: list[dict]) -> str:
    if not items:
        return "Товары не добавлены."
    lines = ["Список товаров:"]
    lines.extend([f"- {_item_label(item, idx)}" for idx, item in enumerate(items)])
    return "\n".join(lines)


def _item_editor_text(item: dict, index: int) -> str:
    attachments = item.get("attachments") or []
    links_count = _count_links(item.get("link"))
    return "\n".join(
        [
            f"Товар {index + 1}",
            f"Наименование: {_display_value(item.get('name'), default='не указано')}",
            f"Характеристики: {_display_value(item.get('specs'), default='не указаны')}",
            f"Марка/аналог: {_display_value(item.get('brand'), default='не указана')}",
            f"Количество: {_display_value(item.get('qty'), default='не указано')}",
            f"Ед. измерения: {_display_value(item.get('unit'), default='не указана')}",
            f"Ссылки: {links_count}",
            f"Вложения: {len(attachments)}",
            f"Примечание: {_display_value(item.get('note'), default='не указано')}",
        ]
    )


async def _edit_request_message(
    bot,
    state: FSMContext,
    text: str,
    reply_markup=None,
    fallback_message: Message | None = None,
) -> None:
    data = await state.get_data()
    chat_id = data.get("req_chat_id")
    message_id = data.get("req_message_id")
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
        req_message_id=new_message.message_id,
        req_chat_id=new_message.chat.id,
    )


async def _delete_tracked_message(
    bot,
    state: FSMContext,
    message_key: str,
    chat_key: str,
    fallback_message: Message | None = None,
) -> None:
    data = await state.get_data()
    chat_id = data.get(chat_key)
    message_id = data.get(message_key)
    if chat_id and message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    elif fallback_message:
        try:
            await fallback_message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    await state.update_data({message_key: None, chat_key: None})


async def _show_request_menu(
    bot,
    state: FSMContext,
    error: str | None = None,
    fallback_message: Message | None = None,
) -> None:
    data = await state.get_data()
    await _edit_request_message(
        bot,
        state,
        _request_menu_text(data, error=error),
        _request_menu_keyboard(data),
        fallback_message=fallback_message,
    )


async def _edit_template_message(
    bot,
    state: FSMContext,
    text: str,
    reply_markup=None,
    fallback_message: Message | None = None,
) -> None:
    data = await state.get_data()
    chat_id = data.get("tmpl_chat_id")
    message_id = data.get("tmpl_message_id")
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
        tmpl_message_id=new_message.message_id,
        tmpl_chat_id=new_message.chat.id,
    )


async def _show_template_departments(
    bot, state: FSMContext, page: int, fallback_message: Message | None = None
) -> None:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Department.id, Department.name).order_by(Department.name)
        )
        items = rows.all()
    items_page, page, total_pages = _paginate(items, page, TEMPLATE_PAGE_SIZE)
    text = "Выберите подразделение."
    if items:
        text += f" Страница {page}/{total_pages}."
    else:
        text += " Подразделения не найдены."
    await _edit_template_message(
        bot,
        state,
        text,
        _template_departments_keyboard(items_page, page, total_pages),
        fallback_message=fallback_message,
    )


async def _show_template_cfos(
    bot, state: FSMContext, page: int, fallback_message: Message | None = None
) -> None:
    async with SessionLocal() as session:
        rows = await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
        items = rows.all()
    items_page, page, total_pages = _paginate(items, page, TEMPLATE_PAGE_SIZE)
    text = "Выберите ЦФО."
    if items:
        text += f" Страница {page}/{total_pages}."
    else:
        text += " ЦФО не найдены."
    await _edit_template_message(
        bot,
        state,
        text,
        _template_cfo_keyboard(items_page, page, total_pages),
        fallback_message=fallback_message,
    )


async def _start_template_download(
    bot,
    state: FSMContext,
    fallback_message: Message | None = None,
    return_to_request: bool = False,
) -> None:
    data = await state.get_data()
    update_payload = {"tmpl_return": "request" if return_to_request else None}
    if return_to_request:
        update_payload["tmpl_message_id"] = data.get("req_message_id")
        update_payload["tmpl_chat_id"] = data.get("req_chat_id")
    await state.update_data(**update_payload)
    await state.set_state(TemplateDownload.department)
    await _show_template_departments(bot, state, page=1, fallback_message=fallback_message)


async def _finish_template_download(
    bot,
    state: FSMContext,
    fallback_message: Message | None = None,
    cancelled: bool = False,
) -> None:
    data = await state.get_data()
    return_to_request = data.get("tmpl_return") == "request"
    await state.update_data(tmpl_return=None, tmpl_message_id=None, tmpl_chat_id=None)
    if return_to_request:
        await state.set_state(RequestCreate.menu)
        await _show_request_menu(bot, state, fallback_message=fallback_message)
        return
    await state.clear()
    if cancelled:
        await _delete_tracked_message(
            bot,
            state,
            "tmpl_message_id",
            "tmpl_chat_id",
            fallback_message=fallback_message,
        )

async def _show_items_menu(
    bot, state: FSMContext, fallback_message: Message | None = None
) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    await _edit_request_message(
        bot,
        state,
        _items_menu_text(items),
        _items_menu_keyboard(items),
        fallback_message=fallback_message,
    )


async def _show_item_editor(
    bot, state: FSMContext, item_index: int, fallback_message: Message | None = None
) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    if item_index < 0 or item_index >= len(items):
        await _show_items_menu(bot, state, fallback_message=fallback_message)
        return
    item = items[item_index]
    await _edit_request_message(
        bot,
        state,
        _item_editor_text(item, item_index),
        _item_editor_keyboard(item_index),
        fallback_message=fallback_message,
    )


async def _show_input_prompt(
    bot,
    state: FSMContext,
    title: str,
    clear_callback: str,
    back_callback: str,
    fallback_message: Message | None = None,
) -> None:
    await _edit_request_message(
        bot,
        state,
        title,
        _input_prompt_keyboard(clear_callback, back_callback),
        fallback_message=fallback_message,
    )


async def _show_attachment_prompt(
    bot, state: FSMContext, item_index: int, fallback_message: Message | None = None
) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    if item_index < 0 or item_index >= len(items):
        await _show_items_menu(bot, state, fallback_message=fallback_message)
        return
    item = items[item_index]
    attachments = item.get("attachments") or []
    links_count = _count_links(item.get("link"))
    text = (
        "Отправьте фото, файл или ссылку на товар.\n"
        "Или нажмите «Готово» / «Пропустить».\n"
        f"Ссылки: {links_count}\n"
        f"Вложения: {len(attachments)}"
    )
    await _edit_request_message(
        bot,
        state,
        text,
        _attachments_keyboard(),
        fallback_message=fallback_message,
    )


def _validate_manual_request(data: dict) -> list[str]:
    errors: list[str] = []
    if not data.get("department_id"):
        errors.append("подразделение")
    if not data.get("cfo_id"):
        errors.append("ЦФО")
    if not _normalize_text(data.get("mol_full_name")):
        errors.append("МОЛ")
    items = data.get("items") or []
    if not items:
        errors.append("хотя бы один товар")
    else:
        for idx, item in enumerate(items):
            if not _normalize_text(item.get("name")):
                errors.append(f"товар {idx + 1}: наименование")
            if not _normalize_text(item.get("qty")):
                errors.append(f"товар {idx + 1}: количество")
            if not _normalize_text(item.get("unit")):
                errors.append(f"товар {idx + 1}: единица измерения")
    return errors


async def _create_request(
    bot,
    data: dict,
    tg_user,
    reply_message: Message,
) -> Request | None:
    async with SessionLocal() as session:
        initiator_id = data.get("initiator_id")
        initiator = await session.get(User, initiator_id) if initiator_id else None
        if not initiator:
            username = await ensure_username_format(tg_user.username)
            initiator = await get_or_create_user(
                session, tg_user.id, username, data.get("initiator_name") or tg_user.full_name
            )
        if not initiator:
            await reply_message.answer("Не удалось определить инициатора заявки.")
            return None

        department_id = data.get("department_id")
        if department_id:
            initiator.department_id = department_id

        status_id = await _get_status_id(session, RequestStatus, REQUEST_STATUS_PENDING)
        items = data.get("items") or []
        if not items:
            await reply_message.answer("В заявке нет товаров. Проверьте данные.")
            return None
        primary_item = items[0]

        if not data.get("department_id") or not data.get("cfo_id"):
            await reply_message.answer("Не заполнены подразделение или ЦФО.")
            return None

        request = Request(
            status_id=status_id,
            initiator_id=initiator.id,
            department_id=data.get("department_id"),
            cfo_id=data.get("cfo_id"),
            description_method=data.get("description_method", "manual"),
            item_name=primary_item.get("name"),
            item_specs=primary_item.get("specs"),
            item_brand=primary_item.get("brand"),
            item_qty=primary_item.get("qty"),
            item_unit=primary_item.get("unit"),
            item_link=primary_item.get("link"),
            item_note=primary_item.get("note"),
            mol_full_name=data.get("mol_full_name"),
            contract_max_price=data.get("contract_max_price"),
            bdds_article_category=data.get("bdds_article_category"),
        )
        session.add(request)
        await session.flush()

        ref_cache = {"omts": {}, "category": {}, "dds": {}}
        for item in items:
            omts_id = await _get_or_create_reference(
                session, OmtsResponsible, item.get("omts_responsible"), ref_cache["omts"]
            )
            category_id = await _get_or_create_reference(
                session, RequestCategory, item.get("category"), ref_cache["category"]
            )
            dds_article_id = await _get_or_create_reference(
                session, DdsArticle, item.get("dds_article"), ref_cache["dds"]
            )
            item_row = RequestItem(
                request_id=request.id,
                name=item.get("name"),
                specs=item.get("specs"),
                brand=item.get("brand"),
                qty=item.get("qty"),
                unit=item.get("unit"),
                link=item.get("link"),
                note=item.get("note"),
                max_price=item.get("max_price"),
                omts_responsible_id=omts_id,
                category_id=category_id,
                dds_article_id=dds_article_id,
            )
            session.add(item_row)
            await session.flush()
            photo_saved = 0
            for item_att in item.get("attachments") or []:
                if item_att.get("file_type") == "photo":
                    if photo_saved >= 3:
                        continue
                    photo_saved += 1
                file_path = await save_telegram_file(
                    bot,
                    item_att["file_id"],
                    dest_dir=settings.files_dir,
                    filename_hint=item_att.get("file_name"),
                )
                session.add(
                    Attachment(
                        request_id=request.id,
                        uploader_id=initiator.id,
                        item_id=item_row.id,
                        file_id=item_att["file_id"],
                        file_unique_id=item_att["file_unique_id"],
                        file_name=item_att.get("file_name"),
                        file_path=file_path,
                        file_type=item_att["file_type"],
                    )
                )

        if data.get("description_method") == "excel":
            excel_file_path = data.get("excel_file_path")
            excel_file_name = data.get("excel_file_name") or "request_template.xlsx"
            if not excel_file_path:
                await reply_message.answer("Файл Excel не найден. Загрузка заявки остановлена.")
                return None
            session.add(
                Attachment(
                    request_id=request.id,
                    uploader_id=initiator.id,
                    item_id=None,
                    file_id=None,
                    file_unique_id=None,
                    file_name=excel_file_name,
                    file_path=excel_file_path,
                    file_type="document",
                )
            )

        ordered_approvers = (
            await session.execute(
                select(User)
                .where(User.is_default_approver.is_(True))
                .order_by(User.full_name, User.id)
            )
        ).scalars().all()
        if not ordered_approvers:
            await reply_message.answer(
                "Не найден пользователь с флагом Default Approver. Обратитесь к администратору."
            )
            return None

        approval_status_id = await _get_status_id(
            session, ApprovalStatus, APPROVAL_STATUS_PENDING
        )

        approvals = []
        for user in ordered_approvers:
            approval = Approval(
                request_id=request.id,
                approver_id=user.id,
                status_id=approval_status_id,
            )
            session.add(approval)
            approvals.append(approval)

        await session.commit()

        if data.get("description_method") != "excel":
            refreshed = await session.execute(
                select(Request)
                .where(Request.id == request.id)
                .options(
                    selectinload(Request.initiator),
                    selectinload(Request.department),
                    selectinload(Request.cfo),
                    selectinload(Request.status),
                    selectinload(Request.items),
                    selectinload(Request.attachments),
                )
                .execution_options(populate_existing=True)
            )
            request = refreshed.scalar_one()
            items_list, attachments_list = await fetch_request_media(session, request.id)
            excel_content = build_request_xlsx(request, items_list, attachments_list)
            excel_name = f"request_{request.id}.xlsx"
            excel_path = save_bytes_file(excel_content, settings.files_dir, excel_name)
            session.add(
                Attachment(
                    request_id=request.id,
                    uploader_id=initiator.id,
                    file_id=None,
                    file_unique_id=None,
                    file_name=excel_name,
                    file_path=excel_path,
                    file_type="document",
                )
            )
            await session.commit()

        refreshed = await session.execute(
            select(Request)
            .where(Request.id == request.id)
            .options(
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.attachments),
                selectinload(Request.items),
            )
            .execution_options(populate_existing=True)
        )
        request = refreshed.scalar_one()

        approver_names = [
            user.full_name or user.tg_username or f"ID {user.id}"
            for user in ordered_approvers
        ]
        approvers_text = ", ".join(approver_names)
        await reply_message.answer(
            f"Ваша заявка №{request.id} отправлена на согласование: {approvers_text}."
        )

        override_user = await _resolve_override_user(session)
        next_pending = await _get_next_pending_approval(session, request.id)
        if next_pending:
            approval, approver = next_pending
            items_list, attachments_list = await fetch_request_media(session, request.id)
            override_tg_id = settings.approval_override_tg_id
            if override_tg_id:
                await _send_approval_to_chat(
                    bot, request, approval.id, override_tg_id, items_list, attachments_list
                )
            else:
                target_user = (
                    override_user if override_user and override_user.tg_id else approver
                )
                if target_user.tg_id:
                    if approver.is_default_approver:
                        await send_to_user(
                            bot,
                            target_user,
                            (
                                f"📌 Заявка №{request.id} требует согласования, "
                                "проверьте \"Мои заявки\"."
                            ),
                        )
                    else:
                        await _send_request_with_attachments(
                            bot, request, target_user, items_list, attachments_list
                        )
                        await send_to_user(
                            bot,
                            target_user,
                            "Примите решение по заявке:",
                            reply_markup=approval_action_keyboard(approval.id),
                        )

        return request


@router.message(F.text == "📥 Скачать шаблон заявки")
async def download_request_template(message: Message, state: FSMContext) -> None:
    await cleanup_main_menu(message, state)
    await state.clear()
    await _start_template_download(message.bot, state, fallback_message=message)


@router.message(F.text == "📝 Создать заявку")
async def create_request_start(message: Message, state: FSMContext) -> None:
    await cleanup_main_menu(message, state)
    await state.clear()
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        await session.commit()
    initiator_name = _normalize_text(user.full_name) or _normalize_text(user.tg_username)
    await state.set_state(RequestCreate.menu)
    await state.update_data(
        request_method=None,
        description_method=None,
        initiator_id=user.id,
        initiator_name=initiator_name,
        initiator_tg_name=message.from_user.full_name,
        contract_max_price=None,
        bdds_article_category=None,
        items=[],
    )
    sent = await message.answer(
        _request_menu_text(await state.get_data()),
        reply_markup=_request_menu_keyboard(await state.get_data()),
    )
    await state.update_data(req_message_id=sent.message_id, req_chat_id=sent.chat.id)


@router.callback_query(StateFilter(RequestCreate), F.data == "req_template")
async def request_template_start(callback: CallbackQuery, state: FSMContext) -> None:
    await _start_template_download(
        callback.bot, state, fallback_message=callback.message, return_to_request=True
    )
    await callback.answer()


@router.callback_query(StateFilter(TemplateDownload), F.data == "tmpl_cancel")
async def template_download_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await _finish_template_download(
        callback.bot, state, fallback_message=callback.message, cancelled=True
    )
    await callback.answer()


@router.callback_query(TemplateDownload.department, F.data.startswith("tmpl_dep_list:"))
async def template_departments_page(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    await _show_template_departments(
        callback.bot, state, page=page, fallback_message=callback.message
    )
    await callback.answer()


@router.callback_query(TemplateDownload.department, F.data.startswith("tmpl_dep_pick:"))
async def template_department_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, dep_id, _page = callback.data.split(":")
    dep_id = int(dep_id)
    async with SessionLocal() as session:
        department = await session.get(Department, dep_id)
    if not department:
        await callback.answer("Подразделение не найдено.")
        return
    await state.update_data(
        tmpl_department_id=department.id,
        tmpl_department_name=department.name,
    )
    await state.set_state(TemplateDownload.cfo)
    await _show_template_cfos(callback.bot, state, page=1, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(TemplateDownload.cfo, F.data == "tmpl_dep_back")
async def template_back_departments(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TemplateDownload.department)
    await _show_template_departments(
        callback.bot, state, page=1, fallback_message=callback.message
    )
    await callback.answer()


@router.callback_query(TemplateDownload.cfo, F.data.startswith("tmpl_cfo_list:"))
async def template_cfo_page(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.split(":")[1])
    await _show_template_cfos(callback.bot, state, page=page, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(TemplateDownload.cfo, F.data.startswith("tmpl_cfo_pick:"))
async def template_cfo_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, cfo_id, _page = callback.data.split(":")
    cfo_id = int(cfo_id)
    async with SessionLocal() as session:
        cfo = await session.get(Cfo, cfo_id)
        username = await ensure_username_format(callback.from_user.username)
        initiator = await get_or_create_user(
            session,
            tg_id=callback.from_user.id,
            username=username,
            full_name=callback.from_user.full_name,
        )
        await session.commit()
    if not cfo:
        await callback.answer("ЦФО не найдено.")
        return
    data = await state.get_data()
    department_name = data.get("tmpl_department_name")
    if not department_name:
        await callback.answer("Не выбрано подразделение.")
        return
    initiator_name = initiator.full_name or callback.from_user.full_name
    try:
        content = build_request_template_prefilled_xlsx(
            department_name=department_name,
            cfo_name=cfo.name,
            initiator_name=initiator_name,
        )
    except FileNotFoundError:
        await callback.message.answer("Шаблон заявки не найден. Сообщите администратору.")
        await _finish_template_download(callback.bot, state, fallback_message=callback.message)
        await callback.answer()
        return

    await callback.message.answer_document(
        BufferedInputFile(content, filename=_TEMPLATE_PATH.name),
        caption="Шаблон заявки",
    )
    await _finish_template_download(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_menu")
async def request_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("manual_wizard") and data.get("request_method") == "manual":
        await state.set_state(RequestCreate.menu)
        async with SessionLocal() as session:
            dep_rows = await session.execute(
                select(Department.id, Department.name).order_by(Department.name)
            )
            deps = dep_rows.all()
        if not deps:
            await callback.answer("Подразделения не найдены")
            return
        await callback.message.answer(
            "🏢 Шаг 1/6. Выберите подразделение:",
            reply_markup=_manual_departments_keyboard(deps),
        )
        await callback.answer()
        return
    await state.set_state(RequestCreate.menu)
    await _show_request_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_cancel")
async def request_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await _delete_tracked_message(
        callback.bot,
        state,
        "req_message_id",
        "req_chat_id",
        fallback_message=callback.message,
    )
    await state.clear()
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_method:"))
async def request_set_method(callback: CallbackQuery, state: FSMContext) -> None:
    method = callback.data.split(":")[1]
    reset_payload = {
        "department_id": None,
        "department_name": None,
        "cfo_id": None,
        "cfo_name": None,
        "mol_full_name": None,
        "contract_max_price": None,
        "bdds_article_category": None,
        "items": [],
        "excel_groups": None,
        "excel_group_index": None,
        "excel_file_path": None,
        "excel_file_name": None,
        "current_item_index": None,
        "input_target": None,
        "manual_wizard": method == "manual",
    }
    await state.update_data(request_method=method, description_method=method, **reset_payload)
    if method == "excel":
        await state.set_state(RequestCreate.excel_file)
        await _show_request_menu(callback.bot, state, fallback_message=callback.message)
        await callback.message.answer("📄 Отправьте Excel файл по шаблону.")
        await callback.answer()
        return
    else:
        await state.set_state(RequestCreate.menu)
        async with SessionLocal() as session:
            dep_rows = await session.execute(
                select(Department.id, Department.name).order_by(Department.name)
            )
            deps = dep_rows.all()
        if not deps:
            await callback.answer("Подразделения не найдены")
            return
        await _edit_request_message(
            callback.bot,
            state,
            "✍️ Ручной режим заполнения запущен.",
            reply_markup=None,
            fallback_message=callback.message,
        )
        await callback.message.answer(
            "🏢 Шаг 1/6. Выберите подразделение:",
            reply_markup=_manual_departments_keyboard(deps),
        )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_excel:upload")
async def request_excel_upload_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RequestCreate.excel_file)
    await _edit_request_message(
        callback.bot,
        state,
        "📄 Ожидаю Excel файл по шаблону. Пришлите его в этот чат.",
        reply_markup=_request_menu_keyboard(await state.get_data()),
        fallback_message=callback.message,
    )
    await callback.answer("Пришлите Excel файл в чат.")


@router.message(RequestCreate.excel_file)
async def request_excel_upload(message: Message, state: FSMContext) -> None:
    await state.update_data(request_method="excel", description_method="excel")
    if not message.document:
        await message.answer("Пожалуйста, отправьте Excel файл (.xlsx).")
        return
    file_name = message.document.file_name or ""
    if Path(file_name).suffix.lower() != ".xlsx":
        await message.answer("Нужен файл формата .xlsx. Проверьте и отправьте заново.")
        await _try_delete_message(message)
        return

    await _edit_request_message(
        message.bot,
        state,
        "⏳ Файл обрабатывается...",
        reply_markup=None,
        fallback_message=message,
    )
    await _try_delete_message(message)

    file_path = await save_telegram_file(
        message.bot,
        message.document.file_id,
        dest_dir=settings.files_dir,
        filename_hint=file_name,
    )
    try:
        parsed = parse_request_template(file_path)
    except TemplateParseError as exc:
        await state.set_state(RequestCreate.excel_file)
        await _show_request_menu(message.bot, state, error=str(exc), fallback_message=message)
        try:
            Path(file_path).unlink()
        except FileNotFoundError:
            pass
        return
    except Exception:
        logger.exception("Failed to parse request template")
        await state.set_state(RequestCreate.excel_file)
        await _show_request_menu(
            message.bot,
            state,
            error="Не удалось прочитать Excel файл. Проверьте шаблон и загрузите заново.",
            fallback_message=message,
        )
        try:
            Path(file_path).unlink()
        except FileNotFoundError:
            pass
        return

    async with SessionLocal() as session:
        initiator_rows = (
            await session.execute(
                select(User).where(User.full_name == parsed["initiator_name"])
            )
        ).scalars().all()
        if not initiator_rows:
            await state.set_state(RequestCreate.excel_file)
            await _show_request_menu(
                message.bot,
                state,
                error=(
                    "Инициатор из ячейки E2 не найден в базе пользователей. "
                    "Исправьте ФИО и загрузите файл заново."
                ),
                fallback_message=message,
            )
            try:
                Path(file_path).unlink()
            except FileNotFoundError:
                pass
            return
        if len(initiator_rows) > 1:
            await state.set_state(RequestCreate.excel_file)
            await _show_request_menu(
                message.bot,
                state,
                error=(
                    "Найдено несколько пользователей с таким ФИО инициатора. "
                    "Уточните ФИО в базе и загрузите файл заново."
                ),
                fallback_message=message,
            )
            try:
                Path(file_path).unlink()
            except FileNotFoundError:
                pass
            return
        initiator = initiator_rows[0]

        dep_rows = await session.execute(select(Department.id, Department.name))
        dep_map: dict[str, list[tuple[int, str]]] = {}
        for dep_id, dep_name in dep_rows.all():
            dep_map.setdefault(_normalize_key(dep_name), []).append((dep_id, dep_name))
        if not dep_map:
            await state.set_state(RequestCreate.excel_file)
            await _show_request_menu(
                message.bot,
                state,
                error="В базе нет подразделений. Загрузка заявки невозможна.",
                fallback_message=message,
            )
            try:
                Path(file_path).unlink()
            except FileNotFoundError:
                pass
            return

        cfo_rows = await session.execute(select(Cfo.id, Cfo.name))
        cfo_map: dict[str, list[tuple[int, str]]] = {}
        for cfo_id, cfo_name in cfo_rows.all():
            cfo_map.setdefault(_normalize_key(cfo_name), []).append((cfo_id, cfo_name))
        if not cfo_map:
            await state.set_state(RequestCreate.excel_file)
            await _show_request_menu(
                message.bot,
                state,
                error="В базе нет ЦФО (Бюджет). Загрузка заявки невозможна.",
                fallback_message=message,
            )
            try:
                Path(file_path).unlink()
            except FileNotFoundError:
                pass
            return

        excel_groups = []
        for group in parsed["groups"]:
            dep_name_from_file = _normalize_text(group.get("department_name"))
            if not dep_name_from_file:
                await state.set_state(RequestCreate.excel_file)
                await _show_request_menu(
                    message.bot,
                    state,
                    error=(
                        "В файле не указано подразделение в ячейке E4. "
                        "Исправьте файл и загрузите заново."
                    ),
                    fallback_message=message,
                )
                try:
                    Path(file_path).unlink()
                except FileNotFoundError:
                    pass
                return

            dep_key = _normalize_key(dep_name_from_file)
            dep_candidates = dep_map.get(dep_key) or []
            if not dep_candidates:
                await state.set_state(RequestCreate.excel_file)
                await _show_request_menu(
                    message.bot,
                    state,
                    error=(
                        f"Подразделение «{group['department_name']}» не найдено в базе. "
                        "Исправьте файл и загрузите заново."
                    ),
                    fallback_message=message,
                )
                try:
                    Path(file_path).unlink()
                except FileNotFoundError:
                    pass
                return
            if len(dep_candidates) > 1:
                dep_list = ", ".join(str(dep_id) for dep_id, _ in dep_candidates)
                await state.set_state(RequestCreate.excel_file)
                await _show_request_menu(
                    message.bot,
                    state,
                    error=(
                        f"Подразделение «{group['department_name']}» неоднозначно (ID: {dep_list}). "
                        "Уточните справочник подразделений."
                    ),
                    fallback_message=message,
                )
                try:
                    Path(file_path).unlink()
                except FileNotFoundError:
                    pass
                return

            cfo_key = _normalize_key(group["cfo_name"])
            cfo_candidates = cfo_map.get(cfo_key) or []
            if not cfo_candidates:
                await state.set_state(RequestCreate.excel_file)
                await _show_request_menu(
                    message.bot,
                    state,
                    error=(
                        f"ЦФО (Бюджет) «{group['cfo_name']}» не найдено в базе. "
                        "Исправьте файл и загрузите заново."
                    ),
                    fallback_message=message,
                )
                try:
                    Path(file_path).unlink()
                except FileNotFoundError:
                    pass
                return
            if len(cfo_candidates) > 1:
                cfo_list = ", ".join(str(cfo_id) for cfo_id, _ in cfo_candidates)
                await state.set_state(RequestCreate.excel_file)
                await _show_request_menu(
                    message.bot,
                    state,
                    error=(
                        f"ЦФО (Бюджет) «{group['cfo_name']}» неоднозначно (ID: {cfo_list}). "
                        "Уточните справочник ЦФО."
                    ),
                    fallback_message=message,
                )
                try:
                    Path(file_path).unlink()
                except FileNotFoundError:
                    pass
                return

            dep_id, dep_name = dep_candidates[0]
            cfo_id, cfo_name = cfo_candidates[0]
            excel_groups.append(
                {
                    "department_id": dep_id,
                    "department_name": dep_name,
                    "cfo_id": cfo_id,
                    "cfo_name": cfo_name,
                    "mol_full_name": group["mol_full_name"],
                    "contract_max_price": group.get("contract_max_price"),
                    "bdds_article_category": group.get("bdds_article_category"),
                    "items": group["items"],
                }
            )

    if not excel_groups:
        await state.set_state(RequestCreate.excel_file)
        await _show_request_menu(
            message.bot,
            state,
            error="В файле нет данных для создания заявки.",
            fallback_message=message,
        )
        try:
            Path(file_path).unlink()
        except FileNotFoundError:
            pass
        return

    await state.set_state(RequestCreate.menu)
    await state.update_data(
        request_method="excel",
        description_method="excel",
        initiator_id=initiator.id,
        initiator_name=initiator.full_name,
        excel_file_path=file_path,
        excel_file_name=file_name,
        excel_groups=excel_groups,
        excel_group_index=0,
    )
    await _show_excel_approver_menu(message, state, message.from_user)


async def _show_excel_approver_menu(message: Message, state: FSMContext, tg_user) -> None:
    data = await state.get_data()
    groups = data.get("excel_groups") or []
    index = data.get("excel_group_index", 0)
    total = len(groups)
    if index >= total:
        await _edit_request_message(
            message.bot,
            state,
            "Загрузка заявок завершена.",
            reply_markup=None,
            fallback_message=message,
        )
        await state.clear()
        return

    while index < total:
        group = groups[index]
        text = (
            f"⏳ Создаю заявки из файла: {index + 1}/{total}\n"
            f"Подразделение: {_display_value(group['department_name'])}\n"
            f"ЦФО: {_display_value(group['cfo_name'])}\n"
            f"МОЛ: {_display_value(group['mol_full_name'], default='не указан')}\n"
            f"Товары: {len(group.get('items') or [])}"
        )
        await _edit_request_message(
            message.bot,
            state,
            text,
            reply_markup=None,
            fallback_message=message,
        )
        await state.update_data(
            department_id=group["department_id"],
            department_name=group["department_name"],
            cfo_id=group["cfo_id"],
            cfo_name=group["cfo_name"],
            mol_full_name=group["mol_full_name"],
            contract_max_price=group.get("contract_max_price"),
            bdds_article_category=group.get("bdds_article_category"),
            items=group["items"],
            excel_group_index=index,
        )
        payload = await state.get_data()
        created = await _create_request(message.bot, payload, tg_user, message)
        if not created:
            await state.set_state(RequestCreate.menu)
            await _show_request_menu(
                message.bot,
                state,
                error="Загрузка заявок остановлена. Исправьте данные и загрузите файл заново.",
                fallback_message=message,
            )
            return
        index += 1
        await state.update_data(excel_group_index=index)

    await _edit_request_message(
        message.bot,
        state,
        "Загрузка заявок завершена.",
        reply_markup=None,
        fallback_message=message,
    )
    await state.clear()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_field:department")
async def request_department_menu(callback: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as session:
        dep_rows = await session.execute(
            select(Department.id, Department.name).order_by(Department.name)
        )
        deps = dep_rows.all()
    await _edit_request_message(
        callback.bot,
        state,
        "🏢 Выберите подразделение",
        _departments_keyboard(deps),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_department:"))
async def request_department_pick(callback: CallbackQuery, state: FSMContext) -> None:
    dep_id = int(callback.data.split(":")[1])
    async with SessionLocal() as session:
        dep = await session.get(Department, dep_id)
    if not dep:
        await callback.answer("Подразделение не найдено.")
        return
    await state.update_data(department_id=dep.id, department_name=dep.name)
    data = await state.get_data()
    if data.get("manual_wizard") and data.get("request_method") == "manual":
        async with SessionLocal() as session:
            cfo_rows = await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
            cfos = cfo_rows.all()
        if not cfos:
            await callback.answer("ЦФО не найдены")
            return
        await callback.message.answer(
            f"🏢 Подразделение: {_display_value(dep.name)}\n🏷️ Шаг 2/6. Выберите ЦФО:",
            reply_markup=_manual_cfo_keyboard(cfos),
        )
        await callback.answer()
        return
    await _show_request_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_field:cfo")
async def request_cfo_menu(callback: CallbackQuery, state: FSMContext) -> None:
    async with SessionLocal() as session:
        rows = await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
        cfos = rows.all()
    await _edit_request_message(
        callback.bot,
        state,
        "🏷️ Выберите ЦФО",
        _cfo_keyboard(cfos),
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_cfo:"))
async def request_cfo_pick(callback: CallbackQuery, state: FSMContext) -> None:
    cfo_id = int(callback.data.split(":")[1])
    async with SessionLocal() as session:
        cfo = await session.get(Cfo, cfo_id)
    if not cfo:
        await callback.answer("ЦФО не найдено.")
        return
    await state.update_data(cfo_id=cfo.id, cfo_name=cfo.name)
    data = await state.get_data()
    if data.get("manual_wizard") and data.get("request_method") == "manual":
        await state.set_state(RequestCreate.text_input)
        await state.update_data(input_target="mol_full_name")
        await callback.message.answer(
            f"🏷️ ЦФО: {_display_value(cfo.name)}\n👤 Шаг 3/6. Введите ФИО МОЛ."
        )
        await callback.answer()
        return
    await _show_request_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_field:mol")
async def request_mol_input(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RequestCreate.text_input)
    await state.update_data(input_target="mol_full_name")
    await _show_input_prompt(
        callback.bot,
        state,
        "👤 Введите ФИО материально ответственного лица (МОЛ).",
        "req_input_clear:mol_full_name",
        "req_menu",
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_field:contract_max_price")
async def request_contract_max_price_input(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RequestCreate.text_input)
    await state.update_data(input_target="contract_max_price")
    await _show_input_prompt(
        callback.bot,
        state,
        (
            "💰 Введите начальную (максимальную) цену договора согласно Плану закупок "
            "с НДС (в тыс.руб.)."
        ),
        "req_input_clear:contract_max_price",
        "req_menu",
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_field:bdds_article_category")
async def request_bdds_article_category_input(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RequestCreate.text_input)
    await state.update_data(input_target="bdds_article_category")
    await _show_input_prompt(
        callback.bot,
        state,
        (
            "📑 Введите значение «В соответствии с управлением холдингом 1С» "
            "(БДДС: Статья ДДС - Товарная категория)."
        ),
        "req_input_clear:bdds_article_category",
        "req_menu",
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_field:approver")
async def request_approver_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_request_menu(
        callback.bot,
        state,
        error="Согласующий назначается автоматически (Default Approver).",
        fallback_message=callback.message,
    )
    await callback.answer("Назначается автоматически.")


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_approver:"))
async def request_approver_pick(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    method = data.get("request_method")

    if method == "excel":
        await _show_excel_approver_menu(callback.message, state, callback.from_user)
        await callback.answer()
        return

    await _show_request_menu(
        callback.bot,
        state,
        error="Согласующий назначается автоматически (Default Approver).",
        fallback_message=callback.message,
    )
    await callback.answer("Назначается автоматически.")


@router.callback_query(StateFilter(RequestCreate), F.data == "req_items:menu")
async def request_items_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    item_index = data.get("current_item_index")
    if item_index is not None and item_index < len(items):
        if _is_item_empty(items[item_index]):
            items.pop(item_index)
            await state.update_data(items=items, current_item_index=None)
    await state.set_state(RequestCreate.menu)
    await _show_items_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_item_add")
async def request_item_add(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    new_item = {"attachments": []}
    items.append(new_item)
    item_index = len(items) - 1
    await state.update_data(items=items, current_item_index=item_index)
    await state.set_state(RequestCreate.text_input)
    await state.update_data(input_target="item_name")
    await _show_input_prompt(
        callback.bot,
        state,
        "🧾 Введите наименование товара.",
        "req_input_clear:item_name",
        "req_items:menu",
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_item_edit:"))
async def request_item_edit(callback: CallbackQuery, state: FSMContext) -> None:
    item_index = int(callback.data.split(":")[1])
    await state.set_state(RequestCreate.menu)
    await state.update_data(current_item_index=item_index)
    await _show_item_editor(callback.bot, state, item_index, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_item_delete:"))
async def request_item_delete(callback: CallbackQuery, state: FSMContext) -> None:
    item_index = int(callback.data.split(":")[1])
    data = await state.get_data()
    items = data.get("items") or []
    if 0 <= item_index < len(items):
        items.pop(item_index)
    current_index = data.get("current_item_index")
    update_payload = {"items": items}
    if current_index == item_index:
        update_payload["current_item_index"] = None
    await state.update_data(**update_payload)
    await _show_items_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_item_field:"))
async def request_item_field(callback: CallbackQuery, state: FSMContext) -> None:
    _, item_index, field = callback.data.split(":")
    item_index = int(item_index)
    await state.set_state(RequestCreate.text_input)
    await state.update_data(current_item_index=item_index, input_target=f"item_{field}")
    prompts = {
        "name": "🧾 Введите наименование товара.",
        "specs": "⚙️ Введите технические характеристики.",
        "brand": "🏷️ Введите марку устройства или аналог.",
        "qty": "🔢 Введите количество.",
        "unit": "📏 Введите единицу измерения.",
        "link": "🔗 Введите ссылку на товар (можно несколько строк).",
        "note": "📝 Введите примечание (при необходимости).",
    }
    prompt = prompts.get(field, "✏️ Введите значение.")
    await _show_input_prompt(
        callback.bot,
        state,
        prompt,
        f"req_input_clear:item_{field}",
        "req_item_back",
        fallback_message=callback.message,
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("req_item_attachments:"))
async def request_item_attachments(callback: CallbackQuery, state: FSMContext) -> None:
    item_index = int(callback.data.split(":")[1])
    await state.set_state(RequestCreate.item_attachment)
    await state.update_data(current_item_index=item_index)
    await _show_attachment_prompt(
        callback.bot, state, item_index, fallback_message=callback.message
    )
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data == "req_item_back")
async def request_item_back(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    item_index = data.get("current_item_index")
    await state.set_state(RequestCreate.menu)
    if item_index is not None:
        await _show_item_editor(
            callback.bot, state, item_index, fallback_message=callback.message
        )
    else:
        await _show_items_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(RequestCreate.item_attachment, F.data == "req_item_attach_done")
async def request_item_attach_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    item_index = data.get("current_item_index")
    manual_wizard = data.get("manual_wizard") and data.get("request_method") == "manual"
    if manual_wizard:
        await state.set_state(RequestCreate.menu)
        await state.update_data(input_target=None)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        item_label = f"Товар {item_index + 1}" if item_index is not None else "Товар"
        await callback.message.answer(
            f"✅ {item_label} сохранен. Добавить еще товар?",
            reply_markup=_manual_item_more_keyboard(),
        )
        await callback.answer()
        return
    await state.set_state(RequestCreate.menu)
    if item_index is not None:
        await _show_item_editor(
            callback.bot, state, item_index, fallback_message=callback.message
        )
    else:
        await _show_items_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.callback_query(RequestCreate.item_attachment, F.data == "req_item_attach_clear")
async def request_item_attach_clear(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    item_index = data.get("current_item_index")
    if item_index is None or item_index >= len(items):
        await _show_items_menu(callback.bot, state, fallback_message=callback.message)
        await callback.answer()
        return
    item = items[item_index]
    item["attachments"] = []
    items[item_index] = item
    await state.update_data(items=items)
    await _show_attachment_prompt(
        callback.bot, state, item_index, fallback_message=callback.message
    )
    await callback.answer()


@router.callback_query(
    RequestCreate.item_attachment,
    F.data.in_({"req_item_attach_back", "req_item_attach_skip"}),
)
async def request_item_attach_back(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    item_index = data.get("current_item_index")
    manual_wizard = data.get("manual_wizard") and data.get("request_method") == "manual"
    if manual_wizard:
        await state.set_state(RequestCreate.menu)
        await state.update_data(input_target=None)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        item_label = f"Товар {item_index + 1}" if item_index is not None else "Товар"
        await callback.message.answer(
            f"➡️ {item_label}: вложения пропущены. Добавить еще товар?",
            reply_markup=_manual_item_more_keyboard(),
        )
        await callback.answer()
        return
    await state.set_state(RequestCreate.menu)
    if item_index is not None:
        await _show_item_editor(
            callback.bot, state, item_index, fallback_message=callback.message
        )
    else:
        await _show_items_menu(callback.bot, state, fallback_message=callback.message)
    await callback.answer()


@router.message(RequestCreate.item_attachment)
async def request_item_attachment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    item_index = data.get("current_item_index")
    if item_index is None or item_index >= len(items):
        await message.answer("Сначала выберите товар.")
        await _try_delete_message(message)
        return
    item = items[item_index]
    attachments = item.get("attachments") or []
    if message.photo:
        if message.media_group_id:
            photo = message.photo[-1]
            await _queue_album_attachment(
                message,
                state,
                {
                    "file_id": photo.file_id,
                    "file_unique_id": photo.file_unique_id,
                    "file_type": "photo",
                    "file_name": None,
                    },
                )
            await _try_delete_message(message)
            return
        photo_count = sum(1 for att in attachments if att.get("file_type") == "photo")
        if photo_count >= 3:
            await message.answer("Можно добавить не более 3 фото для товара.")
            await _try_delete_message(message)
            return
        photo = message.photo[-1]
        attachments.append(
            {
                "file_id": photo.file_id,
                "file_unique_id": photo.file_unique_id,
                "file_type": "photo",
                "file_name": None,
            }
        )
        item["attachments"] = attachments
        items[item_index] = item
        await state.update_data(items=items)
        await message.answer(
            "Фото добавлено. Можно отправить еще или нажмите «Готово» / «Пропустить».",
            reply_markup=_attachments_keyboard(),
        )
        await _try_delete_message(message)
        return
    if message.document:
        if _is_image_document(message):
            if message.media_group_id:
                doc = message.document
                await _queue_album_attachment(
                    message,
                    state,
                    {
                        "file_id": doc.file_id,
                        "file_unique_id": doc.file_unique_id,
                        "file_type": "photo",
                        "file_name": doc.file_name,
                    },
                )
                await _try_delete_message(message)
                return
            photo_count = sum(1 for att in attachments if att.get("file_type") == "photo")
            if photo_count >= 3:
                await message.answer("Можно добавить не более 3 фото для товара.")
                await _try_delete_message(message)
                return
            doc = message.document
            attachments.append(
                {
                    "file_id": doc.file_id,
                    "file_unique_id": doc.file_unique_id,
                    "file_type": "photo",
                    "file_name": doc.file_name,
                }
            )
            item["attachments"] = attachments
            items[item_index] = item
            await state.update_data(items=items)
            await message.answer(
                "Фото добавлено. Можно отправить еще или нажмите «Готово» / «Пропустить».",
                reply_markup=_attachments_keyboard(),
            )
            await _try_delete_message(message)
            return
        doc = message.document
        attachments.append(
            {
                "file_id": doc.file_id,
                "file_unique_id": doc.file_unique_id,
                "file_type": "document",
                "file_name": doc.file_name,
            }
        )
        item["attachments"] = attachments
        items[item_index] = item
        await state.update_data(items=items)
        await message.answer(
            "Файл добавлен. Можно отправить еще или нажмите «Готово» / «Пропустить».",
            reply_markup=_attachments_keyboard(),
        )
        await _try_delete_message(message)
        return
    if message.text:
        current = _normalize_text(item.get("link"))
        if current:
            current = f"{current}\n{message.text.strip()}"
        else:
            current = message.text.strip()
        item["link"] = current
        items[item_index] = item
        await state.update_data(items=items)
        await message.answer(
            "Ссылка сохранена. Можно отправить еще или нажмите «Готово» / «Пропустить».",
            reply_markup=_attachments_keyboard(),
        )
        await _try_delete_message(message)
        return
    await message.answer(
        "Отправьте фото, файл или ссылку, либо нажмите «Готово» / «Пропустить».",
        reply_markup=_attachments_keyboard(),
    )
    await _try_delete_message(message)


@router.callback_query(RequestCreate.text_input, F.data.startswith("req_input_clear:"))
async def request_input_clear(callback: CallbackQuery, state: FSMContext) -> None:
    target = callback.data.split(":")[1]
    data = await state.get_data()
    if target == "mol_full_name":
        await state.update_data(mol_full_name=None)
        await state.set_state(RequestCreate.menu)
        await _show_request_menu(callback.bot, state, fallback_message=callback.message)
        await callback.answer()
        return
    if target == "contract_max_price":
        await state.update_data(contract_max_price=None)
        await state.set_state(RequestCreate.menu)
        await _show_request_menu(callback.bot, state, fallback_message=callback.message)
        await callback.answer()
        return
    if target == "bdds_article_category":
        await state.update_data(bdds_article_category=None)
        await state.set_state(RequestCreate.menu)
        await _show_request_menu(callback.bot, state, fallback_message=callback.message)
        await callback.answer()
        return
    if target.startswith("item_"):
        items = data.get("items") or []
        item_index = data.get("current_item_index")
        if item_index is not None and item_index < len(items):
            field = target.replace("item_", "")
            item = items[item_index]
            item[field] = None
            items[item_index] = item
            await state.update_data(items=items)
        await state.set_state(RequestCreate.menu)
        if item_index is None:
            await _show_items_menu(callback.bot, state, fallback_message=callback.message)
        else:
            await _show_item_editor(
                callback.bot, state, item_index, fallback_message=callback.message
            )
        await callback.answer()
        return
    await callback.answer()


@router.callback_query(StateFilter(RequestCreate), F.data.startswith("item_more:"))
async def request_item_more(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    data = await state.get_data()
    if not (data.get("manual_wizard") and data.get("request_method") == "manual"):
        await callback.answer()
        return
    if action == "yes":
        items = data.get("items") or []
        items.append({"attachments": []})
        item_index = len(items) - 1
        await state.update_data(
            items=items,
            current_item_index=item_index,
            input_target="item_name",
        )
        await state.set_state(RequestCreate.text_input)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        await callback.message.answer(f"🧾 Товар {item_index + 1}: введите наименование.")
        await callback.answer()
        return
    if action == "no":
        errors = _validate_manual_request(data)
        if errors:
            await callback.message.answer("⚠️ Нужно заполнить: " + ", ".join(errors))
            await callback.answer()
            return
        request = await _create_request(callback.bot, data, callback.from_user, callback.message)
        if request:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
            await callback.message.answer("✅ Заявка создана.")
            await state.clear()
        await callback.answer()
        return
    await callback.answer()


@router.message(RequestCreate.text_input)
async def request_text_input(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("✍️ Нужно отправить текст.")
        await _try_delete_message(message)
        return
    data = await state.get_data()
    target = data.get("input_target")
    is_manual_wizard = data.get("manual_wizard") and data.get("request_method") == "manual"
    skip_values = {"-", "—", "нет", "пропустить", "skip"}
    value = message.text.strip()

    if is_manual_wizard:
        if target == "mol_full_name":
            if not value:
                await message.answer("👤 Введите ФИО МОЛ.")
                await _try_delete_message(message)
                return
            await state.update_data(mol_full_name=value, input_target="contract_max_price")
            await message.answer(
                "💰 Шаг 4/6. Введите начальную (максимальную) цену договора в тыс. руб.\n"
                "Если поле не требуется, отправьте «-»."
            )
            await _try_delete_message(message)
            return
        if target == "contract_max_price":
            parsed_value = None if value.casefold() in skip_values else value
            await state.update_data(
                contract_max_price=parsed_value,
                input_target="bdds_article_category",
            )
            await message.answer(
                "📑 Шаг 5/6. Введите БДДС (Статья ДДС - Товарная категория).\n"
                "Если поле не требуется, отправьте «-»."
            )
            await _try_delete_message(message)
            return
        if target == "bdds_article_category":
            parsed_value = None if value.casefold() in skip_values else value
            items = data.get("items") or []
            if not items:
                items = [{"attachments": []}]
            await state.update_data(
                bdds_article_category=parsed_value,
                items=items,
                current_item_index=0,
                input_target="item_name",
            )
            await message.answer("🧾 Шаг 6/6. Товар 1: введите наименование.")
            await _try_delete_message(message)
            return
        if target and target.startswith("item_"):
            items = data.get("items") or []
            item_index = data.get("current_item_index")
            if item_index is None or item_index >= len(items):
                await message.answer("Не удалось определить товар. Начните создание заявки заново.")
                await _try_delete_message(message)
                return
            field = target.replace("item_", "")
            if field in {"name", "qty", "unit"} and not value:
                await message.answer("⚠️ Поле не может быть пустым. Введите значение.")
                await _try_delete_message(message)
                return
            parsed_value = value
            if field in {"specs", "brand", "link", "note"} and value.casefold() in skip_values:
                parsed_value = None
            item = items[item_index]
            item[field] = parsed_value
            items[item_index] = item
            if field == "name":
                await state.update_data(items=items, input_target="item_specs")
                await message.answer(
                    f"⚙️ Товар {item_index + 1}: введите технические характеристики.\n"
                    "Если поле не требуется, отправьте «-»."
                )
                await _try_delete_message(message)
                return
            if field == "specs":
                await state.update_data(items=items, input_target="item_brand")
                await message.answer(
                    f"🏷️ Товар {item_index + 1}: введите марку/аналог.\n"
                    "Если поле не требуется, отправьте «-»."
                )
                await _try_delete_message(message)
                return
            if field == "brand":
                await state.update_data(items=items, input_target="item_qty")
                await message.answer(f"🔢 Товар {item_index + 1}: введите количество.")
                await _try_delete_message(message)
                return
            if field == "qty":
                await state.update_data(items=items, input_target="item_unit")
                await message.answer(f"📏 Товар {item_index + 1}: введите единицу измерения.")
                await _try_delete_message(message)
                return
            if field == "unit":
                await state.update_data(items=items, input_target="item_link")
                await message.answer(
                    f"🔗 Товар {item_index + 1}: введите ссылку на товар.\n"
                    "Если поле не требуется, отправьте «-»."
                )
                await _try_delete_message(message)
                return
            if field == "link":
                await state.update_data(items=items, input_target="item_note")
                await message.answer(
                    f"📝 Товар {item_index + 1}: введите примечание.\n"
                    "Если поле не требуется, отправьте «-»."
                )
                await _try_delete_message(message)
                return
            if field == "note":
                await state.update_data(items=items, input_target=None)
                await state.set_state(RequestCreate.item_attachment)
                await _show_attachment_prompt(
                    message.bot,
                    state,
                    item_index,
                    fallback_message=message,
                )
                await message.answer(
                    (
                        f"📎 Товар {item_index + 1} заполнен. "
                        "Прикрепите фото/файл или нажмите «Готово» / «Пропустить»."
                    ),
                    reply_markup=_attachments_keyboard(),
                )
                await _try_delete_message(message)
                return
            await message.answer("Не удалось распознать поле товара. Начните создание заявки заново.")
            await _try_delete_message(message)
            return
        await message.answer("Не удалось распознать шаг заполнения. Начните создание заявки заново.")
        await _try_delete_message(message)
        return

    if target == "mol_full_name":
        await state.update_data(mol_full_name=value)
        await state.set_state(RequestCreate.menu)
        await _show_request_menu(message.bot, state, fallback_message=message)
        await _try_delete_message(message)
        return
    if target == "contract_max_price":
        parsed_value = None if value.casefold() in skip_values else value
        await state.update_data(contract_max_price=parsed_value)
        await state.set_state(RequestCreate.menu)
        await _show_request_menu(message.bot, state, fallback_message=message)
        await _try_delete_message(message)
        return
    if target == "bdds_article_category":
        parsed_value = None if value.casefold() in skip_values else value
        await state.update_data(bdds_article_category=parsed_value)
        await state.set_state(RequestCreate.menu)
        await _show_request_menu(message.bot, state, fallback_message=message)
        await _try_delete_message(message)
        return
    if target and target.startswith("item_"):
        items = data.get("items") or []
        item_index = data.get("current_item_index")
        if item_index is None or item_index >= len(items):
            await message.answer("Сначала выберите товар.")
            await _try_delete_message(message)
            return
        field = target.replace("item_", "")
        item = items[item_index]
        if field == "link":
            current = _normalize_text(item.get("link"))
            if current:
                value = f"{current}\n{value}"
        item[field] = value
        items[item_index] = item
        await state.update_data(items=items)
        await state.set_state(RequestCreate.menu)
        await _show_item_editor(message.bot, state, item_index, fallback_message=message)
        await _try_delete_message(message)
        return
    await message.answer("Не удалось распознать поле для ввода.")
    await _try_delete_message(message)


@router.callback_query(StateFilter(RequestCreate), F.data == "req_submit")
async def request_submit(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    errors = _validate_manual_request(data)
    if errors:
        await _show_request_menu(
            callback.bot,
            state,
            error="Нужно заполнить: " + ", ".join(errors),
            fallback_message=callback.message,
        )
        await callback.answer()
        return
    request = await _create_request(
        callback.bot, data, callback.from_user, callback.message
    )
    if request:
        await _edit_request_message(
            callback.bot,
            state,
            "Заявка создана.",
            reply_markup=None,
            fallback_message=callback.message,
        )
        await state.clear()
    await callback.answer()
