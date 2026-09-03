"""Process-local coordination for project analysis persistence."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Iterator


_registry_lock = threading.Lock()
_project_locks: dict[int, threading.RLock] = {}


@contextmanager
def project_analysis_lock(project_id: int) -> Iterator[None]:
    """Serialize finding/zone/summary writes for one project.

    Expensive YOLO and LLM inference stays parallel; only the persistence phase
    is serialized so duplicate checks and read-modify-write summaries are safe.
    """
    key = int(project_id)
    with _registry_lock:
        lock = _project_locks.setdefault(key, threading.RLock())
    with lock:
        yield
