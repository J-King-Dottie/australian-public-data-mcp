"""Broad selections retain evidence and explicit gaps without hiding source failures."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx
import test_domestic
from fixtures import comtrade_codes
from test_domestic import CSV, FLOW, abs_payload
from test_validation import entry, response

from ausdata_mcp import artifacts, energy_workbook, macro_data, rba_tables, server
from ausdata_mcp.domestic_data import DomesticDataService
from ausdata_mcp.selection import code_page, sdmx_selection


class SelectionTests(unittest.TestCase):
    def test_code_search_tolerates_spacing_and_unicode_punctuation(self):
        codes = [{"code": "1524", "label": "15 - 24 years"}]
        for query in ("15-24", "15–24", "15—24 YEARS", " 15   24 ", "1524"):
            with self.subTest(query=query):
                page = code_page("test", "AGE", codes, query, 0, 50)
                self.assertEqual(page["codes"], codes)
        self.assertEqual(codes[0]["label"], "15 - 24 years")

    def test_code_search_handles_labels_across_subjects_without_rewriting_codes(self):
        examples = [
            ("CIV", "Côte d’Ivoire", "CÔTE D'IVOIRE"),
            ("USD_PC", "US dollars/person", "us dollars / person"),
            ("_TXCP01_NRG", "All items non-food non-energy", "non food non energy"),
            ("20", "Seasonally adjusted", "SEASONALLY    ADJUSTED"),
            ("0010", "Manufacturing, total", "manufacturing total"),
        ]
        codes = [{"code": code, "label": label} for code, label, _ in examples]
        original = deepcopy(codes)
        for code, label, query in examples:
            with self.subTest(query=query):
                result = code_page("test", "DIM", codes, query, 0, 50)
                self.assertEqual(result["codes"], [{"code": code, "label": label}])
        self.assertEqual(codes, original)
        self.assertEqual(code_page("test", "DIM", codes, "0010", 0, 50)["codes"][0]["code"], "0010")

    def test_similar_codes_stay_distinct_and_retrieval_uses_the_selected_code(self):
        codes = [
            {"code": "A-B", "label": "First measure"},
            {"code": "A_B", "label": "Second measure"},
        ]
        matches = code_page("test", "DIM", codes, "a b", 0, 50)
        self.assertEqual(matches["codes"], codes)
        self.assertEqual(matches["matching_codes"], 2)
        for item in matches["codes"]:
            code = item["code"]
            self.assertEqual(sdmx_selection(["DIM"], {"DIM": [code]}), (code, {"DIM": [code]}))

    def test_normalized_code_search_preserves_pagination_and_source_order(self):
        codes = [
            {"code": "A", "label": "15 - 24 years"},
            {"code": "B", "label": "25 - 34 years"},
            {"code": "C", "label": "15–24 years"},
        ]
        first = code_page("test", "AGE", codes, "15-24", 0, 1)
        second = code_page("test", "AGE", codes, "15-24", first["next_offset"], 1)
        self.assertEqual(first["total_codes"], 3)
        self.assertEqual(first["matching_codes"], 2)
        self.assertEqual(first["codes"] + second["codes"], [codes[0], codes[2]])
        self.assertIsNone(second["next_offset"])

    def test_code_search_keeps_literal_matches_and_empty_browsing(self):
        codes = [{"code": "A_B", "label": "Alpha"}, {"code": "C", "label": "Beta"}]
        for query, expected in (("", codes), ("_", codes[:1]), ("!!!", []), ("missing", [])):
            with self.subTest(query=query):
                self.assertEqual(code_page("test", "DIM", codes, query, 0, 50)["codes"], expected)

    def test_named_positional_and_full_scope(self):
        order = ["FREQ", "REF_AREA", "SEX"]
        expected = (".AUS+NZL.F", {"REF_AREA": ["AUS", "NZL"], "SEX": ["F"]})
        self.assertEqual(
            sdmx_selection(order, {"REF_AREA": ["AUS", "NZL", "AUS"], "SEX": ["F"]}), expected
        )
        self.assertEqual(sdmx_selection(order, key=".AUS+NZL.F"), expected)
        for key in ("", "all", ".."):
            self.assertEqual(sdmx_selection(order, key=key), ("all", {}))

    def test_ambiguous_and_malformed_selections_still_fail(self):
        for filters, key in (
            ({"A": ["x"]}, "all"),
            ({"wrong": ["x"]}, ""),
            ({"A": []}, ""),
            ({"A": ["x+y"]}, ""),
            ({}, "x.y"),
        ):
            with self.subTest(filters=filters, key=key), self.assertRaises(ValueError):
                sdmx_selection(["A"], filters, key)

    def test_abs_named_filters_are_translated_using_source_order(self):
        with (
            patch.object(server, "_route_entry", return_value={"provider": "ABS"}),
            patch.object(server, "get_domestic_service") as factory,
            patch.object(server, "store_retrieval", return_value={"ok": True}),
        ):
            service = factory.return_value
            service.get_data_structure_for_dataflow.return_value = {
                "dimensions": [{"id": "SEX", "position": 1}, {"id": "AREA", "position": 0}]
            }
            service.resolve_dataset.return_value = {"series": []}
            self.assertEqual(
                server.retrieve("ABS,TEST,1.0", sourceFilters={"SEX": ["F", "M"]}), {"ok": True}
            )
            self.assertEqual(service.resolve_dataset.call_args.kwargs["data_key"], ".F+M")

    def test_abs_missing_selection_keeps_observations_and_discloses_gap(self):
        service = DomesticDataService()
        self.addCleanup(service.api_client._client.close)
        with (
            patch.object(service, "resolve_flow", return_value=FLOW),
            patch.object(service.api_client, "get_data", return_value=abs_payload()),
        ):
            result = service.resolve_dataset("ABS,TEST,1.0", data_key="A+B")
        self.assertEqual(len(result["series"][0]["observations"]), 2)
        self.assertEqual(result["coverage_gaps"][0]["codes"], ["B"])


class MacroScopeTests(unittest.TestCase):
    def test_world_bank_accepts_iso2_and_nonannual_periods(self):
        for period, frequency in (("2024M02", "monthly"), ("2024Q2", "quarterly")):
            rows = [{"countryiso3code": "AUS", "country": {"id": "AU"}, "date": period, "value": 2}]
            with (
                self.subTest(period=period),
                patch.object(
                    macro_data.source_http, "get", return_value=response([{"pages": 1}, rows])
                ),
            ):
                result = macro_data._fetch_world_bank(
                    entry("worldbank"), {"series_id": "TEST"}, ["AU"], 2024, 2024
                )
            self.assertEqual(result["series"][0]["country_code"], "AUS")
            self.assertEqual(result["series"][0]["frequency"], frequency)
            self.assertEqual(result["coverage_gaps"], [])

    def test_world_bank_preserves_changing_units(self):
        rows = [
            {"countryiso3code": "AUS", "date": str(year), "value": 2, "unit": unit}
            for year, unit in ((2023, "USD"), (2024, "AUD"))
        ]
        with patch.object(
            macro_data.source_http, "get", return_value=response([{"pages": 1}, rows])
        ):
            result = macro_data._fetch_world_bank(
                entry("worldbank"), {"series_id": "TEST"}, ["AUS"], 2023, 2024
            )
        self.assertEqual([p["unit"] for p in result["series"][0]["points"]], ["USD", "AUD"])
        self.assertEqual(result["series"][0]["unit"], "varies; see point.unit")

    def test_imf_accepts_area_codes_and_keeps_partial_coverage(self):
        with patch.object(
            macro_data.source_http,
            "get",
            return_value=response({"values": {"TEST": {"EU": {"2024": 3}, "AUS": {"2023": 2}}}}),
        ):
            result = macro_data._fetch_imf(
                entry("imf"), {"series_id": "TEST"}, ["EU", "AUS"], 2024, 2024
            )
        self.assertEqual(result["series"][0]["country_code"], "EU")
        self.assertEqual(result["coverage_gaps"][0]["codes"], ["AUS"])

    def test_oecd_named_dimensions_periods_and_missing_codes(self):
        csv = "REF_AREA,SEX,TIME_PERIOD,OBS_VALUE,UNIT_MEASURE\nAUS,F,2024-02,4,PCT\n"
        with (
            patch.object(macro_data, "get_oecd_service") as service,
            patch.object(
                macro_data.source_http,
                "get",
                return_value=httpx.Response(
                    200, request=httpx.Request("GET", "https://example.test"), text=csv
                ),
            ) as get,
        ):
            service.return_value.metadata.return_value = {
                "key_order": ["REF_AREA", "SEX"],
                "annotations": {},
            }
            result = macro_data._fetch_oecd(
                entry("oecd"),
                {"agency": "TEST", "dataflow": "TEST"},
                [],
                None,
                None,
                source_filters={"SEX": ["F", "M"]},
                start_period="2024-02",
                end_period="2024-02",
            )
        self.assertTrue(get.call_args.args[0].endswith("/.F+M"))
        self.assertEqual(get.call_args.kwargs["params"]["startPeriod"], "2024-02")
        self.assertEqual(result["coverage_gaps"][0]["codes"], ["M"])
        self.assertEqual(result["series"][0]["points"][0]["y"], 4)

    def test_oecd_without_geography_preserves_unit_changes(self):
        csv = (
            "MEASURE,TIME_PERIOD,OBS_VALUE,UNIT_MEASURE,UNIT_MULT\nA,2023,1,USD,0\nA,2024,2,USD,3\n"
        )
        with (
            patch.object(macro_data, "get_oecd_service") as service,
            patch.object(
                macro_data.source_http,
                "get",
                return_value=httpx.Response(
                    200, request=httpx.Request("GET", "https://example.test"), text=csv
                ),
            ) as get,
        ):
            service.return_value.metadata.return_value = {
                "key_order": ["MEASURE"],
                "annotations": {},
            }
            result = macro_data._fetch_oecd(
                entry("oecd"), {"agency": "TEST", "dataflow": "TEST"}, [], 2023, 2024
            )
        self.assertTrue(get.call_args.args[0].endswith("/all"))
        item = result["series"][0]
        self.assertEqual(item["country_code"], "")
        self.assertEqual(item["unit"], "varies; see point.unit")
        self.assertEqual(item["points"][1]["unit"], "USD (10^3)")


class FileScopeTests(unittest.TestCase):
    def test_rba_multiple_series_and_dates_equal_slice_of_full_table(self):
        content = (
            CSV.replace(b"Series ID,FTEST", b"Series ID,FTEST,SECOND")
            .replace(b"31-Dec-2023,2", b"31-Dec-2023,2,4")
            .replace(b"29-Feb-2024,3", b"29-Feb-2024,3,6")
        )
        parsed = rba_tables.parse_table(rba_tables.load_rows(content))
        full = rba_tables.build_resolved_dataset(FLOW, parsed, {}, "all")
        selected = rba_tables.build_resolved_dataset(FLOW, parsed, {}, "FTEST+SECOND")
        self.assertEqual(selected["series"], full["series"])
        service = DomesticDataService()
        self.addCleanup(service.api_client._client.close)
        with (
            patch.object(service, "resolve_flow", return_value=FLOW),
            patch.object(service.rba_service, "supports", return_value=True),
            patch.object(service.rba_service, "resolve", return_value=selected),
        ):
            filtered = service.resolve_dataset(
                "CUSTOM_AUS,TEST,1.0",
                data_key="FTEST+SECOND",
                start_period="2024-02",
                end_period="2024-02",
            )
        self.assertEqual(filtered["observationCount"], 2)
        self.assertEqual([s["observations"][0]["value"] for s in filtered["series"]], [3, 6])

    def test_energy_exact_financial_year_excludes_calendar_year(self):
        sheets = {
            "AUS CY": test_domestic.DomesticIntegrityTests.energy_rows({"B": "2023", "C": "2024"}),
            "AUS FY": test_domestic.DomesticIntegrityTests.energy_rows(
                {"B": "2023-24", "C": "2024-25"}
            ),
        }
        full = energy_workbook.build_resolved_dataset(
            FLOW, energy_workbook.discover_curation(sheets), sheets, "all"
        )
        service = DomesticDataService()
        self.addCleanup(service.api_client._client.close)
        with (
            patch.object(service, "resolve_flow", return_value=FLOW),
            patch.object(service.dcceew_service, "supports", return_value=True),
            patch.object(service.dcceew_service, "resolve", return_value=full),
        ):
            result = service.resolve_dataset(
                "CUSTOM_AUS,TEST,1.0", start_period="2023-24", end_period="2023-24"
            )
        self.assertEqual(
            [row["observationKey"] for item in result["series"] for row in item["observations"]],
            ["2023-24"],
        )
        self.assertTrue(result["coverage_gaps"])

    def test_energy_all_equals_union_of_groups_with_original_grain(self):
        sheets = {
            "AUS CY": test_domestic.DomesticIntegrityTests.energy_rows(),
            "AUS FY": test_domestic.DomesticIntegrityTests.energy_rows(
                {"B": "2023-24", "C": "2024-25"}
            ),
        }
        curation = energy_workbook.discover_curation(sheets)
        full = energy_workbook.build_resolved_dataset(FLOW, curation, sheets, "all")
        selected = [
            energy_workbook.build_resolved_dataset(FLOW, curation, sheets, key)
            for key in ("AUS_CY", "AUS_FY")
        ]

        def normalize(result):
            return sorted(json.dumps(s, sort_keys=True) for s in result["series"])

        self.assertEqual(
            normalize(full), sorted(s for result in selected for s in normalize(result))
        )


class ComtradeScopeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(macro_data, "_live_comtrade_codes", side_effect=comtrade_codes)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.args = dict(
            reporter_codes=["36"],
            partner_codes=["0"],
            hs_codes=["TOTAL"],
            flow_code="M",
            frequency_code="A",
            start_year=2023,
            end_year=2023,
        )

    def fetch(self, **changes):
        return macro_data._fetch_comtrade(entry("comtrade"), {}, **{**self.args, **changes})

    @staticmethod
    def reply(url, *, params, **kwargs):
        rows = [
            {
                **{key: value for key, value in params.items() if key != "period"},
                "period": period,
                "primaryValue": 12,
                "classificationCode": "H6",
            }
            for period in params["period"].split(",")
        ]
        return response({"data": rows})

    def test_six_digit_and_non_total_breakdowns_preserve_coordinates(self):
        codes = {
            "HS": [{"code": "090111", "label": "Unroasted coffee"}],
            "TRANSPORT": [{"code": "1000", "label": "Air"}],
            "CUSTOMS": [{"code": "C01", "label": "Test procedure"}],
            "SECOND_PARTNER": [{"code": "554", "label": "New Zealand"}],
        }
        with (
            patch.object(
                macro_data,
                "_live_comtrade_codes",
                side_effect=lambda name: codes.get(name, comtrade_codes(name)),
            ),
            patch.object(macro_data.source_http, "get", side_effect=self.reply),
        ):
            result = self.fetch(
                hs_codes=["090111"],
                source_filters={
                    "TRANSPORT": ["1000"],
                    "CUSTOMS": ["C01"],
                    "SECOND_PARTNER": ["554"],
                },
            )
        dims = result["series"][0]["dimensions"]
        self.assertEqual(
            (dims["HS"], dims["TRANSPORT"], dims["CUSTOMS"], dims["SECOND_PARTNER"]),
            ("090111", "1000", "C01", "554"),
        )

    def test_empty_cube_keeps_other_requested_cube(self):
        def reply(url, *, params, **kwargs):
            return (
                response({"data": []})
                if params["reporterCode"] == "554"
                else self.reply(url, params=params)
            )

        with patch.object(macro_data.source_http, "get", side_effect=reply):
            result = self.fetch(reporter_codes=["36", "554"])
        self.assertEqual(len(result["series"]), 1)
        self.assertIn("554/0/TOTAL", result["coverage_gaps"][0]["codes"][0])

    def test_large_requests_are_chunked_without_artificial_caps(self):
        with patch.object(macro_data.source_http, "get", side_effect=self.reply) as get:
            result = self.fetch(frequency_code="M", start_year=1920, end_year=2024)
        self.assertEqual(get.call_count, 105)
        self.assertEqual(len(result["series"][0]["points"]), 1260)
        self.assertEqual(result["series"][0]["points"][-1]["x"], "2024-12")

    def test_public_preview_truncation_still_fails(self):
        with (
            patch.object(macro_data.settings, "comtrade_api_key", ""),
            patch.object(
                macro_data.source_http, "get", return_value=response({"data": [{}] * 500})
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "500-row"):
                self.fetch()

    def test_unknown_extended_codes_fail_before_data_request(self):
        with (
            patch.object(macro_data, "_live_comtrade_codes", side_effect=comtrade_codes),
            patch.object(
                macro_data.source_http,
                "get",
                side_effect=AssertionError("Data must not be requested"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Invalid TRANSPORT"):
                self.fetch(source_filters={"TRANSPORT": ["wrong"]})


class PartialManifestTests(unittest.TestCase):
    def test_gap_summary_bounded_full_evidence_and_changing_units_preserved(self):
        gaps = [
            {
                "dimension": str(index),
                "codes": [str(code) for code in range(25)],
                "reason": "No data",
            }
            for index in range(12)
        ]
        payload = {
            "kind": "macro_retrieve",
            "coverage_gaps": gaps,
            "series": [
                {
                    "unit": "varies; see point.unit",
                    "points": [
                        {"x": "2023", "y": 1, "unit": "USD"},
                        {"x": "2024", "y": 2, "unit": "AUD"},
                    ],
                }
            ],
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(artifacts, "RUNTIME_DIR", Path(directory)),
        ):
            manifest = artifacts.store_retrieval(payload, "worldbank::TEST", "Test")
            saved = json.loads(Path(manifest["artifact_path"]).read_text())
        self.assertEqual(manifest["coverage_status"], "partial")
        self.assertEqual(len(manifest["coverage_gaps"]), 10)
        self.assertEqual(len(manifest["coverage_gaps"][0]["codes"]), 20)
        self.assertEqual(saved["coverage_gaps"], gaps)
        self.assertEqual(manifest["unit_examples"], ["AUD", "USD"])
        self.assertTrue(any("Units vary" in warning for warning in manifest["warnings"]))
