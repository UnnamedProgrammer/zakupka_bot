from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.keyboards import roles_keyboard, settings_keyboard, settings_list_keyboard
from app.bot.states import AdminAddCfo, AdminAddDepartment, AdminAssignRole
from app.db.models import Cfo, Department, Role, User
from app.db.session import SessionLocal
from app.services.users import ensure_username_format, get_or_create_user

router = Router()


async def _is_admin(session, user_id: int) -> bool:
    role = await session.scalar(
        select(Role.code).join(User, User.role_id == Role.id).where(User.id == user_id)
    )
    return role == "admin"


async def _require_admin(callback: CallbackQuery) -> bool:
    async with SessionLocal() as session:
        username = await ensure_username_format(callback.from_user.username)
        user = await get_or_create_user(
            session, callback.from_user.id, username, callback.from_user.full_name
        )
        if not await _is_admin(session, user.id):
            await callback.answer("Нет доступа")
            return False
    return True


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message) -> None:
    async with SessionLocal() as session:
        username = await ensure_username_format(message.from_user.username)
        user = await get_or_create_user(
            session, message.from_user.id, username, message.from_user.full_name
        )
        if not await _is_admin(session, user.id):
            await message.answer("Нет доступа.")
            return
    await message.answer("Настройки", reply_markup=settings_keyboard())


@router.callback_query(F.data == "settings:departments")
async def settings_departments(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    async with SessionLocal() as session:
        rows = await session.execute(select(Department.id, Department.name).order_by(Department.name))
        deps = rows.all()
    await callback.message.answer(
        "Подразделения", reply_markup=settings_list_keyboard("departments", deps)
    )
    await callback.answer()


@router.callback_query(F.data == "departments:add")
async def department_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddDepartment.name)
    await callback.message.answer("Введите название подразделения")
    await callback.answer()


@router.message(AdminAddDepartment.name)
async def department_add_finish(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        session.add(Department(name=message.text.strip()))
        await session.commit()
    await state.clear()
    await message.answer("Подразделение добавлено.")


@router.callback_query(F.data.startswith("departments:del:"))
async def department_delete(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    dep_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        dep = await session.get(Department, dep_id)
        if dep:
            await session.delete(dep)
            await session.commit()
    await callback.answer("Удалено")


@router.callback_query(F.data == "settings:cfos")
async def settings_cfos(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    async with SessionLocal() as session:
        rows = await session.execute(select(Cfo.id, Cfo.name).order_by(Cfo.name))
        items = rows.all()
    await callback.message.answer("ЦФО", reply_markup=settings_list_keyboard("cfos", items))
    await callback.answer()


@router.callback_query(F.data == "cfos:add")
async def cfo_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAddCfo.name)
    await callback.message.answer("Введите название ЦФО")
    await callback.answer()


@router.message(AdminAddCfo.name)
async def cfo_add_finish(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session:
        session.add(Cfo(name=message.text.strip()))
        await session.commit()
    await state.clear()
    await message.answer("ЦФО добавлено.")


@router.callback_query(F.data.startswith("cfos:del:"))
async def cfo_delete(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    cfo_id = int(callback.data.split(":")[2])
    async with SessionLocal() as session:
        cfo = await session.get(Cfo, cfo_id)
        if cfo:
            await session.delete(cfo)
            await session.commit()
    await callback.answer("Удалено")


@router.callback_query(F.data == "settings:users")
async def settings_users(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminAssignRole.username)
    await callback.message.answer("Введите @username пользователя")
    await callback.answer()


@router.message(AdminAssignRole.username)
async def user_role_username(message: Message, state: FSMContext) -> None:
    username = message.text.strip()
    if not username.startswith("@"):
        username = f"@{username}"
    await state.update_data(username=username)
    async with SessionLocal() as session:
        roles = (await session.execute(select(Role.id, Role.name).order_by(Role.name))).all()
    await state.set_state(AdminAssignRole.role)
    await message.answer("Выберите роль", reply_markup=roles_keyboard(roles))


@router.callback_query(AdminAssignRole.role, F.data.startswith("role:"))
async def user_role_set(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    role_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    username = data.get("username")
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.tg_username == username))
        if not user:
            user = User(tg_username=username, role_id=role_id)
            session.add(user)
        else:
            user.role_id = role_id
        await session.commit()
    await state.clear()
    await callback.message.answer("Роль назначена.")
    await callback.answer()
