from aiogram.fsm.state import State, StatesGroup


class RequestCreate(StatesGroup):
    method = State()
    full_name = State()
    department = State()
    cfo = State()
    description_method = State()
    excel_file = State()
    item_name = State()
    item_specs = State()
    item_brand = State()
    item_qty = State()
    item_unit = State()
    item_link_or_photo = State()
    item_note = State()
    item_add_more = State()
    mol_full_name = State()
    approver_choice = State()
    attachments = State()


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
    status = State()
    initiator = State()
    name = State()
    supplier = State()
    date_from = State()
    date_to = State()


class AdminAddDepartment(StatesGroup):
    name = State()


class AdminAddCfo(StatesGroup):
    name = State()


class AdminAssignRole(StatesGroup):
    username = State()
    role = State()


class AdminAddUser(StatesGroup):
    full_name = State()
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
