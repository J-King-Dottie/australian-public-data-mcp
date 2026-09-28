from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ausdata_mcp import artifacts, server
from ausdata_mcp.pacific_data import PacificDataService, parse_dataset_id

NAMESPACES = 'xmlns:s="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure" xmlns:c="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"'
CATALOGUE = f"""<Root {NAMESPACES}>
<s:Dataflow id="DF_TEST" agencyID="SPC" version="1.0"><c:Name xml:lang="fr">Population française</c:Name><c:Name xml:lang="en">Pacific population</c:Name></s:Dataflow>
<s:Dataflow id="DF_ENERGY" agencyID="SPC" version="2.0"><c:Name>Pacific energy</c:Name></s:Dataflow>
</Root>"""
METADATA = f"""<Root {NAMESPACES}>
<s:Dataflow id="DF_TEST" agencyID="SPC" version="1.0"><c:Name>Pacific population</c:Name>
<c:Annotations><c:Annotation><c:AnnotationType>NonProductionDataflow</c:AnnotationType><c:AnnotationText>true</c:AnnotationText></c:Annotation></c:Annotations>
<s:Structure><Ref id="DSD_TEST" agencyID="SPC" version="1.0"/></s:Structure></s:Dataflow>
<s:DataStructure id="DSD_TEST" agencyID="SPC" version="1.0"><s:DataStructureComponents><s:DimensionList>
<s:Dimension id="INDICATOR" position="4"><s:LocalRepresentation><s:Enumeration><Ref class="Codelist" id="CL_IND" agencyID="SPC" version="1.0"/></s:Enumeration></s:LocalRepresentation></s:Dimension>
<s:TimeDimension id="TIME_PERIOD" position="2"/>
<s:Dimension id="GEO_PICT" position="3"><s:LocalRepresentation><s:Enumeration><Ref class="Codelist" id="CL_GEO" agencyID="SPC" version="2.0"/></s:Enumeration></s:LocalRepresentation></s:Dimension>
<s:Dimension id="FREQ" position="1"><s:LocalRepresentation><s:Enumeration><Ref class="Codelist" id="CL_FREQ" agencyID="OTHER" version="1.1"/></s:Enumeration></s:LocalRepresentation></s:Dimension>
</s:DimensionList><s:AttributeList><s:Attribute id="UNIT_MULT"><s:LocalRepresentation><s:Enumeration><Ref class="Codelist" id="CL_MULT" agencyID="SPC" version="1.0"/></s:Enumeration></s:LocalRepresentation></s:Attribute></s:AttributeList>
</s:DataStructureComponents></s:DataStructure></Root>"""
CSV = """DATAFLOW,FREQ,GEO_PICT,INDICATOR,TIME_PERIOD,OBS_VALUE,UNIT_MEASURE,UNIT_MULT,OBS_STATUS,OBS_COMMENT
SPC:DF_TEST(1.0),A,FJ,POP,2020,1.25,PERSONS,3,,"First line
Second line"
SPC:DF_TEST(1.0),A,FJ,POP,2021,,PERSONS,3,M,Suppressed
"""
DATASET = "pdh::SPC::DF_TEST::1.0"


def codelist(code_id, codes):
    content = "".join(
        f'<s:Code id="{code}"><c:Name>{label}</c:Name></s:Code>' for code, label in codes
    )
    agency, version = {
        "CL_FREQ": ("OTHER", "1.1"),
        "CL_GEO": ("SPC", "2.0"),
    }.get(code_id, ("SPC", "1.0"))
    return f'<Root {NAMESPACES}><s:Codelist id="{code_id}" agencyID="{agency}" version="{version}">{content}</s:Codelist></Root>'


class PacificTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = PacificDataService()
        self.requests = []
        self.csv = CSV

        def get(path, params=None):
            self.requests.append((path, params))
            if path == "dataflow/SPC/all/latest":
                body = CATALOGUE
            elif path == "dataflow/SPC/DF_TEST/1.0":
                body = METADATA
            elif path == "codelist/OTHER/CL_FREQ/1.1":
                body = codelist("CL_FREQ", [("A", "Annual"), ("Q", "Quarterly")])
            elif path == "codelist/SPC/CL_GEO/2.0":
                body = codelist(
                    "CL_GEO", [("FJ", "Fiji"), ("SB", "Solomon Islands"), ("TO", "Tonga")]
                )
            elif path == "codelist/SPC/CL_IND/1.0":
                body = codelist("CL_IND", [("POP", "Population")])
            elif path == "codelist/SPC/CL_MULT/1.0":
                body = codelist("CL_MULT", [("3", "Thousands")])
            elif path.startswith("data/"):
                body = self.csv
            else:
                raise AssertionError(path)
            return httpx.Response(
                200,
                text=body,
                request=httpx.Request("GET", f"https://example.test/{path}", params=params),
            )

        self.get = get
        patcher = patch.object(self.service, "_get", side_effect=get)
        patcher.start()
        self.addCleanup(patcher.stop)
        for module, name, value in (
            (artifacts, "RUNTIME_DIR", self.root / "artifacts"),
            (server, "get_pacific_service", lambda: self.service),
        ):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(
            server,
            "get_unified_catalog_entry",
            side_effect=lambda identity: next(
                (e for e in self.service.catalogue() if e["datasetId"] == identity), None
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_catalogue_normalizes_english_labels(self):
        entries = self.service.catalogue()
        self.assertEqual(entries[0]["title"], "Pacific population")
        self.assertEqual(entries[0]["datasetId"], DATASET)
        self.assertEqual(entries[0]["route"], "pacific")

    def test_search_uses_shared_index_including_provider_filter(self):
        payload = {
            "candidates": [{"datasetId": DATASET, "provider": "Pacific Data Hub"}],
            "total": 1,
        }
        with patch.object(server, "search_unified_catalog", return_value=payload) as search:
            result = server.search_catalog("population", provider="SPC", forceRefresh=True)
        self.assertEqual(result["total"], 1)
        search.assert_called_once_with(
            "population", limit=50, offset=0, force_refresh=True, provider="SPC", match="any"
        )

    def test_metadata_order_and_codelist_agency_pagination(self):
        with self.assertRaisesRegex(ValueError, "Set dimension"):
            server.get_metadata(DATASET, codeSearch="Fiji")
        metadata = server.get_metadata(DATASET)
        self.assertEqual(metadata["key_order"], ["FREQ", "GEO_PICT", "INDICATOR"])
        frequencies = server.get_metadata(DATASET, dimension="FREQ")
        self.assertEqual(frequencies["codes"][0]["code"], "A")
        self.assertTrue(any(path == "codelist/OTHER/CL_FREQ/1.1" for path, _ in self.requests))
        first = server.get_metadata(DATASET, dimension="GEO_PICT", codeLimit=2)
        last = server.get_metadata(
            DATASET, dimension="GEO_PICT", codeOffset=first["next_offset"], codeLimit=2
        )
        self.assertEqual(last["codes"][0]["code"], "TO")
        self.assertIsNone(last["next_offset"])
        self.assertEqual(
            server.get_metadata(DATASET, dimension="GEO_PICT", codeSearch="Fiji")["matching_codes"],
            1,
        )
        self.assertEqual(
            server.get_metadata(DATASET, dimension="UNIT_MULT")["codes"][0]["label"], "Thousands"
        )

    def test_retrieval_manifest_points_to_complete_evidence(self):
        manifest = server.retrieve(
            DATASET, sourceFilters={"FREQ": ["A"], "GEO_PICT": ["FJ"], "INDICATOR": ["POP"]}
        )
        payload = json.loads(Path(manifest["artifact_path"]).read_text(encoding="utf-8"))
        observations = payload["series"][0]["observations"]
        self.assertEqual(manifest["row_count"], 2)
        self.assertIn("GEO_PICT", manifest["dimension_ids"])
        self.assertEqual(manifest["unit_multiplier_codes"], ["3"])
        self.assertEqual(observations[0]["value"], 1.25)
        self.assertEqual(observations[0]["attributes"]["OBS_COMMENT"], "First line\nSecond line")
        self.assertIsNone(observations[1]["value"])
        self.assertEqual(observations[1]["attributes"]["OBS_STATUS"], "M")
        self.assertEqual(payload["source_annotations"]["NonProductionDataflow"], ["true"])
        self.assertTrue(payload["source_references"][0]["api_request_url"])

    def test_full_dataset_needs_no_narrowing_filter(self):
        for arguments in ({}, {"sourceFilters": {}}, {"dataKey": "all"}, {"dataKey": ".."}):
            manifest = server.retrieve(DATASET, startPeriod="2020", endPeriod="2021", **arguments)
            payload = json.loads(Path(manifest["artifact_path"]).read_text())
            self.assertEqual(manifest["row_count"], 2)
            self.assertEqual(payload["retrieval"]["source_filters"], {})
            path, params = self.requests[-1]
            self.assertEqual(path, "data/SPC,DF_TEST,1.0/all")
            self.assertEqual(params["startPeriod"], "2020")
            self.assertEqual(params["endPeriod"], "2021")

    def test_unknown_dimensions_codes_and_invalid_selection_rejected(self):
        for filters in ({"TYPO": ["FJ"]}, {"GEO_PICT": ["FJI"]}, {"GEO_PICT": []}):
            with self.assertRaises(ValueError):
                self.service.retrieve(DATASET, filters)
        with self.assertRaises(ValueError):
            server.retrieve(DATASET, countries=["FJI"])
        with self.assertRaises(ValueError):
            self.service.retrieve(DATASET, {"GEO_PICT": ["FJ"]}, key="A.FJ.POP")
        self.assertFalse(any(path.startswith("data/") for path, _ in self.requests))

    def test_positional_key_is_checked_and_periods_forwarded(self):
        self.service.retrieve(DATASET, key="A.FJ.POP", start="2020", end="2021")
        path, params = self.requests[-1]
        self.assertEqual(path, "data/SPC,DF_TEST,1.0/A.FJ.POP")
        self.assertEqual(params["startPeriod"], "2020")
        self.assertEqual(params["endPeriod"], "2021")

    def test_error_pages_malformed_csv_and_wrong_geography_rejected(self):
        for content in ("<html>Unavailable</html>", CSV.replace("A,FJ", "A,SB"), CSV + "bad,row\n"):
            self.csv = content
            with self.assertRaises(RuntimeError):
                self.service.retrieve(DATASET, {"GEO_PICT": ["FJ"]})

    def test_missing_requested_codes_are_not_silently_omitted(self):
        payload = self.service.retrieve(DATASET, {"GEO_PICT": ["FJ", "SB"]})
        self.assertEqual(payload["coverage_gaps"][0]["codes"], ["SB"])
        self.assertEqual(len(payload["series"][0]["observations"]), 2)

    def test_out_of_period_and_duplicate_observations_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "outside the requested periods"):
            self.service.retrieve(DATASET, {"GEO_PICT": ["FJ"]}, start="2021")
        self.csv = CSV + "SPC:DF_TEST(1.0),A,FJ,POP,2020,1.25,PERSONS,3,,Duplicate\n"
        with self.assertRaisesRegex(RuntimeError, "duplicate observations"):
            self.service.retrieve(DATASET, {"GEO_PICT": ["FJ"]})

    def test_force_refresh_refreshes_validation_codelists(self):
        self.service.retrieve(DATASET, {"GEO_PICT": ["FJ"]})
        self.requests.clear()
        self.service.retrieve(DATASET, {"GEO_PICT": ["FJ"]}, refresh=True)
        self.assertTrue(any(path == "codelist/SPC/CL_GEO/2.0" for path, _ in self.requests))

    def test_bad_catalogue_is_rejected(self):
        for xml in ("<html>invalid", "<Root/>"):
            with patch.object(self.service, "_get", return_value=httpx.Response(200, text=xml)):
                with self.assertRaises(RuntimeError):
                    self.service.catalogue()

    def test_dataset_path_injection_rejected(self):
        for identity in ("pdh::SPC::../secret::1.0", "pdh::SPC::DF_TEST?url=evil::1.0"):
            with self.assertRaises(ValueError):
                parse_dataset_id(identity)


if __name__ == "__main__":
    unittest.main()
