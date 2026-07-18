# CodeMCP prompt replay evaluation

Date: 2026-07-18

Model: `openai-codex/gpt-5.6-terra`, reasoning `low`, ephemeral session, tools and extensions disabled.

The replay uses the seven sanitized routing and stopping cases in `tests/fixtures/prompt-replay.json`. Each run received the same cases and was required to return one action per case. The baseline was the previous search/execute/save guidance; the candidate was the modular guidance in `src/prompts.ts`.

| Variant | Correct | Input tokens | Output tokens | Total tokens | Latency | Cost |
|---|---:|---:|---:|---:|---:|---:|
| Previous guidance | 6/7 (85.7%) | 824 | 81 | 905 | 6 s | $0.003275 |
| Candidate guidance | 7/7 (100%) | 892 | 80 | 972 | 6 s | $0.003430 |

The previous guidance chose ranked capability search when asked to enumerate a known server. The candidate chose paginated inventory and matched all expected decisions: reuse known signatures, inventory for enumeration, programmatic execution for deterministic reduction, preserve model turns for semantic branches, stop at approval boundaries, inspect unfamiliar large values with bounded samples, and test code successfully before saving.

The candidate costs 67 additional input tokens and $0.000155 in this replay, with no observed latency increase. It is retained because the shorter baseline regressed the inventory decision. No `asyncio.gather` example was added: the replay did not expose a failure that justified its context cost.

This replay evaluates prompt-level decisions, not live upstream task success. Runtime task outcomes, call counts, latency, bytes, failure stages, and chain reuse are measured locally by the bounded Stats rollups implemented for this specification.
