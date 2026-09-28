"""Live discovery adapters for the supported source catalogues and publications."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from . import source_http
from .data_config import get_data_settings
from .domestic_data import get_domestic_service

WORLD_BANK_PROVIDER = "World Bank"
IMF_PROVIDER = "IMF"
OECD_PROVIDER = "OECD"
COMTRADE_PROVIDER = "UN Comtrade"
RBA_PROVIDER = "Reserve Bank of Australia"
ENERGY_PROVIDER = "Department of Climate Change, Energy, the Environment and Water"

RBA_PAGE = "https://www.rba.gov.au/statistics/tables/"
ENERGY_PAGE = "https://www.energy.gov.au/energy-data/australian-energy-statistics"
COMTRADE_FLOWS = "https://comtradeapi.un.org/files/v1/app/reference/tradeRegimes.json"
OECD_SEARCH = "https://dotstat-search.oecd.org/api/search"
PROVIDERS = (
    "ABS",
    "World Bank",
    "IMF",
    "OECD",
    "Pacific Data Hub",
    RBA_PROVIDER,
    ENERGY_PROVIDER,
    "UN Comtrade",
)


class Links(HTMLParser):
    """Read publication links and their labels without downloading data files."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = [dict(attrs).get("href", ""), ""]

    def handle_data(self, text):
        if self.current is not None:
            self.current[1] += text

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append((self.current[0], _clean_text(self.current[1])))
            self.current = None


def _page(client, url):
    response = source_http.get(url, client=client, timeout=60)
    response.raise_for_status()
    return response.text


def _custom_entry(flow):
    return {
        "route": "domestic",
        "provider": flow["sourceOrganization"],
        "datasetId": f"CUSTOM_AUS,{flow['id']},1.0",
        "title": flow["name"],
        "description": flow["description"],
        "searchText": _join_search_text(
            [
                flow["id"],
                flow["name"],
                flow["description"],
                flow["sourceOrganization"],
                str((flow.get("curation") or {}).get("tableCode") or ""),
            ]
        ),
        "sourceUrl": flow["sourcePageUrl"],
        "providerConfig": {},
        "sourceRecord": flow,
    }


def fetch_rba_catalog(client):
    entries = []
    title = ""
    for href, label in Links(_page(client, RBA_PAGE)).links:
        url = urljoin(RBA_PAGE, href)
        if urlparse(url).netloc != "www.rba.gov.au":
            continue
        if "/tables/xls/" in url:
            title = label
        filename = Path(urlparse(url).path).name
        # The parser supports RBA series-matrix CSVs. Transaction/event CSVs have
        # different schemas and are deliberately outside this adapter's coverage.
        match = re.fullmatch(r"([a-z][0-9]+(?:\.[0-9]+)?)-data(?:-([a-z0-9-]+))?\.csv", filename)
        if "/tables/csv/" not in url or not match:
            continue
        code = match[1].upper()
        suffix = "_" + match[2].replace("-", "_").upper() if match[2] else ""
        name = title or label or code
        if match[2]:
            name += " — " + label
        flow = {
            "id": "RBA_" + code.replace(".", "_") + suffix,
            "agencyID": "CUSTOM_AUS",
            "version": "1.0",
            "name": name,
            "description": f"Reserve Bank of Australia statistical table {code}: {name}. Live time-series CSV; inspect metadata for individual series.",
            "flowType": "rba_tables_csv",
            "sourceOrganization": RBA_PROVIDER,
            "sourcePageUrl": RBA_PAGE,
            "sourceUrl": url,
            "curation": {"tableCode": code},
        }
        entries.append(_custom_entry(flow))
    return entries


def fetch_energy_catalog(client):
    publications = []
    for href, label in Links(_page(client, ENERGY_PAGE)).links:
        url = urljoin(ENERGY_PAGE, href)
        if (
            urlparse(url).netloc == "www.energy.gov.au"
            and "statistics-table-o-electricity-generation" in url
        ):
            years = re.findall(r"20\d{2}", url)
            if years:
                publications.append((max(map(int, years)), url, label))
    if not publications:
        raise RuntimeError(
            "DCCEEW no longer lists an AES Table O publication at its statistics index."
        )
    _, publication, label = max(publications)
    downloads = []
    for href, text in Links(_page(client, publication)).links:
        url = urljoin(publication, href)
        decoded = unquote(url).lower()
        if (
            urlparse(url).netloc == "www.energy.gov.au"
            and urlparse(url).path.lower().endswith(".xlsx")
            and re.search(r"table[ _-]*o", decoded + " " + text.lower())
        ):
            downloads.append(url)
    downloads = list(dict.fromkeys(downloads))
    if len(downloads) != 1:
        raise RuntimeError(
            f"Expected one current AES Table O workbook, found {len(downloads)}; publication layout may have changed."
        )
    flow = {
        "id": "AES_TABLE_O",
        "agencyID": "CUSTOM_AUS",
        "version": "1.0",
        "name": label,
        "description": label
        + ". Electricity generation by fuel, state, financial year and calendar year. Inspect workbook metadata for available periods and sheets.",
        "flowType": "dcceew_aes_xlsx",
        "sourceOrganization": ENERGY_PROVIDER,
        "sourcePageUrl": publication,
        "sourceUrl": downloads[0],
    }
    return [_custom_entry(flow)]


def fetch_source(provider):
    """Fetch only discovery information; detailed metadata and observations are deferred."""
    if provider == "ABS":
        entries = _build_abs_entries()
    elif provider == "Pacific Data Hub":
        from .pacific_data import get_pacific_service

        entries = get_pacific_service().catalogue()
    else:
        with httpx.Client(
            follow_redirects=True, headers={"User-Agent": "AusData-MCP/0.1"}
        ) as client:
            if provider in {"World Bank", "IMF", "OECD"}:
                fetch = {
                    "World Bank": fetch_world_bank_catalog,
                    "IMF": fetch_imf_catalog,
                    "OECD": fetch_oecd_catalog,
                }[provider]
                entries = fetch(client)
            elif provider == RBA_PROVIDER:
                entries = fetch_rba_catalog(client)
            elif provider == ENERGY_PROVIDER:
                entries = fetch_energy_catalog(client)
            elif provider == "UN Comtrade":
                # Comtrade is one queryable trade cube, not thousands of datasets.
                # Validate the supported import/export operations from its live list.
                response = source_http.get(COMTRADE_FLOWS, client=client, timeout=60)
                response.raise_for_status()
                flows = response.json()["results"]
                if not {"M", "X"}.issubset({str(row["id"]) for row in flows}):
                    raise RuntimeError("Comtrade no longer lists supported import/export flows.")
                entries = build_comtrade_catalog()
                entries[0]["catalogueSourceUrl"] = COMTRADE_FLOWS
                entries[0]["discoveryScope"] = (
                    "One supported goods-trade cube; live import/export capability check. Commodity and country metadata is separate."
                )
            else:
                raise ValueError(f"Unsupported catalogue provider: {provider}")
    if not entries:
        raise RuntimeError(f"{provider} returned no supported catalogue entries.")
    return list({entry["datasetId"]: entry for entry in entries}.values())


def _clean_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text.replace("\u0000", " ")).strip()
    return text


def _join_search_text(parts: list[str]) -> str:
    deduped: list[str] = []
    for part in parts:
        clean = _clean_text(part)
        if clean and clean not in deduped:
            deduped.append(clean)
    return " ".join(deduped)


def fetch_world_bank_catalog(client: httpx.Client) -> list[dict[str, Any]]:
    first = source_http.get(
        get_data_settings().worldbank_base_url.rstrip("/") + "/indicator",
        client=client,
        params={"format": "json", "per_page": 20000, "page": 1},
        timeout=120,
    )
    first.raise_for_status()
    payload = first.json()
    meta = payload[0] if isinstance(payload, list) and payload else {}
    pages = int(meta.get("pages") or 1)
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    if not isinstance(rows, list) or not rows or not meta.get("pages"):
        raise RuntimeError("World Bank returned an invalid indicator catalogue.")

    all_rows = list(rows) if isinstance(rows, list) else []
    for page in range(2, pages + 1):
        response = source_http.get(
            get_data_settings().worldbank_base_url.rstrip("/") + "/indicator",
            client=client,
            params={"format": "json", "per_page": 20000, "page": page},
            timeout=120,
        )
        response.raise_for_status()
        page_payload = response.json()
        page_rows = (
            page_payload[1] if isinstance(page_payload, list) and len(page_payload) > 1 else []
        )
        if not isinstance(page_rows, list) or not page_rows:
            raise RuntimeError(f"World Bank indicator page {page} was empty or invalid.")
        page_meta = page_payload[0]
        if (
            not isinstance(page_meta, dict)
            or int(page_meta.get("page", page)) != page
            or int(page_meta.get("pages", pages)) != pages
            or page_meta.get("total", meta.get("total")) != meta.get("total")
        ):
            raise RuntimeError("World Bank catalogue changed during pagination; retry the refresh.")
        all_rows.extend(page_rows)

    if meta.get("total") is not None and len(all_rows) != int(meta["total"]):
        raise RuntimeError("World Bank catalogue pagination was incomplete.")
    entries: list[dict[str, Any]] = []
    for row in all_rows:
        if not isinstance(row, dict):
            continue
        indicator_id = _clean_text(row.get("id"))
        label = _clean_text(row.get("name"))
        if not indicator_id or not label:
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        source_label = _clean_text(source.get("value"))
        source_id = _clean_text(source.get("id"))
        source_note = _clean_text(row.get("sourceNote"))
        source_org = _clean_text(row.get("sourceOrganization"))
        topics = []
        for topic in row.get("topics") or []:
            if isinstance(topic, dict):
                topic_label = _clean_text(topic.get("value"))
                if topic_label:
                    topics.append(topic_label)
        description = _clean_text(
            " ".join(part for part in [label, source_note, source_org] if part)
        )
        search_text = _join_search_text(
            [
                indicator_id,
                label,
                source_label,
                source_note,
                source_org,
                *topics,
                "world bank",
                "worldbank",
            ]
        )
        source_url = f"https://data.worldbank.org/indicator/{indicator_id}"
        entries.append(
            {
                "datasetId": f"worldbank::{indicator_id}"
                if source_id == "2"
                else f"worldbank::{source_id}::{indicator_id}",
                "route": "macro",
                "sourceUrl": source_url,
                "providerKey": "worldbank",
                "provider": WORLD_BANK_PROVIDER,
                "title": label,
                "unit": "",
                "description": description or label,
                "searchText": search_text,
                "providerConfig": {
                    "series_id": indicator_id,
                    "source_id": source_id,
                    "source_name": source_label,
                },
            }
        )
    return entries


def fetch_imf_catalog(client: httpx.Client) -> list[dict[str, Any]]:
    response = source_http.get(
        get_data_settings().imf_base_url.rstrip("/") + "/indicators", client=client, timeout=120
    )
    response.raise_for_status()
    payload = response.json()
    indicators = payload.get("indicators") if isinstance(payload, dict) else {}
    if not isinstance(indicators, dict):
        return []

    entries: list[dict[str, Any]] = []
    for series_id, item in indicators.items():
        if not isinstance(item, dict):
            continue
        clean_series_id = _clean_text(series_id)
        label = _clean_text(item.get("label"))
        if not clean_series_id or not label:
            continue
        description = _clean_text(item.get("description"))
        dataset = _clean_text(item.get("dataset"))
        source = _clean_text(item.get("source"))
        unit = _clean_text(item.get("unit"))
        source_url = (
            f"https://www.imf.org/external/datamapper/{clean_series_id}@{dataset}"
            if dataset
            else f"https://www.imf.org/external/datamapper/{clean_series_id}"
        )
        entries.append(
            {
                "datasetId": f"imf::{clean_series_id}",
                "route": "macro",
                "sourceUrl": source_url,
                "providerKey": "imf",
                "provider": IMF_PROVIDER,
                "title": label,
                "unit": unit,
                "description": _clean_text(
                    " ".join(part for part in [label, description, source, unit] if part)
                )
                or label,
                "searchText": _join_search_text(
                    [
                        clean_series_id,
                        label,
                        description,
                        dataset,
                        source,
                        unit,
                        "imf",
                        "international monetary fund",
                    ]
                ),
                "providerConfig": {
                    "series_id": clean_series_id,
                    "dataset": dataset,
                },
            }
        )
    return entries


def fetch_oecd_catalog(client: httpx.Client) -> list[dict[str, Any]]:
    # OECD Data Explorer uses this public search index for dataset discovery.
    # Its SDMX dataflow endpoint can challenge server-side requests, while the
    # detailed structures and observations remain separate SDMX operations.
    flows = []
    total = None
    while total is None or len(flows) < total:
        response = source_http.get(
            OECD_SEARCH,
            client=client,
            params={"tenant": "oecd", "rows": 1500, "start": len(flows)},
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        page = payload.get("dataflows")
        found = payload.get("numFound")
        if (
            not isinstance(page, list)
            or not isinstance(found, int)
            or found < 1
            or payload.get("start") != len(flows)
        ):
            raise RuntimeError("OECD search returned an invalid catalogue page.")
        if total is not None and found != total:
            raise RuntimeError("OECD catalogue changed during pagination; retry the refresh.")
        total = found
        if not page or len(flows) + len(page) > total:
            raise RuntimeError("OECD search returned an incomplete catalogue page.")
        flows.extend(page)

    entries: list[dict[str, Any]] = []
    for flow in flows:
        if flow.get("datasourceId") != "dsDisseminateFinalDMZ":
            continue
        agency_id = _clean_text(flow.get("agencyId"))
        dataflow_id = _clean_text(flow.get("dataflowId"))
        version = _clean_text(flow.get("version"))
        if not (agency_id.startswith("OECD") and dataflow_id and version):
            raise RuntimeError("OECD search returned a public dataflow without its SDMX identity.")
        label = _clean_text(
            html.unescape(re.sub(r"<[^>]*>", " ", str(flow.get("name") or dataflow_id)))
        )
        description = _clean_text(
            html.unescape(re.sub(r"<[^>]*>", " ", str(flow.get("description") or label)))
        )[:4000]
        source_url = f"{get_data_settings().oecd_base_url.rstrip('/')}/data/{agency_id},{dataflow_id},{version}"
        entries.append(
            {
                "datasetId": f"oecd::{agency_id}::{dataflow_id}::{version}",
                "route": "macro",
                "sourceUrl": source_url,
                "providerKey": "oecd",
                "provider": OECD_PROVIDER,
                "title": label,
                "unit": "",
                "description": description or label,
                "searchText": _join_search_text(
                    [
                        dataflow_id,
                        label,
                        description,
                        agency_id,
                        "oecd",
                    ]
                ),
                "providerConfig": {
                    "agency": agency_id,
                    "dataflow": dataflow_id,
                    "version": version,
                },
            }
        )
    return entries


def build_comtrade_catalog() -> list[dict[str, Any]]:
    label = "UN Comtrade goods trade (imports and exports by partner and HS code)"
    description = (
        "UN Comtrade goods trade retrieval for imports and exports, bilateral trade, world totals, "
        "and HS product codes down to 6-digit subheadings. Metadata exposes reporter countries, "
        "partner areas, annual or monthly frequency, HS descriptions, secondary partners, customs and transport modes."
    )
    return [
        {
            "datasetId": "comtrade::goods_trade",
            "route": "macro",
            "sourceUrl": "https://comtradeplus.un.org/TradeFlow",
            "providerKey": "comtrade",
            "provider": COMTRADE_PROVIDER,
            "title": label,
            "unit": "US Dollars",
            "description": description,
            "searchText": _join_search_text(
                [
                    "goods trade",
                    "imports",
                    "exports",
                    "import",
                    "export",
                    "bilateral trade",
                    "partner",
                    "hs code",
                    "hs4",
                    "hs6",
                    "customs transport mode second partner",
                    "hs 4 digit heading",
                    "commodity",
                    "merchandise trade",
                    "un comtrade",
                    "united nations comtrade",
                    "comtrade",
                ]
            ),
            "providerConfig": {
                "series_id": "UN_COMTRADE_GOODS_TRADE",
            },
        }
    ]


def _build_abs_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for flow in get_domestic_service().get_abs_data_flows():
        flow_id = _clean_text(flow.get("id"))
        agency_id = _clean_text(flow.get("agencyID")) or "ABS"
        version = _clean_text(flow.get("version"))
        if not flow_id or not version:
            continue
        dataset_id = f"{agency_id},{flow_id},{version}"
        entries.append(
            {
                "route": "domestic",
                "provider": "ABS",
                "datasetId": dataset_id,
                "title": _clean_text(flow.get("name")) or flow_id,
                "description": _clean_text(flow.get("description")),
                "searchText": _join_search_text(
                    [
                        flow_id,
                        dataset_id,
                        agency_id,
                        _clean_text(flow.get("name")),
                        _clean_text(flow.get("description")),
                    ]
                ),
                "sourceUrl": f"{get_data_settings().abs_api_base.rstrip('/')}/rest/dataflow/{agency_id}/{flow_id}/{version}",
                "sourceRecord": flow,
            }
        )
    return entries
