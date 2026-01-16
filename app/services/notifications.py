from aiogram import Bot

from app.db.models import User


async def send_to_user(bot: Bot, user: User, text: str, **kwargs) -> bool:
    if not user.tg_id:
        return False
    await bot.send_message(user.tg_id, text, **kwargs)
    return True
