# Project Agent Guide

This project is an MCP for public-data retrieval and analysis support. The calling agent owns reasoning, calculations and output formats. The web app and its orchestration have been removed.

## Design principle

Keep this a clean, concise data codebase. Prefer the smallest clear change that solves a real need; use straightforward names and one source of truth, and remove obsolete paths when replacing them. Add abstractions only when they simplify repeated work, and keep provider details out of shared interfaces where possible.

## Architecture

- `ausdata_mcp/server.py`: three MCP tools, threaded dispatch, parameter validation, protocol instructions, resource and prompt.
- `ausdata_mcp/artifacts.py`: atomic JSON evidence storage, bounded series summaries and retrieval manifests.
- `ausdata_mcp/errors.py`: bounded, equivalent structured/text tool errors with repair guidance and failed request context.
- `ausdata_mcp/source_http.py`: pooled HTTP connections, shared per-host GET concurrency, cooldowns and bounded transient retries.
- `ausdata_mcp/fetch_cache.py`: bounded metadata/file/reference-list caching and concurrent identical-fetch sharing.
- `ausdata_mcp/unified_catalog.py`: normalized session catalogue, atomic generation checks and one SQLite FTS index.
- `ausdata_mcp/catalog_sources.py`: live source-specific discovery adapters; no dataset observations.
- `ausdata_mcp/catalog_refresh.py`: parallel refresh, expiry, retry and source coverage status.
- `ausdata_mcp/runtime.py`: common runtime/session identity for cache and artifacts.
- `ausdata_mcp/domestic_data.py`: ABS and direct Australian sources, with bounded metadata/file caches.
- `ausdata_mcp/rba_tables.py` and `ausdata_mcp/energy_workbook.py`: in-process official-file parsers; no subprocess or spreadsheet-app dependencies.
- `ausdata_mcp/macro_data.py`: OECD, World Bank, IMF and UN Comtrade retrieval.
- `ausdata_mcp/selection.py`: shared SDMX key construction, codelist pagination and explicit coverage gaps.
- `ausdata_mcp/sdmx_structure.py`: shared bounded structure/codelist cache and code browsing for OECD and Pacific.
- `ausdata_mcp/pacific_data.py`: Pacific Data Hub/SPC live catalogue, SDMX structure/codelists and validated retrieval, adapted from the Pacific Data Hub Agent MCP.
- `ausdata_mcp/data_config.py`: source configuration without model or database dependencies.
- `AGENT_SYSTEM_PROMPT.md`: single source of truth for analyst behavior, evidence standards, calculations, caveats and presentation guidance. Supplied to clients at MCP initialization and through `ausdata://guide`.
- Tool descriptions: single source of truth for call mechanics.
- The catalogue record is the retrieval input; do not add mirrored catalogue objects or duplicate identity fields. Input annotations and search/retrieval result types define the MCP schemas. Keep structured and text results equivalent; keep source-specific metadata fields intact.
- `.mcp.json`: optional project-local stdio configuration for clients that support this format.
- `ausdata_mcp/__main__.py`: canonical stdio startup; `scripts/run_mcp.py` makes it callable by absolute path from any working directory.
- `benchmarks/`: historical answer-benchmark results and their report generator; preserve these records and regenerate with `python benchmarks/build_report.py` when historical records change. New usable-data diagnostics follow the local improvement guide when present, with separate trial records. No benchmark logic belongs in MCP tools.

## Development rules

- Keep analyst judgment in `AGENT_SYSTEM_PROMPT.md`, not source adapters or tool routing. Update it in the same change when analysis behavior changes.
- Guide agents through search -> shortlist selection -> inspect metadata -> retrieve -> analysis, without enforcing a call sequence or narrowing policy. Retrieval must honor the agent's requested scope, including full datasets; expose source limitations and preserve source dimensions rather than rejecting valid analytical choices. Reuse known metadata and retrieval files where appropriate. Keep only the three core MCP tools: `search_catalog`, `get_metadata`, `retrieve`.
- Never require a particular model vendor, app, account, database, output format or approval ceremony for ordinary data analysis.
- Keep data acquisition inside the MCP's supported pathways unless the user explicitly authorizes another route. If no suitable dataset is retrievable here, the agent should report that specific limitation and ask before seeking workbooks, websites or other APIs; do not imply the publisher has no data.
- Do not add topic-specific report routes or duplicate analyst prompts.
- Prefer live official-source retrieval. Discovery fetches live source lists into a disposable normalized file and FTS cache, not a checked-in catalogue or raw data mirror.
- All providers, including Pacific, share the same session-scoped catalogue and FTS text-match order. Support explicit any/all/phrase matching in that same index, without silently broadening narrow searches. Return up to 50 candidates by default without scores or rank labels; the agent selects suitable data after inspecting definitions and coverage. Default sessions start fresh; cache successful source lists for 24 hours, disclose stale/unavailable sources and retry failures after 60 seconds. Keep dataset metadata/codelists separate from catalogue discovery.
- RBA and DCCEEW routing must use live discovered download URLs. Validate file schemas; never accept an unknown layout as valid observations. Comtrade discovery is a supported-cube descriptor validated against live trade flows, not a complete indicator catalogue. Browse live reference lists; do not maintain a bundled code mirror or separate metadata ranking system.
- Preserve PDH dimension codes, UNIT_MULT, UNIT_MEASURE, OBS_STATUS, raw suppressed values and source annotations. Metadata pagination must disclose remaining codes. Dataset IDs are namespaced as `pdh::agency::dataflow::version`.
- Keep Australian custom sources in the domestic catalogue/retrieval flow. Align geography, period, frequency, seasonal treatment, units and definitions before comparisons.
- Tool output must disclose truncation/pagination. Retrieval returns a bounded manifest with an absolute path to complete JSON evidence; it must never inline the full dataset, silently drop requested series or return empty data as successful evidence.
- Scope ABS concept metadata to full agency/scheme/version/concept references from the selected data structure, including time, measures and roles; disclose omitted unrelated concepts and preserve complete codelists.
- Keep metadata code previews bounded and remaining codes reachable through `get_metadata` pagination. Never truncate the cached source structure used to validate retrieval. Resolve OECD/Pacific dataflows, structures and codelists by agency, ID and version (SDMX 2.1 omitted versions default to 1.0); reject partial, external-only or ambiguous structures. Reuse complete embedded SDMX codelists by that same identity; previews must not trigger extra downloads.
- Agent presentation guidance should prefer in-chat charts and answers and avoid optional PNG, CSV, Excel or report files by default. The MCP's saved retrieval JSON is working evidence, not an automatic deliverable; honor explicit user requests for export files.
- Reject malformed or known-invalid codes, contradictory parameters, ignored filters and unverifiable/truncated source responses with repair guidance. Preserve available observations when other requested selections are absent, disclosing `coverage_gaps` in evidence and the bounded manifest. Never silently omit requested selections or substitute sources. Unit changes remain explicit on observations rather than blocking retrieval.
- Preserve source references and retrieval timestamps in the saved data and manifest. Use unique session-scoped file paths; filtering and derived-output lineage belong to the calling agent.
- Keep stdout exclusively for MCP protocol messages. Diagnostics and subprocess progress go to stderr.
- Source GETs use `source_http.get`; keep retries bounded, preserve request parameters and honor provider cooldowns. Tool errors carry `isError` with equivalent structured/text `error` objects; success schemas apply to successful results.
- Summary counts and bounds describe returned evidence. Distinguish all rows from non-null values; do not infer missing periods, comparability or quality from these summaries. Preserve observation-level dimensions and flags in the full evidence.
- Independent requests may run concurrently. Keep shared index creation atomic. Explicit session IDs must not be shared by concurrent server processes.

## Verification

- Run MCP checks locally. Preserve the existing daily GitHub traffic collection workflow; do not add hosted verification or other repository automation unless the user explicitly requests it.

- Run `python -m unittest discover -s tests -v` and `python -m compileall -q ausdata_mcp scripts`.
- Check the real stdio MCP path from a different working directory, without model credentials or app packages.
- Run `ruff check ausdata_mcp scripts tests` and `ruff format --check ausdata_mcp scripts tests` with `requirements-dev.txt` installed.
- Keep deterministic regressions offline; use opt-in live smoke checks to distinguish provider/network failures from code failures.
- Confirm README, this file, AGENT_SYSTEM_PROMPT.md, llms.txt, tool descriptions and .mcp.json agree.
- This checkout lives in WSL at `/home/projects/abs-mcp`, accessible on Windows through `\\wsl.localhost\Ubuntu\home\projects\abs-mcp`.
