from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile
from sqlalchemy import select
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
from app.services.users import ensure_username_format
from app.services.datetime import to_naive_utc

router = Router()


async def _get_status_id(session, model, code: str) -> int:
    return await session.scalar(select(model.id).where(model.code == code))


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
                await send_to_user(callback.bot, next_approver, format_request_summary(request))
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
                                await callback.bot.send_photo(next_approver.tg_id, att.file_id)
                for att in attachments:
                    if att.file_type != "document":
                        continue
                    if att.file_id:
                        await callback.bot.send_document(next_approver.tg_id, att.file_id)
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
                .join(Role, Role.id == User.role_id)
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

        if approver.role_id:
            approver_role = await session.scalar(select(Role.code).where(Role.id == approver.role_id))
        else:
            approver_role = None
        if approver_role == "chief_approver" and approver.tg_id:
            await send_to_user(callback.bot, approver, format_request_summary(request))
            await send_to_user(
                callback.bot,
                approver,
                "Выберите исполнителя для заявки:",
                reply_markup=executor_assign_keyboard(executors, request.id),
            )
        else:
            chiefs = (
                await session.execute(
                    select(User).join(Role, Role.id == User.role_id).where(Role.code == "chief_approver")
                )
            ).scalars().all()
            for chief in chiefs:
                await send_to_user(callback.bot, chief, format_request_summary(request))
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
        session.add(
            Comment(
                request_id=request.id,
                author_id=approver.id,
                text=message.text.strip(),
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
                .join(Role, Role.id == User.role_id)
                .where(Role.code.in_(["approver", "chief_approver"]))
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
