"""Reuse only complete, exactly identified structures already sent by the source."""

import unittest
from unittest.mock import Mock

import httpx

from ausdata_mcp.sdmx_structure import SDMXStructureClient

NS = 'xmlns:s="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure" xmlns:c="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"'
CODES = "".join(f'<s:Code id="C{i}"><c:Name>Code {i}</c:Name></s:Code>' for i in range(12))
LIST = f'<s:Codelist id="CL_AREA" agencyID="A" version="1.0">{CODES}</s:Codelist>'
STRUCTURE = f"""<Root {NS}>
<s:Dataflow id="FLOW" agencyID="A" version="1.0"><s:Structure><Ref id="DSD" agencyID="A" version="1.0"/></s:Structure></s:Dataflow>
<s:DataStructure id="DSD" agencyID="A" version="1.0"><s:DataStructureComponents><s:DimensionList>
<s:Dimension id="AREA" position="1">
<s:ConceptIdentity><Ref id="AREA" agencyID="A" maintainableParentID="CS" maintainableParentVersion="1.0"/></s:ConceptIdentity>
<s:LocalRepresentation><s:Enumeration><Ref class="Codelist" id="CL_AREA" agencyID="A" version="1.0"/></s:Enumeration></s:LocalRepresentation>
</s:Dimension></s:DimensionList></s:DataStructureComponents></s:DataStructure>
<s:ConceptScheme id="CS" agencyID="OTHER" version="1.0"><s:Concept id="AREA"><c:Name>Wrong label</c:Name></s:Concept></s:ConceptScheme>
<s:ConceptScheme id="CS" agencyID="A" version="1.0"><s:Concept id="AREA"><c:Name>Reference area</c:Name></s:Concept></s:ConceptScheme>
{{codelist}}</Root>"""
DATASET = "test::A::FLOW::1.0"


class EmbeddedStructureTests(unittest.TestCase):
    def service(self, embedded):
        service = SDMXStructureClient("https://source.test", 30, "test", "Test")
        service._get = Mock(
            side_effect=lambda path, params: httpx.Response(
                200,
                text=STRUCTURE.format(codelist=embedded)
                if path.startswith("dataflow/")
                else f"<Root {NS}>{LIST}</Root>",
            )
        )
        return service

    def test_labels_bounded_previews_pagination_and_full_validation_without_extra_fetch(self):
        service = self.service(LIST)
        dimension = service.metadata(DATASET)["dimensions"][0]
        self.assertEqual(dimension["label"], "Reference area")
        preview = dimension["code_preview"]
        self.assertEqual(len(preview["codes"]), 10)
        self.assertEqual(preview["total_codes"], 12)
        self.assertEqual(preview["next_offset"], 10)
        page = service.codes(DATASET, "AREA", offset=10)
        self.assertEqual([item["code"] for item in page["codes"]], ["C10", "C11"])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(len(service._codes(dimension["codelist"])), 12)
        service._get.assert_called_once()

    def test_foreign_partial_and_external_lists_cannot_replace_codelist_request(self):
        for embedded in (
            LIST.replace('agencyID="A"', 'agencyID="OTHER"'),
            LIST.replace('version="1.0"', 'version="2.0"'),
            LIST.replace("<s:Codelist ", '<s:Codelist isPartial="true" '),
            LIST.replace("<s:Codelist ", '<s:Codelist isExternalReference="true" '),
            LIST.replace(' agencyID="A"', ""),
            "",
        ):
            with self.subTest(embedded=embedded[:90]):
                service = self.service(embedded)
                metadata = service.metadata(DATASET)
                self.assertNotIn("code_preview", metadata["dimensions"][0])
                self.assertEqual(len(service.codes(DATASET, "AREA")["codes"]), 12)
                self.assertEqual(service._get.call_count, 2)

    def test_refresh_fetches_again_and_unknown_concept_falls_back_to_id(self):
        service = self.service(LIST)
        service.metadata(DATASET)
        service.metadata(DATASET, refresh=True)
        self.assertEqual(service._get.call_count, 2)
        service._get = Mock(
            return_value=httpx.Response(
                200,
                text=STRUCTURE.format(codelist=LIST).replace(
                    'maintainableParentVersion="1.0"', 'maintainableParentVersion="2.0"'
                ),
            )
        )
        self.assertEqual(service.metadata(DATASET, refresh=True)["dimensions"][0]["label"], "AREA")

    def test_missing_or_unmatched_structure_reference_does_not_guess_the_only_dsd(self):
        for reference in ('<Ref id="WRONG"/>', ""):
            with self.subTest(reference=reference):
                service = self.service(LIST)
                service._get = Mock(
                    return_value=httpx.Response(
                        200,
                        text=STRUCTURE.format(codelist=LIST).replace(
                            '<Ref id="DSD" agencyID="A" version="1.0"/>', reference
                        ),
                    )
                )
                with self.assertRaisesRegex(RuntimeError, "unambiguous data structure"):
                    service.metadata(DATASET)

    def test_same_id_other_agency_or_version_cannot_supply_flow_or_structure(self):
        for kind, identifier, error in (
            ("Dataflow", "FLOW", "complete dataflow"),
            ("DataStructure", "DSD", "unambiguous data structure"),
        ):
            opening = f'<s:{kind} id="{identifier}" agencyID="A" version="1.0">'
            for replacement in (
                opening.replace('agencyID="A"', 'agencyID="OTHER"'),
                opening.replace('version="1.0"', 'version="2.0"'),
                opening.replace(' agencyID="A"', ""),
                opening.replace(">", ' isExternalReference="true">'),
            ):
                with self.subTest(kind=kind, replacement=replacement):
                    service = self.service(LIST)
                    service._get = Mock(
                        return_value=httpx.Response(
                            200, text=STRUCTURE.format(codelist=LIST).replace(opening, replacement)
                        )
                    )
                    with self.assertRaisesRegex(RuntimeError, error):
                        service.metadata(DATASET)

    def test_foreign_objects_before_correct_ones_do_not_change_selection(self):
        foreign = (
            '<s:Dataflow id="FLOW" agencyID="OTHER" version="1.0"/>'
            '<s:DataStructure id="DSD" agencyID="A" version="2.0"/>'
        )
        service = self.service(LIST)
        service._get.side_effect = lambda *args: httpx.Response(
            200,
            text=STRUCTURE.format(codelist=LIST).replace(
                '<s:Dataflow id="FLOW"', foreign + '<s:Dataflow id="FLOW"', 1
            ),
        )
        self.assertEqual(service.metadata(DATASET)["key_order"], ["AREA"])

    def test_direct_codelist_response_must_be_complete_and_exact(self):
        for response in (
            LIST.replace('agencyID="A"', 'agencyID="OTHER"'),
            LIST.replace('version="1.0"', 'version="2.0"'),
            LIST.replace("<s:Codelist ", '<s:Codelist isPartial="1" '),
            LIST.replace("<s:Codelist ", '<s:Codelist isExternalReference="true" '),
            LIST + LIST,
        ):
            with self.subTest(response=response[:90]):
                service = self.service("")
                service._get.side_effect = lambda path, params, response=response: httpx.Response(
                    200,
                    text=STRUCTURE.format(codelist="")
                    if path.startswith("dataflow/")
                    else f"<Root {NS}>{response}</Root>",
                )
                with self.assertRaisesRegex(RuntimeError, "complete codelist"):
                    service.codes(DATASET, "AREA")

    def test_omitted_reference_version_means_one_not_latest(self):
        service = self.service(LIST)
        service._get.side_effect = lambda *args: httpx.Response(
            200, text=STRUCTURE.format(codelist=LIST).replace(' version="1.0"', "")
        )
        metadata = service.metadata(DATASET)
        self.assertEqual(metadata["dimensions"][0]["codelist"]["version"], "1.0")
        self.assertEqual(metadata["dimensions"][0]["label"], "Reference area")
        self.assertEqual(len(service.codes(DATASET, "AREA")["codes"]), 12)
        service._get.assert_called_once()

    def test_latest_requires_one_resolved_version_and_does_not_reuse_unrelated_cache(self):
        service = self.service(LIST)
        service.metadata(DATASET)
        reference = {"agency": "A", "id": "CL_AREA", "version": "latest"}
        self.assertEqual(len(service._codes(reference)), 12)
        self.assertEqual(service._get.call_count, 2)
        self.assertEqual(service.metadata(DATASET.replace("1.0", "latest"))["version"], "1.0")
        service._get.side_effect = lambda *args: httpx.Response(
            200, text=f"<Root {NS}>{LIST}{LIST.replace('1.0', '2.0')}</Root>"
        )
        with self.assertRaisesRegex(RuntimeError, "complete codelist"):
            service._codes(reference, refresh=True)

    def test_codelist_reference_without_agency_is_not_guessed(self):
        service = self.service(LIST)
        service._get.side_effect = lambda *args: httpx.Response(
            200,
            text=STRUCTURE.format(codelist=LIST).replace(
                'class="Codelist" id="CL_AREA" agencyID="A"',
                'class="Codelist" id="CL_AREA"',
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete codelist reference"):
            service.metadata(DATASET)
