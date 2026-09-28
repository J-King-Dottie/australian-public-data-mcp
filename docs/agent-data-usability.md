# Research: flexible public-data retrieval and analysis

Reviewed 28 September 2026. This note records primary-source research and the resulting changes. The sources provide useful design precedents; they do not establish measured accuracy gains for this MCP.

## Evidence and application

| Source | Relevant evidence | Application here |
| --- | --- | --- |
| [Data Commons MCP](https://docs.datacommons.org/mcp/) | Separate tools discover indicators, inspect metadata including sources and available dates, and fetch observations. | Retain discovery, metadata and retrieval as three reusable capabilities. Expose actual returned coverage alongside complete observations. |
| [dbt MCP](https://docs.getdbt.com/docs/dbt-ai/about-mcp) | Data access includes metadata, lineage and freshness. | Keep source references, dimensions and timestamps with the evidence. Add compact per-series identities and coverage so agents can inspect what was returned. |
| [Anthropic: effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents) | Clear tool boundaries, meaningful context, token-efficient responses and task-based evaluations support agent use. | Keep the existing small interface; make errors repairable and summaries bounded. Test varied data shapes and recovery paths. |
| [Anthropic: code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) | Processing data with code can avoid passing large intermediate results through model context. | Continue saving full evidence for the calling agent to process; return a compact manifest. |
| [MCP tools specification, 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) | Structured results and model-visible tool execution errors support client interpretation and recovery. | Return equivalent structured/text errors with `isError`; retain successful output schemas. |
| [HTTP semantics, RFC 9110 section 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3) | `Retry-After` accepts an HTTP date or delay in seconds. | Parse both, coordinate cooldowns by host and return long waits to the agent. |

The transferable pattern is to give the agent enough information to choose, inspect and repair its own requests. The calling agent continues to own analysis, alignment, calculations and presentation. Each dataset retains its source dimensions.

## Implemented decisions

1. **Returned coverage is explicit.** Overall and per-series summaries distinguish row bounds from non-null value bounds and counts. This addresses future null rows and suppressed observations for any subject. Source period codes remain unchanged. Explicit null counts do not detect missing calendar periods, and non-null values still require status/definition checks.
2. **Series can be inspected cheaply.** Up to 20 summaries expose series identity, counts, units and frequency; `series_index` locates the full series in the saved file. Three previews sample across up to three series. Caps and truncation flags bound context; all observations and source flags remain in the artifact. Observation-level dimensions must still be inspected there.
3. **Errors carry recovery context.** Failures expose category, message, provider host/status, retry eligibility, delay and the original tool arguments, with bounded omission for large requests. An SDMX 404 explicitly retains the ambiguity between a missing dataset and an empty combination. No recovery step changes source or selection automatically.
4. **Transient failures have a shared policy.** All provider GET paths use one helper: two concurrent requests per host, at most one retry, and a five-second added wait budget. Long provider cooldowns are surfaced; other hosts remain independent. These numeric limits are conservative implementation choices, not values prescribed by the research. Coordination is within one process; network timeout and queue time are separate.

No new MCP tools, model dependencies or databases were needed. The architecture already supported several of the researched patterns, including paginated metadata, complete evidence files, source provenance and agent-owned calculations.

## Verification and its limits

Deterministic regressions cover:

- mixed subjects and dimensions (trade flows, regional categories and environmental observations);
- multiple frequencies, unit changes, zero values, explicit nulls and suppression flags;
- large selections with bounded summaries and complete saved evidence;
- input, output-validation, source and transport failures with equivalent error representations;
- short/long cooldowns, both Retry-After formats, unchanged retry parameters, retry limits and per-host concurrency;
- the real stdio subprocess, launched from a different directory without model credentials, for discovery, metadata, retrieval and errors.

The full unit suite, formatting/lint checks and compile checks are required alongside these regressions. Live smoke checks are separate because publisher availability can fail independently of code.

These checks establish observable protocol and data-handling behavior. They do not measure end-to-end agent accuracy or justify a claimed percentage improvement. Future agent evaluations should use the existing benchmark workflow with held-out subjects and varied question types; record correct scope, coverage/units, repair success, tool calls, latency and context size without enforcing one call sequence.

## Verification recorded on 28 September 2026

- `python -m unittest discover -s tests -v`: 146 tests passed (129 existing tests plus 17 new tests).
- `python -m compileall -q ausdata_mcp scripts`: passed.
- `ruff check ausdata_mcp scripts tests` and `ruff format --check ausdata_mcp scripts tests`: passed.
- The unit suite exercises the actual stdio launcher from a temporary working directory with model credentials removed.
- Live check launched from `/tmp` using `scripts/smoke_mcp.py --live-abs --live-macro`: ABS, IMF, OECD and UN Comtrade metadata/retrieval/artifact checks passed; discovery refreshed all eight source lists, with no unavailable-source warnings. Live responses confirm those selections at that time, not universal source availability.
- No end-to-end agent benchmark was run or performance gain claimed.
