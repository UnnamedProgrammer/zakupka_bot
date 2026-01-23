from datetime import datetime, time, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.bot.keyboards import archive_status_keyboard, skip_keyboard
from app.bot.states import ArchiveFilter
from app.db.models import Request, RequestItem, RequestStatus, User
from app.db.session import SessionLocal
from app.services.excel import build_archive_xlsx
from app.services.users import ensure_username_format, get_or_create_user

router = Router()


@router.message(F.text == "📚 Архив")
async def archive_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with SessionLocal() as session:
        status_rows = await session.execute(select(RequestStatus.code, RequestStatus.name))
        statuses = status_rows.all()
    await state.set_state(ArchiveFilter.status)
    await message.answer("Выберите статус для фильтра", reply_markup=archive_status_keyboard(statuses))


@router.callback_query(ArchiveFilter.status, F.data.startswith("arch_status:"))
async def archive_status(callback: CallbackQuery, state: FSMContext) -> None:
    status_code = callback.data.split(":")[1]
    await state.update_data(status_code=None if status_code == "all" else status_code)
    await state.set_state(ArchiveFilter.name)
    await callback.message.answer(
        "Введите наименование для поиска или пропустите",
        reply_markup=skip_keyboard("arch_skip:name"),
    )
    await callback.answer()


@router.message(ArchiveFilter.name)
async def archive_name(message: Message, state: FSMContext) -> None:
    await state.update_data(item_name=message.text.strip())
    await state.set_state(ArchiveFilter.supplier)
    await message.answer(
        "Введите поставщика для поиска или пропустите",
        reply_markup=skip_keyboard("arch_skip:supplier"),
    )


@router.callback_query(ArchiveFilter.name, F.data == "arch_skip:name")
async def archive_name_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(item_name=None)
    await state.set_state(ArchiveFilter.supplier)
    await callback.message.answer(
        "Введите поставщика для поиска или пропустите",
        reply_markup=skip_keyboard("arch_skip:supplier"),
    )
    await callback.answer()


@router.message(ArchiveFilter.supplier)
async def archive_supplier(message: Message, state: FSMContext) -> None:
    await state.update_data(supplier_name=message.text.strip())
    await state.set_state(ArchiveFilter.initiator)
    await message.answer(
        "Введите инициатора для поиска или пропустите",
        reply_markup=skip_keyboard("arch_skip:initiator"),
    )


@router.callback_query(ArchiveFilter.supplier, F.data == "arch_skip:supplier")
async def archive_supplier_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(supplier_name=None)
    await state.set_state(ArchiveFilter.initiator)
    await callback.message.answer(
        "Введите инициатора для поиска или пропустите",
        reply_markup=skip_keyboard("arch_skip:initiator"),
    )
    await callback.answer()


@router.message(ArchiveFilter.initiator)
async def archive_initiator(message: Message, state: FSMContext) -> None:
    await state.update_data(initiator_name=message.text.strip())
    await state.set_state(ArchiveFilter.date_from)
    await message.answer(
        "Введите дату начала (DD-MM-YYYY) или пропустите",
        reply_markup=skip_keyboard("arch_skip:date_from"),
    )


@router.callback_query(ArchiveFilter.initiator, F.data == "arch_skip:initiator")
async def archive_initiator_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(initiator_name=None)
    await state.set_state(ArchiveFilter.date_from)
    await callback.message.answer(
        "Введите дату начала (DD-MM-YYYY) или пропустите",
        reply_markup=skip_keyboard("arch_skip:date_from"),
    )
    await callback.answer()


@router.message(ArchiveFilter.date_from)
async def archive_date_from(message: Message, state: FSMContext) -> None:
    await state.update_data(date_from=message.text.strip())
    await state.set_state(ArchiveFilter.date_to)
    await message.answer(
        "Введите дату окончания (DD-MM-YYYY) или пропустите",
        reply_markup=skip_keyboard("arch_skip:date_to"),
    )


@router.callback_query(ArchiveFilter.date_from, F.data == "arch_skip:date_from")
async def archive_date_from_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(date_from=None)
    await state.set_state(ArchiveFilter.date_to)
    await callback.message.answer(
        "Введите дату окончания (DD-MM-YYYY) или пропустите",
        reply_markup=skip_keyboard("arch_skip:date_to"),
    )
    await callback.answer()


@router.message(ArchiveFilter.date_to)
async def archive_date_to(message: Message, state: FSMContext) -> None:
    await state.update_data(date_to=message.text.strip())
    await _archive_finish(message, state)


@router.callback_query(ArchiveFilter.date_to, F.data == "arch_skip:date_to")
async def archive_date_to_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(date_to=None)
    await _archive_finish(callback.message, state)
    await callback.answer()


async def _archive_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )

        query = select(Request).options(
            selectinload(Request.initiator),
            selectinload(Request.department),
            selectinload(Request.cfo),
            selectinload(Request.status),
            selectinload(Request.items),
        )

        if data.get("status_code"):
            status_id = await session.scalar(
                select(RequestStatus.id).where(RequestStatus.code == data["status_code"])
            )
            if status_id:
                query = query.where(Request.status_id == status_id)
        if data.get("item_name"):
            query = query.outerjoin(RequestItem).where(
                or_(
                    Request.item_name.ilike(f"%{data['item_name']}%"),
                    RequestItem.name.ilike(f"%{data['item_name']}%"),
                )
            )
            query = query.distinct()
        if data.get("initiator_name"):
            query = query.join(User, User.id == Request.initiator_id).where(
                User.full_name.ilike(f"%{data['initiator_name']}%")
            )
        if data.get("supplier_name"):
            query = query.where(Request.supplier_name.ilike(f"%{data['supplier_name']}%"))
        date_from = _parse_date(data.get("date_from"))
        date_to = _parse_date(data.get("date_to"))
        if date_from:
            query = query.where(
                Request.created_at >= datetime.combine(date_from, time.min)
            )
        if date_to:
            date_to_end = datetime.combine(date_to, time.min) + timedelta(days=1)
            query = query.where(Request.created_at < date_to_end)

        rows = await session.execute(query.order_by(Request.created_at.desc()))
        requests = rows.scalars().all()

    if not requests:
        await message.answer("По вашему запросу заявок не найдено.")
        return
    await message.answer("Формирую Excel файл, пожалуйста подождите...")
    content = build_archive_xlsx(requests)
    filename = f"archive_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    await message.answer_document(BufferedInputFile(content, filename=filename))


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%m-%Y").date()
    except ValueError:
        return None
