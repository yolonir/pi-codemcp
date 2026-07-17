import { expect, test } from "bun:test";
import { DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES } from "@earendil-works/pi-coding-agent";
import { formatCodeMcpOutput } from "../../src/output.js";

test("final output uses Pi limits and does not persist oversized content", () => {
  const result = formatCodeMcpOutput({
    lines: Array.from({ length: DEFAULT_MAX_LINES + 100 }, (_, index) => `line-${index}`),
    oversized: "x".repeat(DEFAULT_MAX_BYTES + 100),
  });

  expect(result.details.truncated).toBe(true);
  expect(result.details.outputLines).toBeLessThanOrEqual(DEFAULT_MAX_LINES);
  expect(result.details.outputBytes).toBeLessThanOrEqual(DEFAULT_MAX_BYTES);
  expect(result.text).toContain("The full result was not persisted");
  expect(result.text).not.toContain(`line-${DEFAULT_MAX_LINES + 99}`);
});
