from sqlalchemy import select

from app.db.models import Role, User


async def get_or_create_user(session, tg_id: int, username: str | None, full_name: str | None) -> User:
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user:
        if username and user.tg_username != username:
            user.tg_username = username
        if full_name and user.full_name != full_name:
            user.full_name = full_name
        return user

    if username:
        user = await session.scalar(select(User).where(User.tg_username == username))
        if user:
            user.tg_id = tg_id
            if full_name and user.full_name != full_name:
                user.full_name = full_name
            return user

    role = await session.scalar(select(Role).where(Role.code == "employee"))
    user = User(
        tg_id=tg_id,
        tg_username=username,
        full_name=full_name,
        role_id=role.id if role else 1,
    )
    session.add(user)
    await session.flush()
    return user


async def ensure_username_format(username: str | None) -> str | None:
    if not username:
        return None
    return username if username.startswith("@") else f"@{username}"
