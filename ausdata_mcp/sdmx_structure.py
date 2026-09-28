"""Cached SDMX structure and codelist browsing shared by OECD and Pacific."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from . import source_http
from .fetch_cache import FetchCache
from .selection import METADATA_PREVIEW_LIMIT, code_page

NS = {
    "structure": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure",
    "common": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common",
}


def _text(element: ET.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _name(element: ET.Element, field: str = "Name") -> str:
    names = element.findall(f"common:{field}", NS)
    return _text(
        next(
            (
                item
                for item in names
                if item.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"
            ),
            names[0] if names else None,
        )
    )


def _annotations(element: ET.Element) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for annotation in element.findall(".//common:Annotation", NS):
        kind = _text(annotation.find("common:AnnotationType", NS)) or "annotation"
        values = [
            _text(annotation.find(f"common:{field}", NS))
            for field in ("AnnotationTitle", "AnnotationText")
        ]
        result.setdefault(kind, []).extend(value for value in values if value)
    return result


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_@-][A-Za-z0-9_.@-]*", value):
        raise ValueError("Invalid SDMX identifier. Use IDs returned by the catalogue or metadata.")
    return value


def parse_dataset_id(dataset_id: str, prefix: str) -> tuple[str, str, str]:
    parts = dataset_id.split("::")
    if len(parts) != 4 or parts[0] != prefix:
        raise ValueError(
            f"Expected a {prefix}::agency::dataflow::version datasetId from search_catalog."
        )
    return tuple(_identifier(part) for part in parts[1:])


class SDMXStructureClient:
    metadata_kind = "sdmx_metadata"

    def __init__(self, base_url: str, timeout: float, prefix: str, provider: str):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.prefix = prefix
        self.provider = provider
        self._xml_cache = FetchCache(limit=64)

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        response = source_http.get(
            f"{self.base_url}/{path}",
            params=params,
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "AusData-MCP/0.1"},
        )
        response.raise_for_status()
        return response

    def _xml(self, path: str, refresh: bool = False) -> ET.Element:
        def load():
            try:
                return ET.fromstring(
                    self._get(path, {"references": "children", "detail": "full"}).text
                )
            except ET.ParseError as exc:
                raise RuntimeError(
                    f"{self.provider} returned invalid structure XML; retry metadata later."
                ) from exc

        root, _ = self._xml_cache.get(path, load, refresh=refresh)
        return root

    @staticmethod
    def _maintainable(
        root: ET.Element, kind: str, agency: str | None, identifier: str | None, version: str
    ) -> ET.Element | None:
        """Resolve one complete object; SDMX 2.1 defaults omitted versions to 1.0."""
        matches = [
            item
            for item in root.findall(f".//structure:{kind}", NS)
            if agency
            and identifier
            and item.get("agencyID") == agency
            and item.get("id") == identifier
            and (
                item.get("version", "1.0") not in {"", "latest"}
                and (version == "latest" or item.get("version", "1.0") == version)
            )
            and not any(
                item.get(flag, "").strip().lower() in {"true", "1"}
                for flag in ("isExternalReference", "isPartial")
            )
        ]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _embedded_codelist(cls, root: ET.Element, reference: dict) -> ET.Element | None:
        # A cached response cannot establish what "latest" currently resolves to.
        if reference["version"] == "latest":
            return None
        return cls._maintainable(
            root, "Codelist", reference["agency"], reference["id"], reference["version"]
        )

    @staticmethod
    def _code_items(codelist: ET.Element) -> list[dict[str, Any]]:
        return [
            {
                "code": item.get("id"),
                "label": _name(item),
                "description": _name(item, "Description"),
            }
            for item in codelist.findall("structure:Code", NS)
        ]

    @staticmethod
    def _component_label(root: ET.Element, element: ET.Element) -> str:
        ref = element.find("structure:ConceptIdentity/Ref", NS)
        if ref is not None:
            for scheme in root.findall(".//structure:ConceptScheme", NS):
                if (
                    scheme.get("id") == ref.get("maintainableParentID")
                    and scheme.get("agencyID") == ref.get("agencyID")
                    and scheme.get("version", "1.0") == ref.get("maintainableParentVersion", "1.0")
                ):
                    concept = next(
                        (
                            item
                            for item in scheme.findall("structure:Concept", NS)
                            if item.get("id") == ref.get("id")
                        ),
                        None,
                    )
                    if concept is not None and _name(concept):
                        return _name(concept)
        return _name(element) or element.get("id", "")

    def metadata(self, dataset_id: str, refresh: bool = False) -> dict[str, Any]:
        agency, flow_id, version = parse_dataset_id(dataset_id, self.prefix)
        path = f"dataflow/{agency}/{flow_id}/{version}"
        root = self._xml(path, refresh)
        flow = self._maintainable(root, "Dataflow", agency, flow_id, version)
        if flow is None:
            raise RuntimeError(
                f"{self.provider} did not return one complete dataflow "
                f"{agency}::{flow_id}::{version}. Refresh metadata or use a current catalogue ID."
            )
        structure_ref = flow.find("structure:Structure/Ref", NS)
        dsd = (
            self._maintainable(
                root,
                "DataStructure",
                structure_ref.get("agencyID"),
                structure_ref.get("id"),
                structure_ref.get("version", "1.0"),
            )
            if structure_ref is not None
            else None
        )
        if dsd is None:
            raise RuntimeError(
                f"{self.provider} did not return an unambiguous data structure matching "
                "the referenced agency, ID and version. Refresh metadata; refusing to guess the key order."
            )
        dimensions, attributes = [], []
        for group, target, tags in (
            ("DimensionList", dimensions, {"Dimension", "MeasureDimension", "TimeDimension"}),
            ("AttributeList", attributes, {"Attribute"}),
        ):
            for element in dsd.findall(f".//structure:{group}/*", NS):
                tag = element.tag.rsplit("}", 1)[-1]
                if tag not in tags:
                    continue
                reference = next(
                    (
                        item
                        for item in element.iter()
                        if item.tag.rsplit("}", 1)[-1] == "Ref" and item.get("class") == "Codelist"
                    ),
                    None,
                )
                if reference is not None and not all(
                    reference.get(field) for field in ("id", "agencyID")
                ):
                    raise RuntimeError(
                        f"{self.provider} returned an incomplete codelist reference for "
                        f"{element.get('id')}. Refresh metadata before selecting codes."
                    )
                target.append(
                    {
                        "id": element.get("id"),
                        "label": self._component_label(root, element),
                        "position": int(element.get("position", "999")),
                        "is_time": tag == "TimeDimension",
                        "codelist": {
                            "id": reference.get("id"),
                            "agency": reference.get("agencyID"),
                            "version": reference.get("version", "1.0"),
                        }
                        if reference is not None
                        else None,
                    }
                )
        for component in dimensions + attributes:
            reference = component["codelist"]
            embedded = self._embedded_codelist(root, reference) if reference else None
            if embedded is not None:
                codes = self._code_items(embedded)
                component["code_preview"] = {
                    "codes": codes[:METADATA_PREVIEW_LIMIT],
                    "total_codes": len(codes),
                    "next_offset": METADATA_PREVIEW_LIMIT
                    if len(codes) > METADATA_PREVIEW_LIMIT
                    else None,
                }
        dimensions.sort(key=lambda item: item["position"])
        if not dimensions:
            raise RuntimeError(
                f"{self.provider} returned no dimensions; refusing an unbounded data request."
            )
        return {
            "kind": self.metadata_kind,
            "dataset_id": dataset_id,
            "provider": self.provider,
            "title": _name(flow),
            "description": _name(flow, "Description"),
            "version": flow.get("version", "1.0"),
            "dimensions": dimensions,
            "attributes": attributes,
            "annotations": _annotations(flow),
            "key_order": [item["id"] for item in dimensions if not item["is_time"]],
            "source_url": f"{self.base_url}/{path}",
        }

    def _codes(self, reference: dict, refresh: bool = False) -> list[dict[str, Any]]:
        agency, code_id, version = (
            _identifier(reference[key]) for key in ("agency", "id", "version")
        )
        if not refresh:
            for cached_root in reversed(self._xml_cache.values()):
                embedded = self._embedded_codelist(cached_root, reference)
                if embedded is not None:
                    return self._code_items(embedded)
        root = self._xml(f"codelist/{agency}/{code_id}/{version}", refresh)
        codelist = self._maintainable(root, "Codelist", agency, code_id, version)
        if codelist is None:
            raise RuntimeError(
                f"{self.provider} did not return one complete codelist "
                f"{agency}::{code_id}::{version}. Refresh metadata; partial or ambiguous "
                "code lists cannot validate selections."
            )
        return self._code_items(codelist)

    def codes(
        self,
        dataset_id: str,
        dimension: str,
        search: str = "",
        offset: int = 0,
        limit: int = 50,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("codeOffset must be non-negative and codeLimit must be 1-200.")
        metadata = self.metadata(dataset_id, refresh)
        component = next(
            (
                item
                for item in metadata["dimensions"] + metadata["attributes"]
                if item["id"] == dimension
            ),
            None,
        )
        if component is None or not component["codelist"]:
            raise ValueError("Choose a dimension or attribute with a codelist from get_metadata.")
        codes = self._codes(component["codelist"], refresh)
        return code_page(
            dataset_id, dimension, codes, search, offset, limit, codelist=component["codelist"]
        )
