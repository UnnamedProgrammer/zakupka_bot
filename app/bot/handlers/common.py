from pathlib import Path

from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.types import FSInputFile, Message
from app.bot.keyboards import main_menu_keyboard
from app.db.session import SessionLocal
from app.services.users import ensure_username_format, get_or_create_user, get_user_role_codes

router = Router()
_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "request_template.xlsx"


async def _send_main_menu(message: Message) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session,
            tg_id=message.from_user.id,
            username=username,
            full_name=message.from_user.full_name,
        )
        await session.commit()
        role_codes = await get_user_role_codes(session, user.id)
        await message.answer("Главное меню", reply_markup=main_menu_keyboard(role_codes))


@router.message(CommandStart())
async def start(message: Message) -> None:
    await _send_main_menu(message)


@router.message(F.text.lower() == "меню")
async def menu(message: Message) -> None:
    await _send_main_menu(message)


@router.message(F.text == "📥 Скачать шаблон заявки")
async def download_request_template(message: Message) -> None:
    if not _TEMPLATE_PATH.exists():
        await message.answer("Шаблон заявки не найден. Сообщите администратору.")
        return
    await message.answer_document(
        FSInputFile(_TEMPLATE_PATH, filename=_TEMPLATE_PATH.name)
    )


@router.message(StateFilter(None))
async def fallback(message: Message) -> None:
    if not message.text:
        return
    await _send_main_menu(message)
