import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  EXECUTE_PROMPT_GUIDELINES,
  MANAGE_CHAIN_PROMPT_GUIDELINES,
  SAVE_CHAIN_PROMPT_GUIDELINES,
  SEARCH_PROMPT_GUIDELINES,
} from "../../src/prompts.js";

interface ReplayCase {
  name: string;
  situation: string;
  expected: string;
}

const replay = JSON.parse(
  readFileSync(join(import.meta.dir, "..", "fixtures", "prompt-replay.json"), "utf8"),
) as ReplayCase[];

const allGuidelines = [
  ...SEARCH_PROMPT_GUIDELINES,
  ...EXECUTE_PROMPT_GUIDELINES,
  ...SAVE_CHAIN_PROMPT_GUIDELINES,
  ...MANAGE_CHAIN_PROMPT_GUIDELINES,
];

test("agent prompt policy is lean, decision-based, and non-contradictory", () => {
  const rendered = allGuidelines.join("\n");
  expect(rendered.length).toBeLessThan(1_500);
  expect(new Set(allGuidelines).size).toBe(allGuidelines.length);
  expect(rendered).toContain("complete typed SDK stub");
  expect(rendered).toContain("server.method");
  expect(rendered).toContain("asyncio.gather");
  expect(rendered).toContain("explicitly");
});

test("sanitized replay set covers observed routing and stopping decisions", () => {
  expect(replay).toHaveLength(7);
  const expected = replay.map((entry) => entry.expected).join("\n");
  for (const term of [
    "search before execution",
    "capability search",
    "programmatic execution",
    "model turn",
    "request approval",
    "inspect_json",
    "exact schemas",
  ]) {
    expect(expected).toContain(term);
  }
  expect(replay.every((entry) => entry.name && entry.situation && entry.expected)).toBe(true);
});
