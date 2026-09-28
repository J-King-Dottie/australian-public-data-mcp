import json
import sqlite3
import tempfile
import unicodedata
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import httpx

from ausdata_mcp import catalog_refresh as refresh
from ausdata_mcp import catalog_sources as sources
from ausdata_mcp import unified_catalog as catalog
from ausdata_mcp.domestic_data import DomesticDataService
from ausdata_mcp.energy_workbook import discover_curation, extract_sheet_records


def entry(provider, suffix="one"):
    return {
        "datasetId": provider + "::" + suffix,
        "provider": provider,
        "title": "Population " + suffix,
        "description": "",
        "searchText": "population",
        "sourceUrl": "https://example.test",
        "route": "macro",
    }


class LiveCatalogueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (
            ("CATALOG_PATH", self.root / "catalog.json"),
            ("FTS_DB_PATH", self.root / "search.sqlite3"),
        ):
            patcher = patch.object(catalog, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        catalog._invalidate_caches()
        self.addCleanup(catalog._invalidate_caches)
        self.fetch = patch.object(
            refresh, "fetch_source", side_effect=lambda provider: [entry(provider)]
        )
        self.mock_fetch = self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def test_cold_parallel_fetch_then_cache_and_resumed_disk_cache(self):
        result = catalog.search_unified_catalog("population", limit=40)
        self.assertEqual(self.mock_fetch.call_count, len(sources.PROVIDERS))
        self.assertEqual({row["provider"] for row in result["candidates"]}, set(sources.PROVIDERS))
        self.assertTrue(result["catalogue"]["complete"])
        catalog.search_unified_catalog("population", provider="PDH")
        catalog._invalidate_caches()
        catalog.search_unified_catalog("population")
        self.assertEqual(self.mock_fetch.call_count, len(sources.PROVIDERS))

    def test_unicode_search_retains_source_words_and_equivalent_accents(self):
        titles = ["Électricité", "Côte", "水", "Population"]
        self.mock_fetch.side_effect = lambda provider: [
            {**entry(provider, str(i)), "title": title, "searchText": title}
            for i, title in enumerate(titles)
        ]
        for query, title in (
            ("ÉLECTRICITÉ", "Électricité"),
            ("electricite", "Électricité"),
            (unicodedata.normalize("NFD", "électricité"), "Électricité"),
            ("côte", "Côte"),
            ("水", "水"),
        ):
            with self.subTest(query=query):
                result = catalog.search_unified_catalog(query, provider="OECD")
                self.assertEqual([row["title"] for row in result["candidates"]], [title])
        self.assertEqual(catalog.search_unified_catalog("不存在", provider="OECD")["total"], 0)

    def test_unicode_queries_keep_pagination_and_provider_scope(self):
        self.mock_fetch.side_effect = lambda provider: [
            {**entry(provider, str(i)), "title": f"Électricité {i}", "searchText": "électricité"}
            for i in range(3)
        ]
        first = catalog.search_unified_catalog("électricité", provider="PDH", limit=2)
        second = catalog.search_unified_catalog(
            "électricité", provider="PDH", limit=2, offset=first["next_offset"]
        )
        self.assertEqual((first["total"], second["total"]), (3, 3))
        rows = first["candidates"] + second["candidates"]
        self.assertEqual(len({row["datasetId"] for row in rows}), 3)
        self.assertEqual({row["provider"] for row in rows}, {"Pacific Data Hub"})
        self.assertIsNone(second["next_offset"])

    def test_query_punctuation_is_literal_and_repeated_terms_do_not_change_results(self):
        expected = catalog.search_unified_catalog("population")
        for query in ('"population":*()', "population population", "POPULATION"):
            result = catalog.search_unified_catalog(query)
            self.assertEqual(result["candidates"], expected["candidates"])
        # The search builder never exposes caller-supplied FTS operators.
        self.assertEqual(catalog._match_query('水" OR (côte*)'), '"水"* OR "or"* OR "côte"*')

    def test_explicit_matching_modes_narrow_without_changing_provider_or_pagination(self):
        titles = ["Total population", "Population total", "Total exports", "Population estimates"]
        self.mock_fetch.side_effect = lambda provider: [
            {**entry(provider, str(i)), "title": title, "searchText": title}
            for i, title in enumerate(titles)
        ]
        broad = catalog.search_unified_catalog("total population", provider="OECD")
        self.assertEqual(broad["total"], 4)
        first = catalog.search_unified_catalog(
            "total population", provider="OECD", match="all", limit=1
        )
        second = catalog.search_unified_catalog(
            "total population", provider="OECD", match="all", limit=1, offset=first["next_offset"]
        )
        self.assertEqual(first["total"], 2)
        self.assertNotEqual(first["candidates"], second["candidates"])
        self.assertIsNone(second["next_offset"])
        phrase = catalog.search_unified_catalog("total population", provider="OECD", match="phrase")
        self.assertEqual([row["title"] for row in phrase["candidates"]], ["Total population"])
        self.assertEqual(phrase["match"], "phrase")
        absent = catalog.search_unified_catalog("population nonexistent", match="all")
        self.assertEqual(absent["total"], 0)

    def test_phrase_keeps_word_order_repetition_filler_words_and_literal_syntax(self):
        self.assertEqual(
            catalog._match_query('data data OR "water"', "phrase"), '"data data or water"'
        )
        self.assertEqual(catalog._match_query("SP.POP.TOTL", "phrase"), '"sp pop totl"')
        self.assertEqual(catalog._match_query("Côte 水", "all"), '"côte"* AND "水"*')
        with self.assertRaisesRegex(ValueError, "match must"):
            catalog.search_unified_catalog("population", match="typo")
        self.mock_fetch.assert_not_called()

    def test_concurrent_first_queries_only_fetch_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: catalog.search_unified_catalog("population"), range(8))
            )
        self.assertEqual(self.mock_fetch.call_count, len(sources.PROVIDERS))
        self.assertEqual(len({r["catalogue"]["generation"] for r in results}), 1)

    def test_source_fetches_run_concurrently(self):
        barrier = Barrier(len(sources.PROVIDERS))

        def fetch(provider):
            barrier.wait(timeout=5)
            return [entry(provider)]

        self.mock_fetch.side_effect = fetch
        self.assertTrue(catalog.search_unified_catalog("population")["catalogue"]["complete"])

    def test_empty_source_does_not_replace_last_good_entries(self):
        catalog.ensure_unified_catalog_artifacts()
        self.mock_fetch.side_effect = lambda provider: (
            [] if provider == "ABS" else [entry(provider)]
        )
        result = catalog.search_unified_catalog("population", force_refresh=True)
        self.assertEqual(result["catalogue"]["sources"]["ABS"]["status"], "stale")
        self.assertIsNotNone(catalog.get_unified_catalog_entry("ABS::one"))

    def test_force_refresh_replaces_entries_and_invalidates_lookup(self):
        catalog.search_unified_catalog("population")
        self.assertIsNotNone(catalog.get_unified_catalog_entry("ABS::one"))
        self.mock_fetch.side_effect = lambda provider: [entry(provider, "two")]
        catalog.search_unified_catalog("population", force_refresh=True)
        self.assertIsNone(catalog.get_unified_catalog_entry("ABS::one"))
        self.assertIsNotNone(catalog.get_unified_catalog_entry("ABS::two"))

    def test_expiry_refreshes_only_due_source(self):
        payload = catalog.ensure_unified_catalog_artifacts()
        payload["sources"]["ABS"]["refresh_after"] = 0
        self.mock_fetch.reset_mock()
        catalog.search_unified_catalog("population")
        self.mock_fetch.assert_called_once_with("ABS")

    def test_stale_fallback_disclosed_and_retry_only_failed_source(self):
        original = catalog.ensure_unified_catalog_artifacts()
        fetched_at = original["sources"]["ABS"]["fetched_at"]

        def fetch(provider):
            if provider == "ABS":
                raise httpx.ConnectError("offline")
            return [entry(provider, "new")]

        self.mock_fetch.side_effect = fetch
        result = catalog.search_unified_catalog("population", force_refresh=True)
        status = result["catalogue"]["sources"]["ABS"]
        self.assertEqual(status["status"], "stale")
        self.assertEqual(status["fetched_at"], fetched_at)
        self.assertTrue(result["warnings"])
        self.mock_fetch.reset_mock()
        catalog.search_unified_catalog("population")
        self.mock_fetch.assert_not_called()
        catalog._payload["sources"]["ABS"]["refresh_after"] = 0
        self.mock_fetch.side_effect = lambda provider: [entry(provider, "recovered")]
        result = catalog.search_unified_catalog("population")
        self.mock_fetch.assert_called_once_with("ABS")
        self.assertTrue(result["catalogue"]["complete"])

    def test_cold_partial_failure_and_explicit_provider_error(self):
        def fetch(provider):
            if provider == "OECD":
                raise httpx.ConnectError("offline")
            return [entry(provider)]

        self.mock_fetch.side_effect = fetch
        result = catalog.search_unified_catalog("population")
        self.assertFalse(result["catalogue"]["complete"])
        self.assertEqual(result["catalogue"]["sources"]["OECD"]["status"], "unavailable")
        with self.assertRaisesRegex(RuntimeError, "OECD catalogue unavailable"):
            catalog.search_unified_catalog("population", provider="OECD")

    def test_all_sources_fail_does_not_write_empty_catalogue(self):
        self.mock_fetch.side_effect = httpx.ConnectError("offline")
        with self.assertRaisesRegex(RuntimeError, "No live catalogues"):
            catalog.search_unified_catalog("population")
        self.assertFalse(catalog.CATALOG_PATH.exists())

    def test_all_failed_sources_respect_retry_interval(self):
        self.mock_fetch.side_effect = httpx.ConnectError("offline")
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "No live catalogues"):
                catalog.search_unified_catalog("population")
        self.assertEqual(self.mock_fetch.call_count, len(sources.PROVIDERS))
        catalog._payload["sources"]["ABS"]["refresh_after"] = 0
        self.mock_fetch.side_effect = lambda provider: [entry(provider)]
        result = catalog.search_unified_catalog("population")
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(self.mock_fetch.call_count, len(sources.PROVIDERS) + 1)

    def test_corrupt_snapshot_is_rebuilt(self):
        original = catalog.ensure_unified_catalog_artifacts()
        for field, value in (("generation", None), ("sources", []), ("entries", [{}])):
            catalog._invalidate_caches()
            catalog.CATALOG_PATH.write_text(json.dumps({**original, field: value}))
            self.mock_fetch.reset_mock()
            self.assertTrue(catalog.search_unified_catalog("population")["candidates"])
            self.assertEqual(self.mock_fetch.call_count, len(sources.PROVIDERS))

    def test_invalid_provider_does_not_fetch_any_catalogue(self):
        with self.assertRaises(ValueError):
            catalog.search_unified_catalog("population", provider="typo")
        self.mock_fetch.assert_not_called()

    def test_index_uri_handles_reserved_path_characters(self):
        with patch.object(catalog, "FTS_DB_PATH", self.root / "search?#.sqlite3"):
            self.assertTrue(catalog.search_unified_catalog("population")["candidates"])
            self.mock_fetch.reset_mock()
            self.assertTrue(catalog.search_unified_catalog("population")["candidates"])
            self.mock_fetch.assert_not_called()

    def test_missing_or_mismatched_index_repaired_offline(self):
        catalog.ensure_unified_catalog_artifacts()
        with sqlite3.connect(catalog.FTS_DB_PATH) as connection:
            connection.execute("UPDATE generation SET id='old'")
            connection.execute("DELETE FROM catalog")
        self.mock_fetch.reset_mock()
        self.assertTrue(catalog.search_unified_catalog("population")["candidates"])
        catalog.FTS_DB_PATH.unlink()
        self.assertTrue(catalog.search_unified_catalog("population")["candidates"])
        self.mock_fetch.assert_not_called()

    def test_invalid_provider_is_not_a_silent_empty_search(self):
        with self.assertRaisesRegex(ValueError, "Unknown provider"):
            catalog.search_unified_catalog("population", provider="typo")


class SourceCatalogueTests(unittest.TestCase):
    def test_legacy_energy_layout_still_reads_label_and_unit_columns(self):
        records = extract_sheet_records(
            "AUS FY",
            [
                {"B": "Table O1"},
                {"C": "2023-24", "D": "2024-25"},
                {"C": "GWh", "D": "GWh"},
                {"B": "Wind", "C": "100", "D": "120"},
            ],
            "test",
        )
        self.assertEqual(records[0]["category"], "Wind")
        self.assertEqual(records[0]["unit"], "GWh")
        self.assertEqual(records[0]["value"], 100)

    def test_current_energy_layout_preserves_first_fuel_first_year_units_and_missingness(self):
        rows = [
            {"A": "Table O1 - Generation"},
            {"A": "Gigawatt hours (GWh)"},
            {"A": "Fuel type", "B": "2023-24", "C": "2024-25"},
            {"A": "Black coal", "B": "100", "C": "95"},
            {"A": "Wind", "B": "", "C": "20"},
        ]
        records = extract_sheet_records("AUS FY", rows, "national_financial_year")
        self.assertEqual(len(records), 4)
        self.assertEqual(
            (records[0]["category"], records[0]["column_value"], records[0]["value"]),
            ("Black coal", "2023-24", 100),
        )
        self.assertEqual(records[0]["unit"], "GWh")
        self.assertIsNone(records[2]["value"])
        summary = [
            {"A": "Table O9 - Generation, 2024-25"},
            {"A": "Gigawatt hours (GWh)"},
            {"A": "Fuel type", "B": "NSW", "C": "AUS"},
            {"A": "Per cent renewable generation", "B": "40", "C": "35"},
        ]
        records = extract_sheet_records("State summary 2024-25", summary, "state_summary")
        self.assertEqual(records[0]["unit"], "percent")
        self.assertEqual(records[0]["time_period"], "2024-25")

    def test_unknown_energy_layout_fails_instead_of_reading_numbers_as_fuel_names(self):
        with self.assertRaisesRegex(ValueError, "header row"):
            extract_sheet_records(
                "AUS FY", [{"A": "Wind", "B": "", "C": "100", "D": "200"}], "test"
            )

    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_rba_discovers_series_files_with_current_names(self):
        html = """<a href="/statistics/tables/xls/f01.xlsx">Interest Rates – F1</a>
                  <a href="/statistics/tables/csv/f1-data.csv">Data</a>
                  <a href="/statistics/tables/csv/f1-data.csv">Duplicate</a>
                  <a href="/statistics/tables/csv/a3-transactions.csv">Transactions</a>"""
        rows = sources.fetch_rba_catalog(self.client(lambda r: httpx.Response(200, text=html)))
        self.assertEqual(rows[0]["title"], "Interest Rates – F1")
        self.assertEqual(rows[0]["datasetId"], "CUSTOM_AUS,RBA_F1,1.0")
        self.assertTrue(rows[0]["sourceRecord"]["sourceUrl"].endswith("f1-data.csv"))
        self.assertFalse(any("a3-transactions" in row["sourceRecord"]["sourceUrl"] for row in rows))

    def test_energy_follows_newest_publication_without_downloading_workbook(self):
        calls = []

        def get(request):
            calls.append(str(request.url))
            if str(request.url) == sources.ENERGY_PAGE:
                return httpx.Response(
                    200,
                    text="""<a href="/publications/statistics-table-o-electricity-generation-2024">Old</a>
                    <a href="/publications/statistics-table-o-electricity-generation-2026">Current</a>""",
                )
            self.assertTrue(str(request.url).endswith("2026"))
            return httpx.Response(
                200, text='<a href="/files/AES%202026%20Table%20O.xlsx">Table O</a>'
            )

        rows = sources.fetch_energy_catalog(self.client(get))
        self.assertEqual(len(calls), 2)
        self.assertIn("2026", rows[0]["sourceRecord"]["sourceUrl"])
        self.assertEqual(rows[0]["title"], "Current")
        curation = discover_curation(
            {"AUS FY": [], "NSW FY": [], "State summary 2025-26": [], "Title page": []}
        )
        self.assertIn("State summary 2025-26", curation["sheetGroups"][2]["sheets"])

    def test_worldbank_pages_must_be_complete(self):
        row = {"id": "TEST", "name": "Test", "source": {"id": "2", "value": "WDI"}}

        def get(request):
            page = int(request.url.params["page"])
            return httpx.Response(200, json=[{"pages": 2, "total": 2}, [dict(row, id=str(page))]])

        rows = sources.fetch_world_bank_catalog(self.client(get))
        self.assertEqual(len(rows), 2)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            sources.fetch_world_bank_catalog(
                self.client(lambda r: httpx.Response(200, json=[{"pages": 1, "total": 2}, [row]]))
            )

    def test_worldbank_rejects_changed_or_repeated_catalogue_pages(self):
        row = {"id": "TEST", "name": "Test", "source": {"id": "2", "value": "WDI"}}
        for second in (
            {"page": 1, "pages": 2, "total": 2},
            {"page": 2, "pages": 3, "total": 2},
            {"page": 2, "pages": 2, "total": 3},
        ):

            def get(request, second=second):
                meta = (
                    {"page": 1, "pages": 2, "total": 2}
                    if request.url.params["page"] == "1"
                    else second
                )
                return httpx.Response(200, json=[meta, [row]])

            with (
                self.subTest(second=second),
                self.assertRaisesRegex(RuntimeError, "changed during pagination"),
            ):
                sources.fetch_world_bank_catalog(self.client(get))

    def test_oecd_reads_dataflows_without_fetching_dataset_metadata(self):
        calls = []

        def get(request):
            calls.append(str(request.url))
            start = int(request.url.params["start"])
            rows = [
                {
                    "agencyId": "OECD.TEST",
                    "dataflowId": "DF_TEST",
                    "version": "1.2",
                    "datasourceId": "dsDisseminateFinalDMZ",
                    "name": "Population",
                    "description": "<p>People &amp; households</p>",
                },
                {
                    "agencyId": "OECD.TEST",
                    "dataflowId": "DF_LABOUR",
                    "version": "1.0",
                    "datasourceId": "dsDisseminateFinalDMZ",
                    "name": "Labour force",
                    "description": "Monthly survey",
                },
            ]
            return httpx.Response(
                200, json={"numFound": 2, "start": start, "dataflows": rows[start : start + 1]}
            )

        rows = sources.fetch_oecd_catalog(self.client(get))
        self.assertEqual(rows[0]["datasetId"], "oecd::OECD.TEST::DF_TEST::1.2")
        self.assertEqual(rows[0]["title"], "Population")
        self.assertEqual(rows[0]["description"], "People & households")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.startswith(sources.OECD_SEARCH) for call in calls))
        self.assertIn("start=1", calls[1])

    def test_oecd_rejects_incomplete_search_page(self):
        response = {"numFound": 2, "start": 0, "dataflows": []}
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            sources.fetch_oecd_catalog(self.client(lambda _: httpx.Response(200, json=response)))

    def test_domestic_resolves_live_source_record_without_refreshing_discovery(self):
        flow = {
            "id": "AES_TABLE_O",
            "agencyID": "CUSTOM_AUS",
            "version": "1.0",
            "sourceUrl": "https://example.test/new.xlsx",
        }
        with patch.object(
            catalog,
            "get_unified_catalog_entry",
            return_value={"route": "domestic", "sourceRecord": flow},
        ) as get:
            resolved = DomesticDataService().resolve_flow("CUSTOM_AUS,AES_TABLE_O,1.0")
        self.assertEqual(resolved["sourceUrl"], flow["sourceUrl"])
        get.assert_called_once_with("CUSTOM_AUS,AES_TABLE_O,1.0")
