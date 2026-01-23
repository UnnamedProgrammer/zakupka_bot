from sqlalchemy import select

from app.db.models import Role, User, user_roles


async def get_or_create_user(session, tg_id: int, username: str | None, full_name: str | None) -> User:
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user:
        return user

    if username:
        user = await session.scalar(select(User).where(User.tg_username == username))
        if user:
            if not user.tg_id:
                user.tg_id = tg_id
            return user

    user = User(
        tg_id=tg_id,
        tg_username=username,
        full_name=full_name,
    )
    role = await session.scalar(select(Role).where(Role.code == "employee"))
    if role:
        user.roles.append(role)
    session.add(user)
    await session.flush()
    return user


async def ensure_username_format(username: str | None) -> str | None:
    if not username:
        return None
    return username if username.startswith("@") else f"@{username}"


async def get_user_role_codes(session, user_id: int) -> set[str]:
    rows = await session.execute(
        select(Role.code)
        .join(user_roles, user_roles.c.role_id == Role.id)
        .where(user_roles.c.user_id == user_id)
    )
    return {row[0] for row in rows.all()}


async def user_has_role(session, user_id: int, role_code: str) -> bool:
    role_id = await session.scalar(
        select(Role.id)
        .join(user_roles, user_roles.c.role_id == Role.id)
        .where(user_roles.c.user_id == user_id, Role.code == role_code)
    )
    return role_id is not None
