"""Low-level generic access to kit_records. Kernel code should use Knowledge, not this."""

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.loader import search_text
from app.knowledge.models import KitRecord


class Store:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, kind: str, key: str) -> Any | None:
        return await self.session.scalar(
            select(KitRecord.payload).where(KitRecord.kind == kind, KitRecord.key == key)
        )

    async def all(self, kind: str) -> list[Any]:
        # Kit order is meaningful (catalog order in the prompt), and keys like SC01..SC40 sort in it.
        rows = await self.session.scalars(
            select(KitRecord.payload).where(KitRecord.kind == kind).order_by(KitRecord.key)
        )
        return list(rows)

    async def keys(self, kind: str) -> list[str]:
        rows = await self.session.scalars(
            select(KitRecord.key).where(KitRecord.kind == kind).order_by(KitRecord.key)
        )
        return list(rows)

    async def items(self, kind: str, key_prefix: str = "") -> list[tuple[str, Any]]:
        stmt = select(KitRecord.key, KitRecord.payload).where(KitRecord.kind == kind)
        if key_prefix:
            stmt = stmt.where(KitRecord.key.startswith(key_prefix, autoescape=True))
        return [(k, p) for k, p in await self.session.execute(stmt.order_by(KitRecord.key))]

    async def find(self, kind: str, **fields: str) -> list[Any]:
        """Exact match on top-level payload fields, e.g. find("client", phone="+7701...")."""
        stmt = select(KitRecord.payload).where(KitRecord.kind == kind)
        for field, value in fields.items():
            stmt = stmt.where(KitRecord.payload[field].astext == value)
        return list(await self.session.scalars(stmt.order_by(KitRecord.key)))

    async def put(self, kind: str, key: str, payload: Any, *, commit: bool = True) -> None:
        stmt = insert(KitRecord).values(
            kind=kind, key=key, payload=payload, search_text=search_text(payload), origin="user"
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[KitRecord.kind, KitRecord.key],
            set_={
                "payload": stmt.excluded.payload,
                "search_text": stmt.excluded.search_text,
                "origin": "user",
            },
        )
        await self.session.execute(stmt)
        if commit:
            await self.session.commit()
            if kind in ("kb", "office", "clinic", "inspection_point"):
                from app.knowledge.rag import schedule_reindex

                schedule_reindex(self.session.bind)

    async def delete(self, kind: str, key: str) -> bool:
        result = await self.session.execute(
            delete(KitRecord).where(KitRecord.kind == kind, KitRecord.key == key)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def kinds(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(KitRecord.kind, func.count()).group_by(KitRecord.kind).order_by(KitRecord.kind)
        )
        return {kind: n for kind, n in rows}
