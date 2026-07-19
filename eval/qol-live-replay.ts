import { access, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

interface ReplayTask {
  name: string;
  prompt: string;
  expected: unknown;
}

interface ToolResultSummary {
  name: string;
  isError: boolean;
  outputBytes: number;
  callsMade: number;
  error?: string;
}

const TASKS: ReplayTask[] = [
  {
    name: "inventory",
    prompt:
      "Use CodeMCP. List every tool exposed by the bench server. Return only one JSON array of SDK call identifiers, sorted alphabetically, with no prose.",
    expected: [
      "bench.get_dependencies",
      "bench.get_events",
      "bench.get_owner",
      "bench.list_incidents",
      "bench.service_metrics",
    ],
  },
  {
    name: "filter incidents",
    prompt:
      "Use CodeMCP. For the payments service, list the IDs of open incidents whose severity is at least 3. Request 12 incidents. Return only one sorted JSON array of IDs, with no prose.",
    expected: ["PAY-002", "PAY-004", "PAY-007", "PAY-008"],
  },
  {
    name: "three-source aggregate",
    prompt:
      "Use CodeMCP. For the api service, return its owning team, p95 latency in milliseconds, and number of open incidents among the first 12 incidents. Return only one JSON object with keys owner, p95_latency_ms, and open_incident_count, with no prose.",
    expected: { owner: "team-api", p95_latency_ms: 180, open_incident_count: 8 },
  },
  {
    name: "dependent metrics",
    prompt:
      "Use CodeMCP. Get the direct dependencies of the web service, then get metrics for each dependency. Return only one JSON object mapping each dependency name to its error_rate, with no prose.",
    expected: { api: 0.02, payments: 0.01 },
  },
  {
    name: "large reduction",
    prompt:
      "Use CodeMCP. Analyze 120 events for the payments service without returning raw events. Return only one JSON object with keys count, kinds (a sorted array of unique kinds), and max_latency_ms, with no prose.",
    expected: { count: 120, kinds: ["request", "retry", "timeout"], max_latency_ms: 132 },
  },
];

const args = process.argv.slice(2);
const model = argument(args, "--model");
const outputPath = resolve(argument(args, "--output"));
const baselineRoot = resolve(argument(args, "--baseline-root"));
const currentRootArgument = optionalArgument(args, "--current-root");
const currentRoot = currentRootArgument
  ? resolve(currentRootArgument)
  : resolve(import.meta.dir, "..");
const thinking = optionalArgument(args, "--thinking") ?? "low";
await assertPreparedRoot(baselineRoot);
await assertPreparedRoot(currentRoot);

const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-live-eval-"));
try {
  const runs: Array<Awaited<ReturnType<typeof runTask>>> = [];
  for (const [index, task] of TASKS.entries()) {
    const pair = await Promise.all([
      runTask("baseline", baselineRoot, task, index),
      runTask("current", currentRoot, task, index),
    ]);
    runs.push(...pair);
  }
  const result = {
    schemaVersion: 1,
    model,
    thinking,
    tasks: TASKS.map((task) => task.name),
    variants: {
      baseline: summarize(runs.filter((run) => run.variant === "baseline")),
      current: summarize(runs.filter((run) => run.variant === "current")),
    },
    runs,
  };
  await mkdir(dirname(outputPath), { recursive: true });
  await Bun.write(outputPath, `${JSON.stringify(result, null, 2)}\n`);
  console.log(
    `${model}: baseline ${result.variants.baseline.correct}/${TASKS.length}, current ${result.variants.current.correct}/${TASKS.length}`,
  );
} finally {
  await rm(temporary, { recursive: true, force: true });
}

async function runTask(
  variant: "baseline" | "current",
  packageRoot: string,
  task: ReplayTask,
  index: number,
) {
  const runRoot = join(temporary, `${variant}-${index + 1}`);
  const agentDir = join(temporary, variant, "agent");
  await mkdir(runRoot, { recursive: true });
  await mkdir(agentDir, { recursive: true });
  await writeFile(
    join(agentDir, "mcp.json"),
    `${JSON.stringify({
      mcpServers: {
        bench: {
          command: "uv",
          args: [
            "run",
            "--project",
            join(currentRoot, "sidecar"),
            "--frozen",
            "--no-sync",
            join(currentRoot, "eval", "qol-bench-server.py"),
          ],
        },
      },
    })}\n`,
  );
  const extensionPath = join(runRoot, "extension.ts");
  await writeFile(
    extensionPath,
    [
      `import { createCodeMcpExtension } from ${JSON.stringify(join(packageRoot, "extensions", "index.ts"))};`,
      `export default createCodeMcpExtension({ agentDir: ${JSON.stringify(agentDir)} });`,
      "",
    ].join("\n"),
  );
  const started = performance.now();
  const child = Bun.spawn(
    [
      "pi",
      "--print",
      "--mode",
      "json",
      "--no-session",
      "--no-extensions",
      "--no-builtin-tools",
      "--no-skills",
      "--no-prompt-templates",
      "--no-context-files",
      "--model",
      model,
      "--thinking",
      thinking,
      "--extension",
      extensionPath,
      task.prompt,
    ],
    { stdout: "pipe", stderr: "pipe", env: { ...process.env, PI_TELEMETRY: "0" } },
  );
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    child.kill();
  }, 240_000);
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ]).finally(() => clearTimeout(timeout));
  if (timedOut) throw new Error(`${variant}/${task.name} timed out after 240 seconds`);
  if (exitCode !== 0) {
    throw new Error(`${variant}/${task.name} failed (${exitCode}): ${stderr.trim()}`);
  }
  const events = parseEvents(stdout);
  const answer = finalAnswer(events);
  const parsedAnswer = parseJsonAnswer(answer);
  const tools = toolResults(events);
  const usage = aggregateUsage(events);
  return {
    variant,
    task: task.name,
    correct: JSON.stringify(parsedAnswer) === JSON.stringify(task.expected),
    answer: parsedAnswer,
    elapsedMs: Math.round(performance.now() - started),
    toolCalls: tools.length,
    searches: tools.filter((tool) => tool.name === "codemcp_search").length,
    inspects: tools.filter((tool) => tool.name === "codemcp_inspect").length,
    executeSuccesses: tools.filter((tool) => tool.name === "codemcp_execute" && !tool.isError)
      .length,
    executeFailures: tools.filter((tool) => tool.name === "codemcp_execute" && tool.isError).length,
    executeErrors: tools.flatMap((tool) =>
      tool.name === "codemcp_execute" && tool.error ? [tool.error] : [],
    ),
    upstreamCalls: tools.reduce((total, tool) => total + tool.callsMade, 0),
    toolOutputBytes: tools.reduce((total, tool) => total + tool.outputBytes, 0),
    searchOutputBytes: tools
      .filter((tool) => tool.name === "codemcp_search" || tool.name === "codemcp_inspect")
      .reduce((total, tool) => total + tool.outputBytes, 0),
    usage,
  };
}

function summarize(runs: Array<Awaited<ReturnType<typeof runTask>>>) {
  return {
    correct: runs.filter((run) => run.correct).length,
    meanElapsedMs: Math.round(runs.reduce((total, run) => total + run.elapsedMs, 0) / runs.length),
    toolCalls: runs.reduce((total, run) => total + run.toolCalls, 0),
    searches: runs.reduce((total, run) => total + run.searches, 0),
    inspects: runs.reduce((total, run) => total + run.inspects, 0),
    executeSuccesses: runs.reduce((total, run) => total + run.executeSuccesses, 0),
    executeFailures: runs.reduce((total, run) => total + run.executeFailures, 0),
    upstreamCalls: runs.reduce((total, run) => total + run.upstreamCalls, 0),
    toolOutputBytes: runs.reduce((total, run) => total + run.toolOutputBytes, 0),
    searchOutputBytes: runs.reduce((total, run) => total + run.searchOutputBytes, 0),
    totalTokens: runs.reduce((total, run) => total + run.usage.totalTokens, 0),
    cost: runs.reduce((total, run) => total + run.usage.cost, 0),
  };
}

function parseEvents(stdout: string): Record<string, unknown>[] {
  return stdout
    .split("\n")
    .filter(Boolean)
    .flatMap((line): Record<string, unknown>[] => {
      const value: unknown = JSON.parse(line);
      return isRecord(value) ? [value] : [];
    });
}

function finalAnswer(events: Record<string, unknown>[]): string {
  const event = events.findLast((item) => item.type === "turn_end");
  if (!event || !isRecord(event.message)) throw new Error("Pi emitted no final turn");
  return textContent(event.message.content);
}

function parseJsonAnswer(answer: string): unknown {
  const fenced = answer.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
  return JSON.parse((fenced ?? answer).trim());
}

function toolResults(events: Record<string, unknown>[]): ToolResultSummary[] {
  return events.flatMap((event): ToolResultSummary[] => {
    if (event.type !== "tool_execution_end" || typeof event.toolName !== "string") return [];
    const result = isRecord(event.result) ? event.result : {};
    const details = isRecord(result.details) ? result.details : {};
    const text = textContent(result.content);
    const isError = event.isError === true || result.isError === true || details.ok === false;
    return [
      {
        name: event.toolName,
        isError,
        outputBytes: Buffer.byteLength(text),
        callsMade: number(details.callsMade),
        ...(isError ? { error: text.slice(0, 500) } : {}),
      },
    ];
  });
}

function aggregateUsage(events: Record<string, unknown>[]) {
  let input = 0;
  let output = 0;
  let cacheRead = 0;
  let reasoning = 0;
  let totalTokens = 0;
  let cost = 0;
  for (const event of events) {
    if (event.type !== "message_end" || !isRecord(event.message)) continue;
    if (event.message.role !== "assistant" || !isRecord(event.message.usage)) continue;
    const usage = event.message.usage;
    input += number(usage.input);
    output += number(usage.output);
    cacheRead += number(usage.cacheRead);
    reasoning += number(usage.reasoning);
    totalTokens += number(usage.totalTokens);
    if (isRecord(usage.cost)) cost += number(usage.cost.total);
  }
  return { input, output, cacheRead, reasoning, totalTokens, cost };
}

function textContent(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value
    .flatMap((item): string[] => {
      if (!isRecord(item) || item.type !== "text" || typeof item.text !== "string") return [];
      return [item.text];
    })
    .join("\n");
}

async function assertPreparedRoot(root: string): Promise<void> {
  await access(join(root, "extensions", "index.ts"));
  try {
    await access(join(root, "node_modules"));
  } catch {
    throw new Error(`Run bun install in ${root} before the live replay`);
  }
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function argument(values: string[], name: string): string {
  const value = optionalArgument(values, name);
  if (value === undefined) throw new Error(`Missing required argument ${name}`);
  return value;
}

function optionalArgument(values: string[], name: string): string | undefined {
  const index = values.indexOf(name);
  return index >= 0 ? values[index + 1] : undefined;
}
