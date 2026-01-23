import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile
from sqlalchemy import or_, select, func
from sqlalchemy.orm import selectinload

from app.bot.keyboards import (
    approval_action_keyboard,
    approver_keyboard,
    add_item_keyboard,
    attachments_done_keyboard,
    cfo_keyboard,
    departments_keyboard,
    request_method_keyboard,
)
from app.bot.states import RequestCreate
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
    Role,
    User,
    user_roles,
)
from app.db.session import SessionLocal
from app.services.constants import APPROVAL_STATUS_PENDING, REQUEST_STATUS_PENDING
from app.config import settings
from app.services.attachments import build_photo_groups_from, fetch_request_media
from app.services.excel import TemplateParseError, build_request_xlsx, parse_request_template
from app.services.files import save_telegram_file, save_bytes_file
from app.services.formatters import format_request_summary
from app.services.notifications import send_to_user
from app.services.users import ensure_username_format, get_or_create_user

router = Router()
logger = logging.getLogger(__name__)
_album_buffers: dict[tuple[int, str], dict] = {}


def _is_image_document(message: Message) -> bool:
    if not message.document:
        return False
    mime_type = message.document.mime_type or ""
    return mime_type.startswith("image/")


async def _queue_album_attachment(message: Message, state: FSMContext, file_info: dict) -> None:
    if not message.media_group_id:
        return
    key = (message.chat.id, message.media_group_id)
    buffer = _album_buffers.get(key)
    if not buffer:
        buffer = {"photos": [], "chat_id": message.chat.id, "bot": message.bot, "task": None}
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
    current_item = data.get("current_item") or {}
    attachments = current_item.get("attachments") or []
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
    current_item["attachments"] = attachments
    await state.update_data(current_item=current_item)
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


async def _get_or_create_reference(session, model, name: str | None, cache: dict[str, int]) -> int | None:
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


async def _fetch_approvers(session) -> list[tuple[int, str]]:
    rows = await session.execute(
        select(User.id, User.full_name)
        .outerjoin(user_roles, user_roles.c.user_id == User.id)
        .outerjoin(Role, Role.id == user_roles.c.role_id)
        .where(or_(Role.code == "approver", User.is_default_approver.is_(True)))
        .distinct()
        .order_by(User.full_name, User.id)
    )
    return [(row[0], row[1]) for row in rows.all()]


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
    photo_groups = build_photo_groups_from(request, items, attachments)
    if photo_groups:
        await bot.send_message(user.tg_id, "Заявка")
        for title, photos in photo_groups:
            await bot.send_message(user.tg_id, title)
            for att in photos:
                if att.file_path:
                    await bot.send_photo(user.tg_id, FSInputFile(att.file_path))
                elif att.file_id:
                    await bot.send_photo(user.tg_id, att.file_id)
    for att in attachments:
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


@router.message(F.text == "📝 Создать заявку")
async def create_request_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(RequestCreate.method)
    await message.answer(
        "Как будете создавать заявку?", reply_markup=request_method_keyboard()
    )


@router.callback_query(RequestCreate.method, F.data.startswith("req_method:"))
async def create_request_method(callback: CallbackQuery, state: FSMContext) -> None:
    method = callback.data.split(":")[1]
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    if method == "manual":
        await state.update_data(full_name=callback.from_user.full_name.strip())
        await _prompt_departments(callback.message, state)
    else:
        await state.set_state(RequestCreate.excel_file)
        await callback.message.answer("Загрузите Excel файл по шаблону.")


@router.message(RequestCreate.excel_file)
async def create_request_excel_file(message: Message, state: FSMContext) -> None:
    if not message.document:
        await message.answer("Пожалуйста, отправьте Excel файл (.xlsx).")
        return
    file_name = message.document.file_name or ""
    if Path(file_name).suffix.lower() != ".xlsx":
        await message.answer("Нужен файл формата .xlsx. Проверьте и отправьте заново.")
        return

    file_path = await save_telegram_file(
        message.bot,
        message.document.file_id,
        dest_dir=settings.files_dir,
        filename_hint=file_name,
    )
    try:
        parsed = parse_request_template(file_path)
    except TemplateParseError as exc:
        await message.answer(str(exc))
        await state.clear()
        try:
            Path(file_path).unlink()
        except FileNotFoundError:
            pass
        return
    except Exception:
        logger.exception("Failed to parse request template")
        await message.answer("Не удалось прочитать Excel файл. Проверьте шаблон и загрузите заново.")
        await state.clear()
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
            await message.answer(
                "Инициатор из ячейки G2 не найден в базе пользователей. "
                "Исправьте ФИО и загрузите файл заново."
            )
            await state.clear()
            try:
                Path(file_path).unlink()
            except FileNotFoundError:
                pass
            return
        if len(initiator_rows) > 1:
            await message.answer(
                "Найдено несколько пользователей с таким ФИО инициатора. "
                "Уточните ФИО в базе и загрузите файл заново."
            )
            await state.clear()
            try:
                Path(file_path).unlink()
            except FileNotFoundError:
                pass
            return
        initiator = initiator_rows[0]

        dep_rows = await session.execute(select(Department.id, Department.name))
        department_map = {
            _normalize_key(name): (dep_id, name) for dep_id, name in dep_rows.all()
        }
        cfo_rows = await session.execute(select(Cfo.id, Cfo.name))
        cfo_map = {
            _normalize_key(name): (cfo_id, name) for cfo_id, name in cfo_rows.all()
        }
        if not cfo_map:
            await message.answer("В базе нет ЦФО. Загрузка заявки невозможна.")
            await state.clear()
            try:
                Path(file_path).unlink()
            except FileNotFoundError:
                pass
            return

        excel_groups = []
        for group in parsed["groups"]:
            dept_key = _normalize_key(group["department_name"])
            dept = department_map.get(dept_key)
            if not dept:
                await message.answer(
                    f"Подразделение «{group['department_name']}» не найдено в базе. "
                    "Исправьте файл и загрузите заново."
                )
                await state.clear()
                try:
                    Path(file_path).unlink()
                except FileNotFoundError:
                    pass
                return
            cfo_key = _normalize_key(group["cfo_name"])
            cfo = cfo_map.get(cfo_key)
            if not cfo:
                await message.answer(
                    f"ЦФО «{group['cfo_name']}» не найдено в базе. "
                    "Исправьте файл и загрузите заново."
                )
                await state.clear()
                try:
                    Path(file_path).unlink()
                except FileNotFoundError:
                    pass
                return
            excel_groups.append(
                {
                    "department_id": dept[0],
                    "department_name": dept[1],
                    "cfo_id": cfo[0],
                    "mol_full_name": group["mol_full_name"],
                    "items": group["items"],
                }
            )

    if not excel_groups:
        await message.answer("В файле нет данных для создания заявки.")
        await state.clear()
        try:
            Path(file_path).unlink()
        except FileNotFoundError:
            pass
        return

    await state.update_data(
        initiator_id=initiator.id,
        full_name=initiator.full_name,
        description_method="excel",
        excel_file_path=file_path,
        excel_file_name=file_name,
        excel_groups=excel_groups,
        excel_group_index=0,
    )
    await _prompt_excel_approver(message, state)


@router.message(RequestCreate.full_name)
async def create_request_full_name(message: Message, state: FSMContext) -> None:
    await state.update_data(full_name=message.text.strip())
    await _prompt_departments(message, state)


async def _prompt_departments(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
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
        rows = await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
        cfos = rows.all()
    await state.set_state(RequestCreate.cfo)
    await callback.message.answer("Выберите ЦФО", reply_markup=cfo_keyboard(cfos))
    await callback.answer()


@router.callback_query(RequestCreate.cfo, F.data.startswith("cfo:"))
async def create_request_cfo(callback: CallbackQuery, state: FSMContext) -> None:
    cfo_id = int(callback.data.split(":")[1])
    await state.update_data(cfo_id=cfo_id, description_method="manual", items=[])
    await state.set_state(RequestCreate.item_name)
    await callback.message.answer("Наименование")
    await callback.answer()


@router.callback_query(RequestCreate.description_method, F.data.startswith("desc:"))
async def create_request_description_method(callback: CallbackQuery, state: FSMContext) -> None:
    method = callback.data.split(":")[1]
    await state.update_data(description_method=method)
    if method == "excel":
        await callback.message.answer(
            "Для загрузки Excel выберите этот вариант в начале создания заявки."
        )
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
    await state.update_data(
        items=items, current_item={"name": message.text.strip(), "attachments": []}
    )
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
    current.setdefault("attachments", [])
    await state.update_data(current_item=current)
    await state.set_state(RequestCreate.item_link_or_photo)
    await message.answer(
        "Отправьте фото, файл или ссылку на товар (если нужно) и нажмите «Готово».",
        reply_markup=attachments_done_keyboard(),
    )


@router.message(RequestCreate.item_link_or_photo)
async def create_request_item_link_or_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_item = data.get("current_item") or {}
    attachments = current_item.get("attachments") or []
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
            return
        photo_count = sum(1 for att in attachments if att.get("file_type") == "photo")
        if photo_count >= 3:
            await message.answer("Можно добавить не более 3 фото для товара.")
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
        current_item["attachments"] = attachments
        await state.update_data(current_item=current_item)
        await message.answer("Фото добавлено. Можно отправить еще или нажмите «Готово».")
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
                return
            photo_count = sum(1 for att in attachments if att.get("file_type") == "photo")
            if photo_count >= 3:
                await message.answer("Можно добавить не более 3 фото для товара.")
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
            current_item["attachments"] = attachments
            await state.update_data(current_item=current_item)
            await message.answer("Фото добавлено. Можно отправить еще или нажмите «Готово».")
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
        current_item["attachments"] = attachments
        await state.update_data(current_item=current_item)
        await message.answer("Файл добавлен. Можно отправить еще или нажмите «Готово».")
        return
    if message.text:
        current = current_item.get("link", "")
        if current:
            current = f"{current}\n{message.text.strip()}"
        else:
            current = message.text.strip()
        current_item["link"] = current
        await state.update_data(current_item=current_item)
        await message.answer("Ссылка сохранена. Можно отправить еще или нажмите «Готово».")
        return
    await message.answer("Отправьте фото, файл или ссылку, либо нажмите «Готово».")


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
        approvers = await _fetch_approvers(session)
    await state.set_state(RequestCreate.approver_choice)
    await message.answer(
        "Выберите согласующего руководителя", reply_markup=approver_keyboard(approvers)
    )


async def _prompt_excel_approver(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    groups = data.get("excel_groups") or []
    index = data.get("excel_group_index", 0)
    if index >= len(groups):
        await state.clear()
        await message.answer("Загрузка заявок завершена.")
        return
    group = groups[index]
    await state.update_data(
        department_id=group["department_id"],
        cfo_id=group["cfo_id"],
        items=group["items"],
        mol_full_name=group["mol_full_name"],
        description_method="excel",
    )
    async with SessionLocal() as session:
        approvers = await _fetch_approvers(session)
    await state.set_state(RequestCreate.approver_choice)
    await message.answer(
        f"Заявка из файла для подразделения «{group['department_name']}». "
        "Выберите согласующего руководителя",
        reply_markup=approver_keyboard(approvers),
    )


@router.callback_query(RequestCreate.approver_choice, F.data.startswith("approver:"))
async def create_request_approver(callback: CallbackQuery, state: FSMContext) -> None:
    selected_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    async with SessionLocal() as session:
        initiator_id = data.get("initiator_id")
        is_excel = data.get("description_method") == "excel"
        excel_file_path = data.get("excel_file_path")
        excel_file_name = data.get("excel_file_name") or "request_template.xlsx"
        if initiator_id:
            initiator = await session.get(User, initiator_id)
            if not initiator:
                await callback.message.answer(
                    "Инициатор из файла не найден. Загрузка заявки остановлена."
                )
                await state.clear()
                await callback.answer()
                return
        else:
            username = await ensure_username_format(callback.from_user.username)
            initiator = await get_or_create_user(
                session, callback.from_user.id, username, data.get("full_name")
            )

        initiator.department_id = data.get("department_id")

        status_id = await _get_status_id(session, RequestStatus, REQUEST_STATUS_PENDING)
        items = data.get("items") or []
        if not items:
            await callback.message.answer("В заявке нет товаров. Проверьте данные.")
            await state.clear()
            await callback.answer()
            return
        primary_item = items[0]

        request = Request(
            status_id=status_id,
            initiator_id=initiator.id,
            department_id=data["department_id"],
            cfo_id=data["cfo_id"],
            description_method=data.get("description_method", "manual"),
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

        ref_cache = {
            "omts": {},
            "category": {},
            "dds": {},
        }
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
                    callback.bot,
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

        if is_excel:
            if not excel_file_path:
                await callback.message.answer(
                    "Файл Excel не найден. Загрузка заявки остановлена."
                )
                await state.clear()
                await callback.answer()
                return
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

        if not is_excel:
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

            items, attachments = await fetch_request_media(session, request.id)
            excel_content = build_request_xlsx(request, items, attachments)
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
            items, attachments = await fetch_request_media(session, request.id)
            override_tg_id = settings.approval_override_tg_id
            if override_tg_id:
                await _send_approval_to_chat(
                    callback.bot, request, approval.id, override_tg_id, items, attachments
                )
            else:
                target_user = (
                    override_user if override_user and override_user.tg_id else approver
                )
                if target_user.tg_id:
                    if approver.is_default_approver:
                        await send_to_user(
                            callback.bot,
                            target_user,
                            (
                                f"📌 Заявка №{request.id} требует согласования, "
                                "проверьте \"Мои заявки\"."
                            ),
                        )
                    else:
                        await _send_request_with_attachments(
                            callback.bot, request, target_user, items, attachments
                        )
                        await send_to_user(
                            callback.bot,
                            target_user,
                            "Примите решение по заявке:",
                            reply_markup=approval_action_keyboard(approval.id),
                        )

    excel_groups = data.get("excel_groups")
    if excel_groups:
        next_index = data.get("excel_group_index", 0) + 1
        if next_index < len(excel_groups):
            await state.update_data(excel_group_index=next_index)
            await _prompt_excel_approver(callback.message, state)
            await callback.answer()
            return

    await state.clear()
    await callback.answer()
