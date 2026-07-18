import { expect, test } from "bun:test";
import { DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES } from "@earendil-works/pi-coding-agent";
import { formatCodeMcpOutput } from "../../src/output.js";

test("final output uses configured limits and does not persist oversized content", () => {
  const result = formatCodeMcpOutput(
    {
      lines: Array.from({ length: DEFAULT_MAX_LINES + 100 }, (_, index) => `line-${index}`),
      oversized: "x".repeat(DEFAULT_MAX_BYTES + 100),
    },
    { maxBytes: 8_192, maxLines: 100 },
  );

  expect(result.details.truncated).toBe(true);
  expect(result.details.outputLines).toBeLessThanOrEqual(100);
  expect(result.details.outputBytes).toBeLessThanOrEqual(8_192);
  expect(result.details.outputTokens).toBe(Math.ceil(result.text.length / 4));
  expect(result.details.outputLines).toBe(1);
  expect(result.text.split("\n")[0]).not.toContain(": ");
  expect(result.text).toContain("The full result was not persisted");
  expect(result.text).not.toContain(`line-${DEFAULT_MAX_LINES + 99}`);
});

test("compact serialization removes model-facing pretty-print overhead", () => {
  const value = {
    items: Array.from({ length: 20 }, (_, index) => ({ index, enabled: true })),
  };
  const result = formatCodeMcpOutput(value);
  const pretty = JSON.stringify(value, null, 2);

  expect(result.text).toBe(JSON.stringify(value));
  expect(result.text.length).toBeLessThan(pretty.length * 0.6);
});
