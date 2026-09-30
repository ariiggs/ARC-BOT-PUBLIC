"""Compatibility exports for integrations using the original flat module path."""

from arc_bot.storage.slot_storage import SlotStateStore, SlotStorageError

__all__ = ["SlotStateStore", "SlotStorageError"]