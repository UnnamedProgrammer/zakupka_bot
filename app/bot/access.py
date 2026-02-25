from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.db.models import User
from app.db.session import SessionLocal
from app.services.users import ensure_username_format

NO_ACCESS_TEXT = (
    "У вас нет доступа к боту.\n"
    "Обратитесь к администратору, чтобы вас добавили в список пользователей."
)
NO_USERNAME_TEXT = (
    "Для доступа к боту нужен Telegram username (начинается с @).\n"
    "Установите username и обратитесь к администратору."
)


class AccessByUsernameMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user") or getattr(event, "from_user", None)
        if from_user is None or getattr(from_user, "is_bot", False):
            return await handler(event, data)

        username = await ensure_username_format(from_user.username)
        if not username:
            await self._deny(event, NO_USERNAME_TEXT)
            return None

        async with SessionLocal() as session:
            allowed_user = await session.scalar(
                select(User.id).where(
                    func.lower(User.tg_username) == username.lower(),
                    User.is_active.is_(True),
                )
            )
        if not allowed_user:
            await self._deny(event, NO_ACCESS_TEXT)
            return None

        return await handler(event, data)

    async def _deny(self, event: Any, text: str) -> None:
        if isinstance(event, CallbackQuery):
            try:
                await event.answer("Нет доступа", show_alert=True)
            except Exception:
                pass
            if event.message:
                try:
                    await event.message.answer(text)
                except Exception:
                    pass
            return

        if isinstance(event, Message):
            try:
                await event.answer(text)
            except Exception:
                pass
