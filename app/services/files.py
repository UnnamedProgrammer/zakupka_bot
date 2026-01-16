import os
import uuid

from aiogram import Bot


async def save_telegram_file(bot: Bot, file_id: str, dest_dir: str, filename_hint: str | None) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    file = await bot.get_file(file_id)
    ext = ""
    if filename_hint and "." in filename_hint:
        ext = "." + filename_hint.rsplit(".", 1)[1]
    if not ext and file.file_path:
        _, path_ext = os.path.splitext(file.file_path)
        ext = path_ext
    target_name = f"{uuid.uuid4().hex}{ext}"
    target_path = os.path.join(dest_dir, target_name)
    await bot.download_file(file.file_path, destination=target_path)
    return target_path


def save_bytes_file(content: bytes, dest_dir: str, filename_hint: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    _, ext = os.path.splitext(filename_hint)
    ext = ext or ".bin"
    target_name = f"{uuid.uuid4().hex}{ext}"
    target_path = os.path.join(dest_dir, target_name)
    with open(target_path, "wb") as handle:
        handle.write(content)
    return target_path
