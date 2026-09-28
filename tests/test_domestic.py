"""Offline regressions for source decoding and domestic file reuse."""

import io
import unittest
import zipfile
from unittest.mock import patch

import httpx

from ausdata_mcp import energy_workbook, rba_tables
from ausdata_mcp.domestic_data import CustomDomesticService, DomesticDataService

FLOW = {
    "id": "TEST",
    "agencyID": "CUSTOM_AUS",
    "version": "1.0",
    "name": "Test",
    "description": "Fixture",
    "sourceUrl": "https://example.test/table.csv",
    "curation": {},
}
CSV = b"Test table,\nTitle,Test series\nUnits,Percent\nFrequency,Monthly\nSeries ID,FTEST\n31-Dec-2023,2\n31-Jan-2024,\n29-Feb-2024,3\n"


def abs_payload():
    return {
        "data": {
            "structures": [
                {
                    "dimensions": {
                        "series": [{"id": "MEASURE", "values": [{"id": "A", "name": "Actual"}]}],
                        "observation": [
                            {"id": "TIME_PERIOD", "values": [{"id": "2023"}, {"id": "2024"}]}
                        ],
                    },
                    "attributes": {
                        "series": [
                            {
                                "id": "UNIT_MULT",
                                "values": [
                                    {"id": "6", "name": "Millions"},
                                    {"id": "0", "name": "Units"},
                                ],
                            }
                        ],
                        "observation": [
                            {
                                "id": "OBS_STATUS",
                                "values": [
                                    {"id": "A", "name": "Normal"},
                                    {"id": "C", "name": "Confidential"},
                                ],
                            }
                        ],
                    },
                }
            ],
            "dataSets": [
                {
                    "series": {
                        "0": {"attributes": [0], "observations": {"0": [12, 0], "1": ["..", 1]}}
                    }
                }
            ],
        }
    }


class ABSDecodingTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(DomesticDataService)

    def test_attribute_indexes_preserve_codes_and_suppressed_values(self):
        result = self.service._transform_json_data(FLOW, {}, abs_payload())
        series = result["series"][0]
        self.assertEqual(series["attributes"]["UNIT_MULT"], {"code": "6", "label": "Millions"})
        missing = series["observations"][1]
        self.assertIsNone(missing["value"])
        self.assertEqual(missing["attributes"]["OBS_VALUE_RAW"], "..")
        self.assertEqual(missing["attributes"]["OBS_STATUS"]["code"], "C")
        self.assertEqual(missing["dimensions"]["TIME_PERIOD"]["code"], "2024")
        self.assertIn("source_structure", result)

    def test_invalid_coordinates_never_fall_back_to_first_code(self):
        for key in ("-1", "abc", "", "2", "0:1"):
            payload = abs_payload()
            payload["data"]["dataSets"][0]["series"] = {key: {"observations": {"0": [1]}}}
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "invalid"):
                self.service._transform_json_data(FLOW, {}, payload)

    def test_empty_and_multiple_datasets_are_not_successful_evidence(self):
        for datasets in ([{"series": {}}], [{"series": {"0": {"observations": {}}}}], [{}, {}]):
            payload = abs_payload()
            payload["data"]["dataSets"] = datasets
            with self.subTest(datasets=datasets), self.assertRaises(RuntimeError):
                self.service._transform_json_data(FLOW, {}, payload)

    def test_dataset_observations_and_attributes_are_preserved(self):
        payload = abs_payload()
        structure = payload["data"]["structures"][0]
        structure["dimensions"]["dataSet"] = structure["dimensions"].pop("series")
        structure["attributes"]["dataSet"] = structure["attributes"].pop("series")
        payload["data"]["dataSets"] = [{"attributes": [1], "observations": {"0": [5, 0]}}]
        result = self.service._transform_json_data(FLOW, {}, payload)
        self.assertEqual(result["series"][0]["dimensions"]["MEASURE"]["code"], "A")
        self.assertEqual(result["series"][0]["attributes"]["UNIT_MULT"]["code"], "0")

    def test_leaf_xml_references_are_not_discarded(self):
        xml = """<Structure><DataStructure id="TEST"><DataStructureComponents>
        <DimensionList><Dimension id="MEASURE" position="1"><Role><Ref id="ROLE"/></Role></Dimension></DimensionList>
        <AttributeList><Attribute id="UNIT"><AttributeRelationship><Dimension><Ref id="MEASURE"/></Dimension>
        <Group><Ref id="GROUP"/></Group></AttributeRelationship></Attribute></AttributeList>
        </DataStructureComponents></DataStructure><Codelist id="CODES"><Code id="B"><Parent><Ref id="A"/></Parent></Code></Codelist></Structure>"""
        result = self.service._extract_data_structure(xml)
        self.assertEqual(result["dimensions"][0]["role"], "ROLE")
        self.assertEqual(result["attributes"][0]["relatedTo"], ["MEASURE", "GROUP"])
        self.assertEqual(result["codelists"][0]["codes"][0]["parentID"], "A")

    def test_metadata_keeps_only_exact_referenced_concepts_including_measure_and_roles(self):
        xml = """<Structure><DataStructure id="TEST"><DataStructureComponents>
        <DimensionList><Dimension id="MEASURE" position="1"><ConceptIdentity>
        <Ref id="MEASURE" agencyID="ABS" maintainableParentID="CS"/>
        </ConceptIdentity><ConceptRole><Ref class="Concept" id="ROLE" agencyID="ABS"
        maintainableParentID="CS"/></ConceptRole></Dimension>
        <TimeDimension id="TIME_PERIOD"><ConceptIdentity><Ref id="TIME" agencyID="ABS"
        maintainableParentID="CS"/></ConceptIdentity></TimeDimension></DimensionList>
        <MeasureList><PrimaryMeasure id="OBS_VALUE"><ConceptIdentity><Ref id="VALUE"
        agencyID="ABS" maintainableParentID="CS"/></ConceptIdentity></PrimaryMeasure></MeasureList>
        <AttributeList><Attribute id="UNIT"><ConceptIdentity><Ref id="UNIT" agencyID="ABS"
        maintainableParentID="CS"/></ConceptIdentity></Attribute></AttributeList>
        </DataStructureComponents></DataStructure>
        <ConceptScheme id="CS" agencyID="OTHER"><Concept id="MEASURE"><Name>Wrong agency</Name></Concept></ConceptScheme>
        <ConceptScheme id="CS" agencyID="ABS" version="2.0"><Concept id="MEASURE"><Name>Wrong version</Name></Concept></ConceptScheme>
        <ConceptScheme id="CS" agencyID="ABS">
        <Concept id="MEASURE"><Name>Measure</Name><Description>Definition preserved</Description></Concept>
        <Concept id="ROLE"/><Concept id="TIME"/><Concept id="VALUE"/><Concept id="UNIT"/>
        <Concept id="UNUSED"/></ConceptScheme>
        <Codelist id="CL"><Code id="A"/><Code id="B"/></Codelist></Structure>"""
        result = self.service._extract_data_structure(xml)
        self.assertEqual(
            {item["id"] for item in result["concepts"]},
            {"MEASURE", "ROLE", "TIME", "VALUE", "UNIT"},
        )
        self.assertEqual(result["unreferenced_concept_count"], 3)
        self.assertEqual(result["concepts"][0]["description"], "Definition preserved")
        self.assertEqual(len(result["codelists"][0]["codes"]), 2)

    def test_unrelated_concepts_do_not_suggest_unsupported_dimensions(self):
        xml = """<Structure><DataStructure id="TEST"><DataStructureComponents>
        <DimensionList><Dimension id="REGION" position="1"><ConceptIdentity>
        <Ref id="REGION" agencyID="ABS" maintainableParentID="GEOGRAPHY" maintainableParentVersion="1.0.0"/>
        </ConceptIdentity></Dimension></DimensionList></DataStructureComponents></DataStructure>
        <ConceptScheme id="GEOGRAPHY" agencyID="ABS" version="1.0.0">
        <Concept id="REGION"><Name>State</Name></Concept>
        <Concept id="POSTCODE"><Name>Postal area</Name></Concept></ConceptScheme></Structure>"""
        result = self.service._extract_data_structure(xml)
        self.assertEqual([item["id"] for item in result["concepts"]], ["REGION"])
        self.assertEqual(result["unreferenced_concept_count"], 1)


class DomesticFileTests(unittest.TestCase):
    def test_rba_preserves_missingness_and_chronological_periods(self):
        parsed = rba_tables.parse_table(rba_tables.load_rows(CSV))
        result = rba_tables.build_resolved_dataset(FLOW, parsed, {})
        observations = result["series"][0]["observations"]
        self.assertEqual(
            [row["observationKey"] for row in observations],
            ["2023-12-31", "2024-01-31", "2024-02-29"],
        )
        self.assertIsNone(observations[1]["value"])
        self.assertEqual(observations[1]["attributes"]["OBS_VALUE_RAW"], "")
        self.assertEqual(result["observationCount"], 3)

    def test_rba_rejects_duplicate_ids_extra_columns_and_unknown_series(self):
        for content, key in (
            (CSV.replace(b"Series ID,FTEST", b"Series ID,FTEST,FTEST"), "all"),
            (CSV.replace(b"31-Jan-2024,", b"31-Jan-2024,1,2"), "all"),
            (CSV, "MISSING"),
        ):
            with self.subTest(content=content, key=key), self.assertRaises(ValueError):
                rba_tables.build_resolved_dataset(
                    FLOW, rba_tables.parse_table(rba_tables.load_rows(content)), {}, key
                )

    def test_custom_metadata_then_retrieval_reuses_file_and_refresh_bypasses(self):
        response = httpx.Response(200, request=httpx.Request("GET", FLOW["sourceUrl"]), content=CSV)
        service = CustomDomesticService("rba_tables_csv")
        with patch("ausdata_mcp.domestic_data.source_http.get", return_value=response) as get:
            metadata = service.get_metadata(FLOW)
            retrieved = service.resolve(FLOW, data_key="FTEST")
            self.assertEqual(metadata["source_fetched_at"], retrieved["source_fetched_at"])
            self.assertFalse(metadata["source_cache_hit"])
            self.assertTrue(retrieved["source_cache_hit"])
            self.assertEqual(get.call_count, 1)
            service.resolve(FLOW, data_key="FTEST", force_refresh=True)
            self.assertEqual(get.call_count, 2)
            service.get_metadata({**FLOW, "sourceUrl": "https://example.test/new.csv"})
            self.assertEqual(get.call_count, 3)

    def test_abs_metadata_reuses_structure_until_forced(self):
        service = DomesticDataService()
        self.addCleanup(service.api_client._client.close)
        xml = '<Structure><DataStructure id="TEST"/></Structure>'
        with (
            patch.object(service, "resolve_flow", return_value=FLOW),
            patch.object(service.api_client, "get_data_structure_xml", return_value=xml) as get,
        ):
            service.get_data_structure_for_dataflow("ABS,TEST,1.0")
            service.get_data_structure_for_dataflow("ABS,TEST,1.0")
            self.assertEqual(get.call_count, 1)
            service.get_data_structure_for_dataflow("ABS,TEST,1.0", force_refresh=True)
            self.assertEqual(get.call_count, 2)

    def test_workbook_reads_inline_strings_and_absolute_relationships(self):
        file = io.BytesIO()
        with zipfile.ZipFile(file, "w") as book:
            book.writestr(
                "xl/workbook.xml",
                """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="AUS FY" r:id="rId1"/></sheets></workbook>""",
            )
            book.writestr(
                "xl/_rels/workbook.xml.rels",
                """<Relationships><Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml"/></Relationships>""",
            )
            book.writestr(
                "xl/worksheets/sheet1.xml",
                """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>Fuel type</t></is></c><c r="B1"><v>2024</v></c></row></sheetData></worksheet>""",
            )
        sheets = energy_workbook.load_workbook(file)
        self.assertEqual(sheets["AUS FY"][0], {"A": "Fuel type", "B": "2024"})
        curation = energy_workbook.discover_curation(sheets)
        self.assertEqual(
            energy_workbook.select_group("AUS_FY", curation),
            ("national_financial_year", ["AUS FY"]),
        )
        self.assertEqual(energy_workbook.select_group("all", curation), ("all", ["AUS FY"]))


class DomesticIntegrityTests(unittest.TestCase):
    @staticmethod
    def energy_rows(headers=None, title=None):
        return [
            {"A": title or "Table O13 - Electricity generation, calendar year"},
            {"A": "Gigawatt hours (GWh)"},
            {"A": "Fuel type", **(headers or {"B": "2024", "C": "2025 (est.)"})},
            {"A": "Black coal", "B": "1", "C": "2"},
        ]

    def test_energy_estimate_label_preserved_with_normalized_period(self):
        rows = self.energy_rows()
        sheets = {"AUS CY": rows, "AUS FY": rows}
        result = energy_workbook.build_resolved_dataset(
            FLOW, energy_workbook.discover_curation(sheets), sheets, "AUS_CY"
        )
        observation = result["series"][0]["observations"][1]
        self.assertEqual(observation["observationKey"], "2025")
        self.assertEqual(
            observation["dimensions"]["TIME_PERIOD"], {"code": "2025", "label": "2025 (est.)"}
        )
        self.assertEqual(result["dimensions"]["TIME_PERIOD"]["2025"], "2025 (est.)")

    def test_energy_region_summary_requires_explicit_period(self):
        for title in ("Table O - State summary", "Table O - State summary 2023 and 2024"):
            with (
                self.subTest(title=title),
                self.assertRaisesRegex(ValueError, "one observation period"),
            ):
                energy_workbook.extract_sheet_records(
                    "State summary", self.energy_rows({"B": "NSW", "C": "VIC"}, title), "summary"
                )
        rows = self.energy_rows({"B": "NSW", "C": "VIC"}, "Table O - State summary 2024-25")
        records = energy_workbook.extract_sheet_records("State summary", rows, "summary")
        self.assertEqual(records[0]["time_period"], "2024-25")

    def test_energy_rejects_duplicate_coordinates_and_unknown_periods(self):
        for rows in (
            self.energy_rows({"B": "2025", "C": "2025 (est.)"}),
            self.energy_rows({"B": "2024", "C": "unknown 2025"}),
            self.energy_rows() + [{"A": "Black coal", "B": "3", "C": "4"}],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                energy_workbook.extract_sheet_records("AUS CY", rows, "national_calendar_year")

    def test_energy_group_cannot_silently_omit_empty_sheet(self):
        sheets = {"NSW CY": self.energy_rows(), "VIC CY": self.energy_rows()[:-1]}
        curation = {"sheetGroups": [{"id": "states", "sheets": list(sheets)}]}
        with self.assertRaisesRegex(ValueError, "contains no observations"):
            energy_workbook.build_resolved_dataset(FLOW, curation, sheets, "states")

    def test_rba_rejects_malformed_and_duplicate_dates(self):
        for row in (b"31-Jan-2024,9", b"2024/03/31,9", b",9"):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "date"):
                rba_tables.build_resolved_dataset(
                    FLOW, rba_tables.parse_table(rba_tables.load_rows(CSV + row + b"\n")), {}
                )

    def test_rba_short_year_date_formats(self):
        for date in ("31-12-23", "31/Dec/23", "31/12/23", "31-Dec-23"):
            with self.subTest(date=date):
                self.assertEqual(rba_tables.parse_period(date), "2023-12-31")

    def test_rba_omitted_trailing_cells_remain_explicit_missing_values(self):
        content = CSV.replace(b"Series ID,FTEST", b"Series ID,FTEST,SECOND").replace(
            b"31-Jan-2024,", b"31-Jan-2024"
        )
        payload = rba_tables.build_resolved_dataset(
            FLOW, rba_tables.parse_table(rba_tables.load_rows(content)), {}
        )
        self.assertEqual(payload["observationCount"], 6)
        first, second = payload["series"]
        self.assertEqual(first["observations"][0]["value"], 2)
        for row in [first["observations"][1], *second["observations"]]:
            self.assertIsNone(row["value"])
            self.assertEqual(row["attributes"], {"OBS_VALUE_RAW": "", "SOURCE_CELL_OMITTED": True})
