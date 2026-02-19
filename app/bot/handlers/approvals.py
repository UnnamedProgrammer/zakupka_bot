import math
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.bot.keyboards import approval_action_keyboard, executor_assign_keyboard
from app.bot.states import ApprovalComment, ExtraApprovalComment, LeaderComment
from app.db.models import (
    Approval,
    ApprovalStatus,
    Attachment,
    Comment,
    Request,
    RequestItem,
    RequestStatus,
    Role,
    User,
    user_roles,
)
from app.db.session import SessionLocal
from app.config import settings
from app.services.constants import (
    APPROVAL_KIND_EXECUTOR_EXTRA,
    APPROVAL_KIND_LEADER_EXTRA,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_REJECTED,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_REJECTED,
)
from app.services.attachments import build_attachment_groups_from, fetch_request_media
from app.services.excel import upsert_request_excel
from app.services.formatters import format_request_summary, get_request_status_label
from app.services.notifications import send_to_user
from app.services.users import (
    ensure_username_format,
    get_or_create_user,
    get_user_role_codes,
)
from app.services.datetime import to_naive_utc
from app.bot.handlers.common import cleanup_main_menu

router = Router()
LEADER_LIST_PAGE_SIZE = 6
INITIATOR_LIST_PAGE_SIZE = 6


async def _store_my_requests_message(state: FSMContext | None, message: Message) -> None:
    if not state or not message.chat:
        return
    await state.update_data(
        my_requests_message_id=message.message_id,
        my_requests_chat_id=message.chat.id,
    )


async def _get_status_id(session, model, code: str) -> int:
    return await session.scalar(select(model.id).where(model.code == code))


def _approval_status_icon(code: str | None) -> str:
    return {
        APPROVAL_STATUS_PENDING: "🕒",
        APPROVAL_STATUS_APPROVED: "✅",
        APPROVAL_STATUS_REJECTED: "❌",
    }.get(code or "", "📌")


def _approval_request_label(request: Request, status_code: str | None) -> str:
    name = request.item_name or request.supplier_name or "без названия"
    icon = _approval_status_icon(status_code)
    status_name = get_request_status_label(request)
    label = f"{icon} №{request.id} · {name}"
    if status_name:
        label = f"{label} · {status_name}"
    return _truncate_text(label, 64)


def _leader_list_keyboard(
    rows: list[tuple[Approval, ApprovalStatus, Request]],
    page: int,
    total_pages: int,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for approval, status, request in rows:
        builder.button(
            text=_approval_request_label(request, status.code if status else None),
            callback_data=f"leader_pick:{approval.id}:{page}",
        )
    if rows:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"leader_list:{page - 1}",
                )
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"leader_list:{page + 1}",
                )
            )
    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ В главное меню",
            callback_data="main_menu",
        )
    )
    return builder


def _leader_actions_keyboard(approval_id: int, page: int, pending: bool, extra: bool = False):
    builder = InlineKeyboardBuilder()
    if pending:
        builder.button(text="✅ Принять", callback_data=f"approval_accept:{approval_id}")
        builder.button(text="❌ Отмена", callback_data=f"approval_reject:{approval_id}")
        if not extra:
            builder.button(text="💬 Комментарии", callback_data=f"leader_comment:{approval_id}")
            builder.adjust(2)
        else:
            builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=f"leader_list:{page}",
        )
    )
    return builder.as_markup()


def _leader_extra_gate_keyboard(approval_id: int, page: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Требуется дополнительное согласование",
        callback_data=f"leader_extra_need:{approval_id}:{page}",
    )
    builder.button(
        text="Дополнительное согласование не требуется",
        callback_data=f"leader_extra_skip:{approval_id}:{page}",
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=f"leader_list:{page}",
        )
    )
    return builder.as_markup()


def _leader_extra_approvers_keyboard(
    approval_id: int, page: int, approvers: list[tuple[int, str]]
):
    builder = InlineKeyboardBuilder()
    for user_id, name in approvers:
        builder.button(
            text=_truncate_text(name or f"ID {user_id}", 60),
            callback_data=f"leader_extra_pick:{approval_id}:{user_id}:{page}",
        )
    if approvers:
        builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"leader_pick:{approval_id}:{page}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ В главное меню",
            callback_data="main_menu",
        )
    )
    return builder.as_markup()


def _leader_extra_decision_keyboard(approval_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"approval_accept:{approval_id}")
    builder.button(text="❌ Отмена", callback_data=f"approval_reject:{approval_id}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


def _leader_executor_assign_keyboard(
    request_id: int,
    page: int,
    executors: list[tuple[int, str]],
):
    builder = InlineKeyboardBuilder()
    for user_id, name in executors:
        builder.button(text=f"🧑‍🔧 {name}", callback_data=f"assign:{request_id}:{user_id}")
    if executors:
        builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=f"leader_list:{page}",
        )
    )
    return builder.as_markup()


def _normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return " ".join(text.split())


def _truncate_text(text: str, max_len: int = 64) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _executor_assignment_extra_prompt_keyboard(request_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data=f"exec_extra:{request_id}:yes")
    builder.button(text="❌ Нет", callback_data=f"exec_extra:{request_id}:no")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


def _executor_assignment_extra_chiefs_keyboard(
    request_id: int, chiefs: list[tuple[int, str]]
):
    builder = InlineKeyboardBuilder()
    for user_id, name in chiefs:
        builder.button(
            text=_truncate_text(name or f"ID {user_id}", 60),
            callback_data=f"exec_extra_chief:{request_id}:{user_id}",
        )
    if chiefs:
        builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"exec_extra:{request_id}:back",
        )
    )
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


def _executor_assignment_extra_approval_keyboard(approval_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"approval_accept:{approval_id}")
    builder.button(text="❌ Отклонить", callback_data=f"approval_reject:{approval_id}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu"))
    return builder.as_markup()


async def _fetch_executors(session) -> list[tuple[int, str]]:
    rows = await session.execute(
        select(User.id, User.full_name)
        .join(user_roles, user_roles.c.user_id == User.id)
        .join(Role, Role.id == user_roles.c.role_id)
        .where(Role.code == "executor")
        .order_by(User.full_name, User.id)
    )
    return [(row[0], row[1] or f"ID {row[0]}") for row in rows.all()]


async def _fetch_default_approvers(session) -> list[tuple[int, str]]:
    rows = await session.execute(
        select(User.id, User.full_name)
        .where(User.is_default_approver.is_(True))
        .where(User.tg_id.is_not(None))
        .order_by(User.full_name, User.id)
    )
    return [(row[0], row[1] or f"ID {row[0]}") for row in rows.all()]


async def _fetch_secondary_approvers(session) -> list[tuple[int, str]]:
    query = (
        select(User.id, User.full_name, User.tg_username)
        .join(user_roles, user_roles.c.user_id == User.id)
        .join(Role, Role.id == user_roles.c.role_id)
        .where(Role.code == "approver")
        .order_by(User.full_name, User.id)
    )
    rows = await session.execute(query)
    return [(row[0], row[1] or row[2] or f"ID {row[0]}") for row in rows.all()]


async def _resolve_approver_user(session, user: User) -> User | None:
    if user.tg_id:
        return user
    username = _normalize_text(user.tg_username)
    if not username:
        return None
    normalized = username.lstrip("@").casefold()
    matched = await session.scalar(
        select(User)
        .where(User.id != user.id)
        .where(User.tg_id.is_not(None))
        .where(User.tg_username.is_not(None))
        .where(func.replace(func.lower(User.tg_username), "@", "") == normalized)
        .order_by(User.id)
    )
    if not matched or not matched.tg_id:
        return None
    approver_role_id = await session.scalar(select(Role.id).where(Role.code == "approver"))
    if not approver_role_id:
        return None
    linked = await session.scalar(
        select(user_roles.c.user_id).where(
            user_roles.c.user_id == matched.id,
            user_roles.c.role_id == approver_role_id,
        )
    )
    if not linked:
        await session.execute(
            user_roles.insert().values(user_id=matched.id, role_id=approver_role_id)
        )
    return matched


async def _resolve_notification_user(session, user: User | None) -> User | None:
    if not user:
        return None
    if user.tg_id:
        return user
    username = _normalize_text(user.tg_username)
    if not username:
        return None
    normalized = username.lstrip("@").casefold()
    return await session.scalar(
        select(User)
        .where(User.id != user.id)
        .where(User.tg_id.is_not(None))
        .where(User.tg_username.is_not(None))
        .where(func.replace(func.lower(User.tg_username), "@", "") == normalized)
        .order_by(User.id)
    )


async def _has_pending_executor_extra_approval(session, request_id: int) -> bool:
    pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
    if not pending_id:
        return False
    existing_id = await session.scalar(
        select(Approval.id)
        .where(
            Approval.request_id == request_id,
            Approval.status_id == pending_id,
            Approval.kind == APPROVAL_KIND_EXECUTOR_EXTRA,
        )
        .limit(1)
    )
    return existing_id is not None


async def _has_pending_leader_extra_approval(session, request_id: int) -> bool:
    pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
    if not pending_id:
        return False
    existing_id = await session.scalar(
        select(Approval.id)
        .where(
            Approval.request_id == request_id,
            Approval.status_id == pending_id,
            Approval.kind == APPROVAL_KIND_LEADER_EXTRA,
        )
        .limit(1)
    )
    return existing_id is not None


def _initiator_request_label(request: Request) -> str:
    name = _normalize_text(request.item_name) or _normalize_text(request.supplier_name)
    if not name:
        name = "без названия"
    status = get_request_status_label(request)
    label = f"№{request.id} · {name}"
    if status:
        label = f"{label} · {status}"
    return _truncate_text(label, 64)


def _initiator_list_keyboard(
    requests_page: list[Request],
    page: int,
    total_pages: int,
) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for req in requests_page:
        builder.button(
            text=_initiator_request_label(req),
            callback_data=f"initiator_pick:{req.id}:{page}",
        )
    if requests_page:
        builder.adjust(1)

    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"initiator_list:{page - 1}",
                )
            )
        if page < total_pages:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=f"initiator_list:{page + 1}",
                )
            )
    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ В главное меню",
            callback_data="main_menu",
        )
    )
    return builder


def _initiator_actions_keyboard(page: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=f"initiator_list:{page}",
        )
    )
    return builder.as_markup()


async def _get_next_pending_approval(session, request_id: int):
    pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
    rows = await session.execute(
        select(Approval, User)
        .join(User, User.id == Approval.approver_id)
        .where(Approval.request_id == request_id, Approval.status_id == pending_id)
        .order_by(Approval.id)
    )
    return rows.first()


async def _load_request_full(session, request_id: int) -> Request | None:
    result = await session.execute(
        select(Request)
        .where(Request.id == request_id)
        .options(
            selectinload(Request.initiator),
            selectinload(Request.department),
            selectinload(Request.cfo),
            selectinload(Request.status),
            selectinload(Request.attachments),
            selectinload(Request.items),
            selectinload(Request.comments),
        )
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


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


async def _format_approval_summary(session, request_id: int) -> str:
    rows = await session.execute(
        select(Approval, User, ApprovalStatus)
        .join(User, User.id == Approval.approver_id)
        .join(ApprovalStatus, ApprovalStatus.id == Approval.status_id)
        .where(Approval.request_id == request_id)
    )
    lines = ["Статусы согласования:"]
    for approval, user, status in rows.all():
        comment = f" (комментарий: {approval.comment})" if approval.comment else ""
        lines.append(f"- {user.full_name or user.tg_username}: {status.name}{comment}")
    return "\n".join(lines)


async def _fetch_leader_approvals(
    session, approver_id: int
) -> list[tuple[Approval, ApprovalStatus, Request]]:
    rows = await session.execute(
        select(Approval, ApprovalStatus, Request)
        .join(ApprovalStatus, ApprovalStatus.id == Approval.status_id)
        .join(Request, Request.id == Approval.request_id)
        .options(selectinload(Request.status))
        .where(Approval.approver_id == approver_id)
        .order_by(Request.created_at.desc())
    )
    all_rows = rows.all()

    # One request can have multiple approvals for the same user (e.g. extra approval).
    # Show only one "current" row per request to avoid duplicate cards in "My requests".
    def _row_rank(row: tuple[Approval, ApprovalStatus, Request]) -> tuple[int, int]:
        approval, status, _request = row
        is_pending = status.code == APPROVAL_STATUS_PENDING
        return (0 if is_pending else 1, -approval.id)

    unique_by_request: dict[int, tuple[Approval, ApprovalStatus, Request]] = {}
    for row in all_rows:
        request_id = row[2].id
        current = unique_by_request.get(request_id)
        if current is None or _row_rank(row) < _row_rank(current):
            unique_by_request[request_id] = row

    results = list(unique_by_request.values())
    results.sort(key=lambda row: row[2].created_at or datetime.min, reverse=True)
    results.sort(key=lambda row: row[1].code != APPROVAL_STATUS_PENDING)
    return results


async def _show_leader_list(
    message: Message,
    approver_id: int,
    page: int,
    edit: bool = False,
    state: FSMContext | None = None,
) -> None:
    async with SessionLocal() as session:
        approvals = await _fetch_leader_approvals(session, approver_id)
    total = len(approvals)
    if total == 0:
        text = "У вас нет заявок на согласование."
        markup = _leader_list_keyboard([], 1, 1).as_markup()
        if edit:
            await message.edit_text(text, reply_markup=markup)
            await _store_my_requests_message(state, message)
        else:
            sent = await message.answer(text, reply_markup=markup)
            await _store_my_requests_message(state, sent)
        return
    total_pages = max(1, math.ceil(total / LEADER_LIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * LEADER_LIST_PAGE_SIZE
    rows = approvals[start : start + LEADER_LIST_PAGE_SIZE]
    text = f"Ваши заявки на согласование. Страница {page}/{total_pages}."
    markup = _leader_list_keyboard(rows, page, total_pages).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=markup)
        await _store_my_requests_message(state, message)
    else:
        sent = await message.answer(text, reply_markup=markup)
        await _store_my_requests_message(state, sent)


async def _fetch_initiator_requests(session, initiator_id: int) -> list[Request]:
    rows = await session.execute(
        select(Request)
        .where(Request.initiator_id == initiator_id)
        .options(selectinload(Request.status))
        .order_by(Request.created_at.desc())
    )
    return rows.scalars().all()


async def _show_initiator_list(
    message: Message,
    initiator_id: int,
    page: int,
    edit: bool = False,
    state: FSMContext | None = None,
) -> None:
    async with SessionLocal() as session:
        requests = await _fetch_initiator_requests(session, initiator_id)
    total = len(requests)
    if total == 0:
        text = "У вас нет созданных заявок."
        markup = _initiator_list_keyboard([], 1, 1).as_markup()
        if edit:
            await message.edit_text(text, reply_markup=markup)
            await _store_my_requests_message(state, message)
        else:
            sent = await message.answer(text, reply_markup=markup)
            await _store_my_requests_message(state, sent)
        return
    total_pages = max(1, math.ceil(total / INITIATOR_LIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * INITIATOR_LIST_PAGE_SIZE
    requests_page = requests[start : start + INITIATOR_LIST_PAGE_SIZE]
    text = f"Ваши заявки. Страница {page}/{total_pages}."
    markup = _initiator_list_keyboard(requests_page, page, total_pages).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=markup)
        await _store_my_requests_message(state, message)
    else:
        sent = await message.answer(text, reply_markup=markup)
        await _store_my_requests_message(state, sent)


@router.message(F.text == "📌 Мои заявки")
async def my_requests(message: Message, state: FSMContext) -> None:
    await cleanup_main_menu(message, state)
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
    await state.clear()
    if "approver" in role_codes or user.is_default_approver:
        await _show_leader_list(message, user.id, page=1, edit=False, state=state)
        return
    if "executor" in role_codes:
        from app.bot.handlers.executor import _show_my_list  # local import to avoid cycles

        await _show_my_list(message, user.id, page=1, edit=False, state=state)
        return
    await _show_initiator_list(message, user.id, page=1, edit=False, state=state)


@router.callback_query(F.data.startswith("leader_list:"))
async def leader_list(callback: CallbackQuery, state: FSMContext) -> None:
    _, page_str = callback.data.split(":")
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
        if "approver" not in role_codes and not user.is_default_approver:
            await callback.answer("Нет доступа")
            return
    await state.clear()
    await _show_leader_list(callback.message, user.id, page=page, edit=True, state=state)
    await callback.answer()


@router.callback_query(F.data.startswith("leader_pick:"))
async def leader_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, approval_id_str, page_str = callback.data.split(":")
    approval_id = int(approval_id_str)
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
        if "approver" not in role_codes and not user.is_default_approver:
            await callback.answer("Нет доступа")
            return
        row = await session.execute(
            select(Approval, ApprovalStatus)
            .join(ApprovalStatus, ApprovalStatus.id == Approval.status_id)
            .where(Approval.id == approval_id)
        )
        result = row.first()
        if not result:
            await callback.answer("Заявка не найдена")
            return
        approval, approval_status = result
        if approval.approver_id != user.id:
            await callback.answer("Нет доступа")
            return
        request = await _load_request_full(session, approval.request_id)
        if not request:
            await callback.answer("Заявка не найдена")
            return
        pending = approval_status.code == APPROVAL_STATUS_PENDING
        extra = approval.kind in {
            APPROVAL_KIND_EXECUTOR_EXTRA,
            APPROVAL_KIND_LEADER_EXTRA,
        }
        approval_id = approval.id
        is_chief = user.is_default_approver
        status_code = request.status.code if request.status else None
        status_name = _normalize_text(request.status.name if request.status else "").casefold()
        awaiting_executor_choice = (
            is_chief
            and request.executor_id is None
            and (status_code == REQUEST_STATUS_APPROVED or status_name == "выбор исполнителя")
        )
        executors: list[tuple[int, str]] = []
        if awaiting_executor_choice and not await _has_pending_executor_extra_approval(
            session, request.id
        ):
            executors = await _fetch_executors(session)
    if pending and is_chief and not extra:
        markup = _leader_extra_gate_keyboard(approval_id, page)
    elif executors:
        markup = _leader_executor_assign_keyboard(request.id, page, executors)
    else:
        markup = _leader_actions_keyboard(approval_id, page, pending, extra=extra)
    await callback.message.edit_text(
        format_request_summary(request),
        reply_markup=markup,
    )
    await _store_my_requests_message(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("leader_extra_skip:"))
async def leader_extra_skip(callback: CallbackQuery, state: FSMContext) -> None:
    _, approval_id_str, page_str = callback.data.split(":")
    if not approval_id_str.isdigit():
        await callback.answer("Некорректный ID")
        return
    approval_id = int(approval_id_str)
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
        if "approver" not in role_codes and not user.is_default_approver:
            await callback.answer("Нет доступа")
            return
        row = await session.execute(
            select(Approval, ApprovalStatus)
            .join(ApprovalStatus, ApprovalStatus.id == Approval.status_id)
            .where(Approval.id == approval_id)
        )
        result = row.first()
        if not result:
            await callback.answer("Заявка не найдена")
            return
        approval, status = result
        if approval.approver_id != user.id or not user.is_default_approver:
            await callback.answer("Нет доступа")
            return
        if approval.kind in {APPROVAL_KIND_EXECUTOR_EXTRA, APPROVAL_KIND_LEADER_EXTRA}:
            await callback.answer("Недоступно для этого согласования")
            return
        if await _has_pending_leader_extra_approval(session, approval.request_id):
            await callback.answer("Ожидается дополнительное согласование")
            return
        request = await _load_request_full(session, approval.request_id)
        if not request:
            await callback.answer("Заявка не найдена")
            return
        pending = status.code == APPROVAL_STATUS_PENDING
    await callback.message.edit_text(
        format_request_summary(request),
        reply_markup=_leader_actions_keyboard(approval.id, page, pending, extra=False),
    )
    await _store_my_requests_message(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("leader_extra_need:"))
async def leader_extra_need(callback: CallbackQuery, state: FSMContext) -> None:
    _, approval_id_str, page_str = callback.data.split(":")
    if not approval_id_str.isdigit():
        await callback.answer("Некорректный ID")
        return
    approval_id = int(approval_id_str)
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
        if "approver" not in role_codes and not user.is_default_approver:
            await callback.answer("Нет доступа")
            return
        row = await session.execute(
            select(Approval, ApprovalStatus)
            .join(ApprovalStatus, ApprovalStatus.id == Approval.status_id)
            .where(Approval.id == approval_id)
        )
        result = row.first()
        if not result:
            await callback.answer("Заявка не найдена")
            return
        approval, status = result
        if approval.approver_id != user.id or not user.is_default_approver:
            await callback.answer("Нет доступа")
            return
        if status.code != APPROVAL_STATUS_PENDING:
            await callback.answer("Согласование уже обработано")
            return
        if approval.kind in {APPROVAL_KIND_EXECUTOR_EXTRA, APPROVAL_KIND_LEADER_EXTRA}:
            await callback.answer("Недоступно для этого согласования")
            return
        if await _has_pending_leader_extra_approval(session, approval.request_id):
            await callback.answer("Ожидается дополнительное согласование")
            return
        approvers = await _fetch_secondary_approvers(session)
        if not approvers:
            await callback.answer("Согласующие не найдены")
            return
    await callback.message.edit_text(
        "Выберите согласующего для дополнительного согласования:",
        reply_markup=_leader_extra_approvers_keyboard(approval_id, page, approvers),
    )
    await _store_my_requests_message(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("leader_extra_pick:"))
async def leader_extra_pick(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    _, approval_id_str, user_id_str, page_str = parts
    if not approval_id_str.isdigit() or not user_id_str.isdigit():
        await callback.answer("Некорректные данные")
        return
    approval_id = int(approval_id_str)
    selected_user_id = int(user_id_str)
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        current_user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, current_user.id)
        if "approver" not in role_codes and not current_user.is_default_approver:
            await callback.answer("Нет доступа")
            return
        if not current_user.is_default_approver:
            await callback.answer("Нет доступа")
            return

        row = await session.execute(
            select(Approval, ApprovalStatus)
            .join(ApprovalStatus, ApprovalStatus.id == Approval.status_id)
            .where(Approval.id == approval_id)
        )
        result = row.first()
        if not result:
            await callback.answer("Заявка не найдена")
            return
        approval, status = result
        if approval.approver_id != current_user.id:
            await callback.answer("Нет доступа")
            return
        if status.code != APPROVAL_STATUS_PENDING:
            await callback.answer("Согласование уже обработано")
            return
        if approval.kind in {APPROVAL_KIND_EXECUTOR_EXTRA, APPROVAL_KIND_LEADER_EXTRA}:
            await callback.answer("Недоступно для этого согласования")
            return
        if await _has_pending_leader_extra_approval(session, approval.request_id):
            await callback.answer("Ожидается дополнительное согласование")
            return

        selected_user = await session.scalar(
            select(User)
            .join(user_roles, user_roles.c.user_id == User.id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(User.id == selected_user_id)
            .where(Role.code == "approver")
        )
        if not selected_user:
            await callback.answer("Согласующий не найден")
            return
        selected_user = await _resolve_approver_user(session, selected_user)
        if not selected_user:
            await callback.answer(
                "Не удалось определить Telegram-аккаунт согласующего. Попросите пользователя выполнить /start."
            )
            return
        pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
        extra_approval = Approval(
            request_id=approval.request_id,
            approver_id=selected_user.id,
            status_id=pending_id,
            kind=APPROVAL_KIND_LEADER_EXTRA,
            requested_by_id=current_user.id,
        )
        session.add(extra_approval)
        await session.flush()

        request = await _load_request_full(session, approval.request_id)
        if not request:
            await callback.answer("Заявка не найдена")
            return
        items, attachments = await fetch_request_media(session, request.id)
        await session.commit()

    if selected_user.tg_id:
        await _send_request_with_attachments_to_chat(
            callback.bot, request, selected_user.tg_id, items, attachments
        )
        await callback.bot.send_message(
            selected_user.tg_id,
            f"Дополнительное согласование по заявке №{request.id}. Примите решение:",
            reply_markup=_leader_extra_decision_keyboard(extra_approval.id),
        )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⬅️ К списку", callback_data=f"leader_list:{page}")
    )
    selected_name = selected_user.full_name or selected_user.tg_username or f"ID {selected_user.id}"
    await callback.message.edit_text(
        (
            f"Отправлено на дополнительное согласование: {selected_name}.\n"
            "После решения заявка вернется вам со статусом."
        ),
        reply_markup=builder.as_markup(),
    )
    await _store_my_requests_message(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("initiator_list:"))
async def initiator_list(callback: CallbackQuery, state: FSMContext) -> None:
    _, page_str = callback.data.split(":")
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
    await state.clear()
    await _show_initiator_list(callback.message, user.id, page=page, edit=True, state=state)
    await callback.answer()


@router.callback_query(F.data.startswith("initiator_pick:"))
async def initiator_pick(callback: CallbackQuery, state: FSMContext) -> None:
    _, request_id_str, page_str = callback.data.split(":")
    request_id = int(request_id_str)
    page = int(page_str) if page_str.isdigit() else 1
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        request = await _load_request_full(session, request_id)
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if request.initiator_id != user.id:
            await callback.answer("Нет доступа")
            return
    await callback.message.edit_text(
        format_request_summary(request),
        reply_markup=_initiator_actions_keyboard(page),
    )
    await _store_my_requests_message(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("approval_accept:"))
async def approval_accept(callback: CallbackQuery, state: FSMContext) -> None:
    approval_id = int(callback.data.split(":")[1])
    async with SessionLocal() as session:
        approval = await session.get(Approval, approval_id)
        if not approval:
            await callback.answer("Заявка не найдена")
            return
        approver = await session.get(User, approval.approver_id)
        if not approver or (
            approver.tg_id != callback.from_user.id and not await _is_override_user(callback.from_user)
        ):
            await callback.answer("Нет доступа")
            return
        if approval.kind == APPROVAL_KIND_EXECUTOR_EXTRA:
            pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
            if pending_id and approval.status_id != pending_id:
                await callback.answer("Согласование уже обработано")
                return
            await state.set_state(ExtraApprovalComment.comment)
            await state.update_data(extra_approval_id=approval_id, extra_decision="approved")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.message.answer("Введите комментарий")
            await callback.answer()
            return
        if approval.kind == APPROVAL_KIND_LEADER_EXTRA:
            pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
            if pending_id and approval.status_id != pending_id:
                await callback.answer("Согласование уже обработано")
                return
            approved_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_APPROVED)
            approval.status_id = approved_id
            approval.decided_at = to_naive_utc(callback.message.date)
            approval.comment = approval.comment or "Без комментария"

            request = await _load_request_full(session, approval.request_id)
            if not request:
                await callback.answer("Заявка не найдена")
                return
            author_name = approver.full_name or approver.tg_username or f"ID {approver.id}"
            session.add(
                Comment(
                    request_id=request.id,
                    author_id=approver.id,
                    text=f"Доп. согласование (Согласовано) от {author_name}",
                )
            )
            requester = await session.get(User, approval.requested_by_id) if approval.requested_by_id else None
            notify_user = await _resolve_notification_user(session, requester)
            summary = await _format_approval_summary(session, request.id)
            await session.commit()
            notified = False
            if notify_user and notify_user.tg_id:
                await send_to_user(
                    callback.bot,
                    notify_user,
                    (
                        f"✅ Дополнительное согласование по заявке №{request.id}: Согласовано.\n"
                        f"Согласующий: {author_name}\n\n"
                        f"{summary}\n\n"
                        "Откройте «Мои заявки» и примите финальное решение."
                    ),
                )
                notified = True
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if notified:
                await callback.message.answer("Решение отправлено главному согласующему.")
            else:
                await callback.message.answer(
                    "Решение сохранено, но уведомление главному согласующему не доставлено."
                )
            await callback.answer("Принято")
            return
        if approver.is_default_approver and await _has_pending_leader_extra_approval(
            session, approval.request_id
        ):
            await callback.answer("Ожидается дополнительное согласование")
            return
        approved_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_APPROVED)
        approval.status_id = approved_id
        approval.decided_at = to_naive_utc(callback.message.date)
        await session.commit()

        request = await _load_request_full(session, approval.request_id)
        if not request:
            await callback.answer("Заявка не найдена")
            return
        await callback.message.answer(f"Заявка №{request.id} принята.")

        next_pending = await _get_next_pending_approval(session, request.id)
        if next_pending:
            next_approval, next_approver = next_pending
            override_tg_id = settings.approval_override_tg_id
            items, attachments = await fetch_request_media(session, request.id)
            if override_tg_id:
                await _send_request_with_attachments_to_chat(
                    callback.bot, request, override_tg_id, items, attachments
                )
                await callback.bot.send_message(
                    override_tg_id,
                    "Примите решение по заявке:",
                    reply_markup=approval_action_keyboard(next_approval.id),
                )
            elif next_approver.tg_id:
                if next_approver.is_default_approver:
                    await send_to_user(
                        callback.bot,
                        next_approver,
                        (
                            f"📌 Заявка №{request.id} требует согласования, "
                            "проверьте \"Мои заявки\"."
                        ),
                    )
                else:
                    await _send_request_with_attachments_to_chat(
                        callback.bot,
                        request,
                        next_approver.tg_id,
                        items,
                        attachments,
                    )
                    await send_to_user(
                        callback.bot,
                        next_approver,
                        "Примите решение по заявке:",
                        reply_markup=approval_action_keyboard(next_approval.id),
                    )
            return

        if request.status and request.status.code == REQUEST_STATUS_REJECTED:
            await callback.answer("Заявка уже отклонена")
            return
        status_id = await _get_status_id(session, RequestStatus, REQUEST_STATUS_APPROVED)
        request.status_id = status_id
        request.approved_at = to_naive_utc(callback.message.date)
        await session.flush()
        request = await _load_request_full(session, request.id)
        if not request:
            await callback.answer("Заявка не найдена")
            return
        items, attachments = await fetch_request_media(session, request.id)
        await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()

        executors = await _fetch_executors(session)
        if not executors:
            await callback.answer("Исполнители не найдены")
            return

        override_tg_id = settings.approval_override_tg_id
        if override_tg_id:
            await _send_request_with_attachments_to_chat(
                callback.bot, request, override_tg_id, items, attachments
            )
            await callback.bot.send_message(
                override_tg_id,
                "Выберите исполнителя для заявки:",
                reply_markup=executor_assign_keyboard(executors, request.id),
            )
            return

        if approver.is_default_approver and approver.tg_id:
            await _send_request_with_attachments_to_chat(
                callback.bot, request, approver.tg_id, items, attachments
            )
            await send_to_user(
                callback.bot,
                approver,
                "Выберите исполнителя для заявки:",
                reply_markup=executor_assign_keyboard(executors, request.id),
            )
        else:
            chiefs = (
                await session.execute(
                    select(User)
                    .where(User.is_default_approver.is_(True))
                    .where(User.tg_id.is_not(None))
                )
            ).scalars().all()
            for chief in chiefs:
                await _send_request_with_attachments_to_chat(
                    callback.bot, request, chief.tg_id, items, attachments
                )
                await send_to_user(
                    callback.bot,
                    chief,
                    "Выберите исполнителя для заявки:",
                    reply_markup=executor_assign_keyboard(executors, request.id),
                )

    await callback.answer("Принято")


@router.callback_query(F.data.startswith("exec_extra:"))
async def executor_assignment_extra_flow(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, request_id_str, action = parts
    if not request_id_str.isdigit():
        await callback.answer("Некорректный ID")
        return
    request_id = int(request_id_str)
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        await session.commit()
        if not user.is_default_approver and not await _is_override_user(callback.from_user):
            await callback.answer("Нет доступа")
            return
        request = await session.get(
            Request,
            request_id,
            options=[selectinload(Request.status)],
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if request.executor_id:
            await callback.answer("Исполнитель уже назначен")
            return
        if request.status and request.status.code == REQUEST_STATUS_REJECTED:
            await callback.answer("Заявка отклонена")
            return
        if await _has_pending_executor_extra_approval(session, request_id):
            await callback.answer("Ожидается дополнительное согласование")
            return

        if action == "back":
            await callback.message.edit_text(
                "Требуется дополнительное согласование?",
                reply_markup=_executor_assignment_extra_prompt_keyboard(request_id),
            )
            await callback.answer()
            return

        if action == "yes":
            chiefs = await _fetch_default_approvers(session)
            if not chiefs:
                await callback.answer("Главные согласующие не найдены")
                return
            await callback.message.edit_text(
                "Выберите главного согласующего для дополнительного согласования:",
                reply_markup=_executor_assignment_extra_chiefs_keyboard(request_id, chiefs),
            )
            await callback.answer()
            return

        if action == "no":
            executors = await _fetch_executors(session)
            if not executors:
                await callback.answer("Исполнители не найдены")
                return
            await callback.message.edit_text(
                "Выберите исполнителя для заявки:",
                reply_markup=executor_assign_keyboard(executors, request_id),
            )
            await callback.answer()
            return

    await callback.answer()


@router.callback_query(F.data.startswith("exec_extra_chief:"))
async def executor_assignment_extra_chief_pick(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, request_id_str, chief_id_str = parts
    if not request_id_str.isdigit() or not chief_id_str.isdigit():
        await callback.answer("Некорректные данные")
        return
    request_id = int(request_id_str)
    chief_id = int(chief_id_str)
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        await session.commit()
        if not user.is_default_approver and not await _is_override_user(callback.from_user):
            await callback.answer("Нет доступа")
            return
        request = await session.get(
            Request,
            request_id,
            options=[selectinload(Request.status)],
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        if request.executor_id:
            await callback.answer("Исполнитель уже назначен")
            return
        if request.status and request.status.code == REQUEST_STATUS_REJECTED:
            await callback.answer("Заявка отклонена")
            return
        if await _has_pending_executor_extra_approval(session, request_id):
            await callback.answer("Ожидается дополнительное согласование")
            return

        chief = await session.get(User, chief_id)
        if not chief or not chief.is_default_approver:
            await callback.answer("Главный согласующий не найден")
            return
        if not chief.tg_id:
            await callback.answer(
                "У выбранного согласующего не настроен Telegram. Запустите /start с его аккаунта."
            )
            return
        pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
        extra_approval = Approval(
            request_id=request_id,
            approver_id=chief.id,
            status_id=pending_id,
            kind=APPROVAL_KIND_EXECUTOR_EXTRA,
            requested_by_id=user.id,
        )
        session.add(extra_approval)
        await session.flush()

        request_full = await _load_request_full(session, request_id)
        items, attachments = await fetch_request_media(session, request_id)
        await session.commit()

        if request_full:
            await _send_request_with_attachments_to_chat(
                callback.bot, request_full, chief.tg_id, items, attachments
            )
            await callback.bot.send_message(
                chief.tg_id,
                f"Дополнительное согласование по заявке №{request_id}. Примите решение:",
                reply_markup=_executor_assignment_extra_approval_keyboard(extra_approval.id),
            )

        chief_name = chief.full_name or chief.tg_username or f"ID {chief.id}"
        menu_builder = InlineKeyboardBuilder()
        menu_builder.row(
            InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu")
        )
        await callback.message.edit_text(
            f"Отправлено на дополнительное согласование: {chief_name}. Ожидайте решение.",
            reply_markup=menu_builder.as_markup(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("approval_reject:"))
async def approval_reject(callback: CallbackQuery, state: FSMContext) -> None:
    approval_id = int(callback.data.split(":")[1])
    async with SessionLocal() as session:
        approval = await session.get(Approval, approval_id)
        if not approval:
            await callback.answer("Заявка не найдена")
            return
        approver = await session.get(User, approval.approver_id)
        if not approver or (
            approver.tg_id != callback.from_user.id and not await _is_override_user(callback.from_user)
        ):
            await callback.answer("Нет доступа")
            return
        if approval.kind == APPROVAL_KIND_EXECUTOR_EXTRA:
            pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
            if pending_id and approval.status_id != pending_id:
                await callback.answer("Согласование уже обработано")
                return
            await state.set_state(ExtraApprovalComment.comment)
            await state.update_data(extra_approval_id=approval_id, extra_decision="rejected")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.message.answer("Введите комментарий")
            await callback.answer()
            return
        if approval.kind == APPROVAL_KIND_LEADER_EXTRA:
            pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
            if pending_id and approval.status_id != pending_id:
                await callback.answer("Согласование уже обработано")
                return
            rejected_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_REJECTED)
            approval.status_id = rejected_id
            approval.decided_at = to_naive_utc(callback.message.date)
            approval.comment = approval.comment or "Без комментария"

            request = await _load_request_full(session, approval.request_id)
            if not request:
                await callback.answer("Заявка не найдена")
                return
            author_name = approver.full_name or approver.tg_username or f"ID {approver.id}"
            session.add(
                Comment(
                    request_id=request.id,
                    author_id=approver.id,
                    text=f"Доп. согласование (Отклонено) от {author_name}",
                )
            )
            requester = await session.get(User, approval.requested_by_id) if approval.requested_by_id else None
            notify_user = await _resolve_notification_user(session, requester)
            summary = await _format_approval_summary(session, request.id)
            await session.commit()
            notified = False
            if notify_user and notify_user.tg_id:
                await send_to_user(
                    callback.bot,
                    notify_user,
                    (
                        f"❌ Дополнительное согласование по заявке №{request.id}: Отклонено.\n"
                        f"Согласующий: {author_name}\n\n"
                        f"{summary}\n\n"
                        "Откройте «Мои заявки» и примите финальное решение."
                    ),
                )
                notified = True
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if notified:
                await callback.message.answer("Решение отправлено главному согласующему.")
            else:
                await callback.message.answer(
                    "Решение сохранено, но уведомление главному согласующему не доставлено."
                )
            await callback.answer("Отменено")
            return
        if approver.is_default_approver and await _has_pending_leader_extra_approval(
            session, approval.request_id
        ):
            await callback.answer("Ожидается дополнительное согласование")
            return
    await state.set_state(ApprovalComment.comment)
    await state.update_data(approval_id=approval_id)
    await callback.message.answer("Введите комментарий для отказа")
    await callback.answer()


@router.callback_query(F.data.startswith("leader_comment:"))
async def leader_comment_start(callback: CallbackQuery, state: FSMContext) -> None:
    approval_id = int(callback.data.split(":")[1])
    async with SessionLocal() as session:
        approval = await session.get(Approval, approval_id)
        if not approval:
            await callback.answer("Заявка не найдена")
            return
        approver = await session.get(User, approval.approver_id)
        if not approver or (
            approver.tg_id != callback.from_user.id and not await _is_override_user(callback.from_user)
        ):
            await callback.answer("Нет доступа")
            return
    await state.set_state(LeaderComment.comment)
    await state.update_data(approval_id=approval_id)
    await callback.message.answer("Введите комментарий")
    await callback.answer()


@router.message(ExtraApprovalComment.comment)
async def extra_approval_comment(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Нужно отправить текст.")
        return
    data = await state.get_data()
    approval_id = data.get("extra_approval_id")
    decision = data.get("extra_decision")
    if not approval_id or decision not in {"approved", "rejected"}:
        await message.answer("Не удалось определить согласование.")
        await state.clear()
        return
    comment_text = message.text.strip()

    async with SessionLocal() as session:
        approval = await session.get(Approval, approval_id)
        if not approval or approval.kind not in {
            APPROVAL_KIND_EXECUTOR_EXTRA,
            APPROVAL_KIND_LEADER_EXTRA,
        }:
            await message.answer("Согласование не найдено.")
            await state.clear()
            return
        pending_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_PENDING)
        if pending_id and approval.status_id != pending_id:
            await message.answer("Согласование уже обработано.")
            await state.clear()
            return
        approver = await session.get(User, approval.approver_id)
        if not approver or (
            approver.tg_id != message.from_user.id and not await _is_override_user(message.from_user)
        ):
            await message.answer("Нет доступа")
            await state.clear()
            return

        status_code = (
            APPROVAL_STATUS_APPROVED if decision == "approved" else APPROVAL_STATUS_REJECTED
        )
        status_id = await _get_status_id(session, ApprovalStatus, status_code)
        approval.status_id = status_id
        approval.comment = comment_text
        approval.decided_at = to_naive_utc(message.date)
        is_executor_extra = approval.kind == APPROVAL_KIND_EXECUTOR_EXTRA

        request = await _load_request_full(session, approval.request_id)
        if not request:
            await message.answer("Заявка не найдена")
            await state.clear()
            return

        decision_label = "Согласовано" if decision == "approved" else "Отклонено"
        author_name = approver.full_name or approver.tg_username or f"ID {approver.id}"
        session.add(
            Comment(
                request_id=request.id,
                author_id=approver.id,
                text=f"Доп. согласование ({decision_label}) от {author_name} - {comment_text}",
            )
        )

        if is_executor_extra and decision == "rejected":
            rejected_id = await _get_status_id(session, RequestStatus, REQUEST_STATUS_REJECTED)
            request.status_id = rejected_id
            await session.flush()
            await upsert_request_excel(session, request, settings.files_dir)

        requester = await session.get(User, approval.requested_by_id) if approval.requested_by_id else None
        notify_user = await _resolve_notification_user(session, requester)
        executors = []
        if is_executor_extra and decision == "approved":
            executors = await _fetch_executors(session)
        summary = await _format_approval_summary(session, request.id)
        await session.commit()

        if notify_user and notify_user.tg_id:
            if is_executor_extra and decision == "approved":
                await send_to_user(
                    message.bot,
                    notify_user,
                    (
                        f"✅ Дополнительное согласование получено по заявке №{request.id}.\n"
                        f"Комментарий: {comment_text}\n\n"
                        "Выберите исполнителя для заявки:"
                    ),
                    reply_markup=executor_assign_keyboard(executors, request.id),
                )
            elif is_executor_extra:
                await send_to_user(
                    message.bot,
                    notify_user,
                    (
                        f"❌ Дополнительное согласование отклонено по заявке №{request.id}.\n"
                        f"Комментарий: {comment_text}\n\n"
                        "Заявка отменена."
                    ),
                )
            else:
                status_emoji = "✅" if decision == "approved" else "❌"
                await send_to_user(
                    message.bot,
                    notify_user,
                    (
                        f"{status_emoji} Дополнительное согласование по заявке №{request.id}: "
                        f"{decision_label}.\n"
                        f"Согласующий: {author_name}\n"
                        f"Комментарий: {comment_text}\n\n"
                        f"{summary}\n\n"
                        "Откройте «Мои заявки» и примите финальное решение."
                    ),
                )

        if is_executor_extra and decision == "rejected":
            await send_to_user(
                message.bot,
                request.initiator,
                (
                    f"Ваша заявка №{request.id} отклонена на этапе дополнительного согласования.\n"
                    f"Комментарий: {comment_text}"
                ),
            )

    await state.clear()
    await message.answer("Комментарий сохранен.")


@router.message(ApprovalComment.comment)
async def approval_reject_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    approval_id = data.get("approval_id")
    async with SessionLocal() as session:
        approval = await session.get(Approval, approval_id)
        if not approval:
            await message.answer("Заявка не найдена")
            await state.clear()
            return
        rejected_id = await _get_status_id(session, ApprovalStatus, APPROVAL_STATUS_REJECTED)
        approval.status_id = rejected_id
        approval.comment = message.text.strip()
        approval.decided_at = to_naive_utc(message.date)

        status_id = await _get_status_id(session, RequestStatus, REQUEST_STATUS_REJECTED)
        request = await session.get(
            Request,
            approval.request_id,
            options=[
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.items),
                selectinload(Request.attachments),
            ],
        )
        request.status_id = status_id
        await session.flush()
        await upsert_request_excel(session, request, settings.files_dir)
        await session.commit()

        summary = await _format_approval_summary(session, request.id)
        await send_to_user(
            message.bot,
            request.initiator,
            (
                f"{format_request_summary(request)}\n\n"
                f"Ваша заявка отклонена руководителем. "
                f"Комментарий: {approval.comment}\n{summary}"
            ),
        )
    await state.clear()
    await message.answer("Комментарий принят, заявка отклонена.")


@router.message(LeaderComment.comment)
async def leader_comment_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    approval_id = data.get("approval_id")
    async with SessionLocal() as session:
        approval = await session.get(Approval, approval_id)
        if not approval:
            await message.answer("Заявка не найдена")
            await state.clear()
            return
        approver = await session.get(User, approval.approver_id)
        if not approver or (
            approver.tg_id != message.from_user.id and not await _is_override_user(message.from_user)
        ):
            await message.answer("Нет доступа")
            await state.clear()
            return
        request = await session.get(
            Request,
            approval.request_id,
            options=[selectinload(Request.initiator)],
        )
        author_name = approver.full_name or approver.tg_username or f"ID {approver.id}"
        comment_text = message.text.strip()
        session.add(
            Comment(
                request_id=request.id,
                author_id=approver.id,
                text=f"Комментарий от {author_name} - {comment_text}",
            )
        )
        await session.commit()

        await send_to_user(
            message.bot,
            request.initiator,
            f"Оставлен комментарий к заявке №{request.id} от {approver.full_name}.",
        )

        leaders = (
            await session.execute(
                select(User)
                .outerjoin(user_roles, user_roles.c.user_id == User.id)
                .outerjoin(Role, Role.id == user_roles.c.role_id)
                .where(or_(Role.code == "approver", User.is_default_approver.is_(True)))
                .distinct()
            )
        ).scalars().all()
        for leader in leaders:
            if leader.id == approver.id:
                continue
            await send_to_user(
                message.bot,
                leader,
                f"Оставлен комментарий к заявке №{request.id} от {approver.full_name}.",
            )
    await state.clear()
    await message.answer("Комментарий сохранен.")
