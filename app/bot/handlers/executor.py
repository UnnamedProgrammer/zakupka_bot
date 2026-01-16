from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message, FSInputFile
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.keyboards import executor_actions_keyboard, receive_tmc_keyboard
from app.bot.states import ExecutorComment, ExecutorDeliveryDate, ExecutorFile
from app.db.models import Attachment, Comment, Request, RequestStatus, Role, User
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
    build_daily_requests_xlsx,
    build_employee_stats_xlsx,
    upsert_request_excel,
)
from app.services.files import save_telegram_file
from app.services.formatters import format_request_summary
from app.services.notifications import send_to_user
from app.services.users import ensure_username_format, get_or_create_user
from app.services.datetime import to_naive_utc
from app.config import settings

router = Router()


async def _get_request_status_id(session, code: str) -> int:
    return await session.scalar(select(RequestStatus.id).where(RequestStatus.code == code))


async def _is_executor(session, user_id: int) -> bool:
    role = await session.scalar(
        select(Role.code).join(User, User.role_id == Role.id).where(User.id == user_id)
    )
    return role == "executor"


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


@router.callback_query(F.data.startswith("assign:"))
async def assign_executor(callback: CallbackQuery) -> None:
    _, req_id, exec_id = callback.data.split(":")
    request_id = int(req_id)
    executor_id = int(exec_id)
    async with SessionLocal() as session:
        current_user = await _get_user(session, callback.from_user)
        role_code = await session.scalar(
            select(Role.code).where(Role.id == current_user.role_id)
        )
        if role_code != "chief_approver" and not await _is_override_user(callback.from_user):
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
                text=message.text.strip(),
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
                    f"Комментарий: {message.text.strip()}"
                ),
            )
        else:
            updated = await session.get(
                Request, request.id, options=[selectinload(Request.initiator)]
            )
            await send_to_user(
                message.bot,
                updated.initiator if updated else request.initiator,
                f"Комментарий к заявке №{request.id}: {message.text.strip()}",
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
    await state.set_state(ExecutorDeliveryDate.date)
    await state.update_data(request_id=request_id)
    await callback.message.answer("Введите дату поставки в формате YYYY-MM-DD")
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
            Request, request_id, options=[selectinload(Request.executor)]
        )
        if not request:
            await callback.answer("Заявка не найдена")
            return
        status_id = await _get_request_status_id(session, REQUEST_STATUS_RECEIVED)
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
    await callback.answer("Принято")


@router.message(F.text == "📌 Мои заявки")
async def my_requests(message: Message) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        if not await _is_executor(session, user.id):
            await message.answer("Доступно только исполнителям.")
            return
        rows = await session.execute(
            select(Request)
            .where(Request.executor_id == user.id)
            .options(
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.items),
            )
            .order_by(Request.created_at.desc())
        )
        requests = rows.scalars().all()
        if not requests:
            await message.answer("У вас нет назначенных заявок.")
            return
        for req in requests:
            await message.answer(
                format_request_summary(req),
                reply_markup=_executor_keyboard_for_request(req),
            )


@router.message(F.text == "📤 Ежедневные заявки")
async def export_daily_requests(message: Message) -> None:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Request)
            .options(
                selectinload(Request.initiator),
                selectinload(Request.executor),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
            )
            .order_by(Request.created_at.desc())
        )
        content = build_daily_requests_xlsx(rows.scalars().all())
    await message.answer_document(
        document=BufferedInputFile(content, filename="daily_requests.xlsx"),
        caption="Ежедневные заявки",
    )


@router.message(F.text == "📊 Статистика сотрудников")
async def export_employee_stats(message: Message) -> None:
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Request)
            .options(
                selectinload(Request.initiator),
                selectinload(Request.executor),
                selectinload(Request.status),
            )
            .order_by(Request.created_at.desc())
        )
        content = build_employee_stats_xlsx(rows.scalars().all())
    await message.answer_document(
        document=BufferedInputFile(content, filename="employee_stats.xlsx"),
        caption="Статистика сотрудников",
    )


@router.message(F.text == "📅 Срок поставки")
async def delivery_menu(message: Message) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        if not await _is_executor(session, user.id):
            await message.answer("Доступно только исполнителям.")
            return
        rows = await session.execute(
            select(Request)
            .where(Request.executor_id == user.id)
            .options(
                selectinload(Request.initiator),
                selectinload(Request.department),
                selectinload(Request.cfo),
                selectinload(Request.status),
                selectinload(Request.items),
            )
            .order_by(Request.created_at.desc())
        )
        requests = rows.scalars().all()
        if not requests:
            await message.answer("Нет заявок для установки срока поставки.")
            return
        for req in requests:
            await message.answer(
                format_request_summary(req),
                reply_markup=_executor_keyboard_for_request(req),
            )
