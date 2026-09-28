from __future__ import annotations

import io
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import httpx

from . import energy_workbook, rba_tables, source_http
from .data_config import get_data_settings
from .fetch_cache import FetchCache
from .selection import coverage_gaps, sdmx_selection

settings = get_data_settings()


def _local_name(tag: str) -> str:
    return str(tag or "").split("}", 1)[-1]


def _direct_children(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in list(node) if _local_name(child.tag) == name]


def _first_child(node: ET.Element | None, name: str) -> ET.Element | None:
    children = _direct_children(node, name)
    return children[0] if children else None


def _iter_descendants(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in node.iter() if _local_name(child.tag) == name]


def _localized_text(node: ET.Element | None, child_name: str) -> str:
    matches = _direct_children(node, child_name)
    if not matches:
        return ""
    for child in matches:
        lang = (
            child.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
            or child.attrib.get("lang")
            or ""
        )
        if str(lang).strip().lower() == "en":
            return " ".join((child.text or "").split())
    return " ".join((matches[0].text or "").split())


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _component_codelist(component: ET.Element) -> dict[str, str] | None:
    enumeration = _first_child(_first_child(component, "LocalRepresentation"), "Enumeration")
    if enumeration is None:
        enumeration = _first_child(_first_child(component, "Representation"), "Enumeration")
    reference = _first_child(enumeration, "Ref")
    if reference is None:
        return None
    return {key: _clean_text(reference.get(key)) for key in ("id", "agencyID", "version")}


class ABSApiClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=settings.abs_api_base.rstrip("/"),
            timeout=120.0,
            headers={"Accept": "application/xml"},
            follow_redirects=True,
        )

    def get_dataflows_xml(self, agency_id: str = "ABS") -> str:
        response = source_http.get(
            f"/rest/dataflow/{agency_id}",
            client=self._client,
            headers={"Accept": "application/vnd.sdmx.structure+xml;version=2.1"},
        )
        response.raise_for_status()
        return response.text

    def get_data_structure_xml(
        self,
        agency_id: str,
        structure_id: str,
        version: str,
    ) -> str:
        response = source_http.get(
            f"/rest/datastructure/{agency_id}/{structure_id}/{version}",
            client=self._client,
            params={"references": "children", "detail": "full"},
            headers={"Accept": "application/vnd.sdmx.structure+xml;version=2.1"},
        )
        response.raise_for_status()
        return response.text

    def get_data(
        self,
        dataflow_id: str,
        data_key: str = "all",
        *,
        start_period: str = "",
        end_period: str = "",
        dimension_at_observation: str = "",
    ) -> Any:
        params: dict[str, str] = {"format": "jsondata", "detail": "full"}
        if start_period:
            params["startPeriod"] = start_period
        if end_period:
            params["endPeriod"] = end_period
        if dimension_at_observation:
            params["dimensionAtObservation"] = dimension_at_observation
        response = source_http.get(
            f"/rest/data/{dataflow_id}/{quote(data_key, safe='.+_-')}",
            client=self._client,
            params=params,
            headers={"Accept": "application/vnd.sdmx.data+json"},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload["api_request_url"] = str(response.request.url)
        return payload


class CustomDomesticService:
    """Download and parse supported official files in process.

    Reuse up to four parsed files for an hour so metadata followed by retrieval
    does not download and parse the same workbook or table twice. The live
    download URL is part of the cache key; forceRefresh bypasses this cache.
    """

    def __init__(self, flow_type: str) -> None:
        self.flow_type = flow_type
        self._cache = FetchCache(limit=4)

    def supports(self, flow: dict[str, Any]) -> bool:
        return flow.get("flowType") == self.flow_type

    def _parsed(self, flow: dict[str, Any], force_refresh: bool):
        url = flow["sourceUrl"]

        def load():
            response = source_http.get(
                url, timeout=120, follow_redirects=True, headers={"User-Agent": "AusData-MCP/1.0"}
            )
            response.raise_for_status()
            if self.flow_type == "rba_tables_csv":
                parsed = rba_tables.parse_table(rba_tables.load_rows(response.content))
            else:
                parsed = energy_workbook.load_workbook(io.BytesIO(response.content))
            return parsed, datetime.now(UTC).isoformat(timespec="milliseconds")

        (parsed, fetched_at), reused = self._cache.get(url, load, refresh=force_refresh)
        return parsed, fetched_at, reused

    def get_metadata(self, flow: dict[str, Any], force_refresh: bool = False) -> dict[str, Any]:
        parsed, fetched_at, cached = self._parsed(flow, force_refresh)
        if self.flow_type == "rba_tables_csv":
            result = rba_tables.build_metadata(flow, parsed, flow.get("curation", {}))
        else:
            curation = energy_workbook.discover_curation(parsed)
            result = energy_workbook.build_metadata(flow, curation, parsed)
        return {
            **result,
            "source_url": flow["sourceUrl"],
            "source_fetched_at": fetched_at,
            "source_cache_hit": cached,
        }

    def resolve(
        self, flow: dict[str, Any], *, data_key: str = "all", force_refresh: bool = False
    ) -> dict[str, Any]:
        parsed, fetched_at, cached = self._parsed(flow, force_refresh)
        if self.flow_type == "rba_tables_csv":
            payload = rba_tables.build_resolved_dataset(
                flow, parsed, flow.get("curation", {}), data_key
            )
        else:
            curation = energy_workbook.discover_curation(parsed)
            payload = energy_workbook.build_resolved_dataset(flow, curation, parsed, data_key)
        payload["api_request_url"] = flow["sourceUrl"]
        payload["source_fetched_at"] = fetched_at
        payload["source_cache_hit"] = cached
        return payload


class DomesticDataService:
    def __init__(self) -> None:
        self.api_client = ABSApiClient()
        self._structures = FetchCache(limit=64)
        self.dcceew_service = CustomDomesticService("dcceew_aes_xlsx")
        self.rba_service = CustomDomesticService("rba_tables_csv")

    def resolve_flow(self, dataflow_identifier: str) -> dict[str, Any]:
        from .unified_catalog import get_unified_catalog_entry

        entry = get_unified_catalog_entry(dataflow_identifier)
        if not entry or entry.get("route") != "domestic" or not entry.get("sourceRecord"):
            raise ValueError(
                f"Unknown domestic datasetId: {dataflow_identifier}. Search the catalogue first."
            )
        return entry["sourceRecord"]

    def get_data_structure_for_dataflow(
        self, dataflow_identifier: str, force_refresh: bool = False
    ) -> dict[str, Any]:
        flow = self.resolve_flow(dataflow_identifier)
        if self.dcceew_service.supports(flow):
            return self.dcceew_service.get_metadata(flow, force_refresh)
        if self.rba_service.supports(flow):
            return self.rba_service.get_metadata(flow, force_refresh)
        structure = flow.get("structure") if isinstance(flow.get("structure"), dict) else {}
        structure_id = _clean_text(structure.get("id")) or _clean_text(flow.get("id"))
        agency_id = (
            _clean_text(structure.get("agencyID")) or _clean_text(flow.get("agencyID")) or "ABS"
        )
        version = _clean_text(structure.get("version")) or _clean_text(flow.get("version"))
        key = (agency_id, structure_id, version)

        def load():
            xml_text = self.api_client.get_data_structure_xml(agency_id, structure_id, version)
            return self._extract_data_structure(xml_text)

        metadata, _ = self._structures.get(key, load, refresh=force_refresh)
        return {**metadata, "dataflow": flow}

    def resolve_dataset(
        self,
        dataset_id: str,
        *,
        data_key: str = "",
        start_period: str = "",
        end_period: str = "",
        dimension_at_observation: str = "",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        if (
            start_period
            and end_period
            and start_period > end_period
            and not start_period.startswith(end_period)
        ):
            raise ValueError("startPeriod must not be after endPeriod.")
        flow = self.resolve_flow(dataset_id)
        clean_data_key = _clean_text(data_key) or "all"
        if self.dcceew_service.supports(flow) or self.rba_service.supports(flow):
            service = (
                self.dcceew_service if self.dcceew_service.supports(flow) else self.rba_service
            )
            result = service.resolve(flow, data_key=clean_data_key, force_refresh=force_refresh)
            if start_period or end_period:
                for series in result["series"]:
                    series["observations"] = [
                        row
                        for row in series["observations"]
                        if (not start_period or row["observationKey"] >= start_period)
                        and (
                            not end_period
                            or row["observationKey"] <= end_period
                            or row["observationKey"].startswith(end_period)
                        )
                    ]
                result["coverage_gaps"] = [
                    {
                        "dimension": "seriesKey",
                        "codes": [series["seriesKey"]],
                        "reason": "No observations in the requested periods.",
                    }
                    for series in result["series"]
                    if not series["observations"]
                ]
                result["series"] = [series for series in result["series"] if series["observations"]]
                result["observationCount"] = sum(
                    len(series["observations"]) for series in result["series"]
                )
                result["query"].update(startPeriod=start_period, endPeriod=end_period)
            return result
        full_dataset_id = dataset_id
        clean_dimension = _clean_text(dimension_at_observation) or "TIME_PERIOD"
        payload = self.api_client.get_data(
            full_dataset_id,
            clean_data_key,
            start_period=_clean_text(start_period),
            end_period=_clean_text(end_period),
            dimension_at_observation=clean_dimension,
        )
        result = self._transform_json_data(
            flow,
            {
                "dataKey": clean_data_key,
                "startPeriod": _clean_text(start_period),
                "endPeriod": _clean_text(end_period),
                "detail": "full",
                "dimensionAtObservation": clean_dimension,
            },
            payload,
        )
        if clean_data_key != "all":
            dimensions = result["source_structure"].get("dimensions", {})
            ordered = [
                item
                for group in dimensions.values()
                for item in group
                if item["id"] != "TIME_PERIOD"
            ]
            ordered.sort(key=lambda item: item.get("keyPosition", 0))
            _, selected = sdmx_selection([item["id"] for item in ordered], key=clean_data_key)
            observed = {name: set() for name in selected}
            for series in result["series"]:
                for observation in series["observations"]:
                    coordinates = {**series["dimensions"], **observation["dimensions"]}
                    for name, codes in selected.items():
                        code = coordinates[name]["code"]
                        if code not in codes:
                            raise RuntimeError(
                                "ABS returned observations outside the requested codes."
                            )
                        observed[name].add(code)
            result["coverage_gaps"] = coverage_gaps(selected, observed)
        return result

    def get_abs_data_flows(self) -> list[dict[str, Any]]:
        root = ET.fromstring(self.api_client.get_dataflows_xml("ABS"))
        flows: list[dict[str, Any]] = []
        for flow in _iter_descendants(root, "Dataflow"):
            flow_id = _clean_text(flow.attrib.get("id"))
            agency_id = _clean_text(flow.attrib.get("agencyID")) or "ABS"
            version = _clean_text(flow.attrib.get("version"))
            if not flow_id:
                continue
            item: dict[str, Any] = {
                "id": flow_id,
                "agencyID": agency_id,
                "version": version,
                "name": _localized_text(flow, "Name"),
                "description": _localized_text(flow, "Description"),
            }
            structure_node = _first_child(flow, "Structure")
            structure_ref = _first_child(structure_node, "Ref")
            if structure_ref is not None:
                item["structure"] = {
                    "id": _clean_text(structure_ref.attrib.get("id")),
                    "version": _clean_text(structure_ref.attrib.get("version")),
                    "agencyID": _clean_text(structure_ref.attrib.get("agencyID")),
                }
            flows.append(item)
        return flows

    def _extract_data_structure(self, xml_text: str) -> dict[str, Any]:
        root = ET.fromstring(xml_text)
        data_structure_node = None
        for node in _iter_descendants(root, "DataStructure"):
            if _clean_text(node.attrib.get("id")):
                data_structure_node = node
                break
        if data_structure_node is None:
            raise RuntimeError("No data structure found in ABS response.")
        dimensions = self._extract_dimensions(data_structure_node)
        attributes = self._extract_attributes(data_structure_node)
        codelists = self._extract_codelists(root)
        concepts = self._extract_concepts(root)
        # Schemes may contain hundreds of concepts for other datasets. Resolve
        # references from the whole DSD, including time/measure/role components.
        concept_refs = {
            (
                ref.get("agencyID"),
                ref.get("maintainableParentID"),
                ref.get("maintainableParentVersion", "1.0"),
                ref.get("id"),
            )
            for ref in _iter_descendants(data_structure_node, "Ref")
            if ref.get("maintainableParentID") and ref.get("class") in {None, "Concept"}
        }
        referenced_concepts = [
            item
            for item in concepts
            if (
                item["scheme"]["agencyID"],
                item["scheme"]["id"],
                item["scheme"]["version"],
                item["id"],
            )
            in concept_refs
        ]
        return {
            "dataStructure": {
                "id": _clean_text(data_structure_node.attrib.get("id")),
                "agencyID": _clean_text(data_structure_node.attrib.get("agencyID")),
                "version": _clean_text(data_structure_node.attrib.get("version")),
                "name": _localized_text(data_structure_node, "Name"),
                "description": _localized_text(data_structure_node, "Description"),
            },
            "dimensions": dimensions,
            "attributes": attributes,
            "codelists": codelists,
            "concepts": referenced_concepts,
            "concept_scope": "Concepts referenced by this data structure, including measures and roles.",
            "unreferenced_concept_count": len(concepts) - len(referenced_concepts),
        }

    def _extract_dimensions(self, data_structure_node: ET.Element) -> list[dict[str, Any]]:
        components = _first_child(data_structure_node, "DataStructureComponents")
        dimension_list = _first_child(components, "DimensionList")
        result: list[dict[str, Any]] = []
        for index, dimension in enumerate(_direct_children(dimension_list, "Dimension"), start=1):
            concept_identity = _first_child(dimension, "ConceptIdentity")
            concept_ref = _first_child(concept_identity, "Ref")
            role_ref = _first_child(_first_child(dimension, "Role"), "Ref")
            result.append(
                {
                    "id": _clean_text(dimension.attrib.get("id")),
                    "position": int(dimension.attrib.get("position") or index),
                    "conceptId": _clean_text(concept_ref.attrib.get("id"))
                    if concept_ref is not None
                    else "",
                    "role": _clean_text(role_ref.get("id")) if role_ref is not None else "",
                    "codelist": _component_codelist(dimension),
                }
            )
        return result

    def _extract_attributes(self, data_structure_node: ET.Element) -> list[dict[str, Any]]:
        components = _first_child(data_structure_node, "DataStructureComponents")
        attribute_list = _first_child(components, "AttributeList")
        result: list[dict[str, Any]] = []
        for attribute in _direct_children(attribute_list, "Attribute"):
            concept_identity = _first_child(attribute, "ConceptIdentity")
            concept_ref = _first_child(concept_identity, "Ref")
            attachment_level = _clean_text(
                attribute.attrib.get("attachmentLevel") or attribute.attrib.get("AttachmentLevel")
            )
            result.append(
                {
                    "id": _clean_text(attribute.attrib.get("id")),
                    "assignmentStatus": _clean_text(attribute.attrib.get("assignmentStatus")),
                    "attachmentLevel": attachment_level,
                    "conceptId": _clean_text(concept_ref.attrib.get("id"))
                    if concept_ref is not None
                    else "",
                    "codelist": _component_codelist(attribute),
                    "relatedTo": self._extract_attribute_relationship(
                        _first_child(attribute, "AttributeRelationship")
                    )
                    or None,
                }
            )
        return result

    def _extract_attribute_relationship(self, relationship_node: ET.Element | None) -> list[str]:
        if relationship_node is None:
            return []
        related: list[str] = []
        for kind in ("Dimension", "Group"):
            for component in _direct_children(relationship_node, kind):
                ref = _first_child(component, "Ref")
                identifier = _clean_text((ref if ref is not None else component).attrib.get("id"))
                if identifier and identifier not in related:
                    related.append(identifier)
        primary_measure = _first_child(_first_child(relationship_node, "PrimaryMeasure"), "Ref")
        measure_id = (
            _clean_text(primary_measure.attrib.get("id")) if primary_measure is not None else ""
        )
        if measure_id and measure_id not in related:
            related.append(measure_id)
        if (
            _first_child(relationship_node, "Observation") is not None
            and "OBSERVATION" not in related
        ):
            related.append("OBSERVATION")
        return related

    def _extract_codelists(self, root: ET.Element) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for codelist in _iter_descendants(root, "Codelist"):
            codes: list[dict[str, Any]] = []
            for code in _direct_children(codelist, "Code"):
                parent = _first_child(code, "Parent")
                parent_ref = _first_child(parent, "Ref")
                codes.append(
                    {
                        "id": _clean_text(code.attrib.get("id")),
                        "name": _localized_text(code, "Name"),
                        "description": _localized_text(code, "Description"),
                        "parentID": _clean_text(parent_ref.get("id"))
                        if parent_ref is not None
                        else "",
                    }
                )
            result.append(
                {
                    "id": _clean_text(codelist.attrib.get("id")),
                    "agencyID": _clean_text(codelist.attrib.get("agencyID")),
                    "version": _clean_text(codelist.attrib.get("version")),
                    "name": _localized_text(codelist, "Name"),
                    "description": _localized_text(codelist, "Description"),
                    "codes": codes,
                }
            )
        return result

    def _extract_concepts(self, root: ET.Element) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for scheme in _iter_descendants(root, "ConceptScheme"):
            scheme_info = {
                "id": _clean_text(scheme.attrib.get("id")),
                "agencyID": _clean_text(scheme.attrib.get("agencyID")),
                "version": _clean_text(scheme.attrib.get("version", "1.0")),
                "name": _localized_text(scheme, "Name"),
            }
            for concept in _direct_children(scheme, "Concept"):
                result.append(
                    {
                        "id": _clean_text(concept.attrib.get("id")),
                        "name": _localized_text(concept, "Name"),
                        "description": _localized_text(concept, "Description"),
                        "scheme": scheme_info,
                    }
                )
        return result

    def _transform_json_data(
        self, flow: dict[str, Any], query: dict[str, Any], payload: Any
    ) -> dict[str, Any]:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise RuntimeError(f"ABS API returned errors: {json.dumps(errors)}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("ABS response did not include a data section.")
        structures, datasets = data.get("structures", []), data.get("dataSets", [])
        if len(structures) != 1 or len(datasets) != 1:
            raise RuntimeError(
                "ABS response must contain exactly one structure and dataset; incomplete data was not accepted."
            )
        structure, dataset = structures[0], datasets[0]
        dimensions = structure.get("dimensions", {})
        attributes = structure.get("attributes", {})
        dataset_dimensions = dimensions.get("dataSet", [])
        series_dimensions = dimensions.get("series", [])
        observation_dimensions = dimensions.get("observation", [])
        dataset_coordinates = self._build_coordinate_record(
            dataset_dimensions, [0] * len(dataset_dimensions)
        )
        dataset_attributes = self._map_attribute_values(
            attributes.get("dataSet", []), dataset.get("attributes", [])
        )

        def observations(raw):
            if not isinstance(raw, dict):
                raise RuntimeError("ABS response contains invalid observations.")
            result = []
            for key, values in raw.items():
                if not isinstance(values, list) or not values:
                    raise RuntimeError("ABS response contains an invalid observation value array.")
                indices = self._parse_key_indices(key, len(observation_dimensions))
                attrs = self._map_attribute_values(attributes.get("observation", []), values[1:])
                value = self._coerce_value(values[0])
                if values[0] is not None and value is None:
                    attrs["OBS_VALUE_RAW"] = str(values[0])
                result.append(
                    {
                        "observationKey": key,
                        "value": value,
                        "dimensions": self._build_coordinate_record(
                            observation_dimensions, indices
                        ),
                        "attributes": attrs,
                    }
                )
            return result

        groups = []
        raw_series = dataset.get("series", {})
        if not isinstance(raw_series, dict):
            raise RuntimeError("ABS response contains invalid series.")
        if raw_series and dataset.get("observations"):
            raise RuntimeError(
                "ABS response mixes series and dataset observations; refusing to drop records."
            )
        for key, item in raw_series.items():
            indices = self._parse_key_indices(key, len(series_dimensions))
            groups.append(
                {
                    "seriesKey": key,
                    "dimensions": {
                        **dataset_coordinates,
                        **self._build_coordinate_record(series_dimensions, indices),
                    },
                    "attributes": {
                        **dataset_attributes,
                        **self._map_attribute_values(
                            attributes.get("series", []), item.get("attributes", [])
                        ),
                    },
                    "observations": observations(item.get("observations", {})),
                }
            )
        if not raw_series and dataset.get("observations"):
            if series_dimensions:
                raise RuntimeError("ABS response omits coordinates for its series dimensions.")
            groups.append(
                {
                    "seriesKey": "__all__",
                    "dimensions": dataset_coordinates,
                    "attributes": dataset_attributes,
                    "observations": observations(dataset["observations"]),
                }
            )
        count = sum(len(item["observations"]) for item in groups)
        if not count:
            raise RuntimeError("ABS returned no observations for the selected codes and periods.")
        return {
            "provider": "ABS",
            "dataset": {
                key: flow.get(key, "")
                for key in ("id", "agencyID", "version", "name", "description")
            },
            "query": {key: value for key, value in query.items() if value},
            "api_request_url": payload.get("api_request_url", ""),
            "dimensions": self._build_dimension_lookup(
                [*dataset_dimensions, *series_dimensions, *observation_dimensions]
            ),
            "observationCount": count,
            "series": sorted(groups, key=lambda item: item["seriesKey"]),
            "source_structure": structure,
            "source_annotations": {
                key: payload[key] for key in ("meta", "header") if key in payload
            },
        }

    def _build_dimension_lookup(self, dimensions: list[Any]) -> dict[str, dict[str, str]]:
        lookup: dict[str, dict[str, str]] = {}
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                continue
            dimension_id = _clean_text(dimension.get("id"))
            values = self._to_list(dimension.get("values"))
            if not dimension_id or not values:
                continue
            registry = lookup.setdefault(dimension_id, {})
            for value in values:
                if not isinstance(value, dict):
                    continue
                code = _clean_text(value.get("id"))
                if not code or code in registry:
                    continue
                label = self._extract_name(value) or code
                registry[code] = label
        return lookup

    def _build_coordinate_record(
        self, dimensions: list[Any], indices: list[int]
    ) -> dict[str, dict[str, Any]]:
        record = {}
        for dimension, index in zip(dimensions, indices, strict=True):
            values = dimension.get("values", [])
            if not 0 <= index < len(values) or not isinstance(values[index], dict):
                raise RuntimeError(
                    f"ABS returned an invalid coordinate for {dimension.get('id')}: {index}."
                )
            value = values[index]
            if not value.get("id"):
                raise RuntimeError("ABS returned a dimension value without its source code.")
            record[dimension["id"]] = {"code": str(value["id"]), "label": self._extract_name(value)}
        return record

    def _map_attribute_values(self, definitions: list[Any], values: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        raw_values = values if isinstance(values, list) else []
        for index, definition in enumerate(definitions):
            value = raw_values[index] if index < len(raw_values) else None
            if value in (None, ""):
                continue
            key = (
                _clean_text(definition.get("id"))
                if isinstance(definition, dict)
                else f"ATTR_{index}"
            )
            result[key] = self._lookup_value(
                definition.get("values") if isinstance(definition, dict) else [], value
            )
        return result

    def _lookup_value(self, options: Any, value: Any) -> Any:
        if not isinstance(options, list) or not options:
            return value
        # SDMX-JSON attribute values are indexes, even when a code looks numeric.
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < len(options):
            raise RuntimeError(f"ABS returned an invalid attribute index: {value!r}.")
        option = options[value]
        return {"code": str(option["id"]), "label": self._extract_name(option)}

    def _coerce_value(self, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return int(numeric) if numeric.is_integer() else numeric

    def _parse_key_indices(self, key: Any, expected_length: int) -> list[int]:
        if expected_length == 0 and key == "":
            return []
        parts = str(key).split(":")
        if len(parts) != expected_length or any(not part.isdigit() for part in parts):
            raise RuntimeError(f"ABS returned an invalid dimension key: {key!r}.")
        return [int(part) for part in parts]

    def _extract_name(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ("name", "label", "id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
                if isinstance(candidate, dict):
                    english = candidate.get("en")
                    if isinstance(english, str) and english:
                        return english
                    for item in candidate.values():
                        if isinstance(item, str) and item:
                            return item
        return ""

    def _to_list(self, value: Any) -> list[Any]:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


@lru_cache(maxsize=1)
def get_domestic_service() -> DomesticDataService:
    return DomesticDataService()
