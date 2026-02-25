from aiogram import Router

from app.bot.access import AccessByUsernameMiddleware
from app.bot.handlers import admin, approvals, archive, common, executor, requests


def setup_router() -> Router:
    router = Router()
    access = AccessByUsernameMiddleware()
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)
    router.include_router(requests.router)
    router.include_router(approvals.router)
    router.include_router(executor.router)
    router.include_router(archive.router)
    router.include_router(admin.router)
    router.include_router(common.router)
    return router
