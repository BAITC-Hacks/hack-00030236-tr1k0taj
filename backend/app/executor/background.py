"""Фоновый помощник по умолчанию: выключен."""

from app.context import SessionContext


class NoopBackground:
    name = "off"

    def schedule(self, snapshot: SessionContext, turn_id: int) -> None:
        return None
