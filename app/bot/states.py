from aiogram.fsm.state import State, StatesGroup


class RequestCreate(StatesGroup):
    menu = State()
    excel_file = State()
    text_input = State()
    item_attachment = State()


class TemplateDownload(StatesGroup):
    department = State()
    cfo = State()


class ApprovalComment(StatesGroup):
    comment = State()


class LeaderComment(StatesGroup):
    comment = State()


class ExecutorComment(StatesGroup):
    comment = State()


class ExecutorFile(StatesGroup):
    file = State()


class ExecutorDeliveryDate(StatesGroup):
    date = State()


class ArchiveFilter(StatesGroup):
    menu = State()
    initiator_input = State()
    item_input = State()
    supplier_input = State()


class AdminAddDepartment(StatesGroup):
    name = State()


class AdminAddCfo(StatesGroup):
    name = State()


class AdminAssignRole(StatesGroup):
    username = State()
    role = State()


class AdminAddUser(StatesGroup):
    full_name = State()
    tg_username = State()
    role = State()


class AdminEditRequest(StatesGroup):
    request_id = State()
    field_value = State()
    item_value = State()
    item_add_name = State()
    item_add_specs = State()
    item_add_brand = State()
    item_add_qty = State()
    item_add_unit = State()
    item_add_link = State()
    item_add_note = State()
    item_add_max_price = State()
    item_add_omts = State()
    item_add_category = State()
    item_add_dds = State()


class ExportReportEdit(StatesGroup):
    confirm = State()
    file = State()


class ArchiveEdit(StatesGroup):
    confirm = State()
    file = State()
