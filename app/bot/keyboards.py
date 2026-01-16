from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def main_menu_keyboard(role_code: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📝 Создать заявку")
    builder.button(text="📚 Архив")
    if role_code == "executor":
        builder.button(text="📌 Мои заявки")
        builder.button(text="📤 Ежедневные заявки")
        builder.button(text="📊 Статистика сотрудников")
        builder.button(text="📅 Срок поставки")
    if role_code == "admin":
        builder.button(text="⚙️ Настройки")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def departments_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for dep_id, name in items:
        builder.button(text=f"🏢 {name}", callback_data=f"dept:{dep_id}")
    builder.adjust(1)
    return builder.as_markup()


def cfo_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cfo_id, name in items:
        builder.button(text=f"🏷️ {name}", callback_data=f"cfo:{cfo_id}")
    builder.adjust(1)
    return builder.as_markup()


def description_method_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Ввести описание вручную", callback_data="desc:manual")
    builder.button(text="📄 Отправить Excel файл", callback_data="desc:excel")
    builder.adjust(1)
    return builder.as_markup()


def approver_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user_id, name in items:
        builder.button(text=f"👤 {name}", callback_data=f"approver:{user_id}")
    builder.adjust(1)
    return builder.as_markup()


def approval_action_keyboard(approval_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"approval_accept:{approval_id}")
    builder.button(text="❌ Отклонить", callback_data=f"approval_reject:{approval_id}")
    builder.button(text="💬 Комментарий", callback_data=f"leader_comment:{approval_id}")
    builder.adjust(2)
    return builder.as_markup()


def attachments_done_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="attachments:done")
    builder.button(text="⏭️ Пропустить", callback_data="attachments:skip")
    builder.adjust(2)
    return builder.as_markup()


def add_item_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить товар", callback_data="item_more:yes")
    builder.button(text="➡️ Продолжить", callback_data="item_more:no")
    builder.adjust(2)
    return builder.as_markup()


def executor_assign_keyboard(items: list[tuple[int, str]], request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user_id, name in items:
        builder.button(text=f"🧑‍🔧 {name}", callback_data=f"assign:{request_id}:{user_id}")
    builder.adjust(1)
    return builder.as_markup()


def executor_actions_keyboard(
    request_id: int, include_extras: bool = True
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛠️ В работу", callback_data=f"status:{request_id}:in_work")
    builder.button(text="✅ Выполнена", callback_data=f"status:{request_id}:done")
    builder.button(text="❌ Отклонена", callback_data=f"status:{request_id}:rejected")
    if include_extras:
        builder.button(text="💬 Комментарий", callback_data=f"comment:{request_id}")
        builder.button(text="📎 Файл", callback_data=f"file:{request_id}")
        builder.button(text="📅 Срок поставки", callback_data=f"delivery:{request_id}")
        builder.adjust(2)
    else:
        builder.adjust(2)
    return builder.as_markup()


def receive_tmc_keyboard(request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 ТМЦ получено", callback_data=f"received:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


def archive_status_keyboard(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    status_icons = {
        "pending_approval": "🕒",
        "approved": "✅",
        "in_work": "🛠️",
        "done": "🎉",
        "rejected": "❌",
        "received": "📦",
    }
    builder = InlineKeyboardBuilder()
    for code, name in items:
        icon = status_icons.get(code, "📌")
        builder.button(text=f"{icon} {name}", callback_data=f"arch_status:{code}")
    builder.button(text="📋 Все", callback_data="arch_status:all")
    builder.adjust(2)
    return builder.as_markup()


def skip_keyboard(callback_data: str = "skip") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭️ Пропустить", callback_data=callback_data)
    builder.adjust(1)
    return builder.as_markup()


def settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏢 Подразделения", callback_data="settings:departments")
    builder.button(text="🏷️ ЦФО", callback_data="settings:cfos")
    builder.button(text="👥 Пользователи", callback_data="settings:users")
    builder.adjust(1)
    return builder.as_markup()


def settings_list_keyboard(prefix: str, items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item_id, name in items:
        builder.button(text=f"🗑️ {name}", callback_data=f"{prefix}:del:{item_id}")
    builder.button(text="➕ Добавить", callback_data=f"{prefix}:add")
    builder.adjust(1)
    return builder.as_markup()


def roles_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    role_icons = {
        "Сотрудник": "👤",
        "Согласующий": "✅",
        "Главный согласующий": "⭐",
        "Исполнитель": "🧑‍🔧",
        "Администратор": "⚙️",
    }
    builder = InlineKeyboardBuilder()
    for role_id, name in items:
        icon = role_icons.get(name, "👤")
        builder.button(text=f"{icon} {name}", callback_data=f"role:{role_id}")
    builder.adjust(1)
    return builder.as_markup()
