import math
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.bot.keyboards import approval_action_keyboard, executor_assign_keyboard
from app.bot.states import ApprovalComment, LeaderComment
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
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_REJECTED,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_REJECTED,
)
from app.services.attachments import build_photo_groups_from, fetch_request_media
from app.services.excel import upsert_request_excel
from app.services.formatters import format_request_summary
from app.services.notifications import send_to_user
from app.services.users import (
    ensure_username_format,
    get_or_create_user,
    get_user_role_codes,
)
from app.services.datetime import to_naive_utc

router = Router()
LEADER_LIST_PAGE_SIZE = 6
INITIATOR_LIST_PAGE_SIZE = 6


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
    return f"{icon} №{request.id} · {name}"


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
    return builder


def _leader_actions_keyboard(approval_id: int, page: int, pending: bool):
    builder = InlineKeyboardBuilder()
    if pending:
        builder.button(text="✅ Принять", callback_data=f"approval_accept:{approval_id}")
        builder.button(text="❌ Отклонить", callback_data=f"approval_reject:{approval_id}")
        builder.button(text="💬 Комментарий", callback_data=f"leader_comment:{approval_id}")
        builder.adjust(2)
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


def _initiator_request_label(request: Request) -> str:
    name = _normalize_text(request.item_name) or _normalize_text(request.supplier_name)
    if not name:
        name = "без названия"
    status = request.status.name if request.status else ""
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
    photo_groups = build_photo_groups_from(request, items, attachments)
    if photo_groups:
        await bot.send_message(chat_id, "Заявка")
        for title, photos in photo_groups:
            await bot.send_message(chat_id, title)
            for att in photos:
                if att.file_path:
                    await bot.send_photo(chat_id, FSInputFile(att.file_path))
                elif att.file_id:
                    await bot.send_photo(chat_id, att.file_id)
    for att in attachments:
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
        .where(Approval.approver_id == approver_id)
        .order_by(Request.created_at.desc())
    )
    results = rows.all()
    results.sort(key=lambda row: row[2].created_at or datetime.min, reverse=True)
    results.sort(key=lambda row: row[1].code != APPROVAL_STATUS_PENDING)
    return results


async def _show_leader_list(
    message: Message,
    approver_id: int,
    page: int,
    edit: bool = False,
) -> None:
    async with SessionLocal() as session:
        approvals = await _fetch_leader_approvals(session, approver_id)
    total = len(approvals)
    if total == 0:
        text = "У вас нет заявок на согласование."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    total_pages = max(1, math.ceil(total / LEADER_LIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * LEADER_LIST_PAGE_SIZE
    rows = approvals[start : start + LEADER_LIST_PAGE_SIZE]
    text = f"Ваши заявки на согласование. Страница {page}/{total_pages}."
    markup = _leader_list_keyboard(rows, page, total_pages).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


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
) -> None:
    async with SessionLocal() as session:
        requests = await _fetch_initiator_requests(session, initiator_id)
    total = len(requests)
    if total == 0:
        text = "У вас нет созданных заявок."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    total_pages = max(1, math.ceil(total / INITIATOR_LIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * INITIATOR_LIST_PAGE_SIZE
    requests_page = requests[start : start + INITIATOR_LIST_PAGE_SIZE]
    text = f"Ваши заявки. Страница {page}/{total_pages}."
    markup = _initiator_list_keyboard(requests_page, page, total_pages).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(F.text == "📌 Мои заявки")
async def my_requests(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        role_codes = await get_user_role_codes(session, user.id)
    await state.clear()
    if "approver" in role_codes or user.is_default_approver:
        await _show_leader_list(message, user.id, page=1, edit=False)
        return
    if "executor" in role_codes:
        from app.bot.handlers.executor import _show_my_list  # local import to avoid cycles

        await _show_my_list(message, user.id, page=1, edit=False)
        return
    await _show_initiator_list(message, user.id, page=1, edit=False)


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
    await _show_leader_list(callback.message, user.id, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("leader_pick:"))
async def leader_pick(callback: CallbackQuery) -> None:
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
        approval_id = approval.id
    await callback.message.edit_text(
        format_request_summary(request),
        reply_markup=_leader_actions_keyboard(approval_id, page, pending),
    )
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
    await _show_initiator_list(callback.message, user.id, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("initiator_pick:"))
async def initiator_pick(callback: CallbackQuery) -> None:
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
    await callback.answer()


@router.callback_query(F.data.startswith("approval_accept:"))
async def approval_accept(callback: CallbackQuery) -> None:
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
                    await send_to_user(
                        callback.bot, next_approver, format_request_summary(request)
                    )
                    photo_groups = build_photo_groups_from(request, items, attachments)
                    if photo_groups:
                        await callback.bot.send_message(next_approver.tg_id, "Заявка")
                        for title, photos in photo_groups:
                            await callback.bot.send_message(next_approver.tg_id, title)
                            for att in photos:
                                if att.file_path:
                                    await callback.bot.send_photo(
                                        next_approver.tg_id, FSInputFile(att.file_path)
                                    )
                                elif att.file_id:
                                    await callback.bot.send_photo(
                                        next_approver.tg_id, att.file_id
                                    )
                    for att in attachments:
                        if att.file_type != "document":
                            continue
                        if att.file_id:
                            await callback.bot.send_document(
                                next_approver.tg_id, att.file_id
                            )
                        elif att.file_path:
                            await callback.bot.send_document(
                                next_approver.tg_id,
                                FSInputFile(att.file_path, filename=att.file_name or None),
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

        await send_to_user(
            callback.bot,
            request.initiator,
            f"Ваша заявка №{request.id} согласована руководителем и направляется исполнителям.",
        )

        executors = (
            await session.execute(
                select(User.id, User.full_name)
                .join(user_roles, user_roles.c.user_id == User.id)
                .join(Role, Role.id == user_roles.c.role_id)
                .where(Role.code == "executor")
                .order_by(User.full_name)
            )
        ).all()

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
                    select(User).where(User.is_default_approver.is_(True))
                )
            ).scalars().all()
            for chief in chiefs:
                if not chief.tg_id:
                    continue
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
