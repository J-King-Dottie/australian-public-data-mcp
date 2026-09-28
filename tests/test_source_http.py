"""Deterministic transport tests: no provider network and no real backoff sleeps."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from ausdata_mcp import source_http


class SourceHTTPTests(unittest.TestCase):
    def setUp(self):
        source_http._hosts.clear()
        self.addCleanup(source_http._hosts.clear)
        self.clock = 100.0
        patches = [
            patch.object(source_http.time, "monotonic", side_effect=lambda: self.clock),
            patch.object(source_http.time, "sleep", side_effect=self.advance),
            patch.object(source_http, "uniform", return_value=1.0),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def advance(self, seconds):
        self.clock += seconds

    def test_retry_after_parses_seconds_dates_and_invalid_headers(self):
        now = datetime(2026, 9, 28, tzinfo=UTC)
        for header, expected in [
            ("30", 30),
            ("0", 0),
            ("Mon, 28 Sep 2026 00:00:10 GMT", 10),
            ("Sun, 27 Sep 2026 00:00:00 GMT", 0),
            ("bad", None),
            ("-3", None),
            ("1.5", None),
            ("9" * 1000, None),
            ("²", None),
        ]:
            with self.subTest(header=header):
                self.assertEqual(source_http.retry_after_seconds(header, now=now), expected)

    def test_zero_delay_limit_retries_once_with_identical_selection(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                429 if len(requests) == 1 else 200, headers={"Retry-After": "0"}, json={"ok": True}
            )

        with httpx.Client(
            base_url="https://source.test", transport=httpx.MockTransport(handler)
        ) as client:
            response = source_http.get(
                "/data", client=client, params={"AREA": "A+B", "start": "2020"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url, requests[1].url)
        self.assertEqual(self.clock, 101.0)

    def test_long_cooldown_is_shared_across_calls_but_not_other_hosts(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(
                429 if request.url.host == "busy.test" else 200, headers={"Retry-After": "30"}
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            reply = source_http.get("https://busy.test/a", client=client)
            self.assertEqual(reply.status_code, 429)
            with self.assertRaises(source_http.ProviderCooldown) as caught:
                source_http.get("https://busy.test/b", client=client)
            self.assertEqual(caught.exception.retry_after_seconds, 30)
            self.assertEqual(source_http.get("https://free.test/a", client=client).status_code, 200)
        self.assertEqual(calls, ["busy.test", "free.test"])
        self.assertEqual(self.clock, 100)

    def test_short_cooldown_applies_to_later_requests(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "2"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            source_http.get("https://source.test/a", client=client)
            self.assertEqual(self.clock, 102)
            source_http.get("https://source.test/b", client=client)
            self.assertEqual(self.clock, 106)
        self.assertEqual(len(calls), 4)

    def test_nontransient_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                calls = []

                def handler(request, calls=calls, status=status):
                    calls.append(request)
                    return httpx.Response(status)

                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    reply = source_http.get("https://source.test/a", client=client)
                self.assertEqual(reply.status_code, status)
                self.assertEqual(len(calls), 1)
        self.assertEqual(self.clock, 100)

    def test_transient_statuses_and_network_errors_retry_at_most_once(self):
        for failure in (502, 503, 504, httpx.ReadTimeout, httpx.ConnectError):
            source_http._hosts.clear()
            calls = []

            def handler(request, calls=calls, failure=failure):
                calls.append(request)
                if isinstance(failure, int):
                    return httpx.Response(failure)
                raise failure("offline", request=request)

            with (
                self.subTest(failure=failure),
                httpx.Client(transport=httpx.MockTransport(handler)) as client,
            ):
                if isinstance(failure, int):
                    self.assertEqual(
                        source_http.get("https://source.test/a", client=client).status_code, failure
                    )
                else:
                    with self.assertRaises(failure):
                        source_http.get("https://source.test/a", client=client)
                self.assertEqual(len(calls), 2)

    def test_wait_budget_includes_existing_cooldown_and_retry(self):
        source_http._defer(source_http._state("source.test"), 4)
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503, headers={"Retry-After": "2"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            reply = source_http.get("https://source.test/a", client=client)
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.clock, 104)
        self.assertEqual(len(calls), 1)

    def test_host_concurrency_limit_allows_other_hosts_to_proceed(self):
        lock = threading.Lock()
        release = threading.Event()
        two_entered = threading.Event()
        other_entered = threading.Event()
        active = peak = 0

        def handler(request):
            nonlocal active, peak
            if request.url.host == "other.test":
                other_entered.set()
                return httpx.Response(200)
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    two_entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError("test did not release requests")
                return httpx.Response(200)
            finally:
                with lock:
                    active -= 1

        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
            ThreadPoolExecutor(max_workers=5) as pool,
        ):
            pending = [
                pool.submit(source_http.get, "https://source.test/data", client=client)
                for _ in range(4)
            ]
            try:
                self.assertTrue(two_entered.wait(2))
                other = pool.submit(source_http.get, "https://other.test/data", client=client)
                self.assertTrue(other_entered.wait(2))
                self.assertEqual(other.result(timeout=2).status_code, 200)
            finally:
                release.set()
            self.assertTrue(all(f.result(timeout=3).status_code == 200 for f in pending))
        self.assertEqual(peak, 2)

    def test_shared_client_reuse_request_isolation_and_shutdown(self):
        requests = []
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200)
            )
        )
        source_http.close()
        self.addCleanup(source_http.close)
        with patch.object(source_http.httpx, "Client", return_value=client) as create:
            source_http.get(
                "https://source.test/data",
                params={"AREA": "A"},
                headers={"X-Source-Key": "first"},
                timeout=20,
            )
            source_http.get("https://source.test/data", params={"AREA": "B"}, timeout=40)
            create.assert_called_once()
            self.assertFalse(client.is_closed)
            self.assertEqual(requests[0].url.params["AREA"], "A")
            self.assertEqual(requests[1].url.params["AREA"], "B")
            self.assertNotIn("X-Source-Key", requests[1].headers)
            self.assertEqual(requests[1].extensions["timeout"]["read"], 40)
            source_http.close()
            self.assertTrue(client.is_closed)
            self.assertIsNone(source_http._client)

    def test_caller_owned_client_is_not_closed_by_shared_shutdown(self):
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ) as client:
            source_http.get("https://source.test/data", client=client)
            source_http.close()
            self.assertFalse(client.is_closed)
