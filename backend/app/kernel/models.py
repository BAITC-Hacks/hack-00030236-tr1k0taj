"""Compatibility name only: kernel persists through context.sessions/board_entries."""

from app.context import SessionRecord as KernelSession

__all__ = ["KernelSession"]
