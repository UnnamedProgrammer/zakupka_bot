from aiogram import Router

from app.bot.handlers import admin, approvals, archive, common, executor, requests


def setup_router() -> Router:
    router = Router()
    router.include_router(requests.router)
    router.include_router(approvals.router)
    router.include_router(executor.router)
    router.include_router(archive.router)
    router.include_router(admin.router)
    router.include_router(common.router)
    return router
