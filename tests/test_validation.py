"""Data-integrity failures must be errors, never silently successful slices."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fixtures import comtrade_codes

from ausdata_mcp import artifacts, macro_data, runtime, server
from ausdata_mcp.errors import tool_error_result


def entry(provider):
    return {
        "datasetId": f"{provider}::TEST",
        "providerKey": provider,
        "provider": provider,
        "title": "Test",
        "unit": "USD",
        "providerConfig": {},
    }


def response(payload):
    return httpx.Response(
        200, request=httpx.Request("GET", "https://example.test/data"), json=payload
    )


class MacroValidationTests(unittest.TestCase):
    def setUp(self):
        codes = patch.object(macro_data, "_live_comtrade_codes", side_effect=comtrade_codes)
        codes.start()
        self.addCleanup(codes.stop)
        patcher = patch.object(macro_data, "get_oecd_service")
        self.oecd = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.oecd.metadata.return_value = {"key_order": ["REF_AREA"], "annotations": {}}

    def test_oecd_uses_structure_order_and_preserves_subgroups_and_status_changes(self):
        self.oecd.metadata.return_value["key_order"] = ["FREQ", "REF_AREA", "SEX"]
        text = (
            "REF_AREA,TIME_PERIOD,OBS_VALUE,SEX,FREQ,UNIT_MULT,OBS_STATUS\n"
            "AUS,2024-02,4,F,M,0,A\nAUS,2024-01,..,F,M,0,C\n"
            "AUS,2024-01,3,M,M,0,A\n"
        )
        reply = httpx.Response(200, request=httpx.Request("GET", "https://example.test"), text=text)
        with patch.object(macro_data.source_http, "get", return_value=reply) as get:
            result = macro_data._fetch_oecd(
                entry("oecd"), {"agency": "OECD.TEST", "dataflow": "TEST"}, ["AUS"], 2024, 2024
            )
        self.assertTrue(get.call_args.args[0].endswith("/.AUS."))
        self.assertEqual(len(result["series"]), 2)
        female, male = result["series"]
        self.assertEqual(female["series_key"], "M.AUS.F")
        self.assertEqual(male["dimensions"]["SEX"], "M")
        self.assertEqual([p["x"] for p in female["points"]], ["2024-01", "2024-02"])
        self.assertEqual(female["points"][0]["source_row"]["OBS_STATUS"], "C")
        self.assertIsNone(female["points"][0]["y"])

    def test_oecd_rejects_missing_dimensions(self):
        self.oecd.metadata.return_value["key_order"] = ["REF_AREA", "SEX"]
        for text, error in (
            ("REF_AREA,TIME_PERIOD,OBS_VALUE\nAUS,2024,1\n", "missing required"),
            ("REF_AREA,TIME_PERIOD,OBS_VALUE,SEX\nAUS,2024,1,\n", "complete dimension"),
        ):
            reply = httpx.Response(
                200, request=httpx.Request("GET", "https://example.test"), text=text
            )
            with (
                self.subTest(error=error),
                patch.object(macro_data.source_http, "get", return_value=reply),
            ):
                with self.assertRaisesRegex(RuntimeError, error):
                    macro_data._fetch_oecd(
                        entry("oecd"),
                        {"agency": "OECD.TEST", "dataflow": "TEST"},
                        ["AUS"],
                        2023,
                        2024,
                    )

    def test_oecd_rejects_duplicate_columns_and_incomplete_coordinates(self):
        for text in (
            "REF_AREA,TIME_PERIOD,OBS_VALUE,OBS_VALUE\nAUS,2022,1,2\n",
            "REF_AREA,TIME_PERIOD,OBS_VALUE\nAUS,2022,1\nAUS,,2\n",
            "REF_AREA,TIME_PERIOD,OBS_VALUE\nAUS,2022,1\n,2022,2\n",
        ):
            reply = httpx.Response(
                200, request=httpx.Request("GET", "https://example.test"), text=text
            )
            with patch.object(macro_data.source_http, "get", return_value=reply):
                with self.assertRaisesRegex(
                    RuntimeError, "duplicate CSV|without country or period"
                ):
                    macro_data._fetch_oecd(
                        entry("oecd"),
                        {"agency": "OECD.TEST", "dataflow": "TEST"},
                        ["AUS"],
                        2022,
                        2022,
                    )

    def test_imf_preserves_suppressed_source_value_and_rejects_invalid_periods(self):
        payload = {"values": {"TEST": {"AUS": {"2022": ".."}}}}
        with patch.object(macro_data.source_http, "get", return_value=response(payload)):
            result = macro_data._fetch_imf(entry("imf"), {"series_id": "TEST"}, ["AUS"], 2022, 2022)
        point = result["series"][0]["points"][0]
        self.assertIsNone(point["y"])
        self.assertEqual(point["source_row"]["value"], "..")
        payload["values"]["TEST"]["AUS"]["unknown"] = 1
        with patch.object(macro_data.source_http, "get", return_value=response(payload)):
            with self.assertRaisesRegex(RuntimeError, "valid annual period"):
                macro_data._fetch_imf(entry("imf"), {"series_id": "TEST"}, ["AUS"], 2022, 2022)

    def test_missing_requested_countries_are_disclosed_for_each_macro_provider(self):
        cases = [
            (
                macro_data._fetch_world_bank,
                [
                    {"pages": 1, "total": 1},
                    [{"countryiso3code": "AUS", "date": "2022", "value": 1}],
                ],
                {"series_id": "TEST"},
            ),
            (
                macro_data._fetch_imf,
                {"values": {"TEST": {"AUS": {"2022": 1}}}},
                {"series_id": "TEST"},
            ),
            (
                macro_data._fetch_oecd,
                "REF_AREA,TIME_PERIOD,OBS_VALUE\nAUS,2022,1\n",
                {"agency": "OECD.TEST", "dataflow": "TEST"},
            ),
        ]
        for fetch, payload, config in cases:
            reply = (
                response(payload)
                if not isinstance(payload, str)
                else httpx.Response(
                    200, request=httpx.Request("GET", "https://example.test"), text=payload
                )
            )
            with (
                self.subTest(provider=fetch.__name__),
                patch.object(macro_data.source_http, "get", return_value=reply),
            ):
                result = fetch(entry("test"), config, ["AUS", "NZL"], 2022, 2022)
                self.assertEqual(result["coverage_gaps"][0]["codes"], ["NZL"])
                self.assertEqual(result["series"][0]["country_code"], "AUS")

    def test_invalid_scope_fails_before_network(self):
        with patch.object(
            macro_data.source_http, "get", side_effect=AssertionError("No request expected")
        ):
            for fetch in (
                macro_data._fetch_world_bank,
                macro_data._fetch_imf,
                macro_data._fetch_oecd,
            ):
                for countries, all_countries, start, end in [
                    (["AUS", ""], False, 2020, 2022),
                    (["AUS"], True, 2020, 2022),
                    (["AUS"], False, 2023, 2022),
                    (["AUS"], False, 0, 2022),
                ]:
                    with (
                        self.subTest(fetch=fetch.__name__, countries=countries),
                        self.assertRaises(ValueError),
                    ):
                        fetch(entry("test"), {}, countries, start, end, all_countries=all_countries)

    def test_world_bank_rejects_incomplete_pagination(self):
        row = {"countryiso3code": "AUS", "date": "2022", "value": 1}
        for replies in (
            [response([{"pages": 1, "total": 2}, [row]])],
            [response([{"pages": 2}, [row]]), response([{"pages": 2}, []])],
        ):
            with (
                self.subTest(replies=replies),
                patch.object(macro_data.source_http, "get", side_effect=replies),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "incomplete data|pagination was incomplete"
                ):
                    macro_data._fetch_world_bank(
                        entry("worldbank"), {"series_id": "TEST"}, ["AUS"], 2022, 2022
                    )

    def test_world_bank_rejects_wrong_slice_and_duplicate_points(self):
        row = {"countryiso3code": "AUS", "date": "2022", "value": 1}
        for rows in ([row, row], [row, {**row, "countryiso3code": "NZL"}]):
            with patch.object(
                macro_data.source_http, "get", return_value=response([{"pages": 1}, rows])
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "duplicate observations|outside the requested"
                ):
                    macro_data._fetch_world_bank(
                        entry("worldbank"), {"series_id": "TEST"}, ["AUS"], 2022, 2022
                    )

    def test_world_bank_retains_null_values_and_source_flags(self):
        rows = [
            {
                "countryiso3code": "AUS",
                "date": "2022",
                "value": None,
                "obs_status": "M",
                "unit": "%",
            }
        ]
        with patch.object(
            macro_data.source_http,
            "get",
            return_value=response([{"pages": 1, "total": 1}, rows]),
        ):
            result = macro_data._fetch_world_bank(
                entry("worldbank"), {"series_id": "TEST"}, ["aus", "AUS"], 2022, 2022
            )
        self.assertEqual(len(result["series"]), 1)
        self.assertIsNone(result["series"][0]["points"][0]["y"])
        self.assertEqual(result["series"][0]["points"][0]["source_row"]["obs_status"], "M")

    def test_oecd_retains_suppression_and_separates_dimension_series(self):
        self.oecd.metadata.return_value["key_order"] = ["REF_AREA", "MEASURE"]
        for extra in ("", "AUS,2022,1,B,A\n"):
            text = "REF_AREA,TIME_PERIOD,OBS_VALUE,MEASURE,OBS_STATUS\nAUS,2022,..,A,C\n" + extra
            reply = httpx.Response(
                200, request=httpx.Request("GET", "https://example.test"), text=text
            )
            with patch.object(macro_data.source_http, "get", return_value=reply):
                result = macro_data._fetch_oecd(
                    entry("oecd"), {"agency": "OECD.TEST", "dataflow": "TEST"}, ["AUS"], 2022, 2022
                )
                point = result["series"][0]["points"][0]
                self.assertIsNone(point["y"])
                self.assertEqual(point["source_row"]["OBS_STATUS"], "C")
                self.assertEqual(len(result["series"]), 2 if extra else 1)
                if extra:
                    self.assertEqual(
                        result["series"][1]["dimensions"], {"REF_AREA": "AUS", "MEASURE": "B"}
                    )
                    self.assertEqual(result["series"][1]["points"][0]["y"], 1)

    def test_comtrade_does_not_hide_empty_or_invalid_chunks(self):
        row = {
            "period": 2022,
            "primaryValue": 1,
            "reporterCode": 36,
            "partnerCode": 0,
            "cmdCode": "TOTAL",
            "flowCode": "X",
        }
        for invalid in ({"error": "failed"}, {"data": "invalid"}):
            with patch.object(
                macro_data.source_http,
                "get",
                side_effect=[response({"data": [row]}), response(invalid)],
            ):
                with self.assertRaisesRegex(RuntimeError, "incomplete data"):
                    macro_data._fetch_comtrade(
                        entry("comtrade"),
                        {},
                        reporter_codes=["36", "554"],
                        partner_codes=["0"],
                        flow_code="X",
                        frequency_code="A",
                        hs_codes=["TOTAL"],
                        start_year=2022,
                        end_year=2022,
                    )

    def test_comtrade_rejects_wrong_world_partner_instead_of_replacing_zero(self):
        row = {
            "period": 2022,
            "primaryValue": 1,
            "reporterCode": 36,
            "partnerCode": 0,
            "cmdCode": "TOTAL",
            "flowCode": "X",
        }
        with patch.object(macro_data.source_http, "get", return_value=response({"data": [row]})):
            with self.assertRaisesRegex(RuntimeError, "outside the requested codes"):
                macro_data._fetch_comtrade(
                    entry("comtrade"),
                    {},
                    reporter_codes=["36"],
                    partner_codes=["554"],
                    flow_code="X",
                    frequency_code="A",
                    hs_codes=["TOTAL"],
                    start_year=2022,
                    end_year=2022,
                )


class ComtradeReferenceTests(unittest.TestCase):
    def setUp(self):
        for module in (macro_data, server):
            patcher = patch.object(module, "_live_comtrade_codes", side_effect=comtrade_codes)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_comtrade_codes_can_be_paged_and_searched_without_losing_codes(self):
        identity = "comtrade::goods_trade"
        with patch.object(server, "_route_entry", return_value={}):
            for dimension in ("REPORTER", "PARTNER", "HS"):
                codes, offset = [], 0
                while offset is not None:
                    page = server.get_metadata(
                        identity, dimension=dimension, codeOffset=offset, codeLimit=10
                    )
                    codes.extend(page["codes"])
                    offset = page["next_offset"]
                self.assertEqual(codes, comtrade_codes(dimension))
            australia = server.get_metadata(identity, dimension="REPORTER", codeSearch="australia")
            self.assertEqual(australia["codes"], [{"code": "36", "label": "Australia"}])
            coffee = server.get_metadata(identity, dimension="HS", codeSearch="coffee", codeLimit=1)
            self.assertEqual(coffee["codes"][0]["code"], "0901")
            self.assertEqual(coffee["next_offset"], 1)
            with self.assertRaisesRegex(ValueError, "Comtrade dimension"):
                server.get_metadata(identity, dimension="INVALID")

    def test_world_total_is_a_partner_not_a_reporter(self):
        metadata = macro_data._build_comtrade_metadata_payload(entry("comtrade"))
        self.assertNotIn(
            "0", {option["code"] for option in macro_data._live_comtrade_codes("REPORTER")}
        )
        self.assertIn(
            "0", {option["code"] for option in macro_data._live_comtrade_codes("PARTNER")}
        )
        self.assertEqual(metadata["default_filters"]["TRANSPORT"], ["0"])
        self.assertIn("HS", {dimension["id"] for dimension in metadata["dimensions"]})

    def test_comtrade_defaults_to_totals_and_detects_ignored_filters(self):
        row = {
            "period": 2022,
            "primaryValue": 1,
            "reporterCode": 36,
            "partnerCode": 0,
            "cmdCode": "TOTAL",
            "flowCode": "X",
            "partner2Code": 0,
            "motCode": 0,
            "customsCode": "C00",
        }
        arguments = dict(
            reporter_codes=["36"],
            partner_codes=["0"],
            flow_code="X",
            frequency_code="A",
            hs_codes=["TOTAL"],
            start_year=2022,
            end_year=2022,
        )
        with patch.object(
            macro_data.source_http, "get", return_value=response({"data": [row]})
        ) as get:
            macro_data._fetch_comtrade(entry("comtrade"), {}, **arguments)
        self.assertEqual(get.call_args.kwargs["params"]["motCode"], "0")
        self.assertEqual(get.call_args.kwargs["params"]["customsCode"], "C00")
        with patch.object(
            macro_data.source_http,
            "get",
            return_value=response({"data": [{**row, "motCode": 9900}]}),
        ):
            with self.assertRaisesRegex(RuntimeError, "motCode outside"):
                macro_data._fetch_comtrade(entry("comtrade"), {}, **arguments)


class LiveReferenceTests(unittest.TestCase):
    def setUp(self):
        from ausdata_mcp.fetch_cache import FetchCache

        cache = patch.object(macro_data, "_reference_cache", FetchCache(limit=8))
        cache.start()
        self.addCleanup(cache.stop)

    def test_live_reference_cache_refresh_and_failed_refresh_are_recoverable(self):
        first = {"results": [{"id": "090111", "text": "Coffee", "parent": "0901"}]}
        second = {"results": [{"id": "090112", "text": "Decaffeinated coffee", "parent": "0901"}]}
        with patch.object(
            macro_data.source_http,
            "get",
            side_effect=[response(first), response({"results": []}), response(second)],
        ) as get:
            self.assertEqual(macro_data._live_comtrade_codes("HS")[0]["code"], "090111")
            macro_data._live_comtrade_codes("HS")
            self.assertEqual(get.call_count, 1)
            with self.assertRaisesRegex(RuntimeError, "no reference codes"):
                macro_data._live_comtrade_codes("HS", refresh=True)
            self.assertEqual(
                macro_data._live_comtrade_codes("HS", refresh=True)[0]["code"], "090112"
            )

    def test_secondary_partner_reuses_partner_reference_request(self):
        with patch.object(
            macro_data.source_http,
            "get",
            return_value=response({"results": [{"id": 0, "text": "World"}]}),
        ) as get:
            self.assertEqual(
                macro_data._live_comtrade_codes("PARTNER"),
                macro_data._live_comtrade_codes("SECOND_PARTNER"),
            )
        self.assertEqual(get.call_count, 1)

    def test_bad_reference_rows_are_not_silently_omitted(self):
        with patch.object(
            macro_data.source_http,
            "get",
            return_value=response({"results": [{"id": 36, "text": "Australia"}, {"id": 554}]}),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid REPORTER"):
                macro_data._live_comtrade_codes("REPORTER")


class ToolBoundaryTests(unittest.TestCase):
    def test_unused_parameters_are_rejected_before_retrieval(self):
        cases = [
            ("CUSTOM_AUS,RBA_F1,1.0", {"countries": ["AUS"]}),
            ("worldbank::TEST", {"startPeriod": "2020"}),
            ("ABS,TEST,1.0", {"countries": ["NZL"]}),
            ("comtrade::goods_trade", {"countries": ["AUS"]}),
            ("pdh::SPC::TEST::1.0", {"startYear": 2020}),
        ]
        with patch.object(server, "_route_entry", return_value={}):
            for dataset_id, arguments in cases:
                with (
                    self.subTest(dataset=dataset_id),
                    self.assertRaisesRegex(ValueError, "do not apply"),
                ):
                    server.retrieve(dataset_id, **arguments)

    def test_empty_comtrade_codes_are_not_removed_before_validation(self):
        record = {
            "providerKey": "comtrade",
            "providerConfig": {},
            "datasetId": "comtrade::goods_trade",
        }
        with patch.object(server, "_route_entry", return_value=record):
            with self.assertRaisesRegex(ValueError, "Invalid reporterCodes"):
                server.retrieve(
                    "comtrade::goods_trade",
                    reporterCodes=["36", ""],
                    partnerCodes=["0"],
                    flowCode="X",
                    frequencyCode="A",
                    hsCodes=["TOTAL"],
                    startYear=2022,
                    endYear=2022,
                )

    def test_empty_evidence_and_nonfinite_json_leave_no_artifact(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(artifacts, "RUNTIME_DIR", Path(directory)),
        ):
            for payload in (
                {"series": []},
                {"series": [{"observations": []}]},
                {"series": [{"observations": [{"observationKey": "2020", "value": float("nan")}]}]},
            ):
                with self.subTest(payload=payload), self.assertRaises((RuntimeError, ValueError)):
                    artifacts.store_retrieval(payload, "TEST", "Test")
            self.assertEqual(list(Path(directory).rglob("*.json")), [])
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])

    def test_zero_multiplier_and_domestic_frequency_are_in_manifest(self):
        payload = {
            "series": [
                {
                    "dimensions": {"FREQ": {"code": "M"}},
                    "attributes": {"UNIT_MULT": {"code": 0}},
                    "observations": [{"observationKey": "2024-01", "value": 1}],
                }
            ]
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(artifacts, "RUNTIME_DIR", Path(directory)),
        ):
            manifest = artifacts.store_retrieval(payload, "TEST", "Test")
            self.assertEqual(manifest["unit_multiplier_codes"], ["0"])
            self.assertEqual(manifest["frequency_examples"], ["M"])


class LocalRuntimeTests(unittest.TestCase):
    def test_startup_checks_local_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(runtime, "RUNTIME_DIR", root),
                patch.object(runtime, "SESSION_ID", "test"),
            ):
                runtime.validate_local_runtime()
            session = root / "sessions" / "test"
            self.assertEqual({path.name for path in session.iterdir()}, {"catalogue", "artifacts"})
            for name in ("catalogue", "artifacts"):
                self.assertEqual(list((session / name).iterdir()), [])

    def test_startup_reports_unwritable_session_path(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "blocked"
            blocked.write_text("not a directory")
            with patch.object(runtime, "RUNTIME_DIR", blocked):
                with self.assertRaisesRegex(RuntimeError, "AUSDATA_RUNTIME_DIR"):
                    runtime.validate_local_runtime()

    def test_startup_checks_sqlite_fts5(self):
        with patch.object(runtime.sqlite3, "connect", side_effect=runtime.sqlite3.OperationalError):
            with self.assertRaisesRegex(RuntimeError, "SQLite FTS5"):
                runtime.validate_local_runtime()


class MCPArgumentTests(unittest.TestCase):
    def test_provider_errors_include_recovery_guidance(self):
        async def check():
            request = httpx.Request("GET", "https://example.test/data")
            for status, headers, message in (
                (429, {"Retry-After": "30"}, "Retry after 30 seconds"),
                (429, {}, "Retry after 1 seconds"),
                (503, {}, "temporarily unavailable"),
                (403, {}, "denied access"),
                (404, {}, "Verify the dataset"),
            ):
                reply = httpx.Response(status, request=request, headers=headers)
                error = httpx.HTTPStatusError("raw source failure", request=request, response=reply)
                with patch.object(server, "search_unified_catalog", side_effect=error):
                    with self.subTest(status=status):
                        result = await server.server.call_tool("search_catalog", {"query": "test"})
                        self.assertTrue(result.isError)
                        self.assertEqual(
                            result.structuredContent, json.loads(result.content[0].text)
                        )
                        self.assertIn(message, result.structuredContent["error"]["message"])
            reply = httpx.Response(404, request=request)
            error = httpx.HTTPStatusError("empty selection", request=request, response=reply)
            guidance = tool_error_result(
                error, "retrieve", {"datasetId": "oecd::TEST"}
            ).structuredContent["error"]["message"]
            self.assertIn("broaden sourceFilters or dataKey", guidance)
            self.assertIn("series dimensions", guidance)
            self.assertNotIn(
                "broaden sourceFilters",
                tool_error_result(error, "search_catalog", {}).structuredContent["error"][
                    "message"
                ],
            )
            for error, message in (
                (httpx.ReadTimeout("timeout", request=request), "timed out.*Retry later"),
                (httpx.ConnectError("offline", request=request), "Check network availability"),
            ):
                with patch.object(server, "search_unified_catalog", side_effect=error):
                    result = await server.server.call_tool("search_catalog", {"query": "test"})
                    self.assertTrue(result.isError)
                    self.assertRegex(result.structuredContent["error"]["message"], message)

        asyncio.run(check())

    def test_declared_search_output_is_validated(self):
        async def check():
            with patch.object(server, "search_unified_catalog", return_value={"candidates": []}):
                result = await server.server.call_tool("search_catalog", {"query": "test"})
                self.assertTrue(result.isError)
                self.assertIn("Field required", result.structuredContent["error"]["message"])

        asyncio.run(check())

    def test_schemas_require_dataset_and_enforce_integer_bounds_before_discovery(self):
        async def check():
            with (
                patch.object(server, "get_unified_catalog_entry") as lookup,
                patch.object(server, "search_unified_catalog") as search,
            ):
                for name, arguments in (
                    ("get_metadata", {}),
                    ("retrieve", {}),
                    ("retrieve", {"datasetId": ""}),
                    ("retrieve", {"datasetId": "worldbank::TEST", "startYear": True}),
                    ("retrieve", {"datasetId": "worldbank::TEST", "startYear": 10000}),
                    ("search_catalog", {"limit": True}),
                    ("search_catalog", {"limit": 51}),
                    ("search_catalog", {"offset": -1}),
                    ("get_metadata", {"datasetId": "ABS,TEST,1.0", "codeLimit": 201}),
                ):
                    with self.subTest(name=name, arguments=arguments):
                        result = await server.server.call_tool(name, arguments)
                        self.assertTrue(result.isError)
                        self.assertEqual(result.structuredContent["error"]["request"], arguments)
                lookup.assert_not_called()
                search.assert_not_called()
            tools = {tool.name: tool for tool in await server.server.list_tools()}
            self.assertIn("datasetId", tools["retrieve"].inputSchema["required"])
            self.assertEqual(
                tools["search_catalog"].inputSchema["properties"]["limit"]["maximum"], 50
            )
            inputs = tools["retrieve"].inputSchema["properties"]
            self.assertEqual(inputs["frequencyCode"]["enum"], ["", "A", "M"])
            self.assertIn("ABS/OECD/PDH", inputs["sourceFilters"]["description"])
            self.assertIn("artifact_path", tools["retrieve"].outputSchema["required"])

        asyncio.run(check())

    def test_invalid_scope_and_blank_identity_fail_before_catalogue_fetch(self):
        with patch.object(server, "get_unified_catalog_entry") as lookup:
            for dataset, args in (
                (" ", {}),
                ("ABS,TEST,1.0", {"countries": ["NZL"]}),
                ("ABS,TEST,1.0", {"startPeriod": "2024", "endPeriod": "2020"}),
                ("worldbank::TEST", {"startYear": 2024, "endYear": 2020}),
                ("worldbank::TEST", {"countries": ["AUS"], "allCountries": True}),
            ):
                with self.subTest(dataset=dataset, args=args), self.assertRaises(ValueError):
                    server.retrieve(dataset, **args)
            with self.assertRaisesRegex(ValueError, "Set dimension"):
                server.get_metadata("ABS,TEST,1.0", codeSearch="missing dimension")
            lookup.assert_not_called()

    def test_unknown_arguments_fail_before_source_access(self):
        async def check():
            with patch.object(server, "search_unified_catalog") as search:
                result = await server.server.call_tool(
                    "search_catalog", {"query": "test", "provder": "ABS"}
                )
                self.assertTrue(result.isError)
                self.assertRegex(
                    result.structuredContent["error"]["message"], "Unknown arguments.*provder"
                )
                search.assert_not_called()
            tools = await server.server.list_tools()
            self.assertEqual(len(tools), 3)
            for tool in tools:
                self.assertFalse(tool.inputSchema["additionalProperties"])

        asyncio.run(check())
