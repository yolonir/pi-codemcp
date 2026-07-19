# CodeMCP QoL replay evaluation

Date: 2026-07-18

Models:

- `openai-codex/gpt-5.6-sol`, reasoning `low`
- `openai-codex/gpt-5.6-terra`, reasoning `low`

## Reproducible artifacts

The repository contains both runners, the deterministic MCP fixture, sanitized cases, and aggregate outputs:

- `eval/qol-prompt-replay.ts`
- `eval/qol-live-replay.ts`
- `eval/qol-bench-server.py`
- `tests/fixtures/prompt-replay.json`
- `docs/eval-results/qol-prompt-sol.json`
- `docs/eval-results/qol-prompt-terra.json`
- `docs/eval-results/qol-live-sol.json`
- `docs/eval-results/qol-live-terra.json`

The checked-in JSON contains final answers, correctness, latency, tool counts, payload bytes, token usage, and cost. It intentionally excludes session IDs, response IDs, reasoning signatures, tool arguments, and raw JSONL events.

Run the prompt replay directly:

```bash
bun eval/qol-prompt-replay.ts \
  --model openai-codex/gpt-5.6-terra \
  --thinking low \
  --output docs/eval-results/qol-prompt-terra.json
```

For the live before/after replay, prepare the baseline worktree and run paired baseline/current tasks:

```bash
git worktree add --detach /tmp/pi-codemcp-qol-baseline 21652552efc1789caaf6f93efc8ec99ba99e7538
ln -s "$PWD/node_modules" /tmp/pi-codemcp-qol-baseline/node_modules
bun eval/qol-live-replay.ts \
  --model openai-codex/gpt-5.6-terra \
  --thinking low \
  --baseline-root /tmp/pi-codemcp-qol-baseline \
  --output docs/eval-results/qol-live-terra.json
```

The live runner executes each baseline/current task pair concurrently, but runs the five pairs sequentially. This preserves paired conditions without creating ten competing sidecars and model requests at once. Each child has a four-minute fail-fast timeout.

## Prompt-policy replay

The seven sanitized cases cover signature reuse, inventory, deterministic aggregation, semantic branching, approval boundaries, unknown large outputs, and saved-chain persistence.

| Model | Baseline | Current |
|---|---:|---:|
| Sol | 7/7 | 7/7 |
| Terra | 7/7 | 7/7 |

This replay establishes policy parity, not superiority. The live replay below is the behavioral acceptance test.

## Five-task live A/B

The same deterministic tasks were used for both versions:

1. complete server inventory;
2. incident filtering;
3. three-source aggregate;
4. dependent metrics calls;
5. reduction of 120 events without returning raw events.

Baseline is commit `2165255`. Current uses progressive discovery with paginated inventory, ranked signatures, shared prelude, exact stubs for up to three top matches, batch inspect for other selected calls, compact execute results, and explicit SDK/import guidance.

### Terra

| Metric | Baseline | Current |
|---|---:|---:|
| Correct answers | 5/5 | 5/5 |
| Mean latency | 11.39 s | 11.41 s |
| Tool calls | 12 | 10 |
| Searches | 8 | 6 |
| Inspects | 0 | 0 |
| Execute success / failure | 4 / 0 | 4 / 0 |
| Upstream MCP calls | 8 | 8 |
| Search/inspect output | 17,752 B | 11,961 B |
| All tool output | 18,260 B | 12,167 B |
| Total tokens | 32,961 | 35,385 |
| Cost | $0.064705 | $0.049277 |

Current preserved correctness and first-attempt execution reliability. It used two fewer searches and tool calls, while reducing discovery payload by 32.6% and all tool payload by 33.4%. Tokens were 7.4% higher; latency was effectively unchanged. Cost is reported but treated as noisy because provider cache allocation varies between paired requests.

### Sol

| Metric | Baseline | Current |
|---|---:|---:|
| Correct answers | 5/5 | 5/5 |
| Mean latency | 19.14 s | 15.40 s |
| Tool calls | 20 | 12 |
| Searches | 15 | 8 |
| Inspects | 0 | 0 |
| Execute success / failure | 4 / 1 | 4 / 0 |
| Upstream MCP calls | 8 | 8 |
| Search/inspect output | 20,473 B | 14,064 B |
| All tool output | 21,153 B | 14,270 B |
| Total tokens | 55,626 | 36,388 |
| Cost | $0.238302 | $0.154827 |

Current preserved correctness, removed the observed preflight retry, reduced tool calls by 40%, reduced discovery payload by 31.3%, and reduced total tokens by 34.6%.

## Interpretation

The earlier progressive candidate forced a separate inspect before nearly every execution and used ambiguous import guidance. Terra then attempted imports such as `import bench` and dynamic `__import__("asyncio")`, producing repeated preflight failures.

The retained design fixes those failure modes without returning to full stubs everywhere:

- signature search includes exact stubs for up to three highest-ranked matches;
- remaining selected alternatives can be batch-inspected;
- SDK facades are explicitly documented as prebound globals;
- normal `import asyncio` is shown, and unavailable `__import__` is explicitly rejected.

The synthetic repeated-schema test still enforces at least 60% payload reduction for the repeated discovery pattern. The broader five-task live replay shows a smaller 31–33% payload reduction because it includes one-off searches and shared response metadata; both model families retain full answer correctness.
