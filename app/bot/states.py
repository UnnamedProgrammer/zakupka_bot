from aiogram.fsm.state import State, StatesGroup


class RequestCreate(StatesGroup):
    full_name = State()
    department = State()
    cfo = State()
    description_method = State()
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
