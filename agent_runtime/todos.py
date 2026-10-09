from __future__ import annotations
import threading
from .errors import RuntimeFault

STATUSES = {"pending", "in_progress", "completed"}


class TodoStore:
    """Persistent task list (TodoWrite-style) backed by the state file.

    The agent keeps its task list here instead of only in context,
    so progress survives server restarts and stays visible across
    long sessions. Ids are stable integers; the store is best-effort
    and degrades to in-memory when no state file is configured.
    """

    def __init__(self, state_store):
        self._store = state_store
        self._lock = threading.Lock()
        self._items: list[dict] = []
        if state_store is not None:
            saved = state_store.load().get("todos")
            if isinstance(saved, list):
                self._items = [
                    dict(item) for item in saved
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), int)
                    and isinstance(item.get("title"), str)
                    and item.get("status") in STATUSES
                ]

    def _persist(self):
        if self._store is not None:
            self._store.save_todos([dict(item) for item in self._items])

    def _next_id(self):
        return max((item["id"] for item in self._items), default=0) + 1

    def get(self):
        with self._lock:
            return [dict(item) for item in self._items]

    def add(self, title):
        with self._lock:
            item = {"id": self._next_id(), "title": title, "status": "pending"}
            self._items.append(item)
            self._persist()
            return dict(item)

    def update(self, todo_id, title=None, status=None):
        if status is not None and status not in STATUSES:
            raise RuntimeFault("invalid_arguments",
                               f"status must be one of: {', '.join(sorted(STATUSES))}")
        with self._lock:
            for item in self._items:
                if item["id"] != todo_id:
                    continue
                if title is not None:
                    item["title"] = title
                if status is not None:
                    item["status"] = status
                self._persist()
                return dict(item)
        raise RuntimeFault("todo_not_found", f"Unknown todo id: {todo_id}")

    def delete(self, todo_id):
        with self._lock:
            for index, item in enumerate(self._items):
                if item["id"] != todo_id:
                    continue
                del self._items[index]
                self._persist()
                return {"id": todo_id, "deleted": True}
        raise RuntimeFault("todo_not_found", f"Unknown todo id: {todo_id}")

    def clear(self):
        with self._lock:
            count = len(self._items)
            self._items = []
            self._persist()
            return {"cleared": count}
