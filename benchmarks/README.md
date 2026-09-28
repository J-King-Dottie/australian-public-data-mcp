# Historical answer benchmark

This directory preserves the earlier eight-question answer benchmark, its reviewed
results, and the report generator. It is historical evidence, not the active
usable-data diagnostic. Do not append new-style trials to this incompatible schema.

Regenerate historical reports with:
python benchmarks/build_report.py
python benchmarks/build_report.py --check

The old runs used fixed call order, sequential shared context and answer correctness.
Their notes document token-boundary and prior-exposure limitations. They do not
establish a controlled improvement in time to suitable evidence.

The current optional diagnostic is maintained with the local improvement process
under .agents/improvement/BENCHMARK.md (Git-ignored, when present).
It evaluates retrieval suitability and effort, accepts flexible call paths and
missing telemetry, and preserves unsuccessful attempts. It introduces no benchmark
logic into MCP tools. See the local guide for cadence and interpretation.
