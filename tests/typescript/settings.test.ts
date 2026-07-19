import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
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

test("settings migrate the removed version-one line limit", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-settings-"));
  const path = join(temporary, "settings.json");
  try {
    await writeFile(
      path,
      JSON.stringify({ version: 1, outputLimitKiB: 100, outputLineLimit: 500 }),
      "utf8",
    );
    const migrated = loadCodeMcpSettings(path);
    expect(migrated.outputLimitKiB).toBe(100);
    expect(migrated).not.toHaveProperty("outputLineLimit");

    saveCodeMcpSettings(path, migrated);
    const persisted = JSON.parse(await readFile(path, "utf8"));
    expect(persisted.version).toBe(2);
    expect(persisted).not.toHaveProperty("outputLineLimit");
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
