from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def main_menu_keyboard(role_codes) -> ReplyKeyboardMarkup:
    codes = {role_codes} if isinstance(role_codes, str) else set(role_codes or [])
    builder = ReplyKeyboardBuilder()
    builder.button(text="📝 Создать заявку")
    builder.button(text="📥 Скачать шаблон заявки")
    builder.button(text="📚 Архив")
    if codes:
        builder.button(text="📌 Мои заявки")
    if "executor" in codes:
        builder.button(text="📤 Выгрузить ежедневные заявки")
        builder.button(text="📊 Выгрузить статистику сотрудников")
        builder.button(text="📅 Срок поставки")
    if "admin" in codes:
        builder.button(text="⚙️ Настройки")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def request_method_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Вручную", callback_data="req_method:manual")
    builder.button(text="📄 Загрузить Excel", callback_data="req_method:excel")
    builder.adjust(1)
    return builder.as_markup()


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
    if include_extras:
        builder.button(text="💬 Комментарий", callback_data=f"comment:{request_id}")
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
    builder.button(text="🏷️ ЦФО (Бюджет)", callback_data="settings:cfos")
    builder.button(text="👥 Пользователи", callback_data="settings:users")
    builder.button(text="📝 Заявки", callback_data="settings:requests")
    builder.button(text="⬅️ В главное меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def settings_list_keyboard(
    prefix: str,
    items: list[tuple[int, str]],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item_id, name in items:
        builder.button(text=f"🗑️ {name}", callback_data=f"{prefix}:del:{item_id}:{page}")
    builder.adjust(1)
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}:list:{page - 1}")
            )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="➡️", callback_data=f"{prefix}:list:{page + 1}")
            )
    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(InlineKeyboardButton(text="➕ Добавить", callback_data=f"{prefix}:add"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:menu"))
    return builder.as_markup()


def requests_list_keyboard(
    items: list[tuple[int, str]],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for request_id, label in items:
        builder.button(text=label, callback_data=f"req_edit:menu:{request_id}")
    builder.adjust(1)
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️", callback_data=f"requests:list:{page - 1}")
            )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="➡️", callback_data=f"requests:list:{page + 1}")
            )
    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(InlineKeyboardButton(text="🔎 Ввести ID", callback_data="requests:enter_id"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:menu"))
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


def users_menu_keyboard(role_items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    role_icons = {
        "employee": "👤",
        "approver": "✅",
        "executor": "🧑‍🔧",
        "admin": "⚙️",
    }
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить пользователя", callback_data="users:add")
    for code, name in role_items:
        icon = role_icons.get(code, "👥")
        builder.button(text=f"{icon} {name}", callback_data=f"users:list:{code}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:menu"))
    return builder.as_markup()


def users_list_keyboard(role_key: str, items: list[tuple[int, str, bool]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user_id, name, is_active in items:
        prefix = "🟢" if is_active else "🔴"
        builder.button(
            text=f"{prefix} {name}",
            callback_data=f"users:toggle:{role_key}:{user_id}",
        )
    builder.button(text="➕ Добавить", callback_data="users:add")
    builder.button(text="⬅️ Назад", callback_data="settings:users")
    builder.adjust(1)
    return builder.as_markup()


def request_edit_keyboard(request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Реквизиты", callback_data=f"req_edit:fields:{request_id}")
    builder.button(text="🧾 Товары", callback_data=f"req_edit:items:{request_id}")
    builder.button(text="➕ Добавить товар", callback_data=f"req_edit:item_add:{request_id}")
    builder.button(text="⬅️ Назад", callback_data="settings:requests")
    builder.adjust(1)
    return builder.as_markup()


def request_fields_keyboard(request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Инициатор", callback_data=f"req_edit_field:{request_id}:initiator")
    builder.button(text="🏢 Подразделение", callback_data=f"req_edit_field:{request_id}:department")
    builder.button(text="🏷️ ЦФО (Бюджет)", callback_data=f"req_edit_field:{request_id}:cfo")
    builder.button(text="👔 МОЛ", callback_data=f"req_edit_field:{request_id}:mol")
    builder.button(text="📌 Статус", callback_data=f"req_edit_field:{request_id}:status")
    builder.button(text="🧑‍🔧 Исполнитель", callback_data=f"req_edit_field:{request_id}:executor")
    builder.button(text="🏭 Поставщик", callback_data=f"req_edit_field:{request_id}:supplier")
    builder.button(text="📅 Срок поставки", callback_data=f"req_edit_field:{request_id}:delivery")
    builder.button(text="⬅️ Назад", callback_data=f"req_edit:menu:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


def request_items_keyboard(
    request_id: int, items: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item_id, name in items:
        label = name or "-"
        builder.button(
            text=label,
            callback_data=f"req_item:{request_id}:{item_id}",
        )
    builder.button(text="➕ Добавить товар", callback_data=f"req_edit:item_add:{request_id}")
    builder.button(text="⬅️ Назад", callback_data=f"req_edit:menu:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


def request_item_fields_keyboard(request_id: int, item_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Наименование", callback_data=f"req_item_field:{request_id}:{item_id}:name")
    builder.button(text="Характеристики", callback_data=f"req_item_field:{request_id}:{item_id}:specs")
    builder.button(text="Марка/аналог", callback_data=f"req_item_field:{request_id}:{item_id}:brand")
    builder.button(text="Количество", callback_data=f"req_item_field:{request_id}:{item_id}:qty")
    builder.button(text="Ед.", callback_data=f"req_item_field:{request_id}:{item_id}:unit")
    builder.button(text="Ссылка", callback_data=f"req_item_field:{request_id}:{item_id}:link")
    builder.button(text="Примечание", callback_data=f"req_item_field:{request_id}:{item_id}:note")
    builder.button(text="Макс. цена", callback_data=f"req_item_field:{request_id}:{item_id}:max_price")
    builder.button(
        text="Ответственный ОМТС",
        callback_data=f"req_item_field:{request_id}:{item_id}:omts",
    )
    builder.button(
        text="Категория",
        callback_data=f"req_item_field:{request_id}:{item_id}:category",
    )
    builder.button(
        text="Статья ДДС",
        callback_data=f"req_item_field:{request_id}:{item_id}:dds",
    )
    builder.button(text="🗑️ Удалить", callback_data=f"req_item_field:{request_id}:{item_id}:delete")
    builder.button(text="⬅️ Назад", callback_data=f"req_edit:items:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


def request_status_keyboard(items: list[tuple[int, str]], request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for status_id, name in items:
        builder.button(
            text=name,
            callback_data=f"req_status:{request_id}:{status_id}",
        )
    builder.button(text="⬅️ Назад", callback_data=f"req_edit:fields:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


def export_edit_keyboard(report_type: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data=f"export_edit:{report_type}:yes")
    builder.button(text="❌ Нет", callback_data=f"export_edit:{report_type}:no")
    builder.adjust(2)
    return builder.as_markup()
