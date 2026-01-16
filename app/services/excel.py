from io import BytesIO
from typing import Iterable

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from app.db.models import Attachment, Request, RequestItem


def build_daily_requests_xlsx(requests: Iterable[Request]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Ежедневные заявки"
    ws.append(
        [
            "ID",
            "Дата",
            "Инициатор",
            "Подразделение",
            "ЦФО",
            "Наименование",
            "Количество",
            "Ед.",
            "Статус",
            "Исполнитель",
        ]
    )
    for req in requests:
        ws.append(
            [
                req.id,
                req.created_at.strftime("%Y-%m-%d"),
                req.initiator.full_name or "",
                req.department.name,
                req.cfo.name,
                req.item_name or "",
                req.item_qty or "",
                req.item_unit or "",
                req.status.name,
                req.executor.full_name if req.executor else "",
            ]
        )
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


def build_employee_stats_xlsx(requests: Iterable[Request]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Статистика сотрудников"
    ws.append(
        [
            "ID",
            "Инициатор",
            "Исполнитель",
            "Статус",
            "Дата создания",
            "Дата выполнения",
        ]
    )
    for req in requests:
        ws.append(
            [
                req.id,
                req.initiator.full_name or "",
                req.executor.full_name if req.executor else "",
                req.status.name,
                req.created_at.strftime("%Y-%m-%d"),
                req.updated_at.strftime("%Y-%m-%d"),
            ]
        )
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _load_photo_paths(attachments: Iterable[Attachment]) -> list[str]:
    paths = []
    for att in attachments:
        if att.file_type != "photo":
            continue
        if not att.file_path:
            continue
        paths.append(att.file_path)
    return paths


def build_request_xlsx(
    request: Request, items: Iterable[RequestItem], attachments: Iterable[Attachment]
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = f"Заявка {request.id}"

    header_fill = PatternFill("solid", fgColor="D9E1F2")
    section_fill = PatternFill("solid", fgColor="F2F2F2")
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="D0D0D0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    created_at = request.created_at.strftime("%Y-%m-%d %H:%M") if request.created_at else ""
    ws.append(
        [
            "Заявка №",
            "Дата создания",
            "Инициатор",
            "Подразделение",
            "ЦФО",
            "МОЛ",
            "Статус",
        ]
    )
    ws.append(
        [
            request.id,
            created_at,
            request.initiator.full_name if request.initiator else "",
            request.department.name if request.department else "",
            request.cfo.name if request.cfo else "",
            request.mol_full_name or "",
            request.status.name if request.status else "",
        ]
    )
    ws.append([])

    ws.append(
        [
            "№",
            "Наименование",
            "Характеристики",
            "Марка/аналог",
            "Количество",
            "Ед.",
            "Ссылка",
            "Примечание",
            "Фото",
        ]
    )
    header_row = ws.max_row
    for col in range(1, 10):
        cell = ws.cell(row=header_row, column=col)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border

    item_list = list(items)
    photo_paths = _load_photo_paths(attachments)
    if not item_list:
        ws.append(
            [
                1,
                request.item_name or "",
                request.item_specs or "",
                request.item_brand or "",
                request.item_qty or "",
                request.item_unit or "",
                request.item_link or "-",
                request.item_note or "",
                "-",
            ]
        )
    else:
        for idx, item in enumerate(item_list, start=1):
            ws.append(
                [
                    idx,
                    item.name or "",
                    item.specs or "",
                    item.brand or "",
                    item.qty or "",
                    item.unit or "",
                    item.link or "-",
                    item.note or "",
                    "-",
                ]
            )
    for row in ws.iter_rows(min_row=header_row + 1, max_col=9):
        for cell in row:
            cell.alignment = wrap
            cell.border = border

    for col in range(1, 8):
        cell = ws.cell(row=1, column=col)
        cell.font = bold
        cell.fill = section_fill
        cell.alignment = center
        cell.border = border
        ws.cell(row=2, column=col).alignment = wrap
        ws.cell(row=2, column=col).border = border

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 32
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 8
    ws.column_dimensions["G"].width = 26
    ws.column_dimensions["H"].width = 28
    ws.column_dimensions["I"].width = 18

    if photo_paths:
        max_photos = min(len(photo_paths), max(1, len(item_list)))
        for idx in range(max_photos):
            row_idx = header_row + 1 + idx
            try:
                img = XLImage(photo_paths[idx])
            except Exception:
                continue
            ws.cell(row=row_idx, column=9).value = ""
            img.width = 120
            img.height = 120
            ws.row_dimensions[row_idx].height = 90
            ws.add_image(img, f"I{row_idx}")

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()
