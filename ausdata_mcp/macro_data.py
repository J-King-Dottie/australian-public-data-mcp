from __future__ import annotations

import csv
import io
import math
import re
from functools import lru_cache
from itertools import product
from typing import Any
from urllib.parse import quote

from . import source_http
from .data_config import get_data_settings
from .fetch_cache import FetchCache
from .sdmx_structure import SDMXStructureClient
from .selection import coverage_gaps, sdmx_selection

settings = get_data_settings()


WORLD_BANK_PROVIDER = "World Bank"
IMF_PROVIDER = "IMF"
OECD_PROVIDER = "OECD"
COMTRADE_PROVIDER = "UN Comtrade"


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\s]+", " ", str(value or "").lower()).strip()


COMTRADE_REFERENCES = {
    "REPORTER": "Reporters",
    "PARTNER": "partnerAreas",
    "HS": "HS",
    "FLOW": "tradeRegimes",
    "CUSTOMS": "CustomsCodes",
    "TRANSPORT": "ModeOfTransportCodes",
}
COMTRADE_DEFAULTS = {"SECOND_PARTNER": ["0"], "CUSTOMS": ["C00"], "TRANSPORT": ["0"]}
_reference_cache = FetchCache(limit=8)


def _live_comtrade_codes(name: str, *, refresh: bool = False) -> list[dict[str, Any]]:
    if name == "SECOND_PARTNER":
        return _live_comtrade_codes("PARTNER", refresh=refresh)
    if name == "FREQUENCY":
        return [{"code": "A", "label": "Annual"}, {"code": "M", "label": "Monthly"}]
    if name not in COMTRADE_REFERENCES:
        raise ValueError("Choose a Comtrade dimension ID from get_metadata.")
    url = f"https://comtradeapi.un.org/files/v1/app/reference/{COMTRADE_REFERENCES[name]}.json"

    def load():
        response = source_http.get(
            url,
            timeout=settings.macro_timeout_seconds,
        )
        response.raise_for_status()
        rows = response.json().get("results")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"Comtrade returned no reference codes for {name}.")
        codes = []
        for row in rows:
            code = row.get("id", row.get("reporterCode"))
            label = row.get("text", row.get("reporterDesc"))
            if code is None or not label:
                raise RuntimeError(f"Comtrade returned an invalid {name} reference code.")
            codes.append(
                {
                    "code": str(code),
                    "label": str(label),
                    **({"parent": str(row["parent"])} if row.get("parent") else {}),
                }
            )
        return codes

    codes, _ = _reference_cache.get(("comtrade", url), load, refresh=refresh)
    return codes


def _comtrade_period_values(start_year: int, end_year: int, frequency_code: str) -> list[str]:
    if frequency_code == "M":
        return [
            f"{year}{month:02d}"
            for year in range(start_year, end_year + 1)
            for month in range(1, 13)
        ]
    return [str(year) for year in range(start_year, end_year + 1)]


def _chunk_period_values(period_values: list[str], max_periods: int = 12) -> list[str]:
    if not period_values:
        return []
    size = max(1, int(max_periods))
    return [
        ",".join(period_values[index : index + size])
        for index in range(0, len(period_values), size)
    ]


def _validated_comtrade_codes(
    values: list[str], options: list[dict[str, Any]], parameter: str
) -> list[str]:
    by_key = {}
    for option in options:
        code = str(option.get("code", "")).strip()
        if code:
            by_key[code.upper()] = code
            by_key[_normalize_text(code)] = code
            by_key[_normalize_text(option.get("label", ""))] = code
    resolved, invalid = [], []
    for value in values:
        clean = str(value).strip()
        code = (by_key.get(clean.upper()) or by_key.get(_normalize_text(clean))) if clean else None
        if code is None:
            invalid.append(value)
        elif code not in resolved:
            resolved.append(code)
    if invalid:
        raise ValueError(f"Invalid {parameter} {invalid}. Choose codes from get_metadata.")
    return resolved


def _build_comtrade_metadata_payload(entry: dict[str, Any]) -> dict:
    return {
        "dataset_id": entry["datasetId"],
        "provider": COMTRADE_PROVIDER,
        "title": entry["title"],
        "source_url": "https://comtradeplus.un.org/TradeFlow",
        "required_parameters": [
            "reporterCodes",
            "partnerCodes",
            "hsCodes",
            "flowCode",
            "frequencyCode",
            "startYear",
            "endYear",
        ],
        "default_filters": COMTRADE_DEFAULTS,
        "dimensions": [
            {"id": name, "parameter": parameter}
            for name, parameter in (
                ("REPORTER", "reporterCodes"),
                ("PARTNER", "partnerCodes"),
                ("HS", "hsCodes"),
                ("FLOW", "flowCode"),
                ("FREQUENCY", "frequencyCode"),
                ("SECOND_PARTNER", "sourceFilters"),
                ("CUSTOMS", "sourceFilters"),
                ("TRANSPORT", "sourceFilters"),
            )
        ],
        "scope_note": "Goods trade. Browse live codes with dimension/codeSearch. HS supports TOTAL and 2/4/6-digit products; partner 0 selects World. Source preview limits are checked during retrieval.",
    }


def _area_codes(provider: str, *, refresh: bool = False) -> list[dict]:
    if provider not in {"worldbank", "imf"}:
        raise ValueError("AREA browsing supports World Bank and IMF.")
    base_url = (
        settings.worldbank_base_url if provider == "worldbank" else settings.imf_base_url
    ).rstrip("/")

    def load():
        if provider == "worldbank":
            response = source_http.get(
                f"{base_url}/country",
                params={"format": "json", "per_page": 1000},
                timeout=settings.macro_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if (
                not isinstance(payload, list)
                or len(payload) != 2
                or not isinstance(payload[1], list)
                or int(payload[0].get("pages", 1)) != 1
            ):
                raise RuntimeError("World Bank returned incomplete area metadata.")
            return [
                {"code": row["id"], "label": row["name"], "aliases": [row["iso2Code"]]}
                for row in payload[1]
            ]
        codes = []
        for kind in ("countries", "regions", "groups"):
            response = source_http.get(f"{base_url}/{kind}", timeout=settings.macro_timeout_seconds)
            response.raise_for_status()
            values = response.json().get(kind)
            if not isinstance(values, dict):
                raise RuntimeError(f"IMF returned invalid {kind} metadata.")
            codes.extend(
                {"code": code, "label": value.get("label", code), "kind": kind}
                for code, value in values.items()
            )
        return codes

    codes, _ = _reference_cache.get((provider, base_url), load, refresh=refresh)
    return codes


def _country_scope(countries: list[str], all_countries: bool) -> list[str]:
    if countries and all_countries:
        raise ValueError("Use countries or allCountries, not both.")
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", code.strip()) for code in countries):
        raise ValueError(
            "countries must contain source country/area codes such as AUS; no empty codes or separators."
        )
    return list(dict.fromkeys(code.strip().upper() for code in countries)) or (
        [] if all_countries else ["AUS"]
    )


def _country_gaps(requested: list[str], series: list[dict[str, Any]]) -> list[dict]:
    return coverage_gaps(
        {"countries": requested}, {"countries": {item["country_code"] for item in series}}
    )


def _validate_years(start_year: int | None, end_year: int | None) -> None:
    if any(
        year is not None
        and (isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999)
        for year in (start_year, end_year)
    ):
        raise ValueError("startYear and endYear must be integers between 1 and 9999.")
    if start_year is not None and end_year is not None and start_year > end_year:
        raise ValueError("startYear must not be after endYear.")


def _source_reference(
    provider: str, *, indicator: str, series_id: str, country: str = "", source_url: str = ""
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "provider": provider,
        "indicator": indicator,
        "series_id": series_id,
    }
    if country:
        ref["country"] = country
    if source_url:
        ref["source_url"] = source_url
    return ref


def _parse_numeric(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_world_bank_error(payload: Any) -> str | None:
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    if not isinstance(first, dict):
        return None
    messages = first.get("message")
    if not isinstance(messages, list):
        return None
    parts: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        key = str(item.get("key") or "").strip()
        text = value or key
        if text:
            parts.append(text)
    return "; ".join(parts) if parts else None


def _looks_like_html_error(text: str) -> bool:
    normalized = str(text or "").lstrip().lower()
    return normalized.startswith(("<!doctype html", "<html", "<?xml"))


def _fetch_world_bank(
    entry: dict[str, Any],
    provider_config: dict[str, Any],
    countries: list[str],
    start_year: int | None,
    end_year: int | None,
    *,
    all_countries: bool = False,
) -> dict[str, Any]:
    _validate_years(start_year, end_year)
    countries = _country_scope(countries, all_countries)
    series_id = str(provider_config.get("series_id") or "").strip()
    label = str(entry["title"] or series_id).strip()
    requested_countries = list(countries)
    country_path = "all" if all_countries else ";".join(countries)
    url = f"{settings.worldbank_base_url.rstrip('/')}/country/{country_path}/indicator/{series_id}"
    params: dict[str, Any] = {"format": "json", "per_page": 20000}
    if provider_config.get("source_id"):
        params["source"] = provider_config["source_id"]
    if start_year and end_year:
        params["date"] = f"{start_year}:{end_year}"
    response = source_http.get(url, params=params, timeout=settings.macro_timeout_seconds)
    response.raise_for_status()
    if _looks_like_html_error(response.text):
        raise RuntimeError("World Bank returned an HTML error page.")
    final_request_url = str(response.request.url)
    payload = response.json()
    error_message = _parse_world_bank_error(payload)
    if error_message:
        raise RuntimeError(f"World Bank error for {series_id}: {error_message}")
    if (
        not isinstance(payload, list)
        or len(payload) < 2
        or not isinstance(payload[0], dict)
        or not isinstance(payload[1], list)
    ):
        raise RuntimeError("World Bank returned an unexpected response shape.")

    rows = list(payload[1])
    request_urls = [final_request_url]
    pages = int(payload[0].get("pages", 1)) if isinstance(payload[0], dict) else 1
    for page in range(2, pages + 1):
        page_params = {**params, "page": page}
        page_response = source_http.get(
            url, params=page_params, timeout=settings.macro_timeout_seconds
        )
        page_response.raise_for_status()
        page_payload = page_response.json()
        if (
            not isinstance(page_payload, list)
            or len(page_payload) < 2
            or not isinstance(page_payload[1], list)
        ):
            raise RuntimeError(
                f"World Bank returned an invalid page {page} of {pages}; incomplete data was not accepted."
            )
        page_meta = page_payload[0]
        if (
            not page_payload[1]
            or not isinstance(page_meta, dict)
            or int(page_meta.get("pages", pages)) != pages
            or int(page_meta.get("page", page)) != page
        ):
            raise RuntimeError(
                "World Bank pagination changed or returned an empty page; incomplete data was not accepted."
            )
        rows.extend(page_payload[1])
        request_urls.append(str(page_response.request.url))
    total = payload[0].get("total")
    if total is not None and len(rows) != int(total):
        raise RuntimeError(
            "World Bank pagination was incomplete; row count differs from the source total."
        )
    by_country: dict[str, list[dict[str, Any]]] = {}
    aliases = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("World Bank returned an invalid observation row.")
        value = _parse_numeric(row.get("value"))
        year = str(row.get("date") or "").strip()
        iso3 = str(row.get("countryiso3code") or "").strip().upper()
        iso2 = str((row.get("country") or {}).get("id") or "").strip().upper()
        iso3 = iso3 or iso2
        if iso2:
            aliases[iso2] = iso3
        if not re.fullmatch(r"\d{4}(?:M(?:0[1-9]|1[0-2])|Q[1-4])?", year) or not iso3:
            raise RuntimeError("World Bank returned an observation without country or period.")
        if not all_countries and not {iso3, iso2}.intersection(requested_countries):
            raise RuntimeError(
                "World Bank returned observations outside the requested country codes."
            )
        if (iso3, year) in seen:
            raise RuntimeError(
                "World Bank returned duplicate observations for one country and period."
            )
        seen.add((iso3, year))
        if start_year and int(year[:4]) < start_year:
            continue
        if end_year and int(year[:4]) > end_year:
            continue
        by_country.setdefault(iso3, []).append({"x": year, "y": value, "source_row": row})

    series: list[dict[str, Any]] = []
    source_refs: list[dict[str, Any]] = []
    requested_countries = list(
        dict.fromkeys(aliases.get(code, code) for code in requested_countries)
    )
    country_codes = sorted(by_country.keys()) if all_countries else requested_countries
    for country_code in country_codes:
        points = sorted(by_country.get(country_code) or [], key=lambda item: item["x"])
        if not points:
            continue
        country_name = (points[0]["source_row"].get("country") or {}).get("value") or country_code
        source_units = {point["source_row"].get("unit") for point in points} - {None, ""}
        for point in points:
            point["unit"] = point["source_row"].get("unit") or entry["unit"]
        frequencies = {
            "monthly" if "M" in point["x"] else "quarterly" if "Q" in point["x"] else "annual"
            for point in points
        }
        source_url = f"{entry.get('sourceUrl') or ''}?locations={country_code}"
        series.append(
            {
                "provider": WORLD_BANK_PROVIDER,
                "country": country_name,
                "country_code": country_code,
                "indicator": label,
                "series_id": series_id,
                "unit": next(iter(source_units), entry["unit"])
                if len(source_units) <= 1
                else "varies; see point.unit",
                "frequency": next(iter(frequencies)) if len(frequencies) == 1 else "mixed",
                "points": points,
                "source_url": source_url,
            }
        )
        source_refs.append(
            _source_reference(
                WORLD_BANK_PROVIDER,
                indicator=label,
                series_id=series_id,
                country=country_name,
                source_url=source_url,
            )
        )

    if not series:
        raise RuntimeError(f"World Bank returned no usable data for {series_id}.")

    return {
        "provider": WORLD_BANK_PROVIDER,
        "coverage_gaps": _country_gaps(requested_countries, series),
        "api_request_url": final_request_url,
        "api_request_urls": request_urls,
        "source_annotations": {"response_metadata": payload[0]},
        "series": series,
        "source_references": source_refs,
    }


def _fetch_imf(
    entry: dict[str, Any],
    provider_config: dict[str, Any],
    countries: list[str],
    start_year: int | None,
    end_year: int | None,
    *,
    all_countries: bool = False,
) -> dict[str, Any]:
    _validate_years(start_year, end_year)
    countries = _country_scope(countries, all_countries)
    series_id = str(provider_config.get("series_id") or "").strip()
    label = str(entry["title"] or series_id).strip()
    url = f"{settings.imf_base_url.rstrip('/')}/{series_id}"
    response = source_http.get(url, timeout=settings.macro_timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    values = payload.get("values") if isinstance(payload, dict) else None
    series_values = values.get(series_id) if isinstance(values, dict) else None
    if not isinstance(series_values, dict):
        raise RuntimeError("IMF returned an unexpected response shape.")
    if all_countries:
        countries = sorted(
            str(code or "").strip().upper() for code in series_values if str(code or "").strip()
        )

    series: list[dict[str, Any]] = []
    source_refs: list[dict[str, Any]] = []
    for country_code in countries:
        country_values = (
            series_values.get(country_code)
            if isinstance(series_values.get(country_code), dict)
            else {}
        )
        points = []
        for year, raw_value in country_values.items():
            value = _parse_numeric(raw_value)
            year_text = str(year or "").strip()
            if not re.fullmatch(r"\d{4}", year_text):
                raise RuntimeError("IMF returned an observation without a valid annual period.")
            if start_year and int(year_text) < start_year:
                continue
            if end_year and int(year_text) > end_year:
                continue
            points.append(
                {
                    "x": year_text,
                    "y": value,
                    "source_row": {"period": year_text, "value": raw_value},
                }
            )
        points.sort(key=lambda item: item["x"])
        if not points:
            continue
        country_name = country_code
        source_url = f"{entry.get('sourceUrl') or ''}/{country_code}"
        series.append(
            {
                "provider": IMF_PROVIDER,
                "country": country_name,
                "country_code": country_code,
                "indicator": label,
                "series_id": series_id,
                "unit": entry["unit"],
                "frequency": "annual",
                "points": points,
                "source_url": source_url,
            }
        )
        source_refs.append(
            _source_reference(
                IMF_PROVIDER,
                indicator=label,
                series_id=series_id,
                country=country_name,
                source_url=source_url,
            )
        )

    if not series:
        raise RuntimeError(f"IMF returned no usable data for {series_id}.")

    return {
        "provider": IMF_PROVIDER,
        "coverage_gaps": _country_gaps(countries, series),
        "api_request_url": str(response.request.url),
        "source_annotations": {key: value for key, value in payload.items() if key != "values"},
        "series": series,
        "source_references": source_refs,
    }


@lru_cache(maxsize=1)
def get_oecd_service() -> SDMXStructureClient:
    return SDMXStructureClient(
        settings.oecd_base_url, settings.macro_timeout_seconds, "oecd", OECD_PROVIDER
    )


def _fetch_oecd(
    entry: dict[str, Any],
    provider_config: dict[str, Any],
    countries: list[str],
    start_year: int | None,
    end_year: int | None,
    *,
    all_countries: bool = False,
    source_filters: dict[str, list[str]] | None = None,
    data_key: str = "",
    start_period: str = "",
    end_period: str = "",
) -> dict[str, Any]:
    _validate_years(start_year, end_year)
    if countries:
        countries = _country_scope(countries, all_countries)
    if (start_year is not None and start_period) or (end_year is not None and end_period):
        raise ValueError("Use startYear/endYear or startPeriod/endPeriod for each bound, not both.")
    start = start_period or (str(start_year) if start_year is not None else "")
    end = end_period or (str(end_year) if end_year is not None else "")
    if start and end and start > end and not start.startswith(end):
        raise ValueError("startPeriod must not be after endPeriod.")
    agency = str(provider_config.get("agency") or "").strip()
    dataflow = str(provider_config.get("dataflow") or "").strip()
    version = str(provider_config.get("version") or "1.0").strip()
    if not agency or not dataflow:
        raise RuntimeError("OECD provider configuration is incomplete.")
    metadata = get_oecd_service().metadata(f"oecd::{agency}::{dataflow}::{version}")
    order = metadata["key_order"]
    filters = dict(source_filters or {})
    if data_key and (countries or all_countries):
        raise ValueError("A dataKey already specifies area scope; omit countries/allCountries.")
    if countries:
        if "REF_AREA" in filters:
            raise ValueError(
                "Specify area selection in countries or sourceFilters.REF_AREA, not both."
            )
        filters["REF_AREA"] = countries
    elif all_countries and "REF_AREA" in filters:
        raise ValueError("allCountries conflicts with sourceFilters.REF_AREA.")
    elif not filters and not data_key and not all_countries and "REF_AREA" in order:
        filters["REF_AREA"] = ["AUS"]
    key, selected = sdmx_selection(order, filters, data_key)
    url = f"{settings.oecd_base_url.rstrip('/')}/data/{agency},{dataflow},{version}/{quote(key, safe='.+_-')}"
    params = {"dimensionAtObservation": "AllDimensions", "format": "csvfilewithlabels"}
    if start:
        params["startPeriod"] = start
    if end:
        params["endPeriod"] = end
    response = source_http.get(url, params=params, timeout=settings.macro_timeout_seconds)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    fields = reader.fieldnames or []
    if len(fields) != len(set(fields)):
        raise RuntimeError(
            "OECD returned duplicate CSV columns; observations cannot be identified reliably."
        )
    required = set(order) | {"TIME_PERIOD", "OBS_VALUE"}
    if not required.issubset(fields):
        raise RuntimeError("OECD returned invalid SDMX CSV; missing required observation columns.")
    label = str(entry["title"] or dataflow).strip()
    source_url = str(entry.get("sourceUrl") or "").strip()
    grouped, seen = {}, set()
    observed = {name: set() for name in selected}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise RuntimeError(
                "OECD returned malformed CSV rows; incomplete data was not accepted."
            )
        period = row["TIME_PERIOD"].strip()
        identity = tuple(row[name].strip() for name in order)
        if not period or any(not code for code in identity):
            raise RuntimeError(
                "OECD returned an observation without country or period or complete dimension coordinates."
            )
        if not re.fullmatch(r"\d{4}(?:-[A-Za-z0-9-]+)?", period):
            raise RuntimeError("OECD returned an invalid observation period.")
        if (start and period < start and not start.startswith(period)) or (
            end and period > end and not period.startswith(end)
        ):
            raise RuntimeError("OECD returned observations outside the requested periods.")
        if any(row[name] not in codes for name, codes in selected.items()):
            raise RuntimeError("OECD returned observations outside the requested codes.")
        for name in selected:
            observed[name].add(row[name])
        if (identity, period) in seen:
            raise RuntimeError(
                "OECD returned duplicate observations for the same dimensions and period."
            )
        seen.add((identity, period))
        unit = str(
            row.get("Unit of measure") or row.get("UNIT_MEASURE") or entry["unit"] or ""
        ).strip()
        multiplier = str(row.get("UNIT_MULT") or "").strip()
        if multiplier and multiplier != "0":
            unit = f"{unit} (10^{multiplier})" if unit else f"10^{multiplier}"
        item = grouped.setdefault(
            identity,
            {
                "provider": OECD_PROVIDER,
                "country": row.get("Reference area") or row.get("REF_AREA", ""),
                "country_code": row.get("REF_AREA", ""),
                "indicator": label,
                "series_id": dataflow,
                "series_key": ".".join(identity),
                "dimensions": dict(zip(order, identity, strict=True)),
                "unit": unit,
                "frequency": {
                    "A": "annual",
                    "Q": "quarterly",
                    "M": "monthly",
                    "W": "weekly",
                    "D": "daily",
                }.get(row.get("FREQ", ""), ""),
                "points": [],
                "source_url": source_url,
            },
        )
        if item["unit"] != unit:
            item["unit"] = "varies; see point.unit"
        item["points"].append(
            {"x": period, "y": _parse_numeric(row["OBS_VALUE"]), "unit": unit, "source_row": row}
        )
    series = list(grouped.values())
    if not series:
        raise RuntimeError(f"OECD returned no observations for {dataflow} with this selection.")
    for item in series:
        item["points"].sort(key=lambda point: point["x"])
    return {
        "provider": OECD_PROVIDER,
        "api_request_url": str(response.request.url),
        "series": series,
        "source_references": [
            _source_reference(
                OECD_PROVIDER, indicator=label, series_id=dataflow, source_url=source_url
            )
        ],
        "source_structure": metadata,
        "source_annotations": metadata["annotations"],
        "retrieval": {
            "key": key,
            "source_filters": selected,
            "start_period": start,
            "end_period": end,
        },
        "coverage_gaps": coverage_gaps(selected, observed),
        "value_semantics": "Each series retains its complete SDMX dimension key. Values are unscaled; inspect point.unit and source_row for units, multipliers and status before analysis.",
    }


def _fetch_comtrade(
    entry: dict[str, Any],
    provider_config: dict[str, Any],
    *,
    reporter_codes: list[str],
    partner_codes: list[str],
    flow_code: str,
    frequency_code: str,
    hs_codes: list[str],
    start_year: int | None,
    end_year: int | None,
    source_filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    _validate_years(start_year, end_year)
    for parameter, codes in (
        ("reporterCodes", reporter_codes),
        ("partnerCodes", partner_codes),
        ("hsCodes", hs_codes),
    ):
        if not codes:
            raise ValueError(
                f"Comtrade requires {parameter}; browse its dimension with get_metadata."
            )
        if any(not code.strip() for code in codes):
            raise ValueError(f"Invalid {parameter}: empty code. Choose codes from get_metadata.")
    if not flow_code:
        raise ValueError("Comtrade requires flowCode; browse FLOW metadata.")
    if start_year is None or end_year is None:
        raise ValueError("Comtrade requires startYear and endYear.")
    reporters = {item["code"]: item for item in _live_comtrade_codes("REPORTER")}
    hs = {item["code"]: item for item in _live_comtrade_codes("HS")}
    flows = {item["code"]: item for item in _live_comtrade_codes("FLOW")}
    clean_reporters = _validated_comtrade_codes(
        reporter_codes, list(reporters.values()), "reporterCodes"
    )
    clean_partners = _validated_comtrade_codes(
        partner_codes, _live_comtrade_codes("PARTNER"), "partnerCodes"
    )
    clean_hs = _validated_comtrade_codes(hs_codes, list(hs.values()), "hsCodes")
    flow_code = _validated_comtrade_codes([flow_code], list(flows.values()), "flowCode")[0]
    if frequency_code not in ("A", "M"):
        raise ValueError("Comtrade requires frequencyCode A or M.")
    extra = dict(COMTRADE_DEFAULTS)
    for name, codes in (source_filters or {}).items():
        if name not in extra or not isinstance(codes, list) or not codes:
            raise ValueError(
                "Comtrade sourceFilters supports non-empty SECOND_PARTNER, CUSTOMS and TRANSPORT code lists."
            )
        extra[name] = _validated_comtrade_codes(codes, _live_comtrade_codes(name), name)
    periods = _comtrade_period_values(start_year, end_year, frequency_code)
    chunks = _chunk_period_values(periods)
    base_url = (
        f"{settings.comtrade_base_url.rstrip('/')}/C/{frequency_code}/HS"
        if settings.comtrade_api_key
        else f"https://comtradeapi.un.org/public/v1/preview/C/{frequency_code}/HS"
    )
    source_url = "https://comtradeplus.un.org/TradeFlow"
    headers = {"User-Agent": "AusData-MCP/1.0"}
    if settings.comtrade_api_key:
        headers["Ocp-Apim-Subscription-Key"] = settings.comtrade_api_key
    grouped, request_urls, gaps = {}, [], []
    # Exact cubes and bounded period chunks avoid preview truncation without an arbitrary total-request cap.
    for reporter, partner, hs_code, secondary, customs, transport, chunk in product(
        clean_reporters,
        clean_partners,
        clean_hs,
        extra["SECOND_PARTNER"],
        extra["CUSTOMS"],
        extra["TRANSPORT"],
        chunks,
    ):
        params = {
            "reporterCode": reporter,
            "partnerCode": partner,
            "cmdCode": hs_code,
            "flowCode": flow_code,
            "period": chunk,
            "partner2Code": secondary,
            "customsCode": customs,
            "motCode": transport,
        }
        response = source_http.get(
            base_url, params=params, timeout=settings.macro_timeout_seconds, headers=headers
        )
        response.raise_for_status()
        request_urls.append(str(response.request.url))
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or payload.get("error"):
            raise RuntimeError(
                "UN Comtrade returned an invalid response; incomplete data was not accepted."
            )
        if not settings.comtrade_api_key and len(rows) >= 500:
            raise RuntimeError(
                "Comtrade reached its 500-row public preview limit; completeness cannot be verified. Reduce each request's scope or use a source API key."
            )
        if not rows:
            gaps.append(
                {
                    "dimension": "trade_selection",
                    "codes": [
                        f"{reporter}/{partner}/{hs_code}/{flow_code}/{secondary}/{customs}/{transport}"
                    ],
                    "periods": chunk,
                    "reason": "No observations returned for this requested trade selection.",
                }
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("UN Comtrade returned an invalid observation row.")
            period = str(row.get("period") or "")
            if period not in chunk.split(","):
                raise RuntimeError(
                    "UN Comtrade returned observations outside the requested periods."
                )
            for field, expected in params.items():
                if field != "period" and str(row.get(field, expected)) != expected:
                    raise RuntimeError(f"UN Comtrade returned {field} outside the requested codes.")
            identity = (
                reporter,
                partner,
                hs_code,
                flow_code,
                secondary,
                customs,
                transport,
                str(row.get("classificationCode", "HS")),
            )
            item = grouped.setdefault(
                identity,
                {
                    "provider": COMTRADE_PROVIDER,
                    "country": row.get("reporterDesc") or reporters[reporter]["label"],
                    "country_code": reporter,
                    "partner": row.get("partnerDesc") or ("World" if partner == "0" else partner),
                    "partner_code": partner,
                    "indicator": f"{flows[flow_code]['label']} - {hs[hs_code]['label']}",
                    "series_id": provider_config.get("series_id")
                    or entry["datasetId"]
                    or "UN_COMTRADE_GOODS_TRADE",
                    "series_key": ".".join(identity),
                    "dimensions": dict(
                        zip(
                            (
                                "REPORTER",
                                "PARTNER",
                                "HS",
                                "FLOW",
                                "SECOND_PARTNER",
                                "CUSTOMS",
                                "TRANSPORT",
                                "CLASSIFICATION",
                            ),
                            identity,
                            strict=True,
                        )
                    ),
                    "unit": entry["unit"] or "US Dollars",
                    "frequency": "monthly" if frequency_code == "M" else "annual",
                    "flow_code": flow_code,
                    "flow_label": flows[flow_code]["label"],
                    "hs_code": hs_code,
                    "hs_label": hs[hs_code]["label"],
                    "source_url": source_url,
                    "points": {},
                },
            )
            point = {
                "x": f"{period[:4]}-{period[4:]}" if frequency_code == "M" else period,
                "y": _parse_numeric(row.get("primaryValue")),
                "source_row": row,
            }
            previous = item["points"].get(point["x"])
            if previous is not None and previous != point:
                raise RuntimeError(
                    "UN Comtrade returned conflicting values or attributes for the same series and period."
                )
            item["points"][point["x"]] = point
    series = list(grouped.values())
    if not series:
        raise RuntimeError(
            "UN Comtrade returned no observations for the selected codes and periods."
        )
    for item in series:
        item["points"] = [item["points"][key] for key in sorted(item["points"])]
    return {
        "provider": COMTRADE_PROVIDER,
        "api_request_url": request_urls[-1],
        "api_request_urls": request_urls,
        "query_parameters": {
            "reporterCodes": clean_reporters,
            "partnerCodes": clean_partners,
            "hsCodes": clean_hs,
            "flowCode": flow_code,
            "frequencyCode": frequency_code,
            "sourceFilters": extra,
            "startYear": start_year,
            "endYear": end_year,
        },
        "series": series,
        "coverage_gaps": gaps,
        "source_references": [
            _source_reference(
                COMTRADE_PROVIDER,
                indicator=entry["title"],
                series_id=entry["datasetId"],
                source_url=source_url,
            )
        ],
    }
