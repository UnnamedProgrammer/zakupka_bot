from sqlalchemy import inspect, select

from app.db.models import Attachment, Request, RequestItem


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


def build_photo_groups_from(
    request: Request,
    items: list[RequestItem],
    attachments: list[Attachment],
) -> list[tuple[str, list[Attachment]]]:
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


def build_attachment_groups_from(
    request: Request,
    items: list[RequestItem],
    attachments: list[Attachment],
) -> list[tuple[str, list[Attachment]]]:
    titles: list[tuple[int | None, str]] = []
    if items:
        for idx, item in enumerate(items, start=1):
            name = item.name or "-"
            titles.append((item.id, f"Товар {idx} {name}:"))
    else:
        name = request.item_name or "-"
        titles.append((None, f"Товар 1 {name}:"))

    first_item_id = items[0].id if items else None
    known_item_ids = {item_id for item_id, _ in titles}
    grouped: dict[int | None, list[Attachment]] = {}
    orphan: list[Attachment] = []

    for att in attachments:
        item_id = att.item_id if att.item_id is not None else first_item_id
        if item_id in known_item_ids:
            grouped.setdefault(item_id, []).append(att)
        else:
            orphan.append(att)

    result: list[tuple[str, list[Attachment]]] = []
    for item_id, title in titles:
        bucket = grouped.get(item_id) or []
        if bucket:
            result.append((title, bucket))
    if orphan:
        result.append(("Без привязки к товару:", orphan))
    return result


async def fetch_request_media(session, request_id: int) -> tuple[list[RequestItem], list[Attachment]]:
    items = (
        await session.scalars(
            select(RequestItem)
            .where(RequestItem.request_id == request_id)
            .order_by(RequestItem.id)
        )
    ).all()
    attachments = (
        await session.scalars(
            select(Attachment)
            .where(Attachment.request_id == request_id)
            .order_by(Attachment.id)
        )
    ).all()
    return items, attachments
