"""Persist complete retrieval evidence and return a bounded manifest."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import uuid4

from .runtime import RUNTIME_DIR, SESSION_ID

LARGE_ARTIFACT_BYTES = 50 * 1024 * 1024
SERIES_SUMMARY_LIMIT = 20


class SeriesSummary(TypedDict):
    series_index: int
    series_key: str | None
    dimension_codes: dict[str, str]
    row_count: int
    non_null_value_count: int
    missing_value_count: int
    period_start: str | None
    period_end: str | None
    non_null_period_start: str | None
    non_null_period_end: str | None
    unit_examples: list[str]
    frequency_examples: list[str]
    unit_multiplier_codes: list[str]
    truncated: bool


class RetrievalManifest(TypedDict):
    """Public MCP result; complete observations remain in artifact_path."""

    dataset_id: str
    provider: str | None
    label: str
    artifact_path: str
    artifact_format: Literal["json"]
    artifact_bytes: int
    artifact_size_mib: float
    large_artifact: bool
    size_notice: str | None
    record_path: str
    series_count: int
    row_count: int
    missing_value_count: int
    non_null_value_count: int
    non_null_period_start: str | None
    non_null_period_end: str | None
    series_summaries: list[SeriesSummary]
    warnings: list[str]
    coverage_status: Literal["returned", "partial"]
    coverage_gaps: list[dict[str, Any]]
    coverage_gap_count: int
    period_start: str | None
    period_end: str | None
    dimension_ids: list[str]
    attribute_ids: list[str]
    unit_examples: list[str]
    frequency_examples: list[str]
    unit_multiplier_codes: list[str]
    preview_rows: list[dict[str, Any]]
    preview_truncated: bool
    summary_truncated: dict[str, bool]
    api_request_urls: list[str]
    api_request_url_count: int
    source_references: list[dict[str, Any]]
    source_reference_count: int
    source_annotation_keys: list[str]
    value_semantics: str | None
    retrieved_at: str
    source_fetched_at: str
    source_cache_hit: bool


def _clean_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _store_artifact(payload: dict[str, Any]) -> Path:
    payload.setdefault("retrieved_at", datetime.now(UTC).isoformat(timespec="milliseconds"))
    directory = RUNTIME_DIR / "sessions" / SESSION_ID / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"retrieval-{uuid4()}.json"
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path.resolve()


def store_retrieval(payload: dict[str, Any], dataset_id: str, label: str) -> RetrievalManifest:
    """Save complete evidence and describe it without returning the full dataset."""
    is_macro = payload.get("kind") == "macro_retrieve"
    payload["dataset_id"] = dataset_id
    payload.setdefault("kind", "macro_retrieve" if is_macro else "domestic_retrieve")

    series_items = payload.get("series") if isinstance(payload.get("series"), list) else []
    period_start = period_end = None
    non_null_period_start = non_null_period_end = None
    series_summaries: list[SeriesSummary] = []
    preview_groups: list[list[dict[str, Any]]] = []
    dimensions: set[str] = set()
    attributes: set[str] = set()
    units: set[str] = set()
    frequencies: set[str] = set()
    unit_multipliers: set[str] = set()
    preview: list[dict[str, Any]] = []
    row_count = missing_count = 0
    preview_dimensions_truncated = False

    for series_index, series in enumerate(series_items):
        if not isinstance(series, dict):
            raise RuntimeError("Source returned an invalid series; evidence was not saved.")
        series_dimensions = series.get("dimensions") or {}
        series_attributes = series.get("attributes") or {}
        dimensions.update(series_dimensions)
        if is_macro:
            if series.get("unit") != "varies; see point.unit":
                units.add(_clean_text(series.get("unit")))
            frequencies.add(_clean_text(series.get("frequency")))
            records = series.get("points") if isinstance(series.get("points"), list) else []
        else:
            attributes.update(series_attributes.keys())
            records = (
                series.get("observations") if isinstance(series.get("observations"), list) else []
            )
        if not records:
            raise RuntimeError(
                "Source returned a series without observations; evidence was not saved."
            )
        series_missing = 0
        series_start = series_end = None
        value_start = value_end = None
        series_units: set[str] = set()
        series_frequencies: set[str] = set()
        series_multipliers: set[str] = set()
        samples: list[dict[str, Any]] = []
        sample_indices = {0, (len(records) - 1) // 2, len(records) - 1}
        for record_index, record in enumerate(records):
            if not isinstance(record, dict):
                raise RuntimeError(
                    "Source returned an invalid observation; evidence was not saved."
                )
            row_count += 1
            record_dimensions = (
                record.get("dimensions") if isinstance(record.get("dimensions"), dict) else {}
            )
            if is_macro:
                period = _clean_text(record.get("x"))
            else:
                time_dimension = record_dimensions.get(
                    "TIME_PERIOD", series_dimensions.get("TIME_PERIOD")
                )
                period = _clean_text(
                    time_dimension.get("code")
                    if isinstance(time_dimension, dict)
                    else time_dimension
                )
                period = period or _clean_text(record.get("observationKey"))
            if not period or ("y" if is_macro else "value") not in record:
                raise RuntimeError(
                    "Source returned an observation without a period or value field; evidence was not saved."
                )
            period_start = min(period_start, period) if period_start else period
            period_end = max(period_end, period) if period_end else period
            value = record.get("y") if is_macro else record.get("value")
            missing_count += value is None
            series_missing += value is None
            series_start = min(series_start, period) if series_start else period
            series_end = max(series_end, period) if series_end else period
            if value is not None:
                value_start = min(value_start, period) if value_start else period
                value_end = max(value_end, period) if value_end else period
                non_null_period_start = (
                    min(non_null_period_start, period) if non_null_period_start else period
                )
                non_null_period_end = (
                    max(non_null_period_end, period) if non_null_period_end else period
                )
            collect_sample = series_index < 3 and record_index in sample_indices
            if is_macro:
                unit = _clean_text(record.get("unit", series.get("unit")))
                units.add(unit)
                series_units.add(unit)
                series_frequencies.add(_clean_text(series.get("frequency")))
                source_row = record.get("source_row") or {}
                for key in ("UNIT_MULT", "OBS_STATUS", "OBS_CONF"):
                    if key in source_row:
                        attributes.add(key)
                if _clean_text(source_row.get("UNIT_MULT")):
                    unit_multipliers.add(_clean_text(source_row["UNIT_MULT"]))
                    series_multipliers.add(_clean_text(source_row["UNIT_MULT"]))
                if collect_sample:
                    samples.append(
                        {
                            "series_index": series_index,
                            "country_code": series.get("country_code"),
                            "series_id": series.get("series_id"),
                            "period": period,
                            "value": value,
                            "unit": record.get("unit", series.get("unit")),
                            **(
                                {"series_key": series["series_key"]}
                                if "series_key" in series
                                else {}
                            ),
                        }
                    )
                continue
            record_attributes = (
                record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
            )
            dimensions.update(record_dimensions.keys())
            attributes.update(record_attributes.keys())
            for key in ("FREQ", "FREQUENCY"):
                frequency = record_attributes.get(key, series_attributes.get(key))
                frequency = frequency or record_dimensions.get(key, series_dimensions.get(key))
                if isinstance(frequency, dict):
                    frequency = frequency.get("code") or frequency.get("label")
                if frequency:
                    frequencies.add(str(frequency))
                    series_frequencies.add(str(frequency))
            for key in ("UNIT", "UNIT_MEASURE", "Unit of measure"):
                unit = record_attributes.get(key, series_attributes.get(key))
                unit = unit or record_dimensions.get(key, series_dimensions.get(key))
                if isinstance(unit, dict):
                    unit = unit.get("label") or unit.get("code")
                if _clean_text(unit):
                    units.add(_clean_text(unit))
                    series_units.add(_clean_text(unit))
            multiplier = record_attributes.get("UNIT_MULT", series_attributes.get("UNIT_MULT"))
            if isinstance(multiplier, dict):
                multiplier = multiplier.get("code", multiplier.get("label"))
            if _clean_text(multiplier):
                unit_multipliers.add(_clean_text(multiplier))
                series_multipliers.add(_clean_text(multiplier))
            if collect_sample:
                preview_dimensions = {**series_dimensions, **record_dimensions}
                preview_dimensions_truncated |= len(preview_dimensions) > 12 or any(
                    len(str(item.get("code") if isinstance(item, dict) else item)) > 80
                    for item in preview_dimensions.values()
                )
                samples.append(
                    {
                        "series_index": series_index,
                        "series_key": series.get("seriesKey"),
                        "period": period,
                        "value": value,
                        "dimension_codes": {
                            key: str(item.get("code") if isinstance(item, dict) else item)[:80]
                            for key, item in list(preview_dimensions.items())[:12]
                        },
                    }
                )

        if samples:
            preview_groups.append(samples)
        if series_index < SERIES_SUMMARY_LIMIT:
            identity = dict(series_dimensions)
            if is_macro and not identity:
                identity = {
                    key: series[key]
                    for key in ("country_code", "series_id")
                    if series.get(key) is not None
                }
            codes = {
                key: str(item.get("code") if isinstance(item, dict) else item)
                for key, item in identity.items()
            }
            key = series.get("series_key" if is_macro else "seriesKey")
            sets = [series_units - {""}, series_frequencies - {""}, series_multipliers - {""}]
            truncated = (
                len(codes) > 12
                or any(len(value) > 80 for value in codes.values())
                or (key is not None and len(str(key)) > 256)
                or any(len(items) > 10 or any(len(v) > 160 for v in items) for items in sets)
            )
            series_summaries.append(
                {
                    "series_index": series_index,
                    "series_key": str(key)[:256] if key is not None else None,
                    "dimension_codes": {k: v[:80] for k, v in list(codes.items())[:12]},
                    "row_count": len(records),
                    "non_null_value_count": len(records) - series_missing,
                    "missing_value_count": series_missing,
                    "period_start": series_start,
                    "period_end": series_end,
                    "non_null_period_start": value_start,
                    "non_null_period_end": value_end,
                    "unit_examples": [v[:160] for v in sorted(sets[0])[:10]],
                    "frequency_examples": [v[:160] for v in sorted(sets[1])[:10]],
                    "unit_multiplier_codes": [v[:160] for v in sorted(sets[2])[:10]],
                    "truncated": truncated,
                }
            )

    # Round-robin across series before taking another observation from one series.
    preview = [
        group[position]
        for position in range(3)
        for group in preview_groups
        if position < len(group)
    ][:3]

    if not row_count:
        raise RuntimeError("Source returned no observations; evidence was not saved.")
    path = _store_artifact(payload)
    references = (
        payload.get("source_references")
        if isinstance(payload.get("source_references"), list)
        else []
    )
    urls = (
        payload.get("api_request_urls") if isinstance(payload.get("api_request_urls"), list) else []
    )
    if not urls and payload.get("api_request_url"):
        urls = [payload["api_request_url"]]
    artifact_bytes = path.stat().st_size
    artifact_size_mib = round(artifact_bytes / (1024 * 1024), 2)
    large_artifact = artifact_bytes >= LARGE_ARTIFACT_BYTES
    gaps = payload.get("coverage_gaps") or []
    warnings = list(payload.get("warnings") or [])
    if gaps:
        warnings.append(
            "Some requested selections have no observations. Inspect coverage_gaps; available evidence was preserved."
        )
    if any(item.get("unit") == "varies; see point.unit" for item in series_items):
        warnings.append("Units vary within a series; inspect point.unit before comparing values.")
    if missing_count == row_count:
        warnings.append(
            "All returned observations are missing or suppressed; no numeric values are available."
        )
    annotations = payload.get("source_annotations") or {}
    non_production = annotations.get("NonProductionDataflow", [])
    if not isinstance(non_production, list):
        non_production = [non_production]
    if any(str(value).strip().lower() == "true" for value in non_production):
        warnings.append(
            "The publisher marks this dataflow as non-production; inspect source annotations before using it as evidence."
        )
    return {
        "dataset_id": dataset_id,
        "provider": payload.get("provider") or payload.get("provider_key"),
        "label": label,
        "artifact_path": str(path),
        "artifact_format": "json",
        "artifact_bytes": artifact_bytes,
        "artifact_size_mib": artifact_size_mib,
        "large_artifact": large_artifact,
        "size_notice": (
            f"Saved JSON is {artifact_size_mib:.2f} MiB ({row_count:,} observations). "
            "This is a large local file; inspect or process it selectively."
            if large_artifact
            else None
        ),
        "record_path": "series[*].points[*]" if is_macro else "series[*].observations[*]",
        "series_count": len(series_items),
        "row_count": row_count,
        "missing_value_count": missing_count,
        "non_null_value_count": row_count - missing_count,
        "non_null_period_start": non_null_period_start,
        "non_null_period_end": non_null_period_end,
        "series_summaries": series_summaries,
        "warnings": warnings,
        "coverage_status": "partial" if gaps else "returned",
        "coverage_gaps": [{**gap, "codes": gap["codes"][:20]} for gap in gaps[:10]],
        "coverage_gap_count": len(gaps),
        "period_start": period_start,
        "period_end": period_end,
        "dimension_ids": sorted(dimensions)
        if not is_macro
        else sorted(dimensions | {"country_code", "series_id", "frequency", "unit"}),
        "attribute_ids": sorted(attributes),
        "unit_examples": sorted(units - {""})[:10],
        "frequency_examples": sorted(frequencies - {""})[:10],
        "unit_multiplier_codes": sorted(unit_multipliers)[:10],
        "preview_rows": preview,
        "preview_truncated": row_count > len(preview),
        "summary_truncated": {
            "series_summaries": len(series_items) > SERIES_SUMMARY_LIMIT,
            "coverage_gaps": len(gaps) > 10 or any(len(gap["codes"]) > 20 for gap in gaps),
            "unit_examples": len(units - {""}) > 10,
            "frequency_examples": len(frequencies - {""}) > 10,
            "unit_multiplier_codes": len(unit_multipliers) > 10,
            "preview_dimension_codes": preview_dimensions_truncated,
        },
        "api_request_urls": urls[:5],
        "api_request_url_count": len(urls),
        "source_references": references[:5],
        "source_reference_count": len(references),
        "source_annotation_keys": sorted((payload.get("source_annotations") or {}).keys()),
        "value_semantics": payload.get("value_semantics"),
        "retrieved_at": payload["retrieved_at"],
        "source_fetched_at": payload.get("source_fetched_at", payload["retrieved_at"]),
        "source_cache_hit": payload.get("source_cache_hit", False),
    }
