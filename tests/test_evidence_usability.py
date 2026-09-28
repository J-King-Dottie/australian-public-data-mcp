"""Cross-domain evidence summaries and repairable errors, independent of providers."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree.ElementTree import ParseError

import httpx

from ausdata_mcp import artifacts, server
from ausdata_mcp.errors import tool_error_result


class EvidenceSummaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = patch.object(artifacts, "RUNTIME_DIR", Path(directory.name))
        runtime.start()
        self.addCleanup(runtime.stop)

    def test_non_null_bounds_keep_zero_and_do_not_infer_missing_periods(self):
        payload = {
            "kind": "macro_retrieve",
            "series": [
                {
                    "country_code": "NZL",
                    "series_id": "TEST",
                    "frequency": "A",
                    "unit": "USD",
                    "points": [
                        {"x": "2019", "y": None},
                        {"x": "2021", "y": 0},
                        {"x": "2023", "y": 12},
                        {"x": "2027", "y": None},
                    ],
                }
            ],
        }
        manifest = artifacts.store_retrieval(payload, "test", "Test")
        self.assertEqual((manifest["period_start"], manifest["period_end"]), ("2019", "2027"))
        self.assertEqual(
            (manifest["non_null_period_start"], manifest["non_null_period_end"]), ("2021", "2023")
        )
        self.assertEqual(manifest["non_null_value_count"], 2)
        self.assertEqual(manifest["missing_value_count"], 2)
        self.assertEqual(manifest["coverage_gaps"], [])
        summary = manifest["series_summaries"][0]
        self.assertEqual(summary["dimension_codes"], {"country_code": "NZL", "series_id": "TEST"})
        self.assertEqual(summary["non_null_period_end"], "2023")
        self.assertEqual([r["period"] for r in manifest["preview_rows"]], ["2019", "2021", "2027"])

    def test_mixed_series_keep_their_own_coverage_units_and_raw_evidence(self):
        payload = {
            "kind": "macro_retrieve",
            "series": [
                {
                    "series_key": "AREA1.EXPORTS.A",
                    "frequency": "A",
                    "unit": "varies; see point.unit",
                    "dimensions": {"AREA": "AREA1", "FLOW": "EXPORTS", "FREQ": "A"},
                    "points": [
                        {"x": "2022", "y": 2, "unit": "USD", "source_row": {"UNIT_MULT": "6"}},
                        {"x": "2023", "y": 3, "unit": "EUR", "source_row": {"UNIT_MULT": "0"}},
                    ],
                },
                {
                    "series_key": "REGION2.RAIN.M",
                    "frequency": "M",
                    "unit": "mm",
                    "dimensions": {"REGION": "REGION2", "MEASURE": "RAIN", "FREQ": "M"},
                    "points": [
                        {
                            "x": "2024-01",
                            "y": None,
                            "source_row": {"OBS_VALUE": "..", "OBS_STATUS": "C"},
                        }
                    ],
                },
            ],
        }
        original = copy.deepcopy(payload)
        manifest = artifacts.store_retrieval(payload, "test", "Test")
        first, second = manifest["series_summaries"]
        self.assertEqual(first["unit_examples"], ["EUR", "USD"])
        self.assertEqual(first["unit_multiplier_codes"], ["0", "6"])
        self.assertEqual(first["frequency_examples"], ["A"])
        self.assertEqual(second["frequency_examples"], ["M"])
        self.assertEqual(second["period_end"], "2024-01")
        self.assertIsNone(second["non_null_period_start"])
        self.assertIsNone(second["non_null_period_end"])
        self.assertEqual(second["missing_value_count"], 1)
        self.assertEqual([r["series_index"] for r in manifest["preview_rows"]], [0, 1, 0])
        saved = json.loads(Path(manifest["artifact_path"]).read_text())
        self.assertEqual(saved["series"], original["series"])

    def test_domestic_summary_preserves_observation_grain_and_flags(self):
        payload = {
            "series": [
                {
                    "seriesKey": "REGION.A",
                    "dimensions": {
                        "REGION": {"code": "R1", "label": "Region 1"},
                        "FREQ": {"code": "A"},
                    },
                    "attributes": {"UNIT_MULT": {"code": "0"}, "UNIT_MEASURE": {"code": "COUNT"}},
                    "observations": [
                        {
                            "observationKey": "0",
                            "value": 0,
                            "dimensions": {
                                "TIME_PERIOD": {"code": "2022"},
                                "CATEGORY": {"code": "C1"},
                            },
                        },
                        {
                            "observationKey": "1",
                            "value": None,
                            "raw_value": "..",
                            "attributes": {"OBS_STATUS": {"code": "C"}},
                            "dimensions": {
                                "TIME_PERIOD": {"code": "2023"},
                                "CATEGORY": {"code": "C2"},
                            },
                        },
                    ],
                }
            ]
        }
        manifest = artifacts.store_retrieval(payload, "test", "Test")
        summary = manifest["series_summaries"][0]
        self.assertEqual(summary["dimension_codes"], {"REGION": "R1", "FREQ": "A"})
        self.assertIn("CATEGORY", manifest["dimension_ids"])
        self.assertEqual(summary["unit_multiplier_codes"], ["0"])
        self.assertEqual(summary["non_null_period_end"], "2022")
        self.assertEqual(summary["period_end"], "2023")
        self.assertIn("OBS_STATUS", manifest["attribute_ids"])
        self.assertEqual(manifest["preview_rows"][1]["dimension_codes"]["CATEGORY"], "C2")
        saved = json.loads(Path(manifest["artifact_path"]).read_text())
        self.assertEqual(saved["series"][0]["observations"][1]["raw_value"], "..")

    def test_bounded_inventory_does_not_truncate_complete_evidence(self):
        payload = {
            "kind": "macro_retrieve",
            "series": [
                {
                    "series_key": str(i) + "x" * 300,
                    "dimensions": {f"D{j}": "a" * 90 for j in range(15)},
                    "frequency": "A",
                    "unit": "count",
                    "points": [{"x": "2020", "y": i}],
                }
                for i in range(25)
            ],
        }
        manifest = artifacts.store_retrieval(payload, "test", "Test")
        self.assertEqual(manifest["series_count"], 25)
        self.assertEqual(len(manifest["series_summaries"]), 20)
        self.assertTrue(manifest["summary_truncated"]["series_summaries"])
        self.assertTrue(all(s["truncated"] for s in manifest["series_summaries"]))
        self.assertEqual(len(manifest["series_summaries"][0]["dimension_codes"]), 12)
        self.assertEqual([r["series_index"] for r in manifest["preview_rows"]], [0, 1, 2])
        saved = json.loads(Path(manifest["artifact_path"]).read_text())
        self.assertEqual(saved["series"], payload["series"])

    def test_all_null_series_report_no_non_null_coverage(self):
        payload = {"kind": "macro_retrieve", "series": [{"points": [{"x": "2020", "y": None}]}]}
        manifest = artifacts.store_retrieval(payload, "test", "Test")
        self.assertEqual(manifest["non_null_value_count"], 0)
        self.assertIsNone(manifest["non_null_period_start"])
        self.assertIsNone(manifest["non_null_period_end"])
        self.assertTrue(any("suppressed" in w for w in manifest["warnings"]))


class StructuredErrorTests(unittest.TestCase):
    def test_http_failure_preserves_request_and_excludes_transport_secrets(self):
        arguments = {
            "datasetId": "oecd::TEST",
            "sourceFilters": {"AREA": ["A", "B"]},
            "startPeriod": "2020",
        }
        request = httpx.Request(
            "GET",
            "https://example.test/data?api_key=SECRET",
            headers={"Authorization": "Bearer SECRET"},
        )
        response = httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        exc = httpx.HTTPStatusError("SECRET raw body", request=request, response=response)
        result = tool_error_result(exc, "retrieve", arguments)
        error = result.structuredContent["error"]
        self.assertTrue(result.isError)
        self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        self.assertEqual(error["request"], arguments)
        self.assertEqual(error["provider_host"], "example.test")
        self.assertEqual(error["code"], "rate_limited")
        self.assertTrue(error["retryable"])
        self.assertEqual(error["retry_after_seconds"], 1)
        self.assertNotIn("SECRET", result.content[0].text)

    def test_large_failed_request_discloses_omission(self):
        result = tool_error_result(
            ValueError("Invalid selection"), "retrieve", {"sourceFilters": {"AREA": ["x" * 7000]}}
        )
        self.assertTrue(result.structuredContent["error"]["request_truncated"])
        self.assertEqual(result.structuredContent["error"]["request"], {})
        self.assertLess(len(result.content[0].text), 1000)

    def test_wrapped_source_guidance_is_not_lost_to_parser_cause(self):
        try:
            try:
                raise ParseError("line 1")
            except ParseError as exc:
                raise RuntimeError("Source returned invalid XML; retry metadata later.") from exc
        except RuntimeError as exc:
            result = tool_error_result(exc, "get_metadata", {})
        self.assertEqual(result.structuredContent["error"]["code"], "source_result_error")
        self.assertIn("retry metadata", result.structuredContent["error"]["message"])

    def test_all_three_tools_return_equivalent_error_representations(self):
        async def check():
            for name, arguments in [
                ("search_catalog", {"offset": -1}),
                ("get_metadata", {}),
                ("retrieve", {"datasetId": ""}),
                ("not_a_tool", {}),
            ]:
                with self.subTest(tool=name):
                    result = await server.server.call_tool(name, arguments)
                    self.assertTrue(result.isError)
                    self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
                    self.assertEqual(result.structuredContent["error"]["tool"], name)

        asyncio.run(check())
