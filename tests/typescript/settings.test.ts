import { expect, test } from "bun:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DEFAULT_CODEMCP_SETTINGS,
  loadCodeMcpSettings,
  saveCodeMcpSettings,
  setEditableSetting,
  setToolEnabled,
} from "../../src/settings.js";

test("settings persist product controls and per-tool policy", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-settings-"));
  const path = join(temporary, "settings.json");
  try {
    const defaults = loadCodeMcpSettings(path);
    expect(defaults).toEqual(DEFAULT_CODEMCP_SETTINGS);

    const withLimit = setEditableSetting(defaults, "outputLimitKiB", 100);
    const withDisabledTool = setToolEnabled(withLimit, "linear", "delete_issue", false);
    saveCodeMcpSettings(path, withDisabledTool);

    expect(loadCodeMcpSettings(path)).toEqual({
      ...DEFAULT_CODEMCP_SETTINGS,
      outputLimitKiB: 100,
      disabledTools: { linear: ["delete_issue"] },
    });
    expect(
      setToolEnabled(loadCodeMcpSettings(path), "linear", "delete_issue", true).disabledTools,
    ).toEqual({});
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("settings reject unknown and out-of-range controls", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-settings-"));
  const path = join(temporary, "settings.json");
  try {
    await writeFile(path, JSON.stringify({ bootstrapTimeout: 1 }), "utf8");
    expect(() => loadCodeMcpSettings(path)).toThrow("Unknown CodeMCP settings");

    await writeFile(path, JSON.stringify({ maxCalls: 0 }), "utf8");
    expect(() => loadCodeMcpSettings(path)).toThrow("maxCalls must be an integer");
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
