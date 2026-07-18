# CodeMCP Terra A/B evaluation

Date: 2026-07-18

Model: `openai-codex/gpt-5.6-terra`, reasoning `low`, ephemeral sessions.

## Compared versions

- Baseline: commit `2165255`, immediately before the QoL work.
- Current: QoL implementation with the model-facing search interface, search/execute prompt, tool descriptions, and fuzzy ranking restored to the baseline design. Search takes only `query`, optional `limit`, and optional `server`; every result contains its full typed stub. The separate inspect tool is removed.

Both variants used the same warm local deterministic MCP server. Baseline and current sessions were launched simultaneously in pairs.

## Final five-task replay

The replay covered tool enumeration, incident filtering, a three-source aggregate, dependent metrics calls, and reduction of 120 events.

| Metric | Baseline | Current |
|---|---:|---:|
| Correct answers | 5/5 | 5/5 |
| Mean latency | 11.2 s | 11.2 s |
| Parallel wall time | 16 s | 14 s |
| Tool calls | 10 | 10 |
| Searches | 6 | 6 |
| Execute success / failure | 4 / 0 | 4 / 0 |
| Upstream MCP calls | 8 | 8 |
| Total tokens | 30,436 | 30,923 |
| Cost | $0.057742 | $0.064995 |
| Search output bytes | 15,024 | 12,997 |
| All tool output bytes | 15,532 | 13,203 |

Current matched baseline correctness, first-attempt reliability, call counts, and mean latency. It reduced search payload by 13.5% and total tool payload by 15.0%. Total tokens were 1.6% higher. Cost was 12.6% higher in this five-pair sample because provider cache allocation differed materially between paired requests; the raw uncached/cache split should be considered alongside billed cost.

The final simplification removed `mode`, `detail`, `cursor`, inventory, pagination, the separate inspect tool, hybrid/BM25 ranking, score metadata, matched-field metadata, and the longer experimental search/execute prompts. The retained QoL work is below the model-facing orchestration layer: compact execute results, bounded introspection, local telemetry, saved-chain management, and selective internal type stubs.

Earlier progressive-discovery and long-prompt candidates were rejected because live replays showed extra searches, import mistakes, and execution retries.

Raw JSONL events and the aggregate used for this report were retained locally under `/tmp/pi-codemcp-qol-bench/`. The final files are `final3-*.jsonl` and `final3-summary.json`.
