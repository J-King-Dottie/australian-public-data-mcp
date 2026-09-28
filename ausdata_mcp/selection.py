"""Request selection mechanics shared by source adapters; no analytical policy."""

import re

METADATA_PREVIEW_LIMIT = 10


def sdmx_selection(
    order: list[str], filters: dict | None = None, key: str = ""
) -> tuple[str, dict]:
    if key and filters:
        raise ValueError("Use sourceFilters or dataKey, not both.")
    selected = dict(filters or {})
    if key and key != "all":
        parts = key.split(".")
        if len(parts) != len(order):
            raise ValueError(f"dataKey needs {len(order)} positions: {order}.")
        selected = {name: part.split("+") for name, part in zip(order, parts, strict=True) if part}
    unknown = set(selected) - set(order)
    if unknown:
        raise ValueError(f"Unknown dimensions {sorted(unknown)}. Use metadata key_order.")
    for name, values in selected.items():
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(value, str) or not value or re.search(r"[.+/\\?#\s]", value)
                for value in values
            )
        ):
            raise ValueError(
                f"{name} needs a non-empty list of source codes without SDMX key separators."
            )
        selected[name] = list(dict.fromkeys(values))
    resolved = ".".join("+".join(selected.get(name, [])) for name in order) if selected else "all"
    return resolved, selected


def coverage_gaps(selected: dict[str, list[str]], observed: dict[str, set[str]]) -> list[dict]:
    return [
        {
            "dimension": name,
            "codes": sorted(set(codes) - observed.get(name, set())),
            "reason": "No observations returned for these requested codes and periods.",
        }
        for name, codes in selected.items()
        if set(codes) - observed.get(name, set())
    ]


def code_page(
    dataset_id: str,
    dimension: str,
    codes: list[dict],
    search: str,
    offset: int,
    limit: int,
    **context,
) -> dict:
    """The same searchable, bounded code page for every provider."""

    def normalize(text):
        return re.sub(r"[\W_]+", " ", text.casefold()).strip()

    query = normalize(search)
    matches = []
    for code in codes:
        text = " ".join(str(value) for value in code.values()).casefold()
        if search.casefold() in text or (query and query in normalize(text)):
            matches.append(code)
    page = matches[offset : offset + limit]
    return {
        "dataset_id": dataset_id,
        "dimension": dimension,
        **context,
        "codes": page,
        "total_codes": len(codes),
        "matching_codes": len(matches),
        "next_offset": offset + len(page) if offset + len(page) < len(matches) else None,
    }
