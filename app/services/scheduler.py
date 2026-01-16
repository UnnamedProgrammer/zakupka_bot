import asyncio
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import Request, RequestStatus
from app.db.session import SessionLocal
from app.services.constants import REQUEST_STATUS_DONE, REQUEST_STATUS_REJECTED
from app.services.notifications import send_to_user


async def delivery_notifier(bot) -> None:
    while True:
        try:
            await _process_delivery_notifications(bot)
        except Exception:
            logging.exception("Delivery notifier failed")
        await asyncio.sleep(3600)


async def _process_delivery_notifications(bot) -> None:
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    async with SessionLocal() as session:
        done_id = await session.scalar(
            select(RequestStatus.id).where(RequestStatus.code == REQUEST_STATUS_DONE)
        )
        rejected_id = await session.scalar(
            select(RequestStatus.id).where(RequestStatus.code == REQUEST_STATUS_REJECTED)
        )
        query = (
            select(Request)
            .where(Request.expected_delivery_at == tomorrow)
            .where(Request.delivery_notified_at.is_(None))
            .options(selectinload(Request.executor))
        )
        if done_id:
            query = query.where(Request.status_id != done_id)
        if rejected_id:
            query = query.where(Request.status_id != rejected_id)

        rows = await session.execute(query)
        requests = rows.scalars().all()
        for req in requests:
            if req.executor:
                await send_to_user(
                    bot,
                    req.executor,
                    f"Планируемая поставка ожидается: {req.expected_delivery_at}",
                )
                req.delivery_notified_at = dt.datetime.utcnow()
        await session.commit()
