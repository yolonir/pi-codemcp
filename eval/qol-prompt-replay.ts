import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import {
  EXECUTE_PROMPT_GUIDELINES,
  INSPECT_PROMPT_GUIDELINES,
  SAVE_CHAIN_PROMPT_GUIDELINES,
  SEARCH_PROMPT_GUIDELINES,
} from "../src/prompts.js";
import replayCases from "../tests/fixtures/prompt-replay.json";

interface ReplayCase {
  name: string;
  situation: string;
  expected: string;
}

interface ReplayAction {
  name: string;
  action: string;
}

interface Usage {
  input: number;
  output: number;
  cacheRead: number;
  reasoning: number;
  totalTokens: number;
  cost: { total: number };
}

const BASELINE_GUIDANCE = [
  "Use codemcp_search before codemcp_execute; every upstream or saved-chain match includes the complete typed SDK stub needed to write the execution.",
  "Use codemcp_execute if you know tool schemas; call the returned server.method facade and use top-level return for the compact final value.",
  "It is always better to execute multiple MCP calls in one codemcp_execute call rather than multiple single-call invocations.",
  "You can compose upstream SDK calls and saved chains.* calls, running independent work with asyncio.gather or dependent work sequentially.",
  "If a user repeatedly performs the same MCP workflow, you may offer to save it, but do not persist it until the user explicitly asks or accepts.",
] as const;

const CURRENT_GUIDANCE = [
  ...SEARCH_PROMPT_GUIDELINES,
  ...INSPECT_PROMPT_GUIDELINES,
  ...EXECUTE_PROMPT_GUIDELINES,
  ...SAVE_CHAIN_PROMPT_GUIDELINES,
] as const;

const args = process.argv.slice(2);
const model = argument(args, "--model");
const outputPath = resolve(argument(args, "--output"));
const thinking = optionalArgument(args, "--thinking") ?? "low";
const cases = replayCases as ReplayCase[];

const [baseline, current] = await Promise.all([
  runVariant("baseline", BASELINE_GUIDANCE),
  runVariant("current", CURRENT_GUIDANCE),
]);
const result = {
  schemaVersion: 1,
  model,
  thinking,
  caseCount: cases.length,
  variants: { baseline, current },
};
await mkdir(dirname(outputPath), { recursive: true });
await Bun.write(outputPath, `${JSON.stringify(result, null, 2)}\n`);
console.log(
  `${model}: baseline ${baseline.correct}/${cases.length}, current ${current.correct}/${cases.length}`,
);

async function runVariant(variant: string, guidance: readonly string[]) {
  const prompt = replayPrompt(guidance);
  const started = performance.now();
  const process = Bun.spawn(
    [
      "pi",
      "--print",
      "--mode",
      "json",
      "--no-session",
      "--no-tools",
      "--no-extensions",
      "--no-skills",
      "--no-prompt-templates",
      "--no-context-files",
      "--model",
      model,
      "--thinking",
      thinking,
      prompt,
    ],
    { stdout: "pipe", stderr: "pipe", env: processEnv() },
  );
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
    process.exited,
  ]);
  if (exitCode !== 0) {
    throw new Error(`${variant} replay failed (${exitCode}): ${stderr.trim()}`);
  }
  const event = finalTurn(stdout);
  const answer = messageText(event.message);
  const actions = parseActions(answer);
  const actionByName = new Map(actions.map((item) => [item.name, item.action]));
  const outcomes = cases.map((item) => ({
    name: item.name,
    expected: item.expected,
    actual: actionByName.get(item.name) ?? "<missing>",
    correct: actionByName.get(item.name) === item.expected,
  }));
  return {
    correct: outcomes.filter((item) => item.correct).length,
    total: cases.length,
    elapsedMs: Math.round(performance.now() - started),
    usage: compactUsage(event.message.usage),
    outcomes,
  };
}

function replayPrompt(guidance: readonly string[]): string {
  const allowed = cases.map((item) => item.expected);
  return [
    "Evaluate the following CodeMCP policy against each case.",
    "Choose exactly one action from ALLOWED_ACTIONS for every case.",
    'Return only a JSON array in input order with objects {"name": string, "action": string}.',
    "Do not add prose or invent alternative wording.",
    "",
    `POLICY=${JSON.stringify(guidance)}`,
    `ALLOWED_ACTIONS=${JSON.stringify(allowed)}`,
    `CASES=${JSON.stringify(cases.map(({ name, situation }) => ({ name, situation })))}`,
  ].join("\n");
}

function finalTurn(stdout: string): { message: Record<string, unknown> } {
  const events = stdout
    .split("\n")
    .filter(Boolean)
    .map((line): unknown => JSON.parse(line));
  const event = events.findLast((item) => isRecord(item) && item.type === "turn_end");
  if (!isRecord(event) || !isRecord(event.message)) throw new Error("Pi emitted no final turn");
  return { message: event.message };
}

function messageText(message: Record<string, unknown>): string {
  if (!Array.isArray(message.content)) return "";
  return message.content
    .flatMap((item): string[] => {
      if (!isRecord(item) || item.type !== "text" || typeof item.text !== "string") return [];
      return [item.text];
    })
    .join("\n");
}

function parseActions(answer: string): ReplayAction[] {
  const fenced = answer.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
  const source = fenced ?? answer;
  const start = source.indexOf("[");
  const end = source.lastIndexOf("]");
  if (start < 0 || end < start) throw new Error(`Replay returned non-JSON output: ${answer}`);
  const parsed: unknown = JSON.parse(source.slice(start, end + 1));
  if (!Array.isArray(parsed)) throw new Error("Replay output must be an array");
  return parsed.flatMap((item): ReplayAction[] => {
    if (
      typeof item !== "object" ||
      item === null ||
      typeof (item as Record<string, unknown>).name !== "string" ||
      typeof (item as Record<string, unknown>).action !== "string"
    ) {
      return [];
    }
    return [item as ReplayAction];
  });
}

function compactUsage(value: unknown): Usage {
  const usage = isRecord(value) ? value : {};
  const cost = isRecord(usage.cost) ? usage.cost : {};
  return {
    input: number(usage.input),
    output: number(usage.output),
    cacheRead: number(usage.cacheRead),
    reasoning: number(usage.reasoning),
    totalTokens: number(usage.totalTokens),
    cost: { total: number(cost.total) },
  };
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

function processEnv(): Record<string, string | undefined> {
  return { ...process.env, PI_TELEMETRY: "0" };
}
