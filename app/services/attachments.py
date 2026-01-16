from sqlalchemy import inspect

from app.db.models import Attachment, Request


def build_item_label_map(request: Request) -> dict[int | None, str]:
    state = inspect(request)
    if "items" in state.unloaded:
        if request.item_name:
            return {None: f"1. {request.item_name or '-'}"}
        return {}
    labels: dict[int | None, str] = {}
    for idx, item in enumerate(request.items, start=1):
        labels[item.id] = f"{idx}. {item.name or '-'}"
    if None not in labels:
        if labels:
            labels[None] = next(iter(labels.values()))
        elif request.item_name:
            labels[None] = f"1. {request.item_name or '-'}"
    return labels


def build_photo_groups(request: Request) -> list[tuple[str, list[Attachment]]]:
    state = inspect(request)
    items = [] if "items" in state.unloaded else list(request.items or [])
    titles: list[tuple[int | None, str]] = []
    if items:
        for idx, item in enumerate(items, start=1):
            name = item.name or "-"
            titles.append((item.id, f"Товар {idx} {name}:"))
    else:
        name = request.item_name or "-"
        titles.append((None, f"Товар 1 {name}:"))

    first_item_id = items[0].id if items else None
    photos_by_item: dict[int | None, list[Attachment]] = {}
    attachments = [] if "attachments" in state.unloaded else list(request.attachments or [])
    for att in attachments:
        if att.file_type != "photo":
            continue
        item_id = att.item_id if att.item_id is not None else first_item_id
        photos_by_item.setdefault(item_id, []).append(att)

    groups: list[tuple[str, list[Attachment]]] = []
    for item_id, title in titles:
        photos = photos_by_item.get(item_id) or []
        if not photos:
            continue
        groups.append((title, photos[:3]))
    return groups
