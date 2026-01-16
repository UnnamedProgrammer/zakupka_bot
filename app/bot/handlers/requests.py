import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.keyboards import (
    approval_action_keyboard,
    approver_keyboard,
    add_item_keyboard,
    attachments_done_keyboard,
    cfo_keyboard,
    departments_keyboard,
    description_method_keyboard,
)
from app.bot.states import RequestCreate
from app.db.models import (
    Approval,
    ApprovalStatus,
    Attachment,
    Request,
    RequestItem,
    RequestStatus,
    Role,
    User,
)
from app.db.session import SessionLocal
from app.services.constants import APPROVAL_STATUS_PENDING, REQUEST_STATUS_PENDING
from app.config import settings
from app.services.excel import build_request_xlsx
from app.services.files import save_telegram_file, save_bytes_file
from app.services.formatters import format_request_summary
from app.services.notifications import send_to_user
from app.services.users import ensure_username_format, get_or_create_user

router = Router()
logger = logging.getLogger(__name__)


async def _get_status_id(session, model, code: str) -> int:
    return await session.scalar(select(model.id).where(model.code == code))


def _get_loaded_attachments(request: Request):
    from sqlalchemy import inspect

    state = inspect(request)
    if "attachments" in state.unloaded:
        return []
    return request.attachments


async def _send_request_with_attachments(bot, request: Request, user: User) -> None:
    await send_to_user(bot, user, format_request_summary(request))
    attachments = _get_loaded_attachments(request)
    for att in attachments:
        if not user.tg_id:
            continue
        if att.file_type == "photo":
            if att.file_id:
                await bot.send_photo(user.tg_id, att.file_id)
            elif att.file_path:
                await bot.send_photo(user.tg_id, FSInputFile(att.file_path))
        elif att.file_type == "document":
            if att.file_id:
                await bot.send_document(user.tg_id, att.file_id)
            elif att.file_path:
                await bot.send_document(
                    user.tg_id,
                    FSInputFile(att.file_path, filename=att.file_name or None),
                )


async def _send_request_with_attachments_to_chat(bot, request: Request, chat_id: int) -> None:
    await bot.send_message(chat_id, format_request_summary(request))
    attachments = _get_loaded_attachments(request)
    for att in attachments:
        if att.file_type == "photo":
            if att.file_id:
                await bot.send_photo(chat_id, att.file_id)
            elif att.file_path:
                await bot.send_photo(chat_id, FSInputFile(att.file_path))
        elif att.file_type == "document":
            if att.file_id:
                await bot.send_document(chat_id, att.file_id)
            elif att.file_path:
                await bot.send_document(
                    chat_id,
                    FSInputFile(att.file_path, filename=att.file_name or None),
                )


async def _send_approval_to_chat(bot, request: Request, approval_id: int, chat_id: int) -> None:
    try:
        await _send_request_with_attachments_to_chat(bot, request, chat_id)
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


@router.message(F.text == "📝 Создать заявку")
async def create_request_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(RequestCreate.full_name)
    await message.answer("Введите ваше ФИО")


@router.message(RequestCreate.full_name)
async def create_request_full_name(message: Message, state: FSMContext) -> None:
    await state.update_data(full_name=message.text.strip())
    async with SessionLocal() as session:
        from app.db.models import Department

        dep_rows = await session.execute(
            select(Department.id, Department.name).order_by(Department.name)
        )
        deps = dep_rows.all()
    await state.set_state(RequestCreate.department)
    await message.answer("Выберите подразделение", reply_markup=departments_keyboard(deps))


@router.callback_query(RequestCreate.department, F.data.startswith("dept:"))
async def create_request_department(callback: CallbackQuery, state: FSMContext) -> None:
    dep_id = int(callback.data.split(":")[1])
    await state.update_data(department_id=dep_id)
    async with SessionLocal() as session:
        from app.db.models import Cfo

        rows = await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
        cfos = rows.all()
    await state.set_state(RequestCreate.cfo)
    await callback.message.answer("Выберите ЦФО", reply_markup=cfo_keyboard(cfos))
    await callback.answer()


@router.callback_query(RequestCreate.cfo, F.data.startswith("cfo:"))
async def create_request_cfo(callback: CallbackQuery, state: FSMContext) -> None:
    cfo_id = int(callback.data.split(":")[1])
    await state.update_data(cfo_id=cfo_id)
    await state.set_state(RequestCreate.description_method)
    await callback.message.answer(
        "Описание закупаемого товара", reply_markup=description_method_keyboard()
    )
    await callback.answer()


@router.callback_query(RequestCreate.description_method, F.data.startswith("desc:"))
async def create_request_description_method(callback: CallbackQuery, state: FSMContext) -> None:
    method = callback.data.split(":")[1]
    await state.update_data(description_method=method)
    if method == "excel":
        await callback.message.answer("Функционал загрузки Excel пока в разработке.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(items=[])
    await state.set_state(RequestCreate.item_name)
    await callback.message.answer("Наименование")
    await callback.answer()


@router.message(RequestCreate.item_name)
async def create_request_item_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    items = data.get("items") or []
    await state.update_data(items=items, current_item={"name": message.text.strip()})
    await state.set_state(RequestCreate.item_specs)
    await message.answer("Технические характеристики")


@router.message(RequestCreate.item_specs)
async def create_request_item_specs(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("current_item") or {}
    current["specs"] = message.text.strip()
    await state.update_data(current_item=current)
    await state.set_state(RequestCreate.item_brand)
    await message.answer("Марка устройства или указание аналога")


@router.message(RequestCreate.item_brand)
async def create_request_item_brand(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("current_item") or {}
    current["brand"] = message.text.strip()
    await state.update_data(current_item=current)
    await state.set_state(RequestCreate.item_qty)
    await message.answer("Количество")


@router.message(RequestCreate.item_qty)
async def create_request_item_qty(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("current_item") or {}
    current["qty"] = message.text.strip()
    await state.update_data(current_item=current)
    await state.set_state(RequestCreate.item_unit)
    await message.answer("Единица измерения количества")


@router.message(RequestCreate.item_unit)
async def create_request_item_unit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("current_item") or {}
    current["unit"] = message.text.strip()
    attachments = data.get("attachments")
    if attachments is None:
        attachments = []
    await state.update_data(current_item=current, attachments=attachments)
    await state.set_state(RequestCreate.item_link_or_photo)
    await message.answer(
        "Отправьте фото, файл или ссылку на товар (если нужно) и нажмите «Готово».",
        reply_markup=attachments_done_keyboard(),
    )


@router.message(RequestCreate.item_link_or_photo)
async def create_request_item_link_or_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    attachments = data.get("attachments", [])
    if message.photo:
        photo = message.photo[-1]
        attachments.append(
            {
                "file_id": photo.file_id,
                "file_unique_id": photo.file_unique_id,
                "file_type": "photo",
                "file_name": None,
            }
        )
        await state.update_data(attachments=attachments)
        await message.answer("Фото добавлено. Можно отправить еще или нажмите «Готово».")
        return
    if message.document:
        doc = message.document
        attachments.append(
            {
                "file_id": doc.file_id,
                "file_unique_id": doc.file_unique_id,
                "file_type": "document",
                "file_name": doc.file_name,
            }
        )
        await state.update_data(attachments=attachments)
        await message.answer("Файл добавлен. Можно отправить еще или нажмите «Готово».")
        return
    if message.text:
        current_item = data.get("current_item") or {}
        current = current_item.get("link", "")
        if current:
            current = f"{current}\n{message.text.strip()}"
        else:
            current = message.text.strip()
        current_item["link"] = current
        await state.update_data(current_item=current_item)
        await message.answer("Ссылка сохранена. Можно отправить еще или нажмите «Готово».")
        return
    await message.answer("Отправьте фото или ссылку, либо нажмите «Готово».")


@router.callback_query(RequestCreate.item_link_or_photo, F.data == "attachments:done")
async def create_request_attachments_done(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RequestCreate.item_note)
    await callback.message.answer("Примечание (при необходимости)")
    await callback.answer()


@router.callback_query(RequestCreate.item_link_or_photo, F.data == "attachments:skip")
async def create_request_attachments_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RequestCreate.item_note)
    await callback.message.answer("Примечание (при необходимости)")
    await callback.answer()


@router.message(RequestCreate.item_note)
async def create_request_item_note(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_item = data.get("current_item") or {}
    current_item["note"] = message.text.strip()
    items = data.get("items") or []
    items.append(current_item)
    await state.update_data(items=items, current_item=None)
    await state.set_state(RequestCreate.item_add_more)
    await message.answer("Добавить еще товар?", reply_markup=add_item_keyboard())


@router.callback_query(RequestCreate.item_add_more, F.data.startswith("item_more:"))
async def create_request_item_more(callback: CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[1]
    if choice == "yes":
        await state.set_state(RequestCreate.item_name)
        await callback.message.answer("Наименование следующего товара")
    else:
        await state.set_state(RequestCreate.mol_full_name)
        await callback.message.answer("Введите ФИО материально ответственного лица (МОЛ)")
    await callback.answer()


@router.message(RequestCreate.mol_full_name)
async def create_request_mol(message: Message, state: FSMContext) -> None:
    await state.update_data(mol_full_name=message.text.strip())
    async with SessionLocal() as session:
        rows = await session.execute(
            select(User.id, User.full_name)
            .join(Role, Role.id == User.role_id)
            .where(Role.code.in_(["approver", "chief_approver"]))
            .order_by(User.full_name)
        )
        approvers = [(row[0], row[1]) for row in rows.all()]
    await state.set_state(RequestCreate.approver_choice)
    await message.answer(
        "Выберите согласующего руководителя", reply_markup=approver_keyboard(approvers)
    )


@router.callback_query(RequestCreate.approver_choice, F.data.startswith("approver:"))
async def create_request_approver(callback: CallbackQuery, state: FSMContext) -> None:
    selected_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        initiator = await get_or_create_user(
            session, callback.from_user.id, username, data.get("full_name")
        )
        initiator.department_id = data.get("department_id")

        status_id = await _get_status_id(session, RequestStatus, REQUEST_STATUS_PENDING)
        items = data.get("items") or []
        primary_item = items[0] if items else {}

        request = Request(
            status_id=status_id,
            initiator_id=initiator.id,
            department_id=data["department_id"],
            cfo_id=data["cfo_id"],
            description_method=data["description_method"],
            item_name=primary_item.get("name"),
            item_specs=primary_item.get("specs"),
            item_brand=primary_item.get("brand"),
            item_qty=primary_item.get("qty"),
            item_unit=primary_item.get("unit"),
            item_link=primary_item.get("link"),
            item_note=primary_item.get("note"),
            mol_full_name=data.get("mol_full_name"),
        )
        session.add(request)
        await session.flush()

        for item in items:
            session.add(
                RequestItem(
                    request_id=request.id,
                    name=item.get("name"),
                    specs=item.get("specs"),
                    brand=item.get("brand"),
                    qty=item.get("qty"),
                    unit=item.get("unit"),
                    link=item.get("link"),
                    note=item.get("note"),
                )
            )

        approval_status_id = await _get_status_id(
            session, ApprovalStatus, APPROVAL_STATUS_PENDING
        )
        ordered_approvers: list[User] = []
        seen_ids: set[int] = set()

        def _add_approver(user: User | None) -> None:
            if not user or user.id in seen_ids:
                return
            seen_ids.add(user.id)
            ordered_approvers.append(user)

        default_order = [
            "Гайнутдинов Руслан Фаргатович",
            "Тихонова Людмила Васильевна",
        ]
        if default_order:
            rows = await session.execute(
                select(User).where(User.full_name.in_(default_order))
            )
            defaults_by_name = {user.full_name: user for user in rows.scalars().all()}
            for name in default_order:
                _add_approver(defaults_by_name.get(name))

        extra_defaults = (
            await session.execute(
                select(User)
                .where(User.is_default_approver.is_(True))
                .order_by(User.full_name)
            )
        ).scalars().all()
        for user in extra_defaults:
            _add_approver(user)

        selected = await session.get(User, selected_id)
        _add_approver(selected)

        chiefs = (
            await session.execute(
                select(User)
                .join(Role, Role.id == User.role_id)
                .where(Role.code == "chief_approver")
                .order_by(User.full_name)
            )
        ).scalars().all()
        for chief in chiefs:
            _add_approver(chief)

        approvals = []
        for user in ordered_approvers:
            approval = Approval(
                request_id=request.id,
                approver_id=user.id,
                status_id=approval_status_id,
            )
            session.add(approval)
            approvals.append(approval)

        attachments = data.get("attachments", [])
        for item in attachments:
            file_path = await save_telegram_file(
                callback.bot,
                item["file_id"],
                dest_dir=settings.files_dir,
                filename_hint=item.get("file_name"),
            )
            session.add(
                Attachment(
                    request_id=request.id,
                    uploader_id=initiator.id,
                    file_id=item["file_id"],
                    file_unique_id=item["file_unique_id"],
                    file_name=item.get("file_name"),
                    file_path=file_path,
                    file_type=item["file_type"],
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
                selectinload(Request.items),
                selectinload(Request.attachments),
            )
            .execution_options(populate_existing=True)
        )
        request = refreshed.scalar_one()

        excel_content = build_request_xlsx(request, request.items, request.attachments)
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
        await callback.message.answer(
            f"Ваша заявка №{request.id} отправлена на согласование: {approvers_text}."
        )

        override_user = await _resolve_override_user(session)
        next_pending = await _get_next_pending_approval(session, request.id)
        if next_pending:
            approval, approver = next_pending
            override_tg_id = settings.approval_override_tg_id
            if override_tg_id:
                await _send_approval_to_chat(
                    callback.bot, request, approval.id, override_tg_id
                )
            else:
                target_user = (
                    override_user if override_user and override_user.tg_id else approver
                )
                if target_user.tg_id:
                    await _send_request_with_attachments(callback.bot, request, target_user)
                    await send_to_user(
                        callback.bot,
                        target_user,
                        "Примите решение по заявке:",
                        reply_markup=approval_action_keyboard(approval.id),
                    )

    await state.clear()
    await callback.answer()
