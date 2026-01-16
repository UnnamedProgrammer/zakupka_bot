from sqlalchemy import inspect

from app.db.models import Request


def _get_loaded(obj, attr: str):
    state = inspect(obj)
    if attr in state.unloaded:
        return None
    return getattr(obj, attr)


def format_request_summary(req: Request) -> str:
    initiator = _get_loaded(req, "initiator")
    department = _get_loaded(req, "department")
    cfo = _get_loaded(req, "cfo")
    status = _get_loaded(req, "status")
    items = _get_loaded(req, "items") or []

    username = initiator.tg_username if initiator else ""
    username_display = f" ({username})" if username else ""
    initiator_name = initiator.full_name if initiator else "не указано"
    department_name = department.name if department else "-"
    cfo_name = cfo.name if cfo else "-"
    status_name = status.name if status else "-"
    created_at = req.created_at.strftime("%d-%m-%Y %H:%M") if req.created_at else "-"
    done_at = req.done_at.strftime("%d-%m-%Y %H:%M") if req.done_at else "-"
    lines = [
        f"Заявка №{req.id}",
        f"Дата создания: {created_at}",
        f"Дата выполнения: {done_at}",
        f"Инициатор: {initiator_name}{username_display}",
        f"Подразделение: {department_name}",
        f"ЦФО: {cfo_name}",
        f"МОЛ: {req.mol_full_name or '-'}",
        f"Статус: {status_name}",
    ]
    if items:
        lines.append("Товары:")
        for idx, item in enumerate(items, start=1):
            qty_line = f"{item.qty or '-'} {item.unit or ''}".strip()
            lines.append(f"{idx}. {item.name or '-'}")
            lines.append(f"   Характеристики: {item.specs or '-'}")
            lines.append(f"   Марка/аналог: {item.brand or '-'}")
            lines.append(f"   Количество: {qty_line}")
            lines.append(f"   Ссылка: {item.link or '-'}")
            lines.append(f"   Примечание: {item.note or '-'}")
            if idx != len(items):
                lines.append("")
    else:
        lines.extend(
            [
                f"Описание: {req.item_name or '-'}",
                f"Характеристики: {req.item_specs or '-'}",
                f"Марка/аналог: {req.item_brand or '-'}",
                f"Количество: {req.item_qty or '-'} {req.item_unit or ''}".strip(),
                f"Ссылка: {req.item_link or '-'}",
                f"Примечание: {req.item_note or '-'}",
            ]
        )
    return "\n".join(lines)
