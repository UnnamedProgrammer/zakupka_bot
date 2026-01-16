from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.types import Message
from sqlalchemy import select

from app.bot.keyboards import main_menu_keyboard
from app.db.models import Role
from app.db.session import SessionLocal
from app.services.users import ensure_username_format, get_or_create_user

router = Router()


async def _get_role_code(role_id: int, session) -> str:
    role = await session.scalar(select(Role).where(Role.id == role_id))
    return role.code if role else "employee"


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
        role_code = await _get_role_code(user.role_id, session)
        await message.answer("Главное меню", reply_markup=main_menu_keyboard(role_code))


@router.message(CommandStart())
async def start(message: Message) -> None:
    await _send_main_menu(message)


@router.message(F.text.lower() == "меню")
async def menu(message: Message) -> None:
    await _send_main_menu(message)


@router.message(StateFilter(None))
async def fallback(message: Message) -> None:
    if not message.text:
        return
    await _send_main_menu(message)
