"""Bounded caching and duplicate request coordination without network access."""

import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import Mock, patch

from ausdata_mcp.fetch_cache import FetchCache


class FetchCacheTests(unittest.TestCase):
    def test_expiry_lru_refresh_and_fresh_snapshot(self):
        cache = FetchCache(limit=2, ttl=10)
        with patch("ausdata_mcp.fetch_cache.time.monotonic", return_value=100) as clock:
            self.assertEqual(cache.get("a", lambda: "A"), ("A", False))
            cache.get("b", lambda: "B")
            self.assertEqual(cache.get("a", lambda: self.fail("cache miss")), ("A", True))
            cache.get("c", lambda: "C")
            self.assertEqual(cache.values(), ["A", "C"])
            self.assertEqual(cache.get("b", lambda: "new B"), ("new B", False))
            self.assertEqual(cache.get("b", lambda: "fresh B", refresh=True), ("fresh B", False))
            clock.return_value = 111
            self.assertEqual(cache.values(), [])
            self.assertEqual(cache.get("b", lambda: "expired B"), ("expired B", False))

    def test_same_key_waits_other_keys_proceed_and_errors_can_retry(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                cache = FetchCache(limit=2)
                entered, release, waiting = (threading.Event() for _ in range(3))

                class ObservedFuture(Future):
                    def result(self, timeout=None, waiting=waiting):
                        waiting.set()
                        return super().result(timeout)

                def load(entered=entered, release=release, fail=fail):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("loader was not released")
                    if fail:
                        raise ValueError("source unavailable")
                    return "value"

                loader = Mock(side_effect=load)
                with (
                    patch("ausdata_mcp.fetch_cache.Future", ObservedFuture),
                    ThreadPoolExecutor(2) as pool,
                ):
                    first = pool.submit(cache.get, "same", loader)
                    try:
                        self.assertTrue(entered.wait(2))
                        second = pool.submit(cache.get, "same", loader, refresh=True)
                        self.assertTrue(waiting.wait(2))
                        self.assertEqual(
                            cache.get("other", lambda: "independent"), ("independent", False)
                        )
                    finally:
                        release.set()
                    if fail:
                        for future in (first, second):
                            with self.assertRaisesRegex(ValueError, "source unavailable"):
                                future.result(timeout=2)
                        self.assertEqual(
                            cache.get("same", lambda: "recovered"), ("recovered", False)
                        )
                    else:
                        self.assertEqual(first.result(timeout=2), ("value", False))
                        self.assertEqual(second.result(timeout=2), ("value", True))
                loader.assert_called_once()
