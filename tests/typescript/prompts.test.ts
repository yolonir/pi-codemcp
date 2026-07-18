import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  EXECUTE_PROMPT_GUIDELINES,
  INSPECT_PROMPT_GUIDELINES,
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
  ...INSPECT_PROMPT_GUIDELINES,
  ...EXECUTE_PROMPT_GUIDELINES,
  ...SAVE_CHAIN_PROMPT_GUIDELINES,
  ...MANAGE_CHAIN_PROMPT_GUIDELINES,
];

test("agent prompt policy is lean, decision-based, and non-contradictory", () => {
  const rendered = allGuidelines.join("\n");
  expect(rendered.length).toBeLessThan(1_500);
  expect(rendered.toLowerCase()).not.toContain("always");
  expect(new Set(allGuidelines).size).toBe(allGuidelines.length);
  expect(rendered).toContain("deterministically");
  expect(rendered).toContain("approval");
  expect(rendered).toContain("smallest result");
  expect(rendered).toContain("prebound globals");
  expect(rendered).toContain("must not be imported");
  expect(rendered).toContain("__import__");
  expect(rendered).toContain("explicitly");
});

test("sanitized replay set covers observed routing and stopping decisions", () => {
  expect(replay).toHaveLength(7);
  const expected = replay.map((entry) => entry.expected).join("\n");
  for (const term of [
    "without another search",
    "inventory mode",
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
