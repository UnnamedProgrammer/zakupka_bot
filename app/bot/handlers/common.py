from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from app.bot.keyboards import main_menu_keyboard
from app.db.session import SessionLocal
from app.services.users import ensure_username_format, get_or_create_user, get_user_role_codes

router = Router()


MAIN_MENU_MESSAGE_KEYS = [
    ("req_message_id", "req_chat_id"),
    ("tmpl_message_id", "tmpl_chat_id"),
    ("arch_message_id", "arch_chat_id"),
    ("admin_settings_message_id", "admin_settings_chat_id"),
    ("admin_users_message_id", "admin_users_chat_id"),
    ("my_requests_message_id", "my_requests_chat_id"),
]
MAIN_MENU_TRACKER: dict[tuple[int, int], int] = {}


async def _try_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _delete_saved_message(bot, chat_id: int | None, message_id: int | None) -> None:
    if not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _delete_main_menu_message(bot, chat_id: int, user_id: int) -> None:
    message_id = MAIN_MENU_TRACKER.get((chat_id, user_id))
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def cleanup_main_menu(message: Message, state: FSMContext | None) -> None:
    if state:
        data = await state.get_data()
        for message_key, chat_key in MAIN_MENU_MESSAGE_KEYS:
            await _delete_saved_message(
                message.bot,
                data.get(chat_key),
                data.get(message_key),
            )
        await state.update_data(
            **{key: None for pair in MAIN_MENU_MESSAGE_KEYS for key in pair}
        )
    await _try_delete_message(message)


async def _send_main_menu_for_user(
    bot,
    chat_id: int,
    tg_user,
    state: FSMContext | None = None,
) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(tg_user.username)
        user = await get_or_create_user(
            session,
            tg_id=tg_user.id,
            username=username,
            full_name=tg_user.full_name,
        )
        await session.commit()
        role_codes = await get_user_role_codes(session, user.id)
        await _delete_main_menu_message(bot, chat_id, tg_user.id)
        sent = await bot.send_message(
            chat_id, "Главное меню", reply_markup=main_menu_keyboard(role_codes)
        )
        if state:
            await state.update_data(
                main_menu_message_id=sent.message_id,
                main_menu_chat_id=sent.chat.id,
            )
        MAIN_MENU_TRACKER[(chat_id, tg_user.id)] = sent.message_id


async def _send_main_menu(message: Message, state: FSMContext | None = None) -> None:
    await _send_main_menu_for_user(message.bot, message.chat.id, message.from_user, state=state)


@router.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await _delete_saved_message(
        callback.bot,
        data.get("main_menu_chat_id"),
        data.get("main_menu_message_id"),
    )
    await cleanup_main_menu(callback.message, state)
    await state.clear()
    await _send_main_menu_for_user(
        callback.bot,
        callback.message.chat.id,
        callback.from_user,
        state=state,
    )
    await callback.answer()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await _send_main_menu(message, state=state)


@router.message(F.text.lower() == "меню")
async def menu(message: Message, state: FSMContext) -> None:
    await _send_main_menu(message, state=state)


@router.message(StateFilter(None))
async def fallback(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if message.text.startswith("/"):
        return
    await _send_main_menu(message, state=state)
