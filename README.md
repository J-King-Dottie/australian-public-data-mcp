# Australian Public Data MCP

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for AI agents to search live catalogues and retrieve official Australian, Pacific, and global public data. Sources include the Australian Bureau of Statistics (ABS), Reserve Bank of Australia (RBA), DCCEEW's Australian Energy Statistics, Pacific Data Hub/SPC, World Bank, OECD, IMF, and UN Comtrade. No model API key is required.

Produced by [Dottie AI Studio](https://dottieaistudio.com.au/).
Built on existing open source work including [seansoreilly/mcp-server-abs](https://github.com/seansoreilly/mcp-server-abs) and [hanlulong/openecon-data](https://github.com/hanlulong/openecon-data).

## For agents

Requires Python 3.11+ with SQLite FTS5 support.

### Install

```bash
git clone https://github.com/J-King-Dottie/australian-public-data-mcp.git
cd australian-public-data-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Connect an MCP client

Register the server with any MCP client that supports stdio:

- Name: `ausdata`
- Command: `/absolute/path/australian-public-data-mcp/.venv/bin/python`
- Arguments: `/absolute/path/australian-public-data-mcp/scripts/run_mcp.py`

Use paths from the machine running the client. The included [`.mcp.json`](.mcp.json) is an example for clients that support that project configuration format.
Its relative launcher path assumes the repository is the client's working directory; use the absolute paths above otherwise. On Windows, the virtual environment interpreter is `.venv\Scripts\python.exe`. A WSL server must be launched through WSL and its evidence paths must be accessible to the agent.

Confirm the client lists `search_catalog`, `get_metadata` and `retrieve` as callable tools before starting an analysis. An example configuration file or a successful standalone server launch does not establish that connection; register the server in the client’s supported settings and reload its MCP connections.

### Use

1. `search_catalog` finds candidate datasets.
2. `get_metadata` shows a dataset's definitions, dimensions and valid codes.
3. `retrieve` saves the complete data as JSON and returns its `artifact_path`.

The workflow is guidance, not an enforced call sequence. The agent chooses the retrieval scope, including full datasets, and can reuse known IDs and codes.

The agent reads the JSON at `artifact_path` to analyse the data, so it needs access to the server's filesystem. Tool descriptions specify the call parameters. Analyst guidance is supplied at MCP initialization and through `ausdata://guide`.

Catalogue queries preserve Unicode letters and numbers, including accented names, and normalize equivalent Unicode spellings before FTS matching. Search supports explicit match=any|all|phrase through the same index: any matches term prefixes, all requires every searchable term, and phrase preserves adjacent word order and filler words. Narrow searches never silently broaden; retain the matching mode when paging.

Metadata previews are bounded. OECD and Pacific include dimension labels and up to 10 codes when complete codelists are already embedded in the structure response; this adds no source requests. Reuse previewed codes; browse only missing ones. Codelist text search tolerates case, spacing and punctuation across all providers. Returned codes are unchanged; pass the selected source code verbatim to retrieval. Similar matches remain separate choices. Codelists describe valid codes, not available combinations; omitted SDMX dimensions can remain unrestricted and be checked in the returned evidence. All eight providers’ code lists can be searched and paged through `get_metadata`; follow `next_offset` to reach remaining codes. Search and retrieval publish explicit output schemas; structured and text results contain the same data. Metadata keeps each source's structure and always identifies `dataset_id`; ABS exposes `key_order`, `dimensions` and bounded `codelists` directly. ABS concepts are limited to those referenced by the selected data structure, including measures and roles; unreferenced_concept_count discloses omitted scheme members. Complete codelists and used definitions remain available. Unavailable requested selections are disclosed in `coverage_gaps` while available observations are saved. This reports detected gaps, not proof of complete temporal coverage. Invalid inputs, ignored filters, malformed/truncated responses and wholly empty results produce tool errors with repair guidance. Rate limits, timeouts and access failures include specific recovery advice.

Retrieval manifests include up to 20 `series_summaries`, with identity codes, row and non-null value counts, period bounds, units and frequencies. `series_index` points into the saved JSON's `series` array. Truncation is explicit and the complete evidence remains in `artifact_path`. Three preview rows sample across up to three series. Overall and per-series `non_null_period_start`/`non_null_period_end` exclude null values; ordinary period bounds include all returned rows. These are bounds on source period codes, not proof of continuity or comparable coverage. `missing_value_count` counts explicit nulls, not absent periods.

Tool failures return `isError: true` and the same `error` object in text and structured content. It includes a stable `code`, repair `message`, `retryable`, optional `retry_after_seconds`, provider host/status and the failed `request`. Requests longer than 6,000 characters are omitted with `request_truncated: true`; error messages are limited to 2,000 characters. Success output schemas describe successful results. Agents should inspect `isError` before interpreting a result.

Source GETs reuse a bounded HTTP connection pool and share a per-host limit of two concurrent requests within the server process. A request may retry once for HTTP 429/502/503/504, connection failures or timeouts, with at most five seconds of added backoff/cooldown waiting. Network timeout and queue time are separate. `Retry-After` seconds and HTTP dates are honored; longer cooldowns are returned for the agent to handle. Requests retain the same source and parameters. Independent hosts can proceed concurrently. ABS/OECD/Pacific structures, parsed RBA/DCCEEW files and World Bank/IMF/Comtrade reference lists use bounded one-hour caches; simultaneous requests for the same cache entry share one fetch. Forced refresh bypasses the requested stored entry but joins an in-progress fetch; refreshing a reference list leaves unrelated lists cached. Complete embedded SDMX codelists are reused only when agency, ID and version match. OECD/Pacific dataflows, structures and fetched codelists use the same identity checks; partial, external-only or ambiguous metadata cannot supply selection codes or key order. Omitted SDMX 2.1 reference versions mean 1.0.

The [research and design note](docs/agent-data-usability.md) records the evidence, implementation choices and verification limits.

### Sources

| Source | Coverage |
| --- | --- |
| Australian Bureau of Statistics (ABS) | Australian official statistics |
| Reserve Bank of Australia (RBA) | Australian economic and financial time series |
| DCCEEW Australian Energy Statistics | Australian electricity generation, Table O |
| Pacific Data Hub / SPC | Pacific statistical dataflows |
| World Bank | Global development indicators |
| OECD | International statistical dataflows |
| IMF | Global macroeconomic indicators |
| UN Comtrade | International goods trade |

The source interfaces support these selections:

| Source | Agent-controlled scope |
| --- | --- |
| ABS | Named dimension filters, positional SDMX key, or all series; period bounds |
| RBA | One or multiple Series IDs joined by `+`, or full table; period bounds |
| DCCEEW | Discovered sheet, sheet group, or all supported Table O data sheets; period bounds |
| Pacific | Named dimension filters, positional SDMX key, or all series; period bounds |
| World Bank | Source area codes including ISO2 aliases, aggregates or all areas; year bounds; annual/monthly/quarterly observations preserved |
| OECD | Named dimension filters, positional SDMX key, country shortcut or all areas; year/period bounds; each full dimension key retained |
| IMF | Source country, region and group codes or all areas; year bounds |
| Comtrade | Explicit reporter/partner, flow, annual/monthly frequency, years, and HS 2/4/6-digit codes; optional secondary-partner, customs and transport filters |

World Bank and IMF expose `AREA` code browsing. Comtrade uses live official reference lists, including one searchable `HS` dimension for all product levels. Comtrade requests are split into exact selections and period chunks without a total-request or observation cap imposed by this MCP. The public provider's 500-row preview limit is still checked to prevent accepting truncated evidence. Source availability, rate limits and revisions remain outside the MCP's control.


The minimal interface uses `sourceFilters` instead of the former ABS `anchorType`/`anchorCode` shortcut, and `dimension`/`codeSearch` instead of Comtrade’s former metadata `query`. The former `HS_2DIGIT`/`HS_4DIGIT`/`HS_6DIGIT` code lists are now one `HS` list. The unused retrieval `query` and fixed `detail` parameter have been removed; complete observations and attributes are always saved.

## Local operation

No configuration is required. Optional environment variables (also read from a local `.env`):

| Variable | Purpose |
| --- | --- |
| `AUSDATA_RUNTIME_DIR` | Cache/evidence directory; defaults to `runtime/` in the checkout |
| `AUSDATA_SESSION_ID` | Resume a named session; omit for a fresh session. Never share it across concurrent server processes. |
| `AUSDATA_CATALOG_TTL_SECONDS` | Successful catalogue cache lifetime; default `86400` |
| `MACRO_TIMEOUT_SECONDS` | Macro/PDH request timeout; default `120`, range `1–600` seconds |
| `COMTRADE_API_KEY` | Optional subscription key; otherwise uses the public preview endpoint |

On launch, the MCP checks its local configuration, SQLite FTS5 support and write access to its session directory. Startup failures are reported on stderr; set `AUSDATA_RUNTIME_DIR` to a writable location if needed. Evidence is stored under `runtime/sessions/<session>/artifacts/` and retained until you remove it. After finishing an analysis, delete only session directories whose evidence you no longer need, with their server stopped. Tool results disclose source freshness and local file sizes. Diagnostics go to stderr; stdout is reserved for MCP messages.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m compileall -q ausdata_mcp scripts
ruff check ausdata_mcp scripts tests
ruff format --check ausdata_mcp scripts tests
```

The offline suite includes a real stdio subprocess launched from another working directory and a complete retrieval from a local fixture server. For opt-in checks against all eight live providers:

```bash
python scripts/smoke_mcp.py --live --live-abs --live-pacific --live-domestic --live-macro
```

See [AGENTS.md](AGENTS.md) for maintenance rules and [benchmarks/](benchmarks/README.md) for historical agent evaluations; the current diagnostic lives with the local improvement guide.
