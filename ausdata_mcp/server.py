from __future__ import annotations

import asyncio
import logging
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .artifacts import RetrievalManifest, store_retrieval
from .domestic_data import get_domestic_service
from .errors import tool_error_result
from .macro_data import (
    _area_codes,
    _build_comtrade_metadata_payload,
    _fetch_comtrade,
    _fetch_imf,
    _fetch_oecd,
    _fetch_world_bank,
    _live_comtrade_codes,
    _validate_years,
    get_oecd_service,
)
from .pacific_data import get_pacific_service
from .runtime import SESSION_ID
from .selection import METADATA_PREVIEW_LIMIT, code_page, sdmx_selection
from .unified_catalog import (
    SearchResult,
    get_unified_catalog_entry,
    search_unified_catalog,
)

logger = logging.getLogger("ausdata.mcp")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s - %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _mcp_instructions() -> str:
    return (PROJECT_ROOT / "AGENT_SYSTEM_PROMPT.md").read_text(encoding="utf-8")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _domestic_metadata_page(
    dataset_id: str,
    metadata: dict[str, Any],
    dimension: str,
    search: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Bound code previews without mutating the complete cached structure."""
    codelists = {item["id"]: item for item in metadata.get("codelists", [])}
    if dimension:
        component = next(
            (
                item
                for item in metadata.get("dimensions", []) + metadata.get("attributes", [])
                if item["id"] == dimension
            ),
            None,
        )
        codelist_id = (component.get("codelist") or {}).get("id") if component else None
        if codelist_id not in codelists:
            raise ValueError("Choose a dimension or attribute with a codelist from get_metadata.")
        codes = [
            {
                "code": item["id"],
                "label": item.get("name", ""),
                "description": item.get("description", ""),
                "parent_code": item.get("parentID"),
            }
            for item in codelists[codelist_id].get("codes", [])
        ]
        return code_page(
            dataset_id, dimension, codes, search, offset, limit, codelist=component["codelist"]
        )
    compact = {
        **metadata,
        "codelists": [
            {
                **item,
                "codes": item.get("codes", [])[:METADATA_PREVIEW_LIMIT],
                "total_codes": len(item.get("codes", [])),
                "next_offset": METADATA_PREVIEW_LIMIT
                if len(item.get("codes", [])) > METADATA_PREVIEW_LIMIT
                else None,
            }
            for item in codelists.values()
        ],
    }
    return {
        "dataset_id": dataset_id,
        "key_order": [
            item["id"]
            for item in sorted(
                metadata.get("dimensions", []), key=lambda item: item.get("position", 0)
            )
        ],
        **compact,
    }


def _summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    summary = {
        key: payload[key]
        for key in (
            "dataset_id",
            "provider",
            "row_count",
            "artifact_bytes",
            "api_request_url_count",
        )
        if key in payload
    }
    if isinstance(payload.get("candidates"), list):
        summary["candidates"] = len(payload["candidates"])
    return summary


def _route_entry(dataset_id: str) -> dict[str, Any]:
    if not dataset_id:
        raise ValueError("datasetId is required. Choose one from search_catalog.")
    entry = get_unified_catalog_entry(dataset_id)
    if entry is None:
        raise RuntimeError(f"Unknown datasetId '{dataset_id}'. Search the unified catalog first.")
    return entry


def _macro_metadata_from_record(entry: dict[str, Any]) -> dict[str, Any]:
    if entry["providerKey"] != "comtrade":
        return {
            "kind": "catalogue_metadata",
            "browsable_dimensions": ["AREA"],
            "title": entry.get("title"),
            "description": entry.get("description"),
            "unit": entry.get("unit"),
            "source_url": entry.get("sourceUrl"),
            "source_identifiers": entry.get("providerConfig"),
            "metadata_scope": "catalogue-level fields; inspect retrieved dimensions and units before analysis",
            "dataset_id": entry["datasetId"],
            "provider": entry["provider"],
            "summary": "Source identifiers and catalogue definitions; live availability and coverage are established by retrieval.",
        }
    return _build_comtrade_metadata_payload(entry)


def _retrieve_macro_from_record(
    entry: dict[str, Any],
    *,
    countries: list[str] | None = None,
    all_countries: bool = False,
    start_year: int | None = None,
    end_year: int | None = None,
    reporter_codes: list[str] | None = None,
    partner_codes: list[str] | None = None,
    flow_code: str | None = None,
    frequency_code: str | None = None,
    hs_codes: list[str] | None = None,
    source_filters: dict[str, list[str]] | None = None,
    data_key: str = "",
    start_period: str = "",
    end_period: str = "",
) -> dict[str, Any]:
    provider_key = entry["providerKey"]
    provider_config = dict(entry["providerConfig"])
    if provider_key in {"worldbank", "imf"}:
        fetch = {"worldbank": _fetch_world_bank, "imf": _fetch_imf}[provider_key]
        result = fetch(
            entry,
            provider_config,
            countries or [],
            start_year,
            end_year,
            all_countries=all_countries,
        )
    elif provider_key == "oecd":
        result = _fetch_oecd(
            entry,
            provider_config,
            countries or [],
            start_year,
            end_year,
            all_countries=all_countries,
            source_filters=source_filters,
            data_key=data_key,
            start_period=start_period,
            end_period=end_period,
        )
    elif provider_key == "comtrade":
        result = _fetch_comtrade(
            entry,
            provider_config,
            reporter_codes=[_clean_text(code) for code in (reporter_codes or [])],
            partner_codes=[_clean_text(code) for code in (partner_codes or [])],
            flow_code=_clean_text(flow_code).upper() or "",
            frequency_code=_clean_text(frequency_code).upper() or "",
            hs_codes=[_clean_text(code) for code in (hs_codes or [])],
            start_year=start_year,
            end_year=end_year,
            source_filters=source_filters,
        )
    else:
        raise RuntimeError(f"Unsupported macro provider '{provider_key}'.")
    result["kind"] = "macro_retrieve"
    return result


def _validate_retrieval_parameters(dataset_id: str, arguments: dict[str, Any]) -> None:
    common = {"datasetId", "forceRefresh"}
    if dataset_id.startswith("pdh::"):
        allowed = {"sourceFilters", "dataKey", "startPeriod", "endPeriod"}
    elif dataset_id.startswith("ABS,"):
        allowed = {
            "dataKey",
            "sourceFilters",
            "startPeriod",
            "endPeriod",
            "dimensionAtObservation",
        }
    elif dataset_id.startswith("CUSTOM_AUS,"):
        allowed = {"dataKey", "startPeriod", "endPeriod"}
    elif dataset_id.startswith("oecd::"):
        allowed = {
            "countries",
            "allCountries",
            "startYear",
            "endYear",
            "sourceFilters",
            "dataKey",
            "startPeriod",
            "endPeriod",
        }
    elif dataset_id.startswith("comtrade::"):
        allowed = {
            "sourceFilters",
            "reporterCodes",
            "partnerCodes",
            "flowCode",
            "frequencyCode",
            "hsCodes",
            "startYear",
            "endYear",
        }
    else:
        allowed = {"countries", "allCountries", "startYear", "endYear"}
    provided = {key for key, value in arguments.items() if value not in (None, "", False, [])}
    provided.update(key for key in ("startYear", "endYear") if arguments.get(key) is not None)
    invalid = provided - common - allowed
    if invalid:
        raise ValueError(
            f"Parameters {sorted(invalid)} do not apply to {dataset_id}. Supported filters: {sorted(allowed)}."
        )


class PublicDataMCP(FastMCP):
    """Validate unknown arguments and expose consistent tool execution errors."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema["additionalProperties"] = False
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        try:
            for tool in await self.list_tools():
                if tool.name == name:
                    unknown = arguments.keys() - tool.inputSchema.get("properties", {}).keys()
                    if unknown:
                        raise ValueError(
                            f"Unknown arguments for {name}: {', '.join(sorted(unknown))}."
                        )
                    break
            else:
                raise ValueError(f"Unknown tool: {name}. Use tools/list to discover tools.")
            return await super().call_tool(name, arguments)
        except Exception as exc:
            # Error results are returned outside success-output validation.
            return tool_error_result(exc, name, arguments)


server = PublicDataMCP(
    name="australian-public-data-mcp",
    website_url="https://github.com/J-King-Dottie/australian-public-data-mcp",
    instructions=_mcp_instructions(),
)


@server.resource("ausdata://guide", mime_type="text/markdown")
def analyst_guide() -> str:
    """Canonical analyst guidance, also supplied during MCP initialization."""
    return _mcp_instructions()


@server.prompt()
def analyse_public_data(question: str) -> str:
    """Start a public-data analysis with the canonical evidence standards."""
    return f"{_mcp_instructions()}\n\nUser question:\n{question}"


def _threaded_tool(**options):
    """Keep blocking provider I/O off the MCP event loop, preserving tool schemas."""

    def register(function):
        @wraps(function)
        async def dispatch(**arguments):
            started = time.perf_counter()
            logger.info("session=%s tool=%s event=start", SESSION_ID, function.__name__)
            try:
                result = await asyncio.to_thread(function, **arguments)
            except Exception as exc:
                logger.error(
                    "session=%s tool=%s event=error error=%s",
                    SESSION_ID,
                    function.__name__,
                    str(exc)[:500],
                )
                raise
            logger.info(
                "session=%s tool=%s event=success duration_ms=%s summary=%s",
                SESSION_ID,
                function.__name__,
                int((time.perf_counter() - started) * 1000),
                _summary(result),
            )
            return result

        server.tool(**options)(dispatch)
        return function

    return register


@_threaded_tool(
    title="Search catalog",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
def search_catalog(
    query: Annotated[
        str, Field(description="Plain subject/source terms; empty browses the catalogue.")
    ] = "",
    forceRefresh: Annotated[
        bool, Field(description="Refresh all source catalogues instead of using the session cache.")
    ] = False,
    limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 50,
    provider: Annotated[
        str,
        Field(
            description="Optional ABS, World Bank, OECD, IMF, RBA, DCCEEW, UN Comtrade or PDH/SPC filter."
        ),
    ] = "",
    offset: Annotated[int, Field(ge=0, strict=True)] = 0,
    match: Annotated[
        Literal["any", "all", "phrase"],
        Field(
            description="any matches any term; all requires every searchable term; phrase requires adjacent words in order. Empty query browses."
        ),
    ] = "any",
) -> SearchResult:
    """Find datasets in the shared live catalogue; returns candidates, not observations.
    Query terms preserve Unicode letters and numbers, including accented names.

    Use concise subject/source terms. Results follow FTS text-match order, not
    suitability ranking; select candidates by definitions and coverage, then call
    get_metadata. Use match=all to narrow broad results, or phrase for an ordered
    phrase or identifier. No raw FTS operators; punctuation separates words.
    any/all use word prefixes and omit common filler words; phrase keeps every word.
    A zero-result narrowed search is not automatically broadened. Follow next_offset
    with the same query, match and provider.
    The first call discovers all sources concurrently; allow time for a cold fetch.
    Successful catalogues are cached per session for 24 hours. catalogue.sources and
    warnings disclose stale/unavailable coverage; failed sources retry after 60 seconds.
    """
    return search_unified_catalog(
        query,
        limit=limit,
        offset=offset,
        force_refresh=forceRefresh,
        provider=provider,
        match=match,
    )


@_threaded_tool(
    title="Get dataset metadata",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
def get_metadata(
    datasetId: Annotated[
        str, Field(min_length=1, description="Exact datasetId returned by search_catalog.")
    ],
    forceRefresh: Annotated[
        bool,
        Field(
            description="Refresh the requested source metadata/file or code list; refresh macro catalogue definitions with search_catalog."
        ),
    ] = False,
    dimension: Annotated[
        str,
        Field(
            description="Dimension or attribute ID from this dataset metadata; enables codelist browsing."
        ),
    ] = "",
    codeSearch: Annotated[
        str,
        Field(
            description="Code/label text filter, tolerant of case, spacing and punctuation; requires dimension."
        ),
    ] = "",
    codeOffset: Annotated[int, Field(ge=0, strict=True)] = 0,
    codeLimit: Annotated[int, Field(ge=1, le=200, strict=True)] = 50,
) -> dict[str, Any]:
    """Inspect a selected dataset's definitions and valid retrieval codes.

    ABS, RBA and DCCEEW preview 10 codes per codelist. PDH and OECD return structure,
    labels and codelist references, plus 10-code previews when complete lists are
    embedded in the source response. Reuse codes already shown; browse only missing codes
    and follow next_offset with the same dimension/codeSearch. Search with code or
    label fragments; use the returned source code verbatim in retrieval, not the label
    or normalized search text. Similar matches remain separate choices. Codelists
    describe codes, not which combinations have observations. Use sourceFilters or a
    positional dataKey using key_order (empty positions are wildcards).
    RBA/DCCEEW provide dataKey choices. World Bank/IMF offer AREA code browsing.
    Metadata does not establish observation coverage. Browse Comtrade REPORTER,
    PARTNER and HS dimensions with codeSearch to find official country/product codes.
    Metadata always identifies dataset_id; source-specific structure is preserved.
    ABS concepts cover actual data-structure references; concept_scope and
    unreferenced_concept_count disclose removal of unrelated scheme members.
    """
    datasetId = datasetId.strip()
    if codeOffset < 0 or not 1 <= codeLimit <= 200:
        raise ValueError("codeOffset must be non-negative and codeLimit must be 1-200.")
    if not dimension and (codeSearch or codeOffset or codeLimit != 50):
        raise ValueError("Set dimension when browsing codelist codes.")
    entry = _route_entry(datasetId)
    if datasetId.startswith(("pdh::", "oecd::")):
        service = get_pacific_service() if datasetId.startswith("pdh::") else get_oecd_service()
        result = (
            service.codes(datasetId, dimension, codeSearch, codeOffset, codeLimit, forceRefresh)
            if dimension
            else service.metadata(datasetId, forceRefresh)
        )
    elif datasetId.startswith("comtrade::") and dimension:
        result = code_page(
            datasetId,
            dimension,
            _live_comtrade_codes(dimension, refresh=forceRefresh),
            codeSearch,
            codeOffset,
            codeLimit,
        )
    elif datasetId.startswith(("worldbank::", "imf::")) and dimension == "AREA":
        codes = _area_codes(entry["providerKey"], refresh=forceRefresh)
        result = code_page(datasetId, dimension, codes, codeSearch, codeOffset, codeLimit)
    elif datasetId.startswith(("ABS,", "CUSTOM_AUS,")):
        payload = get_domestic_service().get_data_structure_for_dataflow(datasetId, forceRefresh)
        result = _domestic_metadata_page(
            datasetId, payload, dimension, codeSearch, codeOffset, codeLimit
        )
    elif dimension or codeSearch or codeOffset or codeLimit != 50:
        raise ValueError("Use a dimension ID from metadata; World Bank/IMF support AREA.")
    else:
        result = _macro_metadata_from_record(entry)
    result.setdefault("dataset_id", datasetId)
    return result


@_threaded_tool(
    title="Retrieve dataset",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
)
def retrieve(
    datasetId: Annotated[
        str, Field(min_length=1, description="Exact datasetId returned by search_catalog.")
    ],
    dataKey: Annotated[
        str,
        Field(
            description="ABS/OECD/PDH positional SDMX key or all; RBA Series IDs joined by + or all; DCCEEW sheet/group or all."
        ),
    ] = "",
    startPeriod: Annotated[
        str,
        Field(
            description="ABS/OECD/PDH/RBA/DCCEEW inclusive start period in source format; e.g. 2020, 2020-Q1, 2020-01, 2020-21."
        ),
    ] = "",
    endPeriod: Annotated[
        str, Field(description="ABS/OECD/PDH/RBA/DCCEEW inclusive end period in source format.")
    ] = "",
    dimensionAtObservation: Annotated[
        str,
        Field(
            description="ABS only: observation dimension ID or AllDimensions; defaults to TIME_PERIOD."
        ),
    ] = "",
    forceRefresh: Annotated[
        bool,
        Field(
            description="Bypass ABS/PDH metadata and domestic file caches; macro observations are always fetched live."
        ),
    ] = False,
    countries: Annotated[
        list[str] | None,
        Field(description="World Bank/IMF/OECD source country/area codes; defaults to AUS."),
    ] = None,
    allCountries: Annotated[
        bool,
        Field(
            description="World Bank/IMF/OECD: retrieve all areas; cannot combine with countries."
        ),
    ] = False,
    startYear: Annotated[int, Field(ge=1, le=9999, strict=True)] | None = None,
    endYear: Annotated[int, Field(ge=1, le=9999, strict=True)] | None = None,
    reporterCodes: Annotated[
        list[str] | None,
        Field(description="Comtrade reporter codes from metadata, e.g. 36 for Australia."),
    ] = None,
    partnerCodes: Annotated[
        list[str] | None,
        Field(
            description="Comtrade partner codes from metadata; 0 explicitly selects World total."
        ),
    ] = None,
    flowCode: Annotated[
        str,
        Field(
            description="Comtrade flow code from FLOW metadata, including M, X and available subflows."
        ),
    ] = "",
    frequencyCode: Literal["", "A", "M"] = "",
    hsCodes: Annotated[
        list[str] | None,
        Field(
            description="Comtrade HS 2/4/6-digit codes from metadata; TOTAL selects all products combined."
        ),
    ] = None,
    sourceFilters: Annotated[
        dict[str, list[str]] | None,
        Field(
            description="ABS/OECD/PDH dimension IDs mapped to source code lists; Comtrade SECOND_PARTNER, CUSTOMS and TRANSPORT override total defaults."
        ),
    ] = None,
) -> RetrievalManifest:
    """Retrieve source observations and save complete JSON evidence locally.

    Reuse known codes or inspect get_metadata. Supply the selected source's parameters.
    ABS/OECD/PDH accept named sourceFilters or a positional dataKey, never both.
    ABS/PDH/RBA/DCCEEW default to all series;
    World Bank/IMF/OECD default to AUS, unless explicit scope is supplied. An OECD
    sourceFilters request leaves omitted dimensions unrestricted. Filter dimensions
    required by the question; other dimensions can be inspected in the returned data.
    A specialised dataflow may already select the measure. RBA accepts
    multiple Series IDs joined by +; DCCEEW accepts a sheet, group or all.
    OECD preserves separate series for each full dimension key. Comtrade requires
    explicit trade codes, frequency and years; sourceFilters can override total
    secondary-partner, customs and transport defaults. Missing selections are
    disclosed in coverage_gaps while available observations are saved. Invalid or
    ignored filters, malformed/truncated responses and wholly empty results are errors.
    Returns a bounded manifest with artifact_path, record_path, counts, coverage,
    provenance, up to 20 series_summaries and three previews sampled across series.
    series_index addresses the saved series array. period_start/end describe returned
    rows; non_null_period_start/end exclude null values. Neither proves completeness.
    Read the complete saved JSON with your own code; the preview is not the evidence base. The agent needs access
    to the server's filesystem. UNIT_MULT is preserved, not applied. large_artifact
    flags files of at least 50 MiB. Individual source requests default to 120 seconds;
    paginated retrievals can take longer.
    """
    arguments = locals().copy()
    datasetId = datasetId.strip()
    _validate_retrieval_parameters(datasetId, arguments)
    _validate_years(startYear, endYear)
    if (
        startPeriod
        and endPeriod
        and startPeriod > endPeriod
        and not startPeriod.startswith(endPeriod)
    ):
        raise ValueError("startPeriod must not be after endPeriod.")
    if countries and allCountries:
        raise ValueError("Use countries or allCountries, not both.")
    entry = _route_entry(datasetId)
    if datasetId.startswith("pdh::"):
        result = get_pacific_service().retrieve(
            datasetId, sourceFilters, dataKey, startPeriod, endPeriod, forceRefresh
        )
    elif datasetId.startswith(("ABS,", "CUSTOM_AUS,")):
        clean_data_key = _clean_text(dataKey)
        if datasetId.startswith("ABS,") and sourceFilters is not None:
            metadata = get_domestic_service().get_data_structure_for_dataflow(
                datasetId, forceRefresh
            )
            order = [
                item["id"]
                for item in sorted(metadata["dimensions"], key=lambda item: item["position"])
            ]
            clean_data_key, _ = sdmx_selection(order, sourceFilters, clean_data_key)
        result = get_domestic_service().resolve_dataset(
            datasetId,
            data_key=clean_data_key or "",
            start_period=_clean_text(startPeriod),
            end_period=_clean_text(endPeriod),
            dimension_at_observation=_clean_text(dimensionAtObservation),
            force_refresh=bool(forceRefresh),
        )
        result.setdefault("provider", entry.get("provider"))
        if not result.get("source_references"):
            result["source_references"] = [
                {
                    "provider": entry.get("provider"),
                    "dataset_id": datasetId,
                    "source_url": entry.get("sourceUrl"),
                    "api_request_url": result.get("api_request_url"),
                }
            ]
    else:
        result = _retrieve_macro_from_record(
            entry,
            countries=countries,
            all_countries=bool(allCountries),
            start_year=startYear,
            end_year=endYear,
            reporter_codes=reporterCodes,
            partner_codes=partnerCodes,
            flow_code=flowCode,
            frequency_code=frequencyCode,
            hs_codes=hsCodes,
            source_filters=sourceFilters,
            data_key=dataKey,
            start_period=startPeriod,
            end_period=endPeriod,
        )
    result["request"] = {
        key: value for key, value in arguments.items() if value not in (None, "", False, [])
    }
    return store_retrieval(result, datasetId, entry.get("title", datasetId))
