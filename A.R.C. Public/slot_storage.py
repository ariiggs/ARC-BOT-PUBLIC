"""Atomic local storage for the small, single-process Discord bot."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class SlotStorageError(RuntimeError):
    """The durable state cannot safely be read or written."""


class SlotStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bot_state "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)"
            )
            connection.commit()
        except Exception:
            connection.close()
            raise
        return connection

    def load(self) -> dict | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT payload FROM bot_state WHERE id = 1"
                ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            if not isinstance(payload, dict):
                raise ValueError("The saved state must be an object.")
            return payload
        except (OSError, sqlite3.Error, ValueError) as error:
            raise SlotStorageError(
                "Unable to read the saved slot state; startup was stopped."
            ) from error

    def save(self, state: dict) -> None:
        try:
            payload = json.dumps(state, ensure_ascii=False)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO bot_state (id, payload) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                    (payload,),
                )
        except (OSError, sqlite3.Error, ValueError) as error:
            raise SlotStorageError(
                "Unable to save the slot state; the action was not recorded."
            ) from error