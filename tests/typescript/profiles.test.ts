import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { manageProfilesFromManager, setServerEnabledFromManager } from "../../extensions/index.js";
import {
  activateProfile,
  activeToolPolicy,
  DEFAULT_CODEMCP_SETTINGS,
  deleteProfile,
  loadCodeMcpSettings,
  saveCodeMcpSettings,
  saveProfile,
  setProfileServerEnabled,
  setToolEnabled,
} from "../../src/settings.js";

const fixture = join(import.meta.dir, "../fixtures/profile-settings.json");

test("profiles round-trip selections, isolate edits, and restore the base policy", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-profiles-"));
  const path = join(temporary, "settings.json");
  try {
    const initial = loadCodeMcpSettings(fixture);
    const edited = setProfileServerEnabled(
      setToolEnabled(initial, "alpha", "get_number", false),
      "beta",
      true,
    );
    expect(edited.profiles.research).toEqual({
      servers: ["alpha", "beta"],
      disabledTools: { alpha: ["get_number", "slow_number"] },
    });
    expect(initial.profiles.research?.servers).toEqual(["alpha"]);
    expect(edited.disabledTools).toEqual(initial.disabledTools);
    expect(edited.profiles.writing).toEqual(initial.profiles.writing);

    const saved = saveProfile(edited, "copy", [
      { name: "beta", enabled: true },
      { name: "alpha", enabled: false },
    ]);
    saveCodeMcpSettings(path, saved);
    expect(loadCodeMcpSettings(path)).toEqual(saved);
    expect(saved.activeProfile).toBe("copy");
    expect(saved.profiles.copy?.servers).toEqual(["beta"]);
    const restored = activateProfile(saved, null);
    expect(activeToolPolicy(restored)).toEqual(initial.disabledTools);
    expect(activeToolPolicy(activateProfile(restored, "writing"))).toEqual({
      beta: ["save_number"],
    });
    expect(deleteProfile(saved, "copy").activeProfile).toBeNull();
    expect(deleteProfile(saved, "writing").activeProfile).toBe("copy");
    expect(() => saveProfile(saved, "copy", [])).toThrow("already exists");
    expect(() => activateProfile(saved, "missing")).toThrow("Unknown CodeMCP profile");
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("profile settings reject malformed policies rather than enabling a fallback", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-profiles-"));
  const path = join(temporary, "settings.json");
  try {
    for (const value of [
      { activeProfile: "missing" },
      { profiles: { bad: { servers: "alpha" } } },
      { profiles: { bad: { servers: [1] } } },
      { profiles: { bad: { servers: [""] } } },
      { profiles: { bad: { servers: [], disabledTools: { alpha: [1] } } } },
      { profiles: { bad: { servers: [], url: "https://example.com" } } },
      { profiles: { " ": { servers: [] } } },
    ]) {
      await writeFile(path, JSON.stringify(value));
      expect(() => loadCodeMcpSettings(path)).toThrow();
    }
    const special = saveProfile({ ...DEFAULT_CODEMCP_SETTINGS }, "__proto__", []);
    saveCodeMcpSettings(path, special);
    expect(
      Object.entries(loadCodeMcpSettings(path).profiles).find(([name]) => name === "__proto__")?.[1]
        .servers,
    ).toEqual([]);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("manager saves, activates, edits, and deletes profiles without rewriting mcp.json", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-profiles-"));
  const settingsPath = join(temporary, "settings.json");
  const configPath = join(temporary, "mcp.json");
  const config = '{"mcpServers":{"alpha":{"command":"unused","disabled":true}}}';
  const selections: string[] = [];
  const lifecycleCalls: string[] = [];
  let confirmation = false;
  const server = {
    name: "alpha",
    transport: "stdio",
    enabled: true,
    connected: false,
    discovered: false,
    toolCount: 0,
    totalToolCount: 0,
    tools: [],
  };
  const lifecycle = {
    configPath,
    settingsPath,
    loadSettings: () => loadCodeMcpSettings(settingsPath),
    async reload() {
      lifecycleCalls.push("reload");
    },
    async request(name: string) {
      lifecycleCalls.push(name);
      return { upstreams: [{ name: "alpha", transport: "stdio", enabled: true }] };
    },
  };
  const ctx = {
    ui: {
      async select(_title: string, _options: string[]) {
        return selections.shift();
      },
      async input() {
        return "work";
      },
      async confirm() {
        return confirmation;
      },
      notify() {},
    },
  } as unknown as Parameters<typeof manageProfilesFromManager>[1];
  try {
    await writeFile(configPath, config);
    selections.push("Save current setup as a new profile");
    await manageProfilesFromManager(lifecycle, ctx);
    expect(lifecycle.loadSettings().profiles.work?.servers).toEqual(["alpha"]);
    expect(lifecycleCalls).toEqual(["status", "reload", "status"]);

    await setServerEnabledFromManager(lifecycle, server, false);
    expect(lifecycle.loadSettings().profiles.work?.servers).toEqual([]);
    expect(await readFile(configPath, "utf8")).toBe(config);

    selections.push("Use base configuration");
    await manageProfilesFromManager(lifecycle, ctx);
    expect(lifecycle.loadSettings().activeProfile).toBeNull();
    selections.push("Activate: work");
    await manageProfilesFromManager(lifecycle, ctx);
    expect(lifecycle.loadSettings().activeProfile).toBe("work");

    selections.push("Delete a profile", "work");
    await manageProfilesFromManager(lifecycle, ctx);
    expect(lifecycle.loadSettings().activeProfile).toBe("work");
    confirmation = true;
    selections.push("Delete a profile", "work");
    await manageProfilesFromManager(lifecycle, ctx);
    expect(lifecycle.loadSettings().activeProfile).toBeNull();
    expect(lifecycle.loadSettings().profiles).toEqual({});
    expect(await readFile(configPath, "utf8")).toBe(config);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
