# Research round 2: reaching usable public data efficiently

Date: 28 September 2026
Status: research and recommendations; no runtime changes made in this round.

## Recommendation

Keep the three tools. Improve the amount of useful information returned by metadata calls and remove avoidable work inside retrieval. Implement the small transport and metadata changes first; introduce availability queries only for providers where they are supported and useful.

The target metric is elapsed time from a question to correctly selected, locally readable observations with interpretable dimensions, units and status. Tool count, source request count, transferred bytes, response size and parser effort explain that outcome. A faster response containing the wrong slice is not a success.

This review inspected the current working tree, including the previous improvements. Those improvements already cover bounded evidence manifests, non-null coverage, structured failures and coordinated retries. The recommendations below add to that work.

## Comparable systems and primary evidence

| System/source | What its documentation establishes | Transferable lesson |
| --- | --- | --- |
| [UNICEF DRP SDMX MCP](https://github.com/unicef-drp/sdmx-mcp) | Describes structure caching, DSD-driven labels, query planning, ambiguity handling and compact result tools. | Resolve labels from source structures and expose unresolved choices. Its large tool inventory and analytical result shapes are unnecessary for this project's scope. |
| [Data Commons availability API](https://docs.datacommons.org/api/sdmx/availability.html) | Can inspect available entities, provenance, units and periods for selected variables without fetching observations. | Valid codes and actual available data are different things; selective availability can save exploratory downloads. |
| [sdmx1 client](https://sdmx1.readthedocs.io/en/latest/api.html) | Supports reusable sessions, supplied structures, cached messages and series-key previews on capable sources. | Reuse resolved structures and transport connections; make optional capabilities explicit. |
| [DBnomics data model](https://docs.db.nomics.world/data-model/) and [API](https://docs.db.nomics.world/web-api/) | Provides a consistent observation representation while retaining provider dimensions; supports dimension-filtered retrieval and recommends caching. | Standardize access mechanics while preserving source meaning. |
| [.Stat Suite availability](https://sis-cc.gitlab.io/dotstatsuite-documentation/using-api/availability/) | Documents selection-dependent availability, optional counts, period ranges and differing endpoint versions. | An availability feature needs provider/version checks; a standard's existence does not establish deployment support. |
| [HTTPX clients](https://www.python-httpx.org/advanced/clients/) | Top-level requests establish new connections; Client instances pool connections. | A concrete low-complexity improvement is already available in the project's HTTP dependency. |
| [IMF DataMapper documentation](https://www.imf.org/external/datamapper/api/help) | Documents area identifiers in request paths and a periods parameter. The current page identifies API v2. | Check native filtering before downloading and filtering an entire indicator. The repository currently defaults to v1, so verify its behavior separately. |
| [World Bank query options](https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures) | Distinguishes recent values from recent non-empty values and supports multiple indicators within a source. | Optimizations must preserve selection semantics, source limits and completeness checks. |
| [OECD parameter restrictions](https://www.oecd.org/en/data/insights/data-explainers/2026/03/Restricted-API-parameter.html) | Blocks firstNObservations/lastNObservations for specified large datasets and dissemination spaces. | A universal latest-N shortcut would fail on real supported sources. |
| [Anthropic tool-design evaluation guidance](https://www.anthropic.com/engineering/writing-tools-for-agents) | Recommends meaningful compact context, selective consolidation of repeated operations and evaluations with realistic tasks. | Measure avoided calls and errors rather than rewarding more features or enforcing a single workflow. |

These are design precedents and documented interface behavior. They are not controlled evidence that a particular change improves this MCP's agent accuracy. No new agent benchmark was run.

## Findings in this checkout

| Location | Observed behavior | Consequence |
| --- | --- | --- |
| source_http.get, line 80 | Uses a supplied client when available; otherwise falls back to top-level httpx.get. Macro and shared SDMX calls generally take that fallback. | Connection establishment is repeated across codelists, pages and requests. Existing retries/concurrency do not provide connection pooling. |
| SDMXStructureClient.metadata/_codes, lines 110/190 | Returns dimension IDs/order and codelist references. Code browsing issues a dedicated codelist request; structures already containing useful codes are not indexed for reuse. | Agents can need extra calls for simple code choices; potentially useful source metadata is not surfaced early. |
| get_metadata, line 393 | Accepts one dimension and one codeSearch per call. Domestic sources preview codes; OECD/PDH expose references. | Several independent code lookups can require several agent turns, even when source metadata is cached. |
| ensure_unified_catalog_artifacts/get_unified_catalog_entry, lines 196/249 | Refreshes all due sources before returning; the catalogue lock covers that work. Provider filtering happens after catalogue preparation. | A provider-specific search or known dataset lookup can wait for unrelated sources, including failures. |
| _fetch_imf, line 427 | Requests the entire indicator, then applies country and year selection locally. | Narrow requests transfer and parse unnecessary data. |
| _fetch_world_bank, line 266 | Pushes country selection and two-sided year ranges to the source; one-sided year bounds are applied locally. | Native filtering is already partly implemented; improvements should target specific remaining gaps. |
| SDMXStructureClient._xml and CustomDomesticService._parsed | Cache checks are locked, but fetching occurs after the lock is released. | Two concurrent misses for the same object can download it twice. |
| artifacts.store_retrieval, line 98 | Complete evidence has two main record families: points with x/y, and observations with value plus period dimensions. | Agents still write format-specific extraction code after successful retrieval. |

Line numbers refer to the reviewed working tree. Existing benchmark reports show substantial differences between tool time and whole-case time, and explicitly caution about token attribution. They motivate measuring agent overhead but do not isolate the causal benefit of any recommendation here.

## Prioritized improvements

### 1. Return more useful metadata in the existing call

**Priority: high. Complexity: small to medium.**

Add human-readable dimension/concept labels where the source supplies them. Reuse codelists embedded in an already-fetched structure, keyed by their full agency/ID/version identity. Show bounded code previews when that information is already available or comes from a deliberately bounded metadata fetch.

The useful output is enough to distinguish dimensions and select simple codes without separate lookups. Do not automatically fetch every large codelist just to fill previews: reducing agent calls while multiplying network requests can make the total experience worse.

If real traces still show repeated independent lookups, extend get_metadata to accept a small batch of codelist queries. Each query should retain its own search, pagination and error result. Keep the single-query form and all remaining codes reachable. This is an optional second step, not a prerequisite for the smaller metadata improvement.

Acceptance:

- Source codes and full cached validation structures are unchanged.
- Included code previews disclose their limits.
- A structure with embedded codes does not trigger a duplicate codelist download.
- An example with several small dimensions requires fewer agent calls without more source requests.
- Large-code-list examples do not inflate default responses.

### 2. Reuse HTTP connections and coalesce identical metadata fetches

**Priority: high. Complexity: small to medium.**

Use lifecycle-managed HTTPX clients for paths currently using top-level GETs. Preserve per-request headers, timeout and redirect behavior, and close clients on shutdown. Keep the existing host concurrency and retry policy.

For shared metadata/files, let concurrent requests for the same cache key wait for one fetch. Keep unrelated cache keys independent and remove failed in-flight entries so later calls can recover. Do this inside existing caches; avoid a new generic distributed cache framework.

This helps repeated requests across all providers. ABS and catalogue adapters already use clients in some paths, so the gain will vary.

Acceptance:

- Multiple calls to one host can reuse a connection.
- Two simultaneous cold lookups of the same metadata perform one upstream fetch.
- Different keys remain concurrent.
- forceRefresh and failed-fetch behavior remain clear.
- Existing selection, pagination and source flags survive unchanged.

### 3. Make provider-specific requests independent of unrelated catalogues

**Priority: high for cold starts or outages. Complexity: medium.**

When a search names a provider, refresh that provider's due catalogue. When resolving a known dataset, use its existing catalogue record or refresh its owning provider. A search across all sources can still populate all sources.

Retain one normalized catalogue, one FTS index and the same text-match ordering. Track sources that have not been loaded separately from unavailable sources, and disclose the search's coverage. Do not fabricate catalogue records from guessed IDs or reconstruct RBA download URLs.

This requires updating the current all-sources-first policy in AGENTS.md and the tool descriptions. Publication must remain atomic; avoid holding the read lock while unrelated network work runs.

Acceptance:

- A stalled unrelated provider does not delay a targeted lookup.
- Cross-provider search still reaches all sources and reports missing coverage.
- Fresh sessions remain fresh; this does not require cross-session catalogue persistence.
- Concurrent refreshes cannot lose records or pair the wrong catalogue/index generations.

### 4. Push explicit filters to the publisher

**Priority: high where unnecessary downloads are substantial. Complexity: small per adapter.**

Audit adapters for a simple rule: use the source's native country, dimension and time filtering when it exactly represents the requested scope. Keep local validation and final filtering.

IMF is the clearest current candidate. Test the configured v1 endpoint against the documentation and an equivalent complete response before changing requests or migrating versions. For World Bank, investigate its supported syntax for one-sided time bounds rather than inventing a terminal year. RBA files may require whole-file downloads; preserve cached parsing there.

Do not add an automatic recent-year window, switch datasets or silently approximate unsupported filters. Prefer bounded native requests over a new cross-source query planner.

Acceptance:

- Filtered and full-response paths return the same requested observations, including nulls and flags.
- Original area codes and intended all-area behavior are preserved.
- Transferred bytes and parse time fall on narrow requests.
- Version differences and source request limits are exercised explicitly.

### 5. Offer selection-aware availability through get_metadata

**Priority: valuable for large or sparse datasets. Complexity: medium; provider-dependent.**

Allow an optional partial dimension/time selection in get_metadata to request actual availability where supported. Return the selected scope, available codes, time range and source-provided counts when known. Paginate large code sets and say when availability is unsupported or unknown.

This can prevent a costly download or an empty selection. It should remain optional: known small requests should go directly to retrieval. Marginal lists of available dimension values do not prove every cross-product combination exists.

Implement a small vertical slice for the existing shared SDMX adapter after checking real endpoint support. Use cached file contents for file-based sources only if cheap; do not download a large file merely to claim to avoid its download. Preserve ambiguous 404 handling and do not present an unsupported endpoint as proof that data is absent.

Acceptance:

- A sparse cube demonstrates fewer failed or overbroad retrievals.
- A known exact request incurs no mandatory extra source call.
- Unsupported availability remains distinguishable from no matching data.
- Provider version, language/representation and selection scope are included in caching decisions.

### 6. Reduce post-retrieval extraction work

**Priority: next wave. Complexity: medium, with migration cost.**

Move toward one documented observation envelope for saved evidence: period, value, source dimensions, attributes and raw source fields. Keep source-specific series context and observation-level overrides intact.

The smallest starting point is one tested, optional standard-library reader for the two existing formats, documented for agents that can access the package/files. It performs structural extraction only: no aggregation, scaling, missing-period filling or statistical alignment. Other clients must still be able to read the JSON directly.

Only migrate the stored schema if evaluation shows the reader/documentation is insufficient. A schema change should be versioned, replace obsolete paths deliberately and avoid duplicating every observation into a second export.

Acceptance:

- One extraction routine reads every provider's evidence.
- Tests preserve zero, explicit null, suppressed raw values, varying units and observation-level dimensions.
- End-to-end traces show less repeated parsing/debugging after retrieval.

## Useful ideas to defer

- **Latest-N retrieval:** potentially valuable as an explicit request option, but recent rows, recent non-null values and latest observed rather than forecast values differ. OECD restrictions make a blanket parameter unsafe. Add only with capability checks and measured demand.
- **Conditional HTTP revalidation:** ETag-based reuse can reduce repeated downloads, but needs bounded cache storage and representation-aware keys. Consider for large, repeatedly used metadata/files after connection reuse and duplicate-fetch prevention.
- **Cross-dataset batches:** native batching can help some APIs, but a new public batch surface adds error, scope and response complexity. Agents can already issue independent calls concurrently.
- **Broader search semantics:** inspect failed searches before adding new ranking systems, embeddings or maintained topic dictionaries. The present research does not establish that these are the main bottleneck.
- **Server-side analytical tools:** keep calculations, joins, charting and analytical choices with the caller. New aggregation/report routes would expand the project beyond the identified retrieval problems.

## Suggested implementation order

1. Connection reuse and duplicate metadata-fetch prevention.
2. Metadata labels and reuse of already-returned codelists/previews.
3. Exact native filter pushdown, beginning with IMF after endpoint verification.
4. Provider-scoped discovery and lookup.
5. Optional availability for a verified SDMX deployment.
6. Evidence extraction ergonomics, guided by agent traces.

This order balances breadth, implementation size and confidence. Availability could save the most work on some tasks, but transport and metadata reuse are easier to validate and benefit ordinary calls.

## Evaluation plan

Use the existing benchmark workflow when a benchmark run is requested. Add held-out cases covering known IDs, unfamiliar datasets, cold/warm metadata, sparse multidimensional data, large code lists, file sources, failed providers and mixed observation flags. Permit different valid call sequences in a separate efficiency evaluation; the existing benchmark's prescribed sequence remains useful for its own historical comparisons.

Measure:

- correctness of selected scope, definitions, units and status;
- time to the first correctly interpretable local evidence;
- agent tool calls and upstream requests, including retries;
- source bytes and tool-result size;
- avoidable failed selections and repeated metadata requests;
- post-retrieval parsing/debugging work;
- latency with an unrelated provider unavailable.

For each change, compare equivalent requests with the same source responses where possible. Pair deterministic fixture checks with small opt-in live checks. Report cold and warm results separately. Do not claim a universal speedup from one source or count fewer calls as success if the data becomes incomplete.

## Scope of this research round

Documentation review and code inspection only. No runtime behavior, public tool schema, provider configuration or existing benchmark result was changed. The earlier implementation remains in the working tree.
