"""Small bounded cache for source metadata/files and concurrent identical fetches."""

import time
from collections import OrderedDict
from concurrent.futures import Future
from threading import RLock


class FetchCache:
    def __init__(self, limit: int, ttl: float = 3600):
        self.limit = limit
        self.ttl = ttl
        self._values = OrderedDict()
        self._pending = {}
        self._lock = RLock()

    def values(self):
        """Snapshot fresh values for reuse of embedded source structures."""
        with self._lock:
            now = time.monotonic()
            return [value for stamp, value in self._values.values() if now - stamp < self.ttl]

    def get(self, key, load, *, refresh=False):
        """Return (value, reused); refresh joins an already running fresh fetch."""
        with self._lock:
            pending = self._pending.get(key)
            cached = self._values.get(key)
            if (
                pending is None
                and cached
                and not refresh
                and time.monotonic() - cached[0] < self.ttl
            ):
                self._values.move_to_end(key)
                return cached[1], True
            owner = pending is None
            if owner:
                pending = self._pending[key] = Future()
        if not owner:
            return pending.result(), True
        try:
            value = load()
        except BaseException as exc:
            with self._lock:
                pending.set_exception(exc)
                del self._pending[key]
            raise
        with self._lock:
            self._values[key] = (time.monotonic(), value)
            self._values.move_to_end(key)
            while len(self._values) > self.limit:
                self._values.popitem(last=False)
            pending.set_result(value)
            del self._pending[key]
        return value, False
