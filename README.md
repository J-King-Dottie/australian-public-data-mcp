# Australian Public Data MCP

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for AI agents to search live catalogues and retrieve official Australian, Pacific, and global public data. Sources include the Australian Bureau of Statistics (ABS), Reserve Bank of Australia (RBA), DCCEEW's Australian Energy Statistics, Pacific Data Hub/SPC, World Bank, OECD, IMF, and UN Comtrade. No model API key is required.

Produced by [Dottie AI Studio](https://dottieaistudio.com.au/).
Built on existing open source work including [seansoreilly/mcp-server-abs](https://github.com/seansoreilly/mcp-server-abs) and [hanlulong/openecon-data](https://github.com/hanlulong/openecon-data).

## Quick start

Requires Python 3.11+ with SQLite FTS5 support.

```bash
git clone https://github.com/J-King-Dottie/australian-public-data-mcp.git
cd australian-public-data-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Connect an MCP client

Register a stdio server in your client's MCP settings:

- Name: `ausdata`
- Command: `/absolute/path/australian-public-data-mcp/.venv/bin/python`
- Arguments: `/absolute/path/australian-public-data-mcp/scripts/run_mcp.py`

Use paths from the machine running the client. On Windows, the interpreter is `.venv\Scripts\python.exe`; a WSL server must be launched through WSL. The included [`.mcp.json`](.mcp.json) is an example for clients that support project configuration, with the repository as their working directory.

Reload the client's MCP connections and confirm these three tools are available:

| Tool | Purpose |
| --- | --- |
| `search_catalog` | Find datasets in live official-source catalogues |
| `get_metadata` | Inspect definitions, dimensions and valid codes |
| `retrieve` | Save complete observations as JSON and return a summary with `artifact_path` |

The agent reads the saved JSON to perform analysis, so it must have access to the server's filesystem. It chooses the retrieval scope, including full datasets where supported, and can reuse known IDs and codes. Results disclose source availability, truncation and detected coverage gaps.

Tool descriptions provide call instructions. The [analyst guide](AGENT_SYSTEM_PROMPT.md) is supplied automatically at MCP initialization and through `ausdata://guide`.

## Sources

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

Availability, rate limits and revisions depend on each provider. Comtrade's public endpoint has a preview limit; the MCP rejects responses it cannot verify as complete.

## Configuration

No configuration is required. Optional environment variables are also read from a local `.env`:

| Variable | Purpose |
| --- | --- |
| `AUSDATA_RUNTIME_DIR` | Cache/evidence directory; defaults to `runtime/` in the checkout |
| `AUSDATA_SESSION_ID` | Resume a named session; omit for a fresh session. Never share across concurrent server processes. |
| `AUSDATA_CATALOG_TTL_SECONDS` | Successful catalogue cache lifetime; default `86400` seconds |
| `MACRO_TIMEOUT_SECONDS` | Macro/PDH request timeout; default `120`, range `1-600` seconds |
| `COMTRADE_API_KEY` | Optional subscription key; otherwise uses the public preview endpoint |

Evidence is retained under `<runtime directory>/sessions/<session>/artifacts/` until removed. Stop the corresponding server before deleting session directories, and retain any evidence you still need. Startup checks configuration, SQLite FTS5 and write access; diagnostics go to stderr.

## Development and documentation

- [Project agent guide](AGENTS.md): architecture, maintenance rules and local verification commands.
- [Analyst guide](AGENT_SYSTEM_PROMPT.md): evidence, calculations and presentation guidance.
- [Research and design note](docs/agent-data-usability.md): implementation decisions and verification limits.
- [Historical benchmarks](benchmarks/README.md): previous agent evaluations.

Run verification locally as described in the project agent guide. GitHub Actions is used only for daily traffic collection. For opt-in checks against all eight live providers:

```bash
python scripts/smoke_mcp.py --live --live-abs --live-pacific --live-domestic --live-macro
```
