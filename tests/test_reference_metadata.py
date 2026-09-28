"""Reference metadata freshness, independent refresh and concurrent source reuse."""

import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import patch

import httpx

from ausdata_mcp import macro_data, server
from ausdata_mcp.fetch_cache import FetchCache


class ReferenceMetadataTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(macro_data, "_reference_cache", FetchCache(limit=8))
        cache.start()
        self.addCleanup(cache.stop)

    @staticmethod
    def reply(url, **kwargs):
        kind = url.rsplit("/", 1)[-1]
        if kind == "country":
            data = [{"pages": 1}, [{"id": "AUS", "iso2Code": "AU", "name": "Australia"}]]
        elif kind in {"countries", "regions", "groups"}:
            data = {kind: {"AUS": {"label": "Australia"}} if kind == "countries" else {}}
        else:
            data = {"results": [{"id": "36", "text": "Australia"}]}
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    def test_tool_refresh_is_limited_to_requested_source_or_list(self):
        with (
            patch.object(macro_data.source_http, "get", side_effect=self.reply) as get,
            patch.object(
                server,
                "_route_entry",
                side_effect=lambda dataset: {"providerKey": dataset.split("::")[0]},
            ),
        ):

            def browse(dataset, dimension, refresh=False):
                return server.get_metadata(dataset, dimension=dimension, forceRefresh=refresh)

            browse("worldbank::TEST", "AREA")
            browse("imf::TEST", "AREA")
            browse("worldbank::TEST", "AREA", True)
            browse("imf::TEST", "AREA")
            self.assertEqual(get.call_count, 5)
            browse("comtrade::goods_trade", "REPORTER")
            browse("comtrade::goods_trade", "PARTNER")
            browse("comtrade::goods_trade", "REPORTER", True)
            browse("comtrade::goods_trade", "SECOND_PARTNER")
            self.assertEqual(get.call_count, 8)
            # Refreshing an alias updates the same underlying partner list.
            browse("comtrade::goods_trade", "SECOND_PARTNER", True)
            browse("comtrade::goods_trade", "PARTNER")
            self.assertEqual(get.call_count, 9)

    def test_reference_expiry_and_endpoint_changes_do_not_serve_old_entries(self):
        with (
            patch("ausdata_mcp.fetch_cache.time.monotonic", return_value=100) as clock,
            patch.object(macro_data.source_http, "get", side_effect=self.reply) as get,
        ):
            macro_data._area_codes("worldbank")
            macro_data._live_comtrade_codes("REPORTER")
            clock.return_value = 3699
            macro_data._area_codes("worldbank")
            macro_data._live_comtrade_codes("REPORTER")
            self.assertEqual(get.call_count, 2)
            clock.return_value = 3700
            macro_data._area_codes("worldbank")
            macro_data._live_comtrade_codes("REPORTER")
            self.assertEqual(get.call_count, 4)
            with patch.object(macro_data.settings, "worldbank_base_url", "https://other.test"):
                macro_data._area_codes("worldbank")
            self.assertEqual(get.call_count, 5)

    def test_concurrent_area_requests_share_one_fetch(self):
        entered, waiting, release = (threading.Event() for _ in range(3))

        class ObservedFuture(Future):
            def result(self, timeout=None):
                waiting.set()
                return super().result(timeout)

        def reply(url, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("request was not released")
            return self.reply(url, **kwargs)

        with (
            patch("ausdata_mcp.fetch_cache.Future", ObservedFuture),
            patch.object(macro_data.source_http, "get", side_effect=reply) as get,
            ThreadPoolExecutor(2) as pool,
        ):
            first = pool.submit(macro_data._area_codes, "worldbank")
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(macro_data._area_codes, "worldbank", refresh=True)
                self.assertTrue(waiting.wait(2))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2), second.result(timeout=2))
            get.assert_called_once()
